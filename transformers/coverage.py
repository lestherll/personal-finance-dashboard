"""Statement-period coverage tracking across Bronze data.

All 8 PDF source_types now extract a statement period (see
BRONZE_SILVER_HARDENING_PLAN.md B3, extended by the B4 follow-up): each
prints a "From X to Y" (or equivalent) range on the statement itself, except
First Direct, which only prints a single Statement Date - its `from_date` is
derived (one calendar month earlier, its known fixed billing cycle), not
read from the page (see `FirstDirectPdfAdapter._extract_statement_period`).
The 3 CSV source_types (monzo, natwest, vanguard) have no such concept at
all - flat per-row exports, no printed period anywhere - so they're
deliberately excluded here rather than producing empty/misleading rows.
"""

from datetime import timedelta
from typing import Optional, Set, Union
from pathlib import Path

import pandas as pd

from models.datalake import DataLake, get_datalake
from transformers.account_config import get_account_id

PathLike = Union[str, Path]

# Mirrors adapters/pdf_adapter.py's _PERIOD_BOUNDARY_TOLERANCE: a declared
# period's edge and the next statement's start routinely differ by a day or
# two even with no real gap (inclusive/exclusive date handling varies by
# bank), so a small tolerance avoids flagging phantom gaps.
_COVERAGE_GAP_TOLERANCE = timedelta(days=3)

_PERIOD_SOURCE_TYPES: Set[str] = {
    "amex",
    "natwest-pdf",
    "natwest-statement",
    "monzo-pdf",
    "chase",
    "vanguard-pdf",
    "kroo",
    "firstdirect",
}

_PERIODS_COLUMNS = ["account_id", "source_type", "filename", "period_from", "period_to"]
_GAPS_COLUMNS = ["account_id", "gap_start", "gap_end", "days"]


def find_statement_periods(
    datalake: Optional[DataLake] = None, path: Optional[PathLike] = None
) -> pd.DataFrame:
    """Collect one row per ingested statement file that has a captured
    statement period, resolved to its canonical account_id.

    Accounts not yet registered in the account map (see
    transformers/account_config.py) are skipped rather than raising - this
    is a reporting command, not a pipeline pre-flight check, so an unmapped
    account just means one fewer row shown, not a failure.
    """
    datalake = datalake or get_datalake()
    rows = []

    for source_type in _PERIOD_SOURCE_TYPES:
        df = datalake.read_bronze(source_type)
        if df is None or df.empty or "statement_period_from" not in df.columns:
            continue

        per_file = df.dropna(
            subset=["statement_period_from", "statement_period_to"]
        ).drop_duplicates("filename")

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
                    "period_from": row.statement_period_from,
                    "period_to": row.statement_period_to,
                }
            )

    return pd.DataFrame(rows, columns=_PERIODS_COLUMNS)


def find_coverage_gaps(
    periods: pd.DataFrame, tolerance: timedelta = _COVERAGE_GAP_TOLERANCE
) -> pd.DataFrame:
    """Flag gaps between consecutive statement periods, per account.

    Overlapping/adjacent periods across multiple source_types for the same
    account_id (e.g. a natwest-pdf export and a natwest-statement covering
    an overlapping window) are merged rather than double-flagged, by
    tracking the furthest `period_to` seen so far instead of assuming
    periods are non-overlapping.
    """
    gap_rows = []

    for account_id, group in periods.groupby("account_id"):
        group = group.sort_values("period_from")
        prev_to = None
        for row in group.itertuples():
            if prev_to is not None and row.period_from - prev_to > tolerance:
                gap_rows.append(
                    {
                        "account_id": account_id,
                        "gap_start": prev_to,
                        "gap_end": row.period_from,
                        "days": (row.period_from - prev_to).days,
                    }
                )
            prev_to = row.period_to if prev_to is None else max(prev_to, row.period_to)

    return pd.DataFrame(gap_rows, columns=_GAPS_COLUMNS)
