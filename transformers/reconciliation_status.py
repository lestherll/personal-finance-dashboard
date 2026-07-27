"""Queryable balance-reconciliation status across Bronze data.

B1 (see BRONZE_SILVER_HARDENING_PLAN.md / CLAUDE.md "What Bronze guarantees")
gives every self-checking adapter a structured per-file reconciliation
result, persisted as reconciliation_check/reconciliation_expected_closing/
reconciliation_derived_closing/reconciliation_matches columns on the Bronze
row (models/datalake.py::write_bronze). Until now the only way to see it was
`cli.py ingest`'s per-file console output at ingest time. This module makes
it queryable after the fact, joinable to an account_id - mirroring
transformers/coverage.py's pattern exactly, since reconciliation, like a
statement period, is a per-account-per-file fact, not a per-transaction one.
Usually every row in a Bronze parquet carries the same value (one account
per file), but a source covering multiple accounts in one file (Vanguard's
ISA + Personal Pension wrappers) carries a distinct value per
account_identifier instead - see the drop_duplicates key below.
"""

from typing import Optional, Set, Union
from pathlib import Path

import pandas as pd

from models.datalake import DataLake, get_datalake
from transformers.account_config import get_account_id

PathLike = Union[str, Path]

# Sources that self-check a balance anchor - either a rolled-forward/
# derived balance (Amex, First Direct, Natwest Statement, Chase), or a
# lighter check confirming a direct-read balance matches a separately
# printed closing anchor (Kroo, Monzo Flex, Monzo PDF) - see CLAUDE.md
# "What Bronze guarantees" (B1). Vanguard PDF checks its "Your Vanguard
# account summary" table against each wrapper's holdings total - the only
# source here producing more than one reconciliation result per file (one
# per wrapper), via last_reconciliations rather than last_reconciliation.
_RECONCILIATION_SOURCE_TYPES: Set[str] = {
    "amex",
    "firstdirect",
    "natwest-statement",
    "kroo",
    "chase",
    "monzo-flex",
    "monzo-pdf",
    "vanguard-pdf",
}

_STATUS_COLUMNS = [
    "account_id",
    "source_type",
    "filename",
    "check_name",
    "expected_closing",
    "derived_closing",
    "matches",
]


def find_reconciliation_status(
    datalake: Optional[DataLake] = None, path: Optional[PathLike] = None
) -> pd.DataFrame:
    """Collect one row per ingested statement file that captured a
    reconciliation result, resolved to its canonical account_id.

    Accounts not yet registered in the account map are skipped rather than
    raising - this is a reporting command, not a pipeline pre-flight check,
    same rationale as find_statement_periods().
    """
    datalake = datalake or get_datalake()
    rows = []

    for source_type in _RECONCILIATION_SOURCE_TYPES:
        df = datalake.read_bronze(source_type)
        if df is None or df.empty or "reconciliation_check" not in df.columns:
            continue

        # Keyed on (filename, account_identifier), not filename alone: a
        # source like vanguard-pdf can carry two genuinely different
        # reconciliation verdicts (one per wrapper) within one file - a
        # filename-only key would silently collapse to one row and drop
        # the other wrapper's status.
        per_file = df.dropna(subset=["reconciliation_check"]).drop_duplicates(
            ["filename", "account_identifier"]
        )

        for row in per_file.itertuples():
            try:
                account_id = get_account_id(
                    row.account_identifier, source_type, path=path
                )
            except KeyError:
                continue
            rows.append(
                {
                    "account_id": account_id,
                    "source_type": source_type,
                    "filename": row.filename,
                    "check_name": row.reconciliation_check,
                    "expected_closing": row.reconciliation_expected_closing,
                    "derived_closing": row.reconciliation_derived_closing,
                    "matches": row.reconciliation_matches,
                }
            )

    return pd.DataFrame(rows, columns=_STATUS_COLUMNS)
