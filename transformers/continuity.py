"""Cross-file balance continuity tracking across Bronze data.

B1 (transformers/reconciliation_status.py) only checks that one file's own
transactions roll forward to its own printed closing anchor - it says
nothing about whether the *next* file for the same account picks up where
the last one left off. This module generalizes the check
`NATWEST_TRANSACTIONS_BALANCE_DESIGN.md` sketched for Natwest specifically
(closing balance of statement N == opening balance of statement N+1) to
every source_type that captures an opening anchor (see adapters/base.py's
`ReconciliationResult.expected_opening_minor`, adapters/reconciliation.py).

Tri-state result per consecutive pair of files, mirroring
ReconciliationResult.matches's existing convention:
- True: prior file's closing anchor == next file's opening anchor
- False: a genuine discrepancy - money unaccounted for between statements
- None: inconclusive - either side is missing an anchor (e.g. Monzo PDF,
  which never prints one), or the boundary falls inside a known coverage
  gap (transformers/coverage.py) - not comparable, not a real break.

Each account in practice has exactly one anchored source_type across its
statement history (unlike coverage.py's Natwest cross-format overlap
concern, where two source_types can both cover one account), so pairing is
a plain sequential walk through each account's chronologically-sorted
anchor rows - no overlap-merging high-water-mark is needed here.
"""

from datetime import timedelta
from typing import Dict, Optional, Union
from pathlib import Path

import pandas as pd

from models.datalake import DataLake, get_datalake
from transformers.coverage import _COVERAGE_GAP_TOLERANCE, find_statement_periods
from transformers.reconciliation_status import find_reconciliation_status

PathLike = Union[str, Path]

_CONTINUITY_COLUMNS = [
    "account_id",
    "source_type",
    "next_source_type",
    "ingestion_id",
    "next_ingestion_id",
    "filename",
    "next_filename",
    "expected_closing_minor",
    "expected_opening_minor",
    "matches",
    "gap_related",
]


def find_balance_continuity(
    datalake: Optional[DataLake] = None,
    path: Optional[PathLike] = None,
    tolerance: timedelta = _COVERAGE_GAP_TOLERANCE,
    bronze_frames: Optional[Dict[str, pd.DataFrame]] = None,
) -> pd.DataFrame:
    """Check that each account's consecutive anchored statements connect:
    the prior file's closing anchor should equal the next file's opening
    anchor. Returns one row per consecutive pair of anchored files, per
    account - not one row per file.

    Pass an already-loaded `bronze_frames` dict (source_type -> DataFrame)
    to avoid re-reading Bronze from disk when the caller already has it in
    memory - threaded through to both internal find_reconciliation_status/
    find_statement_periods calls, which otherwise re-read Bronze themselves.
    """
    datalake = datalake or get_datalake()
    anchors = find_reconciliation_status(
        datalake, path=path, bronze_frames=bronze_frames
    )
    if anchors.empty:
        return pd.DataFrame(columns=_CONTINUITY_COLUMNS)

    periods = find_statement_periods(datalake, path=path, bronze_frames=bronze_frames)
    merged = anchors.merge(
        periods, on=["account_id", "source_type", "filename"], how="left"
    )
    # An anchor row with no matching statement period can't be ordered
    # relative to its siblings - drop it from continuity pairing rather than
    # guess. In practice every _RECONCILIATION_SOURCE_TYPES entry is also a
    # _PERIOD_SOURCE_TYPES entry, so this should rarely trigger.
    merged = merged.dropna(subset=["period_from", "period_to"])

    rows = []
    for account_id, group in merged.groupby("account_id"):
        group = group.sort_values("period_from")
        prev = None
        for row in group.itertuples():
            if prev is not None:
                gap_related = (row.period_from - prev.period_to) > tolerance
                if (
                    gap_related
                    or prev.expected_closing_minor is None
                    or pd.isna(prev.expected_closing_minor)
                    or row.expected_opening_minor is None
                    or pd.isna(row.expected_opening_minor)
                ):
                    matches = None
                else:
                    matches = prev.expected_closing_minor == row.expected_opening_minor
                rows.append(
                    {
                        "account_id": account_id,
                        "source_type": prev.source_type,
                        "next_source_type": row.source_type,
                        "ingestion_id": prev.ingestion_id,
                        "next_ingestion_id": row.ingestion_id,
                        "filename": prev.filename,
                        "next_filename": row.filename,
                        "expected_closing_minor": prev.expected_closing_minor,
                        "expected_opening_minor": row.expected_opening_minor,
                        "matches": matches,
                        "gap_related": bool(gap_related),
                    }
                )
            prev = row

    return pd.DataFrame(rows, columns=_CONTINUITY_COLUMNS)
