"""Tests for Bronze -> Silver normalization."""

import pandas as pd
import pytest

from transformers.account_config import get_account_id
from transformers.silver_transformer import (
    SilverTransformer,
)

# Real (test-only) entries so get_account_id resolves without hitting the fallback.
_KROO_ID = "fd7a2651d39e"
_NATWEST_ID = "43ae9e53d8a2"
_FIRSTDIRECT_ID = "8765efc92b23"
_AMEX_ID = "63e97de2060d"
_VANGUARD_ISA_ID = "992add198186"
_MONZO_PDF_ID = "4fa9c17f5f09"
_CHASE_CURRENT_ID = "263b465ff6a8"


def _bronze_frame(
    source_type,
    raw_rows,
    upload_timestamp=None,
    account_identifier=None,
    record_type="transaction",
    statement_period_to=None,
    filename="test_upload",
    line_number_start=1,
    reconciliation_matches=None,
):
    """Build a Bronze-shaped DataFrame matching what datalake.read_bronze() returns.

    upload_timestamp/statement_period_to/filename are per-file constants (as
    they are on a real Bronze write - see models/datalake.py::write_bronze),
    applied identically to every row in one call; line_number increments per
    row within the call, mirroring one real ingested file's row sequence.
    reconciliation_matches mirrors the same per-file broadcast (write_bronze
    only adds the column when the adapter set a ReconciliationResult) -
    defaults to None (no reconciliation info), matching a source/statement
    with no anchor to check, or Bronze data predating the column.
    """
    upload_timestamp = upload_timestamp or pd.Timestamp("2026-06-01")
    return pd.DataFrame(
        [
            {
                "bronze_source_key": f"{source_type}_key_{filename}_{i}",
                "source_type": source_type,
                "raw_data": raw,
                "upload_timestamp": upload_timestamp,
                "filename": filename,
                "account_identifier": account_identifier,
                "record_type": record_type,
                "statement_period_to": statement_period_to,
                "line_number": line_number_start + i,
                "reconciliation_matches": reconciliation_matches,
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
            "amount_minor": -2550,
            "Currency": "GBP",
            "Category": "Groceries",
        }
        df, _ = transformer.normalize_transactions(
            {"monzo": _bronze_frame("monzo", [raw])}
        )

        assert len(df) == 1
        row = df.iloc[0]
        assert (
            row["account_id"] == "acc_monzo_current"
        )  # no identifier -> source_type fallback
        assert row["description"] == "Tesco Groceries"
        assert row["amount_minor"] == -2550
        assert row["currency"] == "GBP"
        assert row["category"] == "Groceries"
        assert row["transaction_date"] == pd.Timestamp("2024-01-15 14:30:00")

    def test_search_export_format(self, transformer):
        raw = {
            "id": "tx_1",
            "created": "2024-01-15T14:30:00Z",
            "title": "Tesco Groceries",
            "subtitle": "Tesco Stores",
            "amount_minor": -2550,
            "currency": "GBP",
            "categories": "groceries",
        }
        df, _ = transformer.normalize_transactions(
            {"monzo": _bronze_frame("monzo", [raw])}
        )

        assert len(df) == 1
        row = df.iloc[0]
        assert row["description"] == "Tesco Groceries"
        assert row["amount_minor"] == -2550
        assert row["category"] == "groceries"
        assert row["transaction_date"] == pd.Timestamp("2024-01-15 14:30:00")


class TestNormalizeTransactionsPdfSources:
    def test_kroo_full_date_and_identifier(self, transformer):
        raw = {"date": "12 January 2026", "description": "Coffee Shop", "amount_minor": -450}
        bronze = _bronze_frame("kroo", [raw], account_identifier=_KROO_ID)
        df, _ = transformer.normalize_transactions({"kroo": bronze})

        row = df.iloc[0]
        assert row["account_id"] == get_account_id(_KROO_ID, "kroo")
        assert row["transaction_date"] == pd.Timestamp("2026-01-12")
        assert row["amount_minor"] == -450

    def test_natwest_transactions_infers_year_from_upload_timestamp(self, transformer):
        raw = {"date": "15 Jan", "description": "Card Payment", "amount_minor": -2000}
        bronze = _bronze_frame(
            "natwest-transactions",
            [raw],
            upload_timestamp=pd.Timestamp("2026-02-01"),
            account_identifier=_NATWEST_ID,
        )
        df, _ = transformer.normalize_transactions({"natwest-transactions": bronze})

        row = df.iloc[0]
        assert row["transaction_date"] == pd.Timestamp("2026-01-15")
        assert row["account_id"] == get_account_id(_NATWEST_ID, "natwest-transactions")

    def test_natwest_statement_infers_year_and_captures_balance(self, transformer):
        raw = {
            "date": "26 FEB",
            "description": "Automated Credit 3305 JPMCB",
            "amount_minor": 272854,
            "balance_minor": 373854,
        }
        bronze = _bronze_frame(
            "natwest-statement",
            [raw],
            upload_timestamp=pd.Timestamp("2026-05-13"),
            account_identifier=_NATWEST_ID,
        )
        df, _ = transformer.normalize_transactions({"natwest-statement": bronze})

        row = df.iloc[0]
        assert row["transaction_date"] == pd.Timestamp("2026-02-26")
        assert row["amount_minor"] == 272854
        assert row["account_id"] == get_account_id(_NATWEST_ID, "natwest-statement")

    def test_amex_infers_year_from_upload_timestamp(self, transformer):
        raw = {"date": "28 Apr", "description": "Amazon", "amount_minor": -9999}
        bronze = _bronze_frame(
            "amex",
            [raw],
            upload_timestamp=pd.Timestamp("2026-05-10"),
            account_identifier=_AMEX_ID,
        )
        df, _ = transformer.normalize_transactions({"amex": bronze})

        row = df.iloc[0]
        assert row["transaction_date"] == pd.Timestamp("2026-04-28")

    def test_amex_handles_year_boundary(self, transformer):
        """A December transaction uploaded in January should resolve to the prior year."""
        raw = {"date": "20 Dec", "description": "Gift Shop", "amount_minor": -1500}
        bronze = _bronze_frame(
            "amex",
            [raw],
            upload_timestamp=pd.Timestamp("2026-01-05"),
            account_identifier=_AMEX_ID,
        )
        df, _ = transformer.normalize_transactions({"amex": bronze})

        row = df.iloc[0]
        assert row["transaction_date"] == pd.Timestamp("2025-12-20")

    def test_amex_uses_year_when_adapter_already_attached_one(self, transformer):
        """When the adapter resolved the year via the statement period
        (resolve_year_in_period), the date string already carries it - the
        upload_timestamp fallback must not be consulted (and could give a
        wrong answer if it were, since it's unrelated to the real year)."""
        raw = {"date": "28 Apr 2024", "description": "Amazon", "amount_minor": -9999}
        bronze = _bronze_frame(
            "amex",
            [raw],
            upload_timestamp=pd.Timestamp("2026-05-10"),  # deliberately wrong year
            account_identifier=_AMEX_ID,
        )
        df, _ = transformer.normalize_transactions({"amex": bronze})

        row = df.iloc[0]
        assert row["transaction_date"] == pd.Timestamp("2024-04-28")

    def test_amex_same_recurring_charge_a_year_apart_both_survive(self, transformer):
        """Regression test: before the adapter attached a year to the date,
        two real charges a year apart (same day/month/amount/description)
        produced an identical bronze_source_key and one silently vanished
        at the Bronze->Silver dedupe step. With the year in the date string,
        source_key naturally differs and both survive normalize_transactions."""
        raw_2025 = {
            "date": "15 Jan 2025",
            "description": "NETFLIX.COM",
            "amount_minor": -1099,
        }
        raw_2026 = {
            "date": "15 Jan 2026",
            "description": "NETFLIX.COM",
            "amount_minor": -1099,
        }
        from adapters.amex_pdf_adapter import AmexPdfAdapter

        amex = AmexPdfAdapter()
        key_2025 = amex.generate_source_key(raw_2025, 1, _AMEX_ID)
        key_2026 = amex.generate_source_key(raw_2026, 1, _AMEX_ID)
        assert key_2025 != key_2026

        bronze_2025 = _bronze_frame("amex", [raw_2025], account_identifier=_AMEX_ID)
        bronze_2025["bronze_source_key"] = key_2025
        bronze_2026 = _bronze_frame("amex", [raw_2026], account_identifier=_AMEX_ID)
        bronze_2026["bronze_source_key"] = key_2026
        combined = pd.concat([bronze_2025, bronze_2026], ignore_index=True)

        df, _ = transformer.normalize_transactions({"amex": combined})
        df = df.drop_duplicates(subset=["bronze_source_key"], keep="last")

        assert len(df) == 2
        assert set(df["transaction_date"]) == {
            pd.Timestamp("2025-01-15"),
            pd.Timestamp("2026-01-15"),
        }

    def test_first_direct_two_digit_year(self, transformer):
        raw = {"date": "15 Jan 24", "description": "Restaurant", "amount_minor": -3000}
        bronze = _bronze_frame("firstdirect", [raw], account_identifier=_FIRSTDIRECT_ID)
        df, _ = transformer.normalize_transactions({"firstdirect": bronze})

        row = df.iloc[0]
        assert row["transaction_date"] == pd.Timestamp("2024-01-15")

    def test_vanguard_pdf_slash_date(self, transformer):
        raw = {"date": "15/01/2024", "description": "Fund Purchase", "amount_minor": -50000}
        bronze = _bronze_frame(
            "vanguard-pdf", [raw], account_identifier=_VANGUARD_ISA_ID
        )
        df, _ = transformer.normalize_transactions({"vanguard-pdf": bronze})

        row = df.iloc[0]
        assert row["account_id"] == get_account_id(_VANGUARD_ISA_ID, "vanguard-pdf")
        assert row["transaction_date"] == pd.Timestamp("2024-01-15")

    def test_monzo_pdf_slash_date(self, transformer):
        raw = {
            "date": "30/06/2026",
            "description": "Lesther Llacuna (Faster Payments) Reference: vrp2524950438141",
            "amount_minor": -10000,
        }
        bronze = _bronze_frame("monzo-pdf", [raw], account_identifier=_MONZO_PDF_ID)
        df, _ = transformer.normalize_transactions({"monzo-pdf": bronze})

        row = df.iloc[0]
        assert row["account_id"] == get_account_id(_MONZO_PDF_ID, "monzo-pdf")
        assert row["transaction_date"] == pd.Timestamp("2026-06-30")
        assert row["amount_minor"] == -10000

    def test_vanguard_pdf_holdings_excluded_from_transactions(self, transformer):
        """A holding-record_type row must not leak into normalize_transactions."""
        holding_raw = {
            "wrapper": "ISA",
            "fund_name": "Some Fund",
            "quantity_text": "5.00",
            "unit_price_minor": 100,
            "total_value_minor": 500,
            "as_of_date": "08 July 2026",
        }
        bronze = _bronze_frame(
            "vanguard-pdf",
            [holding_raw],
            account_identifier=_VANGUARD_ISA_ID,
            record_type="holding",
        )
        df, _ = transformer.normalize_transactions({"vanguard-pdf": bronze})
        assert df.empty


class TestNormalizeTransactionsEmpty:
    def test_no_bronze_data_returns_empty_frame_with_schema(self, transformer):
        df, _ = transformer.normalize_transactions({})
        assert df.empty
        assert "bronze_source_key" in df.columns
        assert "transaction_date" in df.columns


class TestNormalizeHoldings:
    def test_vanguard_pdf_holdings_normalized(self, transformer):
        raw = {
            "wrapper": "ISA",
            "fund_name": "Vanguard FTSE Global All Cap Index Fund Accumulation",
            "quantity_text": "5.68",
            "unit_price_minor": 28713,
            "total_value_minor": 163028,
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
        assert row["quantity"] == "5.68"
        assert row["unit_price_minor"] == 28713
        assert row["total_value_minor"] == 163028
        assert row["as_of_date"] == pd.Timestamp("2026-07-08")
        assert row["isin"] is None

    def test_vanguard_pdf_holdings_handles_dash_quantity(self, transformer):
        """'Cash account' holding rows use '-' for quantity/price."""
        raw = {
            "wrapper": "ISA",
            "fund_name": "Cash account",
            "quantity_text": "-",
            "unit_price_minor": 0,
            "total_value_minor": 15,
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
        assert row["quantity"] == "-"
        assert row["unit_price_minor"] == 0
        assert row["total_value_minor"] == 15

    def test_vanguard_pdf_transactions_excluded_from_holdings(self, transformer):
        """A transaction-record_type row must not leak into normalize_holdings."""
        txn_raw = {"date": "15/01/2024", "description": "Deposit", "amount_minor": 10000}
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


class TestNormalizePlanItInstalments:
    def test_amex_plan_it_instalment_normalized(self, transformer):
        raw = {
            "start_date": "Apr 12 2026",
            "description": "MALAYSIA AIRLINES KUALA KUALA LUMPUR",
            "plan_total_minor": 165639,
            "plan_lifetime_fee_minor": 5168,
            "remaining_balance_minor": 110426,
            "due_this_month_plan_minor": 55213,
            "due_this_month_fee_minor": 1723,
            "due_this_month_total_minor": 56936,
            "instalment_progress": "1 OF 3",
            "as_of_date": "19 Apr 2026",
        }
        bronze = _bronze_frame(
            "amex",
            [raw],
            account_identifier=_AMEX_ID,
            record_type="plan_it_instalment",
        )
        df = transformer.normalize_plan_it_instalments({"amex": bronze})

        assert len(df) == 1
        row = df.iloc[0]
        assert row["account_id"] == get_account_id(_AMEX_ID, "amex")
        assert row["start_date"] == pd.Timestamp("2026-04-12")
        assert row["description"] == "MALAYSIA AIRLINES KUALA KUALA LUMPUR"
        assert row["plan_total_minor"] == 165639
        assert row["plan_lifetime_fee_minor"] == 5168
        assert row["remaining_balance_minor"] == 110426
        assert row["due_this_month_plan_minor"] == 55213
        assert row["due_this_month_fee_minor"] == 1723
        assert row["due_this_month_total_minor"] == 56936
        assert row["instalment_progress"] == "1 OF 3"
        assert row["as_of_date"] == pd.Timestamp("2026-04-19")

    def test_amex_transactions_excluded_from_plan_it_instalments(self, transformer):
        """A transaction-record_type row must not leak into
        normalize_plan_it_instalments."""
        txn_raw = {"date": "19 Apr 2026", "description": "COFFEE SHOP", "amount_minor": -385}
        bronze = _bronze_frame(
            "amex", [txn_raw], account_identifier=_AMEX_ID, record_type="transaction"
        )
        df = transformer.normalize_plan_it_instalments({"amex": bronze})
        assert df.empty

    def test_no_data_returns_empty_frame_with_schema(self, transformer):
        df = transformer.normalize_plan_it_instalments({})
        assert df.empty
        assert "instalment_progress" in df.columns


class TestNormalizeAccountLedger:
    def test_kroo_balance_captured(self, transformer):
        raw = {
            "date": "12 January 2026",
            "description": "Coffee Shop",
            "amount_minor": -450,
            "balance_minor": 49550,
        }
        df = transformer.normalize_account_ledger(
            {"kroo": _bronze_frame("kroo", [raw], account_identifier=_KROO_ID)}
        )

        assert len(df) == 1
        row = df.iloc[0]
        assert row["account_id"] == "acc_kroo_current"
        assert row["balance_minor"] == 49550
        assert row["as_of_date"] == pd.Timestamp("2026-01-12")

    def test_amex_balance_captured(self, transformer):
        raw = {
            "date": "31 Jan",
            "description": "PAYMENT RECEIVED - THANK YOU",
            "amount_minor": 76958,
            "balance_minor": 0,
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
        assert row["balance_minor"] == 0
        assert row["as_of_date"] == pd.Timestamp("2026-01-31")

    def test_amex_balance_captured_with_year_already_stamped(self, transformer):
        """AmEx's adapter now stamps a real year onto `date` at parse time
        when the statement's period header is found (resolve_year_in_period)
        - the ledger normalizer must handle that directly rather than
        assuming a bare 'DD Mmm' and mishandling the extra year token."""
        raw = {
            "date": "31 Jan 2026",
            "description": "PAYMENT RECEIVED - THANK YOU",
            "amount_minor": 76958,
            "balance_minor": 0,
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
        assert row["balance_minor"] == 0
        assert row["as_of_date"] == pd.Timestamp("2026-01-31")

    def test_amex_plan_it_instalment_rows_excluded_from_ledger(self, transformer):
        """Amex now also emits "plan_it_instalment" rows (see
        normalize_plan_it_instalments) alongside its transactions - those
        have no `date`/`amount` shape, so _ledger_from_amex (built for
        transaction rows) must never see them."""
        txn_raw = {
            "date": "31 Jan",
            "description": "PAYMENT RECEIVED - THANK YOU",
            "amount_minor": 76958,
            "balance_minor": 0,
        }
        plan_it_raw = {
            "start_date": "Apr 12 2026",
            "description": "MALAYSIA AIRLINES KUALA KUALA LUMPUR",
            "as_of_date": "19 Apr 2026",
        }
        txn_frame = _bronze_frame(
            "amex",
            [txn_raw],
            upload_timestamp=pd.Timestamp("2026-02-19"),
            account_identifier=_AMEX_ID,
            record_type="transaction",
        )
        plan_it_frame = _bronze_frame(
            "amex",
            [plan_it_raw],
            upload_timestamp=pd.Timestamp("2026-02-19"),
            account_identifier=_AMEX_ID,
            record_type="plan_it_instalment",
            filename="test_upload_2",
        )
        bronze = pd.concat([txn_frame, plan_it_frame], ignore_index=True)

        df = transformer.normalize_account_ledger({"amex": bronze})

        assert len(df) == 1
        assert df.iloc[0]["balance_minor"] == 0

    def test_monzo_pdf_balance_captured(self, transformer):
        raw = {
            "date": "30/06/2026",
            "description": "Lesther Llacuna (Faster Payments)",
            "amount_minor": -10000,
            "balance_minor": 225537,
        }
        df = transformer.normalize_account_ledger(
            {
                "monzo-pdf": _bronze_frame(
                    "monzo-pdf", [raw], account_identifier=_MONZO_PDF_ID
                )
            }
        )

        assert len(df) == 1
        row = df.iloc[0]
        assert row["account_id"] == "acc_monzo_current"
        assert row["balance_minor"] == 225537
        assert row["as_of_date"] == pd.Timestamp("2026-06-30")

    def test_chase_balance_captured(self, transformer):
        raw = {
            "date": "02 Jun 2026",
            "description": "From LLACUNA L - Lesther NW Payment",
            "amount_minor": 20000,
            "balance_minor": 20000,
        }
        df = transformer.normalize_account_ledger(
            {
                "chase": _bronze_frame(
                    "chase", [raw], account_identifier=_CHASE_CURRENT_ID
                )
            }
        )

        assert len(df) == 1
        row = df.iloc[0]
        assert row["account_id"] == "acc_chase_current"
        assert row["balance_minor"] == 20000
        assert row["as_of_date"] == pd.Timestamp("2026-06-02")

    def test_firstdirect_balance_captured(self, transformer):
        raw = {
            "date": "30 Apr 26",
            "description": "PAYMENT RECEIVED - THANK YOU",
            "amount_minor": 4722,
            "balance_minor": 152677,
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
        assert row["balance_minor"] == 152677
        assert row["as_of_date"] == pd.Timestamp("2026-04-30")

    def test_natwest_statement_balance_captured(self, transformer):
        raw = {
            "date": "26 FEB",
            "description": "Automated Credit 3305 JPMCB",
            "amount_minor": 272854,
            "balance_minor": 373854,
        }
        df = transformer.normalize_account_ledger(
            {
                "natwest-statement": _bronze_frame(
                    "natwest-statement",
                    [raw],
                    upload_timestamp=pd.Timestamp("2026-05-13"),
                    account_identifier=_NATWEST_ID,
                )
            }
        )

        assert len(df) == 1
        row = df.iloc[0]
        assert row["account_id"] == "acc_natwest_current"
        assert row["balance_minor"] == 373854
        assert row["as_of_date"] == pd.Timestamp("2026-02-26")

    def test_reconciled_true_captured_on_ledger_row(self, transformer):
        raw = {
            "date": "12 January 2026",
            "description": "Coffee Shop",
            "amount_minor": -450,
            "balance_minor": 49550,
        }
        df = transformer.normalize_account_ledger(
            {
                "kroo": _bronze_frame(
                    "kroo",
                    [raw],
                    account_identifier=_KROO_ID,
                    reconciliation_matches=True,
                )
            }
        )

        assert len(df) == 1
        assert df.iloc[0]["reconciled"] == True  # noqa: E712 - may be numpy bool

    def test_reconciled_false_captured_but_row_not_excluded(self, transformer):
        """A mismatched file's balance must still land in the ledger (see
        Gotcha #17) - filtering it out of "current balance" selection is
        transformers/balance.py::get_current_balances()'s job, not this
        function's. Excluding it here would strand the row if a later
        re-ingest of the same file un-mismatches it (_dedupe_with_existing
        only adds/replaces by bronze_source_key, never removes)."""
        raw = {
            "date": "12 January 2026",
            "description": "Coffee Shop",
            "amount_minor": -450,
            "balance_minor": 49550,
        }
        df = transformer.normalize_account_ledger(
            {
                "kroo": _bronze_frame(
                    "kroo",
                    [raw],
                    account_identifier=_KROO_ID,
                    reconciliation_matches=False,
                )
            }
        )

        assert len(df) == 1
        assert df.iloc[0]["reconciled"] == False  # noqa: E712 - may be numpy bool
        assert df.iloc[0]["balance_minor"] == 49550

    def test_reconciled_none_captured_when_no_reconciliation_column(self, transformer):
        raw = {
            "date": "12 January 2026",
            "description": "Coffee Shop",
            "amount_minor": -450,
            "balance_minor": 49550,
        }
        df = transformer.normalize_account_ledger(
            {"kroo": _bronze_frame("kroo", [raw], account_identifier=_KROO_ID)}
        )

        assert len(df) == 1
        assert df.iloc[0]["reconciled"] is None

    def test_pdf_sources_without_real_balance_excluded_from_ledger(self, transformer):
        """natwest-transactions (no balance data at all) and vanguard-pdf (cash_balance is a
        different metric from Portfolio Value) must not appear in the ledger."""
        natwest_transactions_raw = {
            "date": "26 May",
            "description": "KROO ACCOUNT",
            "amount_minor": -274533,
        }
        vanguard_pdf_raw = {
            "date": "21/04/2026",
            "description": "Regular Deposit",
            "amount_minor": 15000,
            "cash_balance": 50.15,
        }
        df = transformer.normalize_account_ledger(
            {
                "natwest-transactions": _bronze_frame(
                    "natwest-transactions", [natwest_transactions_raw]
                ),
                "vanguard-pdf": _bronze_frame("vanguard-pdf", [vanguard_pdf_raw]),
            }
        )
        assert df.empty

    def test_no_data_returns_empty_frame_with_schema(self, transformer):
        df = transformer.normalize_account_ledger({})
        assert df.empty
        assert "balance_minor" in df.columns

    def test_same_day_rows_carry_ordering_columns(self, transformer):
        """S1: two rows for the same account sharing an as_of_date - here
        from two different ingested files - must carry through distinct
        upload_timestamp/statement_period_to/line_number so a later
        "current balance" query can break the tie correctly (see
        transformers/balance.py). Sort correctness itself is exercised
        there; this only checks the columns survive normalization."""
        raw_first_file = {
            "date": "12 January 2026",
            "description": "Coffee Shop",
            "amount_minor": -450,
            "balance_minor": 49550,
        }
        raw_second_file = {
            "date": "12 January 2026",
            "description": "Later Deposit",
            "amount_minor": 10000,
            "balance_minor": 59550,
        }
        bronze = pd.concat(
            [
                _bronze_frame(
                    "kroo",
                    [raw_first_file],
                    account_identifier=_KROO_ID,
                    upload_timestamp=pd.Timestamp("2026-01-15 09:00:00"),
                    statement_period_to=pd.Timestamp("2026-01-12"),
                    filename="jan_statement_early.pdf",
                    line_number_start=5,
                ),
                _bronze_frame(
                    "kroo",
                    [raw_second_file],
                    account_identifier=_KROO_ID,
                    upload_timestamp=pd.Timestamp("2026-02-01 09:00:00"),
                    statement_period_to=pd.Timestamp("2026-01-31"),
                    filename="jan_statement_late.pdf",
                    line_number_start=1,
                ),
            ],
            ignore_index=True,
        )
        df = transformer.normalize_account_ledger({"kroo": bronze})

        assert len(df) == 2
        assert (df["as_of_date"] == pd.Timestamp("2026-01-12")).all()

        first = df[df["balance_minor"] == 49550].iloc[0]
        second = df[df["balance_minor"] == 59550].iloc[0]
        assert first["upload_timestamp"] == pd.Timestamp("2026-01-15 09:00:00")
        assert second["upload_timestamp"] == pd.Timestamp("2026-02-01 09:00:00")
        assert first["statement_period_to"] == pd.Timestamp("2026-01-12")
        assert second["statement_period_to"] == pd.Timestamp("2026-01-31")
        assert first["line_number"] == 5
        assert second["line_number"] == 1

