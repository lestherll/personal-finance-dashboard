"""Bronze -> Silver normalization.

Unifies the per-adapter `raw_data` shapes (see adapters/*.py) into three
common Silver schemas: transactions, holdings, and account_ledger.

Known limitation: Natwest PDF and AmEx statements never capture a year in
their transaction dates (e.g. "15 Jan"). Year is inferred from the Bronze
upload_timestamp rather than fixed properly at the adapter level - see
CLAUDE.md gotchas. Fixing this at the source belongs to a Phase 1 adapter
follow-up, not this transformer.
"""

import logging
import re
from typing import Any, Dict, List, Optional

import pandas as pd

from adapters.factory import AdapterFactory
from models.datalake import DataLake, get_datalake
from transformers.account_config import (
    UnmappedAccountsError,
    build_accounts_table,
    find_unmapped_accounts,
    get_account_id,
)

logger = logging.getLogger(__name__)

# source_types with a "balance" in raw_data usable for account_ledger.
# "natwest-pdf" (online export) and "vanguard-pdf" (cash_balance is a
# different metric from Portfolio Value) are deliberately excluded - see
# CLAUDE.md gotchas.
LEDGER_SOURCE_TYPES = {
    "natwest",
    "vanguard",
    "kroo",
    "amex",
    "firstdirect",
    "natwest-statement",
    "monzo-pdf",
}

TRANSACTION_SOURCE_TYPES = {
    "monzo",
    "natwest",
    "kroo",
    "natwest-pdf",
    "natwest-statement",
    "firstdirect",
    "amex",
    "vanguard-pdf",
    "monzo-pdf",
    "chase",
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


def _normalize_monzo(raw: Dict[str, Any], reference: Any) -> Dict[str, Any]:
    is_search_format = "id" in raw and "created" in raw
    if is_search_format:
        transaction_date = _parse_date(raw.get("created"))
        description = raw.get("title") or raw.get("subtitle") or ""
        amount = float(raw.get("amount") or 0)
        currency = raw.get("currency") or "GBP"
        category = raw.get("categories")
    else:
        date_str = raw.get("Date", "")
        time_str = raw.get("Time", "")
        transaction_date = _parse_date(
            f"{date_str} {time_str}".strip(), "%d/%m/%Y %H:%M:%S"
        ) or _parse_date(date_str, "%d/%m/%Y")
        description = raw.get("Name") or raw.get("Description") or ""
        amount = float(raw.get("Amount") or 0)
        currency = raw.get("Currency") or "GBP"
        category = raw.get("Category")
    return {
        "transaction_date": transaction_date,
        "description": description,
        "amount": amount,
        "currency": currency,
        "category": category,
    }


def _normalize_natwest_csv(raw: Dict[str, Any], reference: Any) -> Dict[str, Any]:
    return {
        "transaction_date": _parse_date(raw.get("Transaction Date", ""), "%d/%m/%Y"),
        "description": raw.get("Transaction Narrative", ""),
        "amount": float(raw.get("Transaction Amount") or 0),
        "currency": "GBP",
        "category": None,
    }


def _normalize_pdf_full_month_year(
    raw: Dict[str, Any], reference: Any
) -> Dict[str, Any]:
    """Kroo: 'DD Month YYYY'."""
    return {
        "transaction_date": _parse_date(raw.get("date", ""), "%d %B %Y"),
        "description": raw.get("description", ""),
        "amount": float(raw.get("amount") or 0),
        "currency": "GBP",
        "category": None,
    }


def _normalize_pdf_no_year(raw: Dict[str, Any], reference: Any) -> Dict[str, Any]:
    """Natwest PDF, AmEx: 'DD Mmm', with the year attached at the adapter
    level (resolve_year_in_period()) whenever the statement's period header
    was found. Falls back to upload-timestamp inference (Gotcha #7) for
    Bronze rows ingested before that existed, or where the period header
    wasn't present in the statement text.

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
        "amount": float(raw.get("amount") or 0),
        "currency": "GBP",
        "category": None,
    }


def _normalize_pdf_short_year(raw: Dict[str, Any], reference: Any) -> Dict[str, Any]:
    """First Direct: 'DD Mmm YY'."""
    return {
        "transaction_date": _parse_date(raw.get("date", ""), "%d %b %y"),
        "description": raw.get("description", ""),
        "amount": float(raw.get("amount") or 0),
        "currency": "GBP",
        "category": None,
    }


def _normalize_pdf_slash_date(raw: Dict[str, Any], reference: Any) -> Dict[str, Any]:
    """Vanguard PDF, Monzo PDF: 'DD/MM/YYYY'."""
    return {
        "transaction_date": _parse_date(raw.get("date", ""), "%d/%m/%Y"),
        "description": raw.get("description", ""),
        "amount": float(raw.get("amount") or 0),
        "currency": "GBP",
        "category": None,
    }


def _normalize_natwest_statement(raw: Dict[str, Any], reference: Any) -> Dict[str, Any]:
    """Natwest quarterly Statement PDF: 'DD Mmm' (all-caps month), no year in
    source text - same as natwest-pdf/amex."""
    return {
        "transaction_date": _infer_dated_with_year(
            raw.get("date", ""), "%d %b", reference
        ),
        "description": raw.get("description", ""),
        "amount": float(raw.get("amount") or 0),
        "currency": "GBP",
        "category": None,
    }


_TRANSACTION_NORMALIZERS = {
    "monzo": _normalize_monzo,
    "natwest": _normalize_natwest_csv,
    "kroo": _normalize_pdf_full_month_year,
    "natwest-pdf": _normalize_pdf_no_year,
    "natwest-statement": _normalize_natwest_statement,
    "firstdirect": _normalize_pdf_short_year,
    "amex": _normalize_pdf_no_year,
    "vanguard-pdf": _normalize_pdf_slash_date,
    "monzo-pdf": _normalize_pdf_slash_date,
    "chase": _normalize_pdf_no_year,
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

    No ISIN in this format (unlike the Vanguard CSV holdings export) -
    only a fund name.
    """
    return {
        "isin": None,
        "fund_name": raw.get("fund_name"),
        "quantity": _parse_money(raw.get("quantity")),
        "unit_price": _parse_money(raw.get("unit_price")),
        "total_value": _parse_money(raw.get("total_value")),
        "currency": "GBP",
        "as_of_date": _parse_date(raw.get("as_of_date", ""), "%d %B %Y") or reference,
    }


def _ledger_from_natwest(raw: Dict[str, Any], reference: Any) -> Dict[str, Any]:
    return {
        "balance": float(raw.get("Balance") or 0),
        "as_of_date": _parse_date(raw.get("Balance Date", ""), "%d/%m/%Y"),
    }


def _ledger_from_vanguard(raw: Dict[str, Any], reference: Any) -> Dict[str, Any]:
    return {
        "balance": float(raw.get("Portfolio Value") or raw.get("Value") or 0),
        "as_of_date": _parse_date(raw.get("Time", ""), "%d/%m/%Y") or reference,
    }


def _ledger_from_kroo(raw: Dict[str, Any], reference: Any) -> Dict[str, Any]:
    return {
        "balance": float(raw.get("balance") or 0),
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
        "balance": float(raw.get("balance") or 0),
        "as_of_date": as_of_date,
    }


def _ledger_from_firstdirect(raw: Dict[str, Any], reference: Any) -> Dict[str, Any]:
    return {
        "balance": float(raw.get("balance") or 0),
        "as_of_date": _parse_date(raw.get("date", ""), "%d %b %y"),
    }


def _ledger_from_natwest_statement(
    raw: Dict[str, Any], reference: Any
) -> Dict[str, Any]:
    return {
        "balance": float(raw.get("balance") or 0),
        "as_of_date": _infer_dated_with_year(raw.get("date", ""), "%d %b", reference),
    }


def _ledger_from_monzo_pdf(raw: Dict[str, Any], reference: Any) -> Dict[str, Any]:
    return {
        "balance": float(raw.get("balance") or 0),
        "as_of_date": _parse_date(raw.get("date", ""), "%d/%m/%Y"),
    }


_LEDGER_NORMALIZERS = {
    "natwest": _ledger_from_natwest,
    "vanguard": _ledger_from_vanguard,
    "kroo": _ledger_from_kroo,
    "amex": _ledger_from_amex,
    "firstdirect": _ledger_from_firstdirect,
    "natwest-statement": _ledger_from_natwest_statement,
    "monzo-pdf": _ledger_from_monzo_pdf,
}

_TRANSACTIONS_COLUMNS = [
    "bronze_source_key",
    "source_type",
    "account_id",
    "transaction_date",
    "description",
    "amount",
    "currency",
    "category",
    "ingested_at",
]

_HOLDINGS_COLUMNS = [
    "bronze_source_key",
    "account_id",
    "isin",
    "fund_name",
    "quantity",
    "unit_price",
    "total_value",
    "currency",
    "as_of_date",
]

_LEDGER_COLUMNS = [
    "bronze_source_key",
    "account_id",
    "source_type",
    "balance",
    "as_of_date",
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
                        "bronze_source_key": bronze_row.get("bronze_source_key"),
                        "source_type": source_type,
                        "account_id": account_id,
                        "ingested_at": pd.Timestamp.now(),
                        **normalized,
                    }
                )

        if not rows:
            return pd.DataFrame(columns=_TRANSACTIONS_COLUMNS)

        result = pd.DataFrame(rows)
        result["transaction_date"] = pd.to_datetime(result["transaction_date"])
        result = _dedupe_natwest_cross_format(result)
        return result

    def normalize_holdings(
        self, bronze_frames: Dict[str, pd.DataFrame]
    ) -> pd.DataFrame:
        """Holdings come from Vanguard PDF statements' investment tables.

        (Superseded the old Vanguard CSV holdings export - CSV adapters are
        disabled, and the PDF statement already carries this per product
        wrapper, so there's no need for a separate CSV source anymore.)
        """
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

    def normalize_account_ledger(
        self, bronze_frames: Dict[str, pd.DataFrame]
    ) -> pd.DataFrame:
        rows = []
        for source_type in LEDGER_SOURCE_TYPES:
            df = bronze_frames.get(source_type)
            if df is None or df.empty:
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
                        "bronze_source_key": bronze_row.get("bronze_source_key"),
                        "account_id": account_id,
                        "source_type": source_type,
                        **normalized,
                    }
                )

        if not rows:
            return pd.DataFrame(columns=_LEDGER_COLUMNS)

        result = pd.DataFrame(rows)
        result["as_of_date"] = pd.to_datetime(result["as_of_date"])
        return result


_NATWEST_CROSS_FORMAT_TYPES = {"natwest-pdf", "natwest-statement"}


def _dedupe_natwest_cross_format(df: pd.DataFrame) -> pd.DataFrame:
    """Drop duplicate transactions between the two Natwest PDF formats.

    "natwest-pdf" (the online "Transactions" export) and "natwest-statement"
    (the quarterly Statement PDF) can cover overlapping date ranges - if a
    user uploads both, the same real-world transaction appears under two
    different source_types with two different bronze_source_keys, so the
    usual bronze_source_key-based dedup (`_dedupe_with_existing`) can't
    catch it, since that key differs by construction across adapters.

    Matched by (account_id, transaction_date, amount) - description text
    differs materially between the two formats for the same transaction
    (e.g. "KROO ACCOUNT Mobile/Online Transaction" vs "OnLine Transaction
    KROO ACCOUNT SALARY VIA MOBILE - PYMT FP ..."), so it isn't usable as a
    matching key. Prefers keeping the natwest-statement row (carries real
    balance data) over the natwest-pdf one. Uses count-based (multiset)
    removal, not a blanket drop-duplicates, so genuinely repeated same-day/
    same-amount transactions within a single format aren't mistakenly
    dropped.
    """
    is_cross_format = df["source_type"].isin(_NATWEST_CROSS_FORMAT_TYPES)
    if not is_cross_format.any():
        return df

    subset = df[is_cross_format]
    rest = df[~is_cross_format]

    pdf_rows = subset[subset["source_type"] == "natwest-pdf"]
    statement_rows = subset[subset["source_type"] == "natwest-statement"]

    key_cols = ["account_id", "transaction_date", "amount"]
    remaining_statement_matches = dict(statement_rows.groupby(key_cols).size())

    drop_indices = []
    for idx, row in pdf_rows.iterrows():
        key = (row["account_id"], row["transaction_date"], row["amount"])
        if remaining_statement_matches.get(key, 0) > 0:
            drop_indices.append(idx)
            remaining_statement_matches[key] -= 1

    deduped_pdf_rows = pdf_rows.drop(index=drop_indices)

    return pd.concat(
        [rest, deduped_pdf_rows, statement_rows], ignore_index=False
    ).sort_index()


def _dedupe_with_existing(
    datalake: DataLake, entity_type: str, new_df: pd.DataFrame, key_columns: List[str]
) -> pd.DataFrame:
    """Merge freshly computed rows with existing Silver data, keeping the newest."""
    existing = datalake.read_silver(entity_type)
    if existing is None or existing.empty:
        combined = new_df
    else:
        combined = pd.concat([existing, new_df], ignore_index=True)

    if combined.empty:
        return combined
    return combined.drop_duplicates(subset=key_columns, keep="last").reset_index(
        drop=True
    )


def run_bronze_to_silver(
    datalake: Optional[DataLake] = None,
) -> Dict[str, pd.DataFrame]:
    """Run the full Bronze -> Silver transformation and write all four Silver tables.

    Idempotent: re-running against unchanged Bronze data does not create
    duplicate rows (dedup by bronze_source_key / account_id).

    Fails fast, but as a single pre-flight check: if any Bronze account isn't
    mapped yet, raises UnmappedAccountsError listing every unmapped account
    found (not just the first one), before writing anything. See
    transformers/account_config.py::find_unmapped_accounts / register_account.
    """
    datalake = datalake or get_datalake()

    unmapped = find_unmapped_accounts(datalake)
    if not unmapped.empty:
        raise UnmappedAccountsError(unmapped)

    transformer = SilverTransformer(datalake)
    bronze_frames = transformer._read_bronze_frames()

    accounts_df = build_accounts_table()
    transactions_df = transformer.normalize_transactions(bronze_frames)
    holdings_df = transformer.normalize_holdings(bronze_frames)
    ledger_df = transformer.normalize_account_ledger(bronze_frames)

    accounts_df = _dedupe_with_existing(
        datalake, "accounts", accounts_df, ["account_id"]
    )
    transactions_df = _dedupe_with_existing(
        datalake, "transactions", transactions_df, ["bronze_source_key"]
    )
    holdings_df = _dedupe_with_existing(
        datalake, "holdings", holdings_df, ["bronze_source_key"]
    )
    ledger_df = _dedupe_with_existing(
        datalake, "account_ledger", ledger_df, ["bronze_source_key"]
    )

    datalake.write_silver("accounts", accounts_df)
    datalake.write_silver("transactions", transactions_df)
    datalake.write_silver("holdings", holdings_df)
    datalake.write_silver("account_ledger", ledger_df)

    logger.info(
        "Bronze->Silver complete: %d accounts, %d transactions, %d holdings, %d ledger entries",
        len(accounts_df),
        len(transactions_df),
        len(holdings_df),
        len(ledger_df),
    )

    return {
        "accounts": accounts_df,
        "transactions": transactions_df,
        "holdings": holdings_df,
        "account_ledger": ledger_df,
    }
