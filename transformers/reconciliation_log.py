"""Persisted, auditable reconciliation history (item 5).

Unifies the three reconciliation check types into one Silver table with a
`check_type` discriminator, rather than three separate tables - matching
this codebase's existing wide-nullable-plus-discriminator idiom
(ReconciliationResult.account_identifier, Bronze's reconciliation_* columns
being present-or-absent per source_type) and letting "every mismatch across
every check type" be a single query.

- "bronze_self_check": one row per anchored Bronze file (subject: one file)
  - see transformers/reconciliation_status.py.
- "continuity": one row per consecutive pair of anchored files, per account
  (subject: a boundary between two files) - see transformers/continuity.py.
- "silver_rollforward": one row per anchored file, re-derived from the
  Silver transactions that survived matching/dedup (subject: one file's
  Silver-side contribution) - see transformers/silver_reconciliation.py.

Because bronze_self_check rows are keyed by immutable ingestion_id and
every Silver rebuild recomputes the full current Bronze set (not a delta),
every historical file's check reappears, identically, in every build's
log - nothing is lost by models/build.py's KEEP_BUILDS pruning for that
check_type. Only continuity/silver_rollforward verdicts can genuinely
change build-over-build (a new adjacent file, or a matching-logic fix).
"""

from datetime import datetime, timezone

import pandas as pd

RECONCILIATION_LOG_COLUMNS = [
    "check_type",
    "check_name",
    "account_id",
    "source_type",
    "next_source_type",
    "ingestion_id",
    "next_ingestion_id",
    "filename",
    "next_filename",
    "expected_opening_minor",
    "expected_closing_minor",
    "derived_closing_minor",
    "matches",
    "gap_related",
    "build_id",
    "computed_at",
]


class ReconciliationMismatchError(Exception):
    """Raised by run_bronze_to_silver(strict_reconciliation=True) when the
    reconciliation_log contains any genuine (matches == False) mismatch -
    collects every offending row at once, never just the first, mirroring
    transformers.account_config.UnmappedAccountsError."""

    def __init__(self, mismatches: pd.DataFrame):
        self.mismatches = mismatches
        lines = [
            f"  - [{row.check_type}] account_id={row.account_id!r} "
            f"source_type={row.source_type!r} filename={row.filename!r}"
            + (f" -> {row.next_filename!r}" if row.next_filename else "")
            + f": expected {row.expected_closing_minor}, derived "
            f"{row.derived_closing_minor}"
            for row in mismatches.itertuples()
        ]
        message = (
            f"{len(mismatches)} reconciliation mismatch(es) found across the "
            "current Bronze set - refusing to publish a Silver build under "
            "--strict:\n" + "\n".join(lines)
        )
        super().__init__(message)


def build_reconciliation_log(
    bronze_self_check: pd.DataFrame,
    continuity: pd.DataFrame,
    silver_breaks: pd.DataFrame,
    build_id: str,
) -> pd.DataFrame:
    """Assemble the unified reconciliation_log table for one Silver build."""
    computed_at = pd.Timestamp(datetime.now(timezone.utc))
    rows = []

    for row in bronze_self_check.itertuples():
        rows.append(
            {
                "check_type": "bronze_self_check",
                "check_name": row.check_name,
                "account_id": row.account_id,
                "source_type": row.source_type,
                "next_source_type": None,
                "ingestion_id": row.ingestion_id,
                "next_ingestion_id": None,
                "filename": row.filename,
                "next_filename": None,
                "expected_opening_minor": row.expected_opening_minor,
                "expected_closing_minor": row.expected_closing_minor,
                "derived_closing_minor": row.derived_closing_minor,
                "matches": row.matches,
                "gap_related": False,
                "build_id": build_id,
                "computed_at": computed_at,
            }
        )

    for row in continuity.itertuples():
        rows.append(
            {
                "check_type": "continuity",
                "check_name": "balance_continuity",
                "account_id": row.account_id,
                "source_type": row.source_type,
                "next_source_type": row.next_source_type,
                "ingestion_id": row.ingestion_id,
                "next_ingestion_id": row.next_ingestion_id,
                "filename": row.filename,
                "next_filename": row.next_filename,
                "expected_opening_minor": row.expected_opening_minor,
                "expected_closing_minor": row.expected_closing_minor,
                "derived_closing_minor": None,
                "matches": row.matches,
                "gap_related": bool(row.gap_related),
                "build_id": build_id,
                "computed_at": computed_at,
            }
        )

    for row in silver_breaks.itertuples():
        rows.append(
            {
                "check_type": "silver_rollforward",
                "check_name": "silver_rollforward",
                "account_id": row.account_id,
                "source_type": row.source_type,
                "next_source_type": None,
                "ingestion_id": row.ingestion_id,
                "next_ingestion_id": None,
                "filename": row.filename,
                "next_filename": None,
                "expected_opening_minor": row.expected_opening_minor,
                "expected_closing_minor": row.expected_closing_minor,
                "derived_closing_minor": row.silver_derived_closing_minor,
                "matches": row.matches,
                "gap_related": False,
                "build_id": build_id,
                "computed_at": computed_at,
            }
        )

    return pd.DataFrame(rows, columns=RECONCILIATION_LOG_COLUMNS)
