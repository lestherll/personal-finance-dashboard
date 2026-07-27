"""Bronze -> Silver normalization.

Unifies the per-adapter `raw_data` shapes (see adapters/*.py) into three
common Silver schemas: transactions, holdings, and account_ledger.

Natwest Transactions, Natwest Statement, and AmEx statements print
transaction dates with no year (e.g. "15 Jan") - all three now stamp a real
year onto them at the adapter level (`resolve_year_in_period()`) whenever
the statement's own period header is found. `_infer_dated_with_year()` below
is kept only as a fallback: for Bronze rows ingested before an adapter had
this fix, or wherever the period header isn't present in the statement text.
"""

import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from enum import IntEnum
from typing import Any, Dict, List, Optional

import pandas as pd

from adapters.factory import AdapterFactory
from models.datalake import DataLake, get_datalake
from config import SILVER_DIR as _SILVER_DIR
from models.ingestion import STATUS_COMPLETE, load_manifest
from transformers.account_config import (
    UnmappedAccountsError,
    build_accounts_table,
    find_unmapped_accounts,
    get_account_id,
)
from transformers.continuity import find_balance_continuity
from transformers.matching import match_transactions
from transformers.reconciliation_log import (
    ReconciliationMismatchError,
    build_reconciliation_log,
)
from transformers.reconciliation_status import find_reconciliation_status
from transformers.silver_reconciliation import find_silver_reconciliation_breaks
from models.build import generate_build_id, publish_silver_build

logger = logging.getLogger(__name__)

# source_types with a "balance" in raw_data usable for account_ledger.
# "natwest-transactions" (online export) and "vanguard-pdf" (cash_balance is
# a different metric from Portfolio Value) are deliberately excluded - see
# CLAUDE.md gotchas.
LEDGER_SOURCE_TYPES = {
    "kroo",
    "amex",
    "firstdirect",
    "natwest-statement",
    "monzo-pdf",
    "chase",
    "monzo-flex",
}


class Sign(IntEnum):
    """Coefficient for rolling a balance forward through signed amounts.

    ADD   (+1):  balance = previous + amount  (positive amount = money in)
    SUBTRACT (-1): balance = previous - amount  (positive amount = money out)
    """

    ADD = 1
    SUBTRACT = -1


@dataclass(frozen=True)
class DerivedBalanceAnchor:
    """Declares that a dependent source_type (which has no balance anchor
    of its own) should derive balances by rolling forward from another
    source_type's latest confirmed ledger balance."""

    anchor_source_type: str
    sign: Sign


# Source_types with no balance anchor that derive one from another source.
# natwest-transactions (on-demand export) has no balance data structurally
# (CLAUDE.md Gotcha #6); it rolls forward from natwest-statement, which prints
# a real closing balance each quarter.
_DERIVED_BALANCE_ANCHORS: dict[str, DerivedBalanceAnchor] = {
    "natwest-transactions": DerivedBalanceAnchor(
        anchor_source_type="natwest-statement",
        sign=Sign.ADD,
    ),
}

TRANSACTION_SOURCE_TYPES = {
    "monzo",
    "kroo",
    "natwest-transactions",
    "natwest-statement",
    "firstdirect",
    "amex",
    "vanguard-pdf",
    "monzo-pdf",
    "chase",
    "monzo-flex",
}


def _parse_date(value: Any, fmt: Optional[str] = None) -> Optional[pd.Timestamp]:
    """Parse a date string, trying an explicit format first, then a generic fallback.

    Always returns a naive Timestamp - some sources (Monzo search export)
    give ISO8601 with a "Z" suffix, and mixing tz-aware/naive datetimes in
    one Silver column breaks downstream Parquet writes.
    """
    if not value:
        return None
    result = None
    if fmt:
        try:
            result = pd.to_datetime(value, format=fmt)
        except (ValueError, TypeError):
            result = None
    if result is None:
        try:
            result = pd.to_datetime(value)
        except (ValueError, TypeError):
            return None
    if result.tzinfo is not None:
        result = result.tz_localize(None)
    return result


def _reconciled_flag(bronze_row: Any) -> Optional[bool]:
    """Read a Bronze row's per-file reconciliation verdict (Gotcha #14/#17).

    `None` means "not disproven" (no anchor found, or this source/row
    predates the reconciliation_matches column) and is treated as
    includable downstream - only an explicit `False` means a known mismatch.
    """
    raw = bronze_row.get("reconciliation_matches")
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    return bool(raw)


def _infer_dated_with_year(
    date_str: str, fmt_no_year: str, reference: Any
) -> Optional[pd.Timestamp]:
    """Attach a year to a 'DD Mmm'-style date using the Bronze upload time as context.

    Some PDF adapters (Natwest PDF, AmEx) never capture a year in raw_data.
    This is a best-effort inference, not a fix - see module docstring.
    Statements are uploaded after the transaction occurs, so among the
    candidate years we prefer the most recent one that doesn't land after
    the upload time (handles the Dec/Jan year-boundary case).
    """
    if reference is None or pd.isna(reference):
        return None
    reference = pd.Timestamp(reference)
    candidates = []
    for year in (reference.year, reference.year - 1):
        candidate = _parse_date(f"{date_str} {year}", f"{fmt_no_year} %Y")
        if candidate is not None:
            candidates.append(candidate)
    if not candidates:
        return None
    not_after_upload = [c for c in candidates if c <= reference + pd.Timedelta(days=7)]
    if not_after_upload:
        return max(not_after_upload)
    return min(candidates)


def _str_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _normalize_monzo(raw: Dict[str, Any], reference: Any) -> Dict[str, Any]:
    is_search_format = "id" in raw and "created" in raw
    if is_search_format:
        transaction_date = _parse_date(raw.get("created"))
        description = raw.get("title") or raw.get("subtitle") or ""
        amount_minor = int(raw.get("amount_minor") or 0)
        currency = raw.get("currency") or "GBP"
        category = raw.get("categories")
        bank_transaction_id = _str_or_none(raw.get("id"))
    else:
        date_str = raw.get("Date", "")
        time_str = raw.get("Time", "")
        transaction_date = _parse_date(
            f"{date_str} {time_str}".strip(), "%d/%m/%Y %H:%M:%S"
        ) or _parse_date(date_str, "%d/%m/%Y")
        description = raw.get("Name") or raw.get("Description") or ""
        amount_minor = int(raw.get("amount_minor") or 0)
        currency = raw.get("Currency") or "GBP"
        category = raw.get("Category")
        bank_transaction_id = _str_or_none(raw.get("Transaction ID"))
    return {
        "transaction_date": transaction_date,
        "description": description,
        "amount_minor": amount_minor,
        "currency": currency,
        "category": category,
        "bank_transaction_id": bank_transaction_id,
    }


def _normalize_pdf_full_month_year(
    raw: Dict[str, Any], reference: Any
) -> Dict[str, Any]:
    """Kroo: 'DD Month YYYY'."""
    return {
        "transaction_date": _parse_date(raw.get("date", ""), "%d %B %Y"),
        "description": raw.get("description", ""),
        "amount_minor": int(raw.get("amount_minor") or 0),
        "currency": "GBP",
        "category": None,
        "bank_transaction_id": None,
    }


def _normalize_pdf_no_year(raw: Dict[str, Any], reference: Any) -> Dict[str, Any]:
    """Natwest Transactions, Natwest Statement, AmEx: 'DD Mmm', with the year
    attached at the adapter level (resolve_year_in_period()) whenever the
    statement's period header was found. Falls back to upload-timestamp
    inference (Gotcha #7) for Bronze rows ingested before that existed, or
    where the period header wasn't present in the statement text.

    Chase always carries the year natively ('DD Mon YYYY') so it's mapped
    to this same normalizer purely to reuse the "%d %b %Y" parse path -
    it never needs the upload-timestamp fallback in practice.
    """
    date_str = raw.get("date", "")
    transaction_date = (
        _parse_date(date_str, "%d %b %Y") if re.search(r"\d{4}", date_str) else None
    )
    if transaction_date is None:
        transaction_date = _infer_dated_with_year(date_str, "%d %b", reference)
    return {
        "transaction_date": transaction_date,
        "description": raw.get("description", ""),
        "amount_minor": int(raw.get("amount_minor") or 0),
        "currency": "GBP",
        "category": None,
        "bank_transaction_id": None,
    }


def _normalize_pdf_short_year(raw: Dict[str, Any], reference: Any) -> Dict[str, Any]:
    """First Direct: 'DD Mmm YY'."""
    return {
        "transaction_date": _parse_date(raw.get("date", ""), "%d %b %y"),
        "description": raw.get("description", ""),
        "amount_minor": int(raw.get("amount_minor") or 0),
        "currency": "GBP",
        "category": None,
        "bank_transaction_id": None,
    }


def _normalize_pdf_slash_date(raw: Dict[str, Any], reference: Any) -> Dict[str, Any]:
    """Vanguard PDF, Monzo PDF: 'DD/MM/YYYY'."""
    return {
        "transaction_date": _parse_date(raw.get("date", ""), "%d/%m/%Y"),
        "description": raw.get("description", ""),
        "amount_minor": int(raw.get("amount_minor") or 0),
        "currency": "GBP",
        "category": None,
        "bank_transaction_id": None,
    }


_TRANSACTION_NORMALIZERS = {
    "monzo": _normalize_monzo,
    "kroo": _normalize_pdf_full_month_year,
    "natwest-transactions": _normalize_pdf_no_year,
    "natwest-statement": _normalize_pdf_no_year,
    "firstdirect": _normalize_pdf_short_year,
    "amex": _normalize_pdf_no_year,
    "vanguard-pdf": _normalize_pdf_slash_date,
    "monzo-pdf": _normalize_pdf_slash_date,
    "chase": _normalize_pdf_no_year,
    "monzo-flex": _normalize_pdf_slash_date,
}


def _parse_money(value: Any) -> float:
    """Parse a '£1,234.56' / '-' / bare-decimal string into a float."""
    if value is None or value == "-":
        return 0.0
    cleaned = str(value).replace("£", "").replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _normalize_vanguard_pdf_holding(
    raw: Dict[str, Any], reference: Any
) -> Dict[str, Any]:
    """Vanguard PDF statement's 'Your X investments at DATE' table.

    No ISIN in this format - only a fund name.
    """
    return {
        "isin": None,
        "fund_name": raw.get("fund_name"),
        "quantity": raw.get("quantity_text") or str(raw.get("quantity") or ""),
        "unit_price_minor": int(raw.get("unit_price_minor") or 0),
        "total_value_minor": int(raw.get("total_value_minor") or 0),
        "currency": "GBP",
        "as_of_date": _parse_date(raw.get("as_of_date", ""), "%d %B %Y") or reference,
    }


def _normalize_amex_plan_it_instalment(
    raw: Dict[str, Any], reference: Any
) -> Dict[str, Any]:
    """Amex's "Plan It Instalments Summary" table: one row per active
    instalment plan, re-printed (with updated progress) on every statement
    while the plan remains active. Separate from account_ledger's
    aggregate "Plan It Instalments Due" figure (_ledger_from_amex) - this
    is per-plan detail for analysis, not a balance input.
    """
    return {
        "start_date": _parse_date(raw.get("start_date", ""), "%b %d %Y"),
        "description": raw.get("description"),
        "plan_total_minor": int(raw.get("plan_total_minor") or 0),
        "plan_lifetime_fee_minor": int(raw.get("plan_lifetime_fee_minor") or 0),
        "remaining_balance_minor": int(raw.get("remaining_balance_minor") or 0),
        "due_this_month_plan_minor": int(raw.get("due_this_month_plan_minor") or 0),
        "due_this_month_fee_minor": int(raw.get("due_this_month_fee_minor") or 0),
        "due_this_month_total_minor": int(raw.get("due_this_month_total_minor") or 0),
        "instalment_progress": raw.get("instalment_progress"),
        "as_of_date": _parse_date(raw.get("as_of_date") or "", "%d %b %Y") or reference,
    }


def _ledger_from_kroo(raw: Dict[str, Any], reference: Any) -> Dict[str, Any]:
    return {
        "balance_minor": int(raw.get("balance_minor") or 0),
        "as_of_date": _parse_date(raw.get("date", ""), "%d %B %Y"),
    }


def _ledger_from_amex(raw: Dict[str, Any], reference: Any) -> Dict[str, Any]:
    """AmEx's `date` may already carry a year (adapter-level
    `resolve_year_in_period()`, see `_normalize_pdf_no_year` above) or may
    not (Bronze rows from before that existed, or no period header found) -
    same dual-path check as `_normalize_pdf_no_year`, needed here too since
    `_infer_dated_with_year` assumes a bare 'DD Mmm' and mishandles a
    string that already has a year appended.
    """
    date_str = raw.get("date", "")
    as_of_date = (
        _parse_date(date_str, "%d %b %Y") if re.search(r"\d{4}", date_str) else None
    )
    if as_of_date is None:
        as_of_date = _infer_dated_with_year(date_str, "%d %b", reference)
    return {
        "balance_minor": int(raw.get("balance_minor") or 0),
        "as_of_date": as_of_date,
    }


def _ledger_from_firstdirect(raw: Dict[str, Any], reference: Any) -> Dict[str, Any]:
    return {
        "balance_minor": int(raw.get("balance_minor") or 0),
        "as_of_date": _parse_date(raw.get("date", ""), "%d %b %y"),
    }


def _ledger_from_natwest_statement(
    raw: Dict[str, Any], reference: Any
) -> Dict[str, Any]:
    """Same dual-path date check as `_ledger_from_amex` above, needed here
    for the same reason: the adapter now stamps a real year onto `date` via
    resolve_year_in_period() whenever the statement's period header is
    found, and `_infer_dated_with_year` mishandles a string that already has
    a year appended."""
    date_str = raw.get("date", "")
    as_of_date = (
        _parse_date(date_str, "%d %b %Y") if re.search(r"\d{4}", date_str) else None
    )
    if as_of_date is None:
        as_of_date = _infer_dated_with_year(date_str, "%d %b", reference)
    return {
        "balance_minor": int(raw.get("balance_minor") or 0),
        "as_of_date": as_of_date,
    }


def _ledger_from_monzo_pdf(raw: Dict[str, Any], reference: Any) -> Dict[str, Any]:
    return {
        "balance_minor": int(raw.get("balance_minor") or 0),
        "as_of_date": _parse_date(raw.get("date", ""), "%d/%m/%Y"),
    }


def _ledger_from_chase(raw: Dict[str, Any], reference: Any) -> Dict[str, Any]:
    """Chase's `date` always carries a native year ('DD Mon YYYY') - no
    dual-path/_infer_dated_with_year check needed here, unlike
    _ledger_from_amex/_ledger_from_natwest_statement (Gotcha #6/#7's
    date-year trap doesn't apply to Chase)."""
    return {
        "balance_minor": int(raw.get("balance_minor") or 0),
        "as_of_date": _parse_date(raw.get("date", ""), "%d %b %Y"),
    }


_LEDGER_NORMALIZERS = {
    "kroo": _ledger_from_kroo,
    "amex": _ledger_from_amex,
    "firstdirect": _ledger_from_firstdirect,
    "natwest-statement": _ledger_from_natwest_statement,
    "monzo-pdf": _ledger_from_monzo_pdf,
    "chase": _ledger_from_chase,
    "monzo-flex": _ledger_from_monzo_pdf,
}

_TRANSACTIONS_COLUMNS = [
    "bronze_record_id",
    "silver_transaction_id",
    "bronze_source_key",
    "source_type",
    "account_id",
    "transaction_date",
    "description",
    "amount_minor",
    "currency",
    "category",
    "bank_transaction_id",
    "ingested_at",
    "upload_timestamp",
    "statement_period_to",
    "line_number",
]

_HOLDINGS_COLUMNS = [
    "bronze_record_id",
    "bronze_source_key",
    "account_id",
    "isin",
    "fund_name",
    "quantity",
    "unit_price_minor",
    "total_value_minor",
    "currency",
    "as_of_date",
]

_LEDGER_COLUMNS = [
    "bronze_record_id",
    "bronze_source_key",
    "account_id",
    "source_type",
    "balance_minor",
    "as_of_date",
    "upload_timestamp",
    "statement_period_to",
    "line_number",
    "reconciled",
    "balance_source",
]

_PLAN_IT_INSTALMENTS_COLUMNS = [
    "bronze_record_id",
    "bronze_source_key",
    "account_id",
    "start_date",
    "description",
    "plan_total_minor",
    "plan_lifetime_fee_minor",
    "remaining_balance_minor",
    "due_this_month_plan_minor",
    "due_this_month_fee_minor",
    "due_this_month_total_minor",
    "instalment_progress",
    "as_of_date",
]

_TRANSACTION_SOURCES_COLUMNS = [
    "silver_transaction_id",
    "bronze_record_id",
    "ingestion_id",
    "source_type",
    "match_policy",
]


class SilverTransformer:
    """Normalizes Bronze RawRecord data into common Silver schemas."""

    def __init__(self, datalake: Optional[DataLake] = None):
        self.datalake = datalake or get_datalake()

    def _read_bronze_frames(self) -> Dict[str, pd.DataFrame]:
        frames = {}
        all_source_types = (
            AdapterFactory.CSV_SOURCE_TYPES | AdapterFactory.PDF_SOURCE_TYPES
        )
        for source_type in all_source_types:
            df = self.datalake.read_bronze(source_type)
            if df is not None and not df.empty:
                frames[source_type] = df
        return frames

    def normalize_transactions(
        self, bronze_frames: Dict[str, pd.DataFrame]
    ) -> pd.DataFrame:
        rows = []
        for source_type, df in bronze_frames.items():
            normalizer = _TRANSACTION_NORMALIZERS.get(source_type)
            if normalizer is None:
                continue
            if "record_type" in df.columns:
                df = df[df["record_type"] == "transaction"]
            for _, bronze_row in df.iterrows():
                raw = bronze_row.get("raw_data") or {}
                normalized = normalizer(raw, bronze_row.get("upload_timestamp"))
                account_id = get_account_id(
                    bronze_row.get("account_identifier"), source_type
                )
                rows.append(
                    {
                        "bronze_record_id": bronze_row.get("bronze_record_id")
                        or bronze_row.get("bronze_source_key"),
                        "bronze_source_key": bronze_row.get("bronze_source_key"),
                        "ingestion_id": bronze_row.get("ingestion_id"),
                        "source_type": source_type,
                        "account_id": account_id,
                        "ingested_at": pd.Timestamp.now(),
                        "upload_timestamp": bronze_row.get("upload_timestamp"),
                        "statement_period_to": bronze_row.get("statement_period_to"),
                        "line_number": bronze_row.get("line_number"),
                        **normalized,
                    }
                )

        if not rows:
            return pd.DataFrame(columns=_TRANSACTIONS_COLUMNS), pd.DataFrame(
                columns=_TRANSACTION_SOURCES_COLUMNS
            )

        result = pd.DataFrame(rows)
        result["transaction_date"] = pd.to_datetime(result["transaction_date"])
        return match_transactions(result)

    def normalize_holdings(
        self, bronze_frames: Dict[str, pd.DataFrame]
    ) -> pd.DataFrame:
        """Holdings come from Vanguard PDF statements' investment tables."""
        df = bronze_frames.get("vanguard-pdf")
        if df is None or df.empty or "record_type" not in df.columns:
            return pd.DataFrame(columns=_HOLDINGS_COLUMNS)

        holdings_df = df[df["record_type"] == "holding"]
        rows = []
        for _, bronze_row in holdings_df.iterrows():
            raw = bronze_row.get("raw_data") or {}
            normalized = _normalize_vanguard_pdf_holding(
                raw, bronze_row.get("upload_timestamp")
            )
            account_id = get_account_id(
                bronze_row.get("account_identifier"), "vanguard-pdf"
            )
            rows.append(
                {
                    "bronze_record_id": bronze_row.get("bronze_record_id")
                    or bronze_row.get("bronze_source_key"),
                    "bronze_source_key": bronze_row.get("bronze_source_key"),
                    "account_id": account_id,
                    **normalized,
                }
            )

        if not rows:
            return pd.DataFrame(columns=_HOLDINGS_COLUMNS)

        result = pd.DataFrame(rows)
        result["as_of_date"] = pd.to_datetime(result["as_of_date"])
        return result

    def normalize_plan_it_instalments(
        self, bronze_frames: Dict[str, pd.DataFrame]
    ) -> pd.DataFrame:
        """Amex's "Plan It Instalments Summary" table - per-plan detail
        alongside the aggregate "Plan It Instalments Due" figure already
        folded into account_ledger's balance (_ledger_from_amex)."""
        df = bronze_frames.get("amex")
        if df is None or df.empty or "record_type" not in df.columns:
            return pd.DataFrame(columns=_PLAN_IT_INSTALMENTS_COLUMNS)

        plan_it_df = df[df["record_type"] == "plan_it_instalment"]
        rows = []
        for _, bronze_row in plan_it_df.iterrows():
            raw = bronze_row.get("raw_data") or {}
            normalized = _normalize_amex_plan_it_instalment(
                raw, bronze_row.get("upload_timestamp")
            )
            account_id = get_account_id(bronze_row.get("account_identifier"), "amex")
            rows.append(
                {
                    "bronze_record_id": bronze_row.get("bronze_record_id")
                    or bronze_row.get("bronze_source_key"),
                    "bronze_source_key": bronze_row.get("bronze_source_key"),
                    "account_id": account_id,
                    **normalized,
                }
            )

        if not rows:
            return pd.DataFrame(columns=_PLAN_IT_INSTALMENTS_COLUMNS)

        result = pd.DataFrame(rows)
        result["start_date"] = pd.to_datetime(result["start_date"])
        result["as_of_date"] = pd.to_datetime(result["as_of_date"])
        return result

    def normalize_account_ledger(
        self, bronze_frames: Dict[str, pd.DataFrame]
    ) -> pd.DataFrame:
        rows = []
        for source_type in LEDGER_SOURCE_TYPES:
            df = bronze_frames.get(source_type)
            if df is None or df.empty:
                continue
            if "record_type" in df.columns:
                # Amex now also carries "plan_it_instalment" rows (see
                # normalize_plan_it_instalments) alongside its transactions -
                # those have no `date`/`amount` shape and must not reach a
                # ledger normalizer built for transaction rows.
                df = df[df["record_type"] == "transaction"]
            if df.empty:
                continue

            normalizer = _LEDGER_NORMALIZERS[source_type]
            for _, bronze_row in df.iterrows():
                raw = bronze_row.get("raw_data") or {}
                normalized = normalizer(raw, bronze_row.get("upload_timestamp"))
                account_id = get_account_id(
                    bronze_row.get("account_identifier"), source_type
                )
                rows.append(
                    {
                        "bronze_record_id": bronze_row.get("bronze_record_id")
                        or bronze_row.get("bronze_source_key"),
                        "bronze_source_key": bronze_row.get("bronze_source_key"),
                        "account_id": account_id,
                        "source_type": source_type,
                        "upload_timestamp": bronze_row.get("upload_timestamp"),
                        "statement_period_to": bronze_row.get("statement_period_to"),
                        "line_number": bronze_row.get("line_number"),
                        "reconciled": _reconciled_flag(bronze_row),
                        "balance_source": "printed",
                        **normalized,
                    }
                )

        if not rows:
            return pd.DataFrame(columns=_LEDGER_COLUMNS)

        result = pd.DataFrame(rows)
        result["as_of_date"] = pd.to_datetime(result["as_of_date"])
        return result


_COVERAGE_GAP_TOLERANCE = timedelta(days=3)


def _contiguous_coverage_end(
    account_id: str,
    anchor_date: pd.Timestamp,
    dependent_source_type: str,
    datalake: DataLake,
) -> pd.Timestamp:
    """Find the latest date up to which the dependent source_type's
    statement-period coverage is contiguous starting from `anchor_date`.

    Reads Bronze `statement_period_from/to` columns for all files of the
    given source_type that belong to this account_id, sorts them by
    period start, and walks forward: any gap (period i+1's start > the
    running max_to + tolerance) truncates the window. Returns the end of
    the first contiguous segment after anchor_date, or
    `pd.Timestamp.max` if coverage is fully contiguous (or no period
    data exists to judge gaps at all).
    """
    bronze = datalake.read_bronze(dependent_source_type)
    if bronze is None or bronze.empty or "statement_period_from" not in bronze.columns:
        return pd.Timestamp.max

    period_rows = []
    for _, row in bronze.dropna(
        subset=["statement_period_from", "statement_period_to"]
    ).iterrows():
        aid = row.get("account_identifier")
        if aid is None:
            continue
        try:
            aid = get_account_id(aid, dependent_source_type)
        except KeyError:
            continue
        if aid != account_id:
            continue
        period_rows.append(
            (
                pd.Timestamp(row["statement_period_from"]),
                pd.Timestamp(row["statement_period_to"]),
            )
        )

    if not period_rows:
        return pd.Timestamp.max

    period_rows.sort(key=lambda p: p[0])

    max_to = anchor_date
    for pf, pt in period_rows:
        if pf > max_to + _COVERAGE_GAP_TOLERANCE:
            return max_to
        max_to = max(max_to, pt)

    return max_to


def _derive_rollforward_ledger_rows(
    transactions_df: pd.DataFrame,
    ledger_df: pd.DataFrame,
    datalake: DataLake,
) -> pd.DataFrame:
    """For each dependent→anchor pair declared in _DERIVED_BALANCE_ANCHORS,
    take the anchor source's latest confirmed balance per account and roll
    it forward through the dependent source's deduplicated transactions
    dated after that anchor, producing extra ledger rows tagged
    `balance_source="derived"`.

    Coverage gaps (non-contiguous statement periods in the dependent
    source) truncate the derivation window: nothing past the gap gets a
    derived balance. The function appends nothing to the existing
    ledger - it returns only the new rows; callers are responsible for
    concatenating them.
    """
    if transactions_df.empty or ledger_df.empty:
        return pd.DataFrame(columns=_LEDGER_COLUMNS)

    derived_rows: list[dict[str, Any]] = []

    for dependent_st, anchor_config in _DERIVED_BALANCE_ANCHORS.items():
        anchor_st = anchor_config.anchor_source_type
        sign = int(anchor_config.sign)

        dep_mask = transactions_df["source_type"] == dependent_st
        dep_txns = transactions_df[dep_mask]
        if dep_txns.empty:
            continue

        anchor_mask = (ledger_df["source_type"] == anchor_st) & (
            ledger_df["reconciled"] == True  # noqa: E712
        )
        anchor_ledger = ledger_df[anchor_mask]
        if anchor_ledger.empty:
            continue

        for account_id in dep_txns["account_id"].dropna().unique():
            account_anchors = anchor_ledger[anchor_ledger["account_id"] == account_id]
            if account_anchors.empty:
                continue

            latest_anchor = account_anchors.sort_values(
                "as_of_date", ascending=False
            ).iloc[0]
            anchor_date = pd.Timestamp(latest_anchor["as_of_date"])
            anchor_balance = int(latest_anchor["balance_minor"])

            account_txns = dep_txns[dep_txns["account_id"] == account_id].copy()
            account_txns = account_txns[
                pd.to_datetime(account_txns["transaction_date"]) > anchor_date
            ]
            if account_txns.empty:
                continue

            contiguous_end = _contiguous_coverage_end(
                account_id, anchor_date, dependent_st, datalake
            )
            account_txns = account_txns[
                pd.to_datetime(account_txns["transaction_date"]) <= contiguous_end
            ]
            if account_txns.empty:
                continue

            account_txns = account_txns.sort_values("transaction_date")

            running = anchor_balance
            for _, txn in account_txns.iterrows():
                running += int(txn["amount_minor"]) * sign
                derived_rows.append(
                    {
                        "bronze_record_id": f"derived_{txn['bronze_record_id']}",
                        "bronze_source_key": f"derived_{txn['bronze_source_key']}",
                        "account_id": account_id,
                        "source_type": dependent_st,
                        "balance_minor": running,
                        "as_of_date": txn["transaction_date"],
                        "upload_timestamp": txn.get("upload_timestamp") or pd.NaT,
                        "statement_period_to": txn.get("statement_period_to") or pd.NaT,
                        "line_number": txn.get("line_number") or 0,
                        "reconciled": None,
                        "balance_source": "derived",
                    }
                )

    if not derived_rows:
        return pd.DataFrame(columns=_LEDGER_COLUMNS)

    return pd.DataFrame(derived_rows)[_LEDGER_COLUMNS]


def _eligible_ingestion_ids(
    bronze_frames: Dict[str, pd.DataFrame],
) -> tuple[set[str], list[dict[str, str]]]:
    """Determine which ingestion_ids are eligible for Silver promotion.

    Returns (eligible_ids, excluded_list) where excluded_list contains
    dicts with {ingestion_id, source_type, reason} for each ingestion
    quarantined due to reconciliation mismatch or missing manifest override.
    """
    all_ids: set[str] = set()
    for df in bronze_frames.values():
        if "ingestion_id" in df.columns:
            all_ids.update(df["ingestion_id"].dropna().unique().tolist())

    eligible: set[str] = set()
    excluded: list[dict[str, str]] = []

    for iid in all_ids:
        manifest = load_manifest(iid)
        if manifest is None:
            eligible.add(iid)
            continue

        if manifest.status != STATUS_COMPLETE:
            excluded.append(
                {
                    "ingestion_id": iid,
                    "source_type": manifest.source_type or "unknown",
                    "reason": f"manifest status is {manifest.status}, not complete",
                }
            )
            continue

        # Determine if reconciliation failed.
        rec_failed = False
        if manifest.reconciliation_matches is False:
            rec_failed = True
        for rec in manifest.reconciliations or []:
            if rec.get("matches") is False:
                rec_failed = True
                break

        if rec_failed:
            override = manifest.promotion_override or {}
            if override.get("decision") == "allow":
                eligible.add(iid)
            else:
                excluded.append(
                    {
                        "ingestion_id": iid,
                        "source_type": manifest.source_type or "unknown",
                        "reason": "reconciliation mismatch, no allow override",
                    }
                )
        else:
            eligible.add(iid)

    return eligible, excluded


def _filter_to_eligible(
    bronze_frames: Dict[str, pd.DataFrame], eligible_ids: set[str]
) -> Dict[str, pd.DataFrame]:
    """Return bronze_frames with only rows from eligible ingestion_ids."""
    filtered: Dict[str, pd.DataFrame] = {}
    for st, df in bronze_frames.items():
        if "ingestion_id" not in df.columns:
            filtered[st] = df
        else:
            df = df[df["ingestion_id"].isin(eligible_ids)]
            if not df.empty:
                filtered[st] = df
    return filtered


def run_bronze_to_silver(
    datalake: Optional[DataLake] = None,
    strict_reconciliation: bool = False,
) -> Dict[str, pd.DataFrame]:
    """Run the full Bronze -> Silver transformation and publish a
    versioned, atomic Silver build.

    Idempotent: re-running against unchanged Bronze data does not create
    duplicate rows (canonicalized by the matching engine).

    Fails fast, but as a single pre-flight check: if any Bronze account isn't
    mapped yet, raises UnmappedAccountsError listing every unmapped account
    found (not just the first one), before writing anything. See
    transformers/account_config.py::find_unmapped_accounts / register_account.

    `strict_reconciliation` (default False, non-blocking - see item 1 of the
    reconciliation hardening work): by default, a reconciliation mismatch of
    any check_type (bronze_self_check/continuity/silver_rollforward) is only
    logged/recorded in the published reconciliation_log - the build still
    publishes (per-file bronze_self_check mismatches are additionally
    quarantined out of the transaction set entirely by
    _eligible_ingestion_ids, independent of this flag). This mirrors the
    project's explicit historical decision (RECONCILIATION_MISMATCH_CONTEXT.md)
    against hard-failing ingestion on any mismatch, since the real historical
    Amex bug would have made the tool unable to ingest Amex data at all for
    the period it existed. Passing `strict_reconciliation=True` opts into a
    stricter mode instead: if the assembled reconciliation_log contains any
    genuine mismatch (matches == False, across any check_type), raises
    ReconciliationMismatchError - collecting every offending row at once,
    same shape as UnmappedAccountsError - *before* publish_silver_build() is
    called, so nothing new is published and data/silver/current stays on the
    last good build.

    Because every rebuild recomputes bronze_self_check rows from the
    *entire* current Bronze set (not just newly-ingested files),
    `strict_reconciliation=True` gates on every historical mismatch ever
    ingested, not only new ones - a single old, unresolved mismatch blocks
    the very first strict run until it's fixed or excluded.
    """
    datalake = datalake or get_datalake()

    unmapped = find_unmapped_accounts(datalake)
    if not unmapped.empty:
        raise UnmappedAccountsError(unmapped)

    transformer = SilverTransformer(datalake)
    bronze_frames = transformer._read_bronze_frames()

    # Quality gate: quarantine reconciliation-mismatched ingestions.
    eligible_ids, excluded_ingestions = _eligible_ingestion_ids(bronze_frames)
    if excluded_ingestions:
        logger.warning(
            "Quarantining %d ingestion(s) due to reconciliation mismatch "
            "(override with 'cli.py ingestions override'):",
            len(excluded_ingestions),
        )
        for ex in excluded_ingestions:
            logger.warning(
                "  %s (%s): %s",
                ex["ingestion_id"][:12],
                ex["source_type"],
                ex["reason"],
            )
        bronze_frames = _filter_to_eligible(bronze_frames, eligible_ids)

    accounts_df = build_accounts_table()
    transactions_df, sources_df = transformer.normalize_transactions(bronze_frames)
    holdings_df = transformer.normalize_holdings(bronze_frames)
    ledger_df = transformer.normalize_account_ledger(bronze_frames)
    derived_ledger = _derive_rollforward_ledger_rows(
        transactions_df, ledger_df, datalake
    )
    if not derived_ledger.empty:
        assert list(derived_ledger.columns) == _LEDGER_COLUMNS, (
            f"derived ledger columns {list(derived_ledger.columns)} "
            f"do not match _LEDGER_COLUMNS {_LEDGER_COLUMNS}"
        )
        ledger_df = pd.concat([ledger_df, derived_ledger], ignore_index=True)
    plan_it_df = transformer.normalize_plan_it_instalments(bronze_frames)

    # Re-verify Bronze's own per-file B1 self-check still holds after
    # match_transactions() has run - catches a dedup/matching bug that
    # silently drops or duplicates a genuine transaction, which Bronze's
    # own pre-matching check can never see (item 3 of the reconciliation
    # hardening work; see transformers/silver_reconciliation.py).
    bronze_anchors = find_reconciliation_status(datalake)
    silver_breaks_df = find_silver_reconciliation_breaks(
        bronze_anchors, sources_df, transactions_df
    )
    silver_mismatches = silver_breaks_df[silver_breaks_df["matches"] == False]  # noqa: E712
    if not silver_mismatches.empty:
        logger.warning(
            "%d ingestion(s) reconcile against their own Bronze anchor but "
            "no longer roll forward correctly after Silver transaction "
            "matching - the matching/dedup step may have dropped or "
            "duplicated a genuine transaction:",
            len(silver_mismatches),
        )
        for row in silver_mismatches.itertuples():
            logger.warning(
                "  %s (%s): expected %s, derived %s",
                row.ingestion_id[:12],
                row.source_type,
                row.expected_closing_minor,
                row.silver_derived_closing_minor,
            )

    # Cross-file continuity (item 2): does one file's closing anchor equal
    # the next file's opening anchor, per account? Complements the two
    # per-file checks above, which can't see across file boundaries.
    continuity_df = find_balance_continuity(datalake)

    # Collect build metadata from Bronze frames.
    ingestion_ids: List[str] = []
    parser_versions: Dict[str, str] = {}
    for source_type, df in bronze_frames.items():
        if "ingestion_id" in df.columns:
            ingestion_ids.extend(df["ingestion_id"].dropna().unique().tolist())
        if "parser_version" in df.columns:
            versions = df["parser_version"].dropna().unique()
            if len(versions) > 0:
                parser_versions[source_type] = versions[0]

    input_ingestion_ids = sorted(set(ingestion_ids))

    # A stable build_id, generated once, stamps both the reconciliation_log
    # rows below and the published build itself - so a build's log is
    # always identifiable by the same id the build directory/manifest uses.
    build_id = generate_build_id()
    reconciliation_log_df = build_reconciliation_log(
        bronze_anchors, continuity_df, silver_breaks_df, build_id
    )

    if strict_reconciliation:
        genuine_mismatches = reconciliation_log_df[
            reconciliation_log_df["matches"] == False  # noqa: E712
        ]
        if not genuine_mismatches.empty:
            raise ReconciliationMismatchError(genuine_mismatches)

    # Silver is a materialization of the current immutable Bronze set. Never
    # merge with a prior build: that preserves stale rows after parser fixes.
    tables = {
        "accounts": accounts_df,
        "transaction_sources": sources_df,
        "transactions": transactions_df,
        "holdings": holdings_df,
        "account_ledger": ledger_df,
        "plan_it_instalments": plan_it_df,
        "reconciliation_log": reconciliation_log_df,
    }

    build_id = publish_silver_build(
        tables=tables,
        input_ingestion_ids=input_ingestion_ids,
        excluded_ingestions=excluded_ingestions,
        build_id=build_id,
        parser_versions=parser_versions,
        silver_dir=_SILVER_DIR,
    )

    logger.info(
        "Bronze->Silver complete: build %s - %d accounts, %d transactions, "
        "%d holdings, %d ledger entries, %d plan-it instalments, "
        "%d provenance rows",
        build_id,
        len(accounts_df),
        len(transactions_df),
        len(holdings_df),
        len(ledger_df),
        len(plan_it_df),
        len(sources_df),
    )

    return {
        "build_id": build_id,
        "accounts": accounts_df,
        "transaction_sources": sources_df,
        "transactions": transactions_df,
        "holdings": holdings_df,
        "account_ledger": ledger_df,
        "plan_it_instalments": plan_it_df,
        "reconciliation_log": reconciliation_log_df,
    }
