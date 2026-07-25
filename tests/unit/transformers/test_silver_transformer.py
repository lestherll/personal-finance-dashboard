"""Tests for Bronze -> Silver normalization."""

import pandas as pd
import pytest

from transformers.account_config import get_account_id
from transformers.silver_transformer import (
    SilverTransformer,
    _dedupe_natwest_cross_format,
    _dedupe_with_existing,
)

# Real (test-only) entries so get_account_id resolves without hitting the fallback.
_KROO_ID = "fd7a2651d39e"
_NATWEST_PDF_ID = "43ae9e53d8a2"
_FIRSTDIRECT_ID = "8765efc92b23"
_AMEX_ID = "63e97de2060d"
_VANGUARD_ISA_ID = "992add198186"


def _bronze_frame(
    source_type,
    raw_rows,
    upload_timestamp=None,
    account_identifier=None,
    record_type="transaction",
):
    """Build a Bronze-shaped DataFrame matching what datalake.read_bronze() returns."""
    upload_timestamp = upload_timestamp or pd.Timestamp("2026-06-01")
    return pd.DataFrame(
        [
            {
                "bronze_source_key": f"{source_type}_key_{i}",
                "source_type": source_type,
                "raw_data": raw,
                "upload_timestamp": upload_timestamp,
                "filename": "test_upload",
                "account_identifier": account_identifier,
                "record_type": record_type,
            }
            for i, raw in enumerate(raw_rows)
        ]
    )


@pytest.fixture
def transformer():
    # normalize_* methods don't touch self.datalake, so a dummy stand-in is fine.
    return SilverTransformer(datalake=object())


class TestNormalizeTransactionsMonzo:
    def test_full_export_format(self, transformer):
        raw = {
            "Date": "15/01/2024",
            "Time": "14:30:00",
            "Name": "Tesco Groceries",
            "Amount": "-25.50",
            "Currency": "GBP",
            "Category": "Groceries",
        }
        df = transformer.normalize_transactions(
            {"monzo": _bronze_frame("monzo", [raw])}
        )

        assert len(df) == 1
        row = df.iloc[0]
        assert (
            row["account_id"] == "acc_monzo_current"
        )  # no identifier -> source_type fallback
        assert row["description"] == "Tesco Groceries"
        assert row["amount"] == -25.50
        assert row["currency"] == "GBP"
        assert row["category"] == "Groceries"
        assert row["transaction_date"] == pd.Timestamp("2024-01-15 14:30:00")

    def test_search_export_format(self, transformer):
        raw = {
            "id": "tx_1",
            "created": "2024-01-15T14:30:00Z",
            "title": "Tesco Groceries",
            "subtitle": "Tesco Stores",
            "amount": "-25.50",
            "currency": "GBP",
            "categories": "groceries",
        }
        df = transformer.normalize_transactions(
            {"monzo": _bronze_frame("monzo", [raw])}
        )

        assert len(df) == 1
        row = df.iloc[0]
        assert row["description"] == "Tesco Groceries"
        assert row["amount"] == -25.50
        assert row["category"] == "groceries"
        assert row["transaction_date"] == pd.Timestamp("2024-01-15 14:30:00")


class TestNormalizeTransactionsNatwestCsv:
    def test_normalizes_signed_amount_and_narrative(self, transformer):
        raw = {
            "Transaction Type": "DEBIT",
            "Transaction Date": "15/01/2024",
            "Transaction Amount": "-50.00",
            "Transaction Narrative": "FUEL SHELL PETROL STATION",
            "Balance": "450.00",
            "Balance Date": "15/01/2024",
        }
        df = transformer.normalize_transactions(
            {"natwest": _bronze_frame("natwest", [raw])}
        )

        assert len(df) == 1
        row = df.iloc[0]
        assert (
            row["account_id"] == "acc_natwest_current"
        )  # no identifier -> source_type fallback
        assert row["amount"] == -50.00
        assert row["description"] == "FUEL SHELL PETROL STATION"
        assert row["transaction_date"] == pd.Timestamp("2024-01-15")


class TestNormalizeTransactionsPdfSources:
    def test_kroo_full_date_and_identifier(self, transformer):
        raw = {"date": "12 January 2026", "description": "Coffee Shop", "amount": -4.50}
        bronze = _bronze_frame("kroo", [raw], account_identifier=_KROO_ID)
        df = transformer.normalize_transactions({"kroo": bronze})

        row = df.iloc[0]
        assert row["account_id"] == get_account_id(_KROO_ID, "kroo")
        assert row["transaction_date"] == pd.Timestamp("2026-01-12")
        assert row["amount"] == -4.50

    def test_natwest_pdf_infers_year_from_upload_timestamp(self, transformer):
        raw = {"date": "15 Jan", "description": "Card Payment", "amount": -20.0}
        bronze = _bronze_frame(
            "natwest-pdf",
            [raw],
            upload_timestamp=pd.Timestamp("2026-02-01"),
            account_identifier=_NATWEST_PDF_ID,
        )
        df = transformer.normalize_transactions({"natwest-pdf": bronze})

        row = df.iloc[0]
        assert row["transaction_date"] == pd.Timestamp("2026-01-15")
        assert row["account_id"] == get_account_id(_NATWEST_PDF_ID, "natwest-pdf")

    def test_natwest_statement_infers_year_and_captures_balance(self, transformer):
        raw = {
            "date": "26 FEB",
            "description": "Automated Credit 3305 JPMCB",
            "amount": 2728.54,
            "balance": 3738.54,
        }
        bronze = _bronze_frame(
            "natwest-statement",
            [raw],
            upload_timestamp=pd.Timestamp("2026-05-13"),
            account_identifier=_NATWEST_PDF_ID,
        )
        df = transformer.normalize_transactions({"natwest-statement": bronze})

        row = df.iloc[0]
        assert row["transaction_date"] == pd.Timestamp("2026-02-26")
        assert row["amount"] == 2728.54
        assert row["account_id"] == get_account_id(_NATWEST_PDF_ID, "natwest-statement")

    def test_amex_infers_year_from_upload_timestamp(self, transformer):
        raw = {"date": "28 Apr", "description": "Amazon", "amount": -99.99}
        bronze = _bronze_frame(
            "amex",
            [raw],
            upload_timestamp=pd.Timestamp("2026-05-10"),
            account_identifier=_AMEX_ID,
        )
        df = transformer.normalize_transactions({"amex": bronze})

        row = df.iloc[0]
        assert row["transaction_date"] == pd.Timestamp("2026-04-28")

    def test_amex_handles_year_boundary(self, transformer):
        """A December transaction uploaded in January should resolve to the prior year."""
        raw = {"date": "20 Dec", "description": "Gift Shop", "amount": -15.0}
        bronze = _bronze_frame(
            "amex",
            [raw],
            upload_timestamp=pd.Timestamp("2026-01-05"),
            account_identifier=_AMEX_ID,
        )
        df = transformer.normalize_transactions({"amex": bronze})

        row = df.iloc[0]
        assert row["transaction_date"] == pd.Timestamp("2025-12-20")

    def test_first_direct_two_digit_year(self, transformer):
        raw = {"date": "15 Jan 24", "description": "Restaurant", "amount": -30.0}
        bronze = _bronze_frame("firstdirect", [raw], account_identifier=_FIRSTDIRECT_ID)
        df = transformer.normalize_transactions({"firstdirect": bronze})

        row = df.iloc[0]
        assert row["transaction_date"] == pd.Timestamp("2024-01-15")

    def test_vanguard_pdf_slash_date(self, transformer):
        raw = {"date": "15/01/2024", "description": "Fund Purchase", "amount": -500.0}
        bronze = _bronze_frame(
            "vanguard-pdf", [raw], account_identifier=_VANGUARD_ISA_ID
        )
        df = transformer.normalize_transactions({"vanguard-pdf": bronze})

        row = df.iloc[0]
        assert row["account_id"] == get_account_id(_VANGUARD_ISA_ID, "vanguard-pdf")
        assert row["transaction_date"] == pd.Timestamp("2024-01-15")

    def test_vanguard_pdf_holdings_excluded_from_transactions(self, transformer):
        """A holding-record_type row must not leak into normalize_transactions."""
        holding_raw = {
            "wrapper": "ISA",
            "fund_name": "Some Fund",
            "quantity": "5.00",
            "unit_price": "£1.00",
            "total_value": "£5.00",
            "as_of_date": "08 July 2026",
        }
        bronze = _bronze_frame(
            "vanguard-pdf",
            [holding_raw],
            account_identifier=_VANGUARD_ISA_ID,
            record_type="holding",
        )
        df = transformer.normalize_transactions({"vanguard-pdf": bronze})
        assert df.empty


class TestNormalizeTransactionsEmpty:
    def test_no_bronze_data_returns_empty_frame_with_schema(self, transformer):
        df = transformer.normalize_transactions({})
        assert df.empty
        assert "bronze_source_key" in df.columns
        assert "transaction_date" in df.columns


class TestNormalizeHoldings:
    def test_vanguard_pdf_holdings_normalized(self, transformer):
        raw = {
            "wrapper": "ISA",
            "fund_name": "Vanguard FTSE Global All Cap Index Fund Accumulation",
            "quantity": "5.68",
            "unit_price": "£287.13",
            "total_value": "£1,630.28",
            "as_of_date": "08 July 2026",
        }
        bronze = _bronze_frame(
            "vanguard-pdf",
            [raw],
            account_identifier=_VANGUARD_ISA_ID,
            record_type="holding",
        )
        df = transformer.normalize_holdings({"vanguard-pdf": bronze})

        assert len(df) == 1
        row = df.iloc[0]
        assert row["account_id"] == get_account_id(_VANGUARD_ISA_ID, "vanguard-pdf")
        assert (
            row["fund_name"] == "Vanguard FTSE Global All Cap Index Fund Accumulation"
        )
        assert row["quantity"] == 5.68
        assert row["unit_price"] == 287.13
        assert row["total_value"] == 1630.28
        assert row["as_of_date"] == pd.Timestamp("2026-07-08")
        assert row["isin"] is None

    def test_vanguard_pdf_holdings_handles_dash_quantity(self, transformer):
        """'Cash account' holding rows use '-' for quantity/price."""
        raw = {
            "wrapper": "ISA",
            "fund_name": "Cash account",
            "quantity": "-",
            "unit_price": "-",
            "total_value": "£0.15",
            "as_of_date": "08 July 2026",
        }
        bronze = _bronze_frame(
            "vanguard-pdf",
            [raw],
            account_identifier=_VANGUARD_ISA_ID,
            record_type="holding",
        )
        df = transformer.normalize_holdings({"vanguard-pdf": bronze})

        row = df.iloc[0]
        assert row["quantity"] == 0.0
        assert row["unit_price"] == 0.0
        assert row["total_value"] == 0.15

    def test_vanguard_pdf_transactions_excluded_from_holdings(self, transformer):
        """A transaction-record_type row must not leak into normalize_holdings."""
        txn_raw = {"date": "15/01/2024", "description": "Deposit", "amount": 100.0}
        bronze = _bronze_frame(
            "vanguard-pdf",
            [txn_raw],
            account_identifier=_VANGUARD_ISA_ID,
            record_type="transaction",
        )
        df = transformer.normalize_holdings({"vanguard-pdf": bronze})
        assert df.empty

    def test_no_data_returns_empty_frame_with_schema(self, transformer):
        df = transformer.normalize_holdings({})
        assert df.empty
        assert "isin" in df.columns


class TestNormalizeAccountLedger:
    def test_natwest_balance_captured(self, transformer):
        raw = {
            "Transaction Type": "DEBIT",
            "Transaction Date": "15/01/2024",
            "Transaction Amount": "-50.00",
            "Transaction Narrative": "FUEL",
            "Balance": "450.00",
            "Balance Date": "15/01/2024",
        }
        df = transformer.normalize_account_ledger(
            {"natwest": _bronze_frame("natwest", [raw])}
        )

        assert len(df) == 1
        row = df.iloc[0]
        assert row["account_id"] == "acc_natwest_current"
        assert row["balance"] == 450.00
        assert row["as_of_date"] == pd.Timestamp("2024-01-15")

    def test_vanguard_csv_portfolio_value_captured(self, transformer):
        raw = {
            "ISIN": "GB0009374884",
            "Fund Name": "Vanguard FTSE All-World UCITS ETF",
            "Quantity": "50.00",
            "Price": "150.25",
            "Value": "7512.50",
            "Portfolio Value": "50000.00",
            "Time": "15/01/2024",
        }
        df = transformer.normalize_account_ledger(
            {"vanguard": _bronze_frame("vanguard", [raw])}
        )

        assert len(df) == 1
        assert df.iloc[0]["balance"] == 50000.00

    def test_kroo_balance_captured(self, transformer):
        raw = {
            "date": "12 January 2026",
            "description": "Coffee Shop",
            "amount": -4.50,
            "balance": 495.50,
        }
        df = transformer.normalize_account_ledger(
            {"kroo": _bronze_frame("kroo", [raw], account_identifier=_KROO_ID)}
        )

        assert len(df) == 1
        row = df.iloc[0]
        assert row["account_id"] == "acc_kroo_current"
        assert row["balance"] == 495.50
        assert row["as_of_date"] == pd.Timestamp("2026-01-12")

    def test_amex_balance_captured(self, transformer):
        raw = {
            "date": "31 Jan",
            "description": "PAYMENT RECEIVED - THANK YOU",
            "amount": 769.58,
            "balance": 0.0,
        }
        df = transformer.normalize_account_ledger(
            {
                "amex": _bronze_frame(
                    "amex",
                    [raw],
                    upload_timestamp=pd.Timestamp("2026-02-19"),
                    account_identifier=_AMEX_ID,
                )
            }
        )

        assert len(df) == 1
        row = df.iloc[0]
        assert row["account_id"] == "acc_amex_credit_1"
        assert row["balance"] == 0.0
        assert row["as_of_date"] == pd.Timestamp("2026-01-31")

    def test_firstdirect_balance_captured(self, transformer):
        raw = {
            "date": "30 Apr 26",
            "description": "PAYMENT RECEIVED - THANK YOU",
            "amount": 47.22,
            "balance": 1526.77,
        }
        df = transformer.normalize_account_ledger(
            {
                "firstdirect": _bronze_frame(
                    "firstdirect", [raw], account_identifier=_FIRSTDIRECT_ID
                )
            }
        )

        assert len(df) == 1
        row = df.iloc[0]
        assert row["account_id"] == "acc_firstdirect_credit"
        assert row["balance"] == 1526.77
        assert row["as_of_date"] == pd.Timestamp("2026-04-30")

    def test_natwest_statement_balance_captured(self, transformer):
        raw = {
            "date": "26 FEB",
            "description": "Automated Credit 3305 JPMCB",
            "amount": 2728.54,
            "balance": 3738.54,
        }
        df = transformer.normalize_account_ledger(
            {
                "natwest-statement": _bronze_frame(
                    "natwest-statement",
                    [raw],
                    upload_timestamp=pd.Timestamp("2026-05-13"),
                    account_identifier=_NATWEST_PDF_ID,
                )
            }
        )

        assert len(df) == 1
        row = df.iloc[0]
        assert row["account_id"] == "acc_natwest_current"
        assert row["balance"] == 3738.54
        assert row["as_of_date"] == pd.Timestamp("2026-02-26")

    def test_pdf_sources_without_real_balance_excluded_from_ledger(self, transformer):
        """natwest-pdf (no balance data at all) and vanguard-pdf (cash_balance is a
        different metric from Portfolio Value) must not appear in the ledger."""
        natwest_pdf_raw = {
            "date": "26 May",
            "description": "KROO ACCOUNT",
            "amount": -2745.33,
        }
        vanguard_pdf_raw = {
            "date": "21/04/2026",
            "description": "Regular Deposit",
            "amount": 150.0,
            "cash_balance": 50.15,
        }
        df = transformer.normalize_account_ledger(
            {
                "natwest-pdf": _bronze_frame("natwest-pdf", [natwest_pdf_raw]),
                "vanguard-pdf": _bronze_frame("vanguard-pdf", [vanguard_pdf_raw]),
            }
        )
        assert df.empty

    def test_no_data_returns_empty_frame_with_schema(self, transformer):
        df = transformer.normalize_account_ledger({})
        assert df.empty
        assert "balance" in df.columns


class TestDedupeNatwestCrossFormat:
    """natwest-pdf (online export) and natwest-statement (quarterly PDF) can
    describe the same real transaction if both get uploaded for overlapping
    periods - matched by (account_id, transaction_date, amount), not
    bronze_source_key, which differs by construction across the two."""

    def test_drops_matching_pdf_row_keeps_statement_row(self):
        df = pd.DataFrame(
            [
                {
                    "source_type": "natwest-pdf",
                    "account_id": "acc1",
                    "transaction_date": pd.Timestamp("2026-02-26"),
                    "amount": 2728.54,
                    "description": "3305 JPMCB Automated Credit",
                },
                {
                    "source_type": "natwest-statement",
                    "account_id": "acc1",
                    "transaction_date": pd.Timestamp("2026-02-26"),
                    "amount": 2728.54,
                    "description": "Automated Credit 3305 JPMCB",
                },
            ]
        )
        result = _dedupe_natwest_cross_format(df)
        assert len(result) == 1
        assert result.iloc[0]["source_type"] == "natwest-statement"

    def test_unmatched_pdf_row_kept(self):
        df = pd.DataFrame(
            [
                {
                    "source_type": "natwest-pdf",
                    "account_id": "acc1",
                    "transaction_date": pd.Timestamp("2026-06-15"),
                    "amount": -50.0,
                    "description": "No statement coverage for this date",
                },
            ]
        )
        result = _dedupe_natwest_cross_format(df)
        assert len(result) == 1
        assert result.iloc[0]["source_type"] == "natwest-pdf"

    def test_other_source_types_untouched(self):
        df = pd.DataFrame(
            [
                {
                    "source_type": "kroo",
                    "account_id": "acc2",
                    "transaction_date": pd.Timestamp("2026-02-26"),
                    "amount": 2728.54,
                    "description": "unrelated source_type",
                },
            ]
        )
        result = _dedupe_natwest_cross_format(df)
        assert len(result) == 1

    def test_count_based_removal_preserves_genuine_repeats(self):
        """Two identical same-day/same-amount pdf rows with only one statement
        match should only drop one of them, not both."""
        df = pd.DataFrame(
            [
                {
                    "source_type": "natwest-pdf",
                    "account_id": "acc1",
                    "transaction_date": pd.Timestamp("2026-03-01"),
                    "amount": 10.0,
                    "description": "first",
                },
                {
                    "source_type": "natwest-pdf",
                    "account_id": "acc1",
                    "transaction_date": pd.Timestamp("2026-03-01"),
                    "amount": 10.0,
                    "description": "second",
                },
                {
                    "source_type": "natwest-statement",
                    "account_id": "acc1",
                    "transaction_date": pd.Timestamp("2026-03-01"),
                    "amount": 10.0,
                    "description": "statement version",
                },
            ]
        )
        result = _dedupe_natwest_cross_format(df)
        assert len(result) == 2
        assert (result["source_type"] == "natwest-pdf").sum() == 1
        assert (result["source_type"] == "natwest-statement").sum() == 1


class _FakeDatalake:
    """Minimal stand-in exposing only what _dedupe_with_existing needs."""

    def __init__(self, existing=None):
        self._existing = existing

    def read_silver(self, entity_type):
        return self._existing


class TestDedupeWithExisting:
    def test_merges_new_rows_with_existing(self):
        existing = pd.DataFrame([{"bronze_source_key": "a", "amount": 1.0}])
        new_df = pd.DataFrame(
            [
                {"bronze_source_key": "a", "amount": 1.0},
                {"bronze_source_key": "b", "amount": 2.0},
            ]
        )
        result = _dedupe_with_existing(
            _FakeDatalake(existing), "transactions", new_df, ["bronze_source_key"]
        )
        assert len(result) == 2
        assert set(result["bronze_source_key"]) == {"a", "b"}

    def test_no_prior_silver_data(self):
        new_df = pd.DataFrame([{"bronze_source_key": "a", "amount": 1.0}])
        result = _dedupe_with_existing(
            _FakeDatalake(None), "transactions", new_df, ["bronze_source_key"]
        )
        assert len(result) == 1

    def test_rerun_with_unchanged_data_does_not_duplicate(self):
        """Idempotency: running twice on the same Bronze data shouldn't grow the table."""
        new_df = pd.DataFrame(
            [
                {"bronze_source_key": "a", "amount": 1.0},
                {"bronze_source_key": "b", "amount": 2.0},
            ]
        )
        first_run = _dedupe_with_existing(
            _FakeDatalake(None), "transactions", new_df, ["bronze_source_key"]
        )
        second_run = _dedupe_with_existing(
            _FakeDatalake(first_run), "transactions", new_df, ["bronze_source_key"]
        )
        assert len(second_run) == 2
