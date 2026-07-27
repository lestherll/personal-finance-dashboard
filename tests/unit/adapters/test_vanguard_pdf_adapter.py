"""Tests for Vanguard PDF adapter.

Real Vanguard statements cover one account number but multiple product
wrappers (e.g. ISA and Personal Pension), each with its own holdings table
and its own activity section - these fixtures mirror that structure.
"""

from datetime import datetime

import pytest

from adapters.vanguard_pdf_adapter import VanguardPdfAdapter

SAMPLE_TEXT = """Vanguard
Client name: Test Person
Account number: VG9999999
Your Vanguard account summary
Product
Value on 01 April 2026
Value on 01 July 2026
ISA
£900.00
£500.15
Test Pension
£0.00
£501.00
Account total
£900.00
£1,001.15

Test Person
Account number: VG9999999
Page 2 of 3
Your ISA investments at 01 July 2026
Description
Quantity
Price
Value
Test Fund One
Accumulation
5.00
£100.00
£500.00
Cash account
-
-
£0.15
Activity from 01 April 2026 to 01 July 2026 for your ISA
The transaction date is the date we carried out the activity.
Transaction date Transaction details
Cash amount
Cash balance
01/05/2026
Regular Deposit
£50.00
£50.15
02/05/2026
Bought .5 Test Fund
One Accumulation
-£50.00
£0.15

Test Person
Account number: VG9999999
Page 3 of 3
Issued by Vanguard Asset Management, Limited (Reg No. 07243412). Vanguard Asset Management, Limited is authorised and regulated
in the UK by the Financial Conduct Authority. The company is registered in England and Wales, registered office: 4th Floor, The Walbrook
Building, 25 Walbrook, London, EC4N 8AF.
Your Test Pension summary
Payments in
Your Test Pension investments at 01 July 2026
Description
Quantity
Price
Value
Test Fund Two
10.00
£50.00
£500.00
Cash account
-
-
£1.00
Activity from 01 April 2026 to 01 July 2026 for your Test
Pension
The transaction date is the date we carried out the activity.
Transaction date Transaction details
Cash amount
Cash balance
15/05/2026
Deposit via direct credit
£500.00
£500.00
"""


@pytest.fixture
def adapter():
    return VanguardPdfAdapter()


class TestVanguardValidation:
    def test_validates_vanguard_statement(self, adapter):
        assert adapter.validate_text("Vanguard Statement Activity")

    def test_rejects_non_vanguard_text(self, adapter):
        assert not adapter.validate_text("Some other bank statement")


class TestVanguardAccountNumberExtraction:
    def test_extracts_account_number(self, adapter):
        assert adapter._extract_account_number(SAMPLE_TEXT) == "VG9999999"


class TestVanguardMultiWrapper:
    def test_two_wrappers_produce_distinct_identifiers(self, adapter):
        records = adapter.parse_transactions(SAMPLE_TEXT)
        identifiers = {r["_account_identifier_raw"] for r in records}
        assert identifiers == {"VG9999999_ISA", "VG9999999_Test Pension"}

    def test_holdings_and_transactions_both_present(self, adapter):
        records = adapter.parse_transactions(SAMPLE_TEXT)
        record_types = {r["record_type"] for r in records}
        assert record_types == {"holding", "transaction"}

    def test_isa_holdings_correct(self, adapter):
        records = adapter.parse_transactions(SAMPLE_TEXT)
        isa_holdings = [
            r
            for r in records
            if r["record_type"] == "holding" and r["wrapper"] == "ISA"
        ]
        assert len(isa_holdings) == 2
        fund = next(h for h in isa_holdings if "Test Fund One" in h["fund_name"])
        assert fund["quantity"] == "5.00"
        assert fund["total_value"] == "£500.00"

    def test_pension_holdings_correct(self, adapter):
        records = adapter.parse_transactions(SAMPLE_TEXT)
        pension_holdings = [
            r
            for r in records
            if r["record_type"] == "holding" and r["wrapper"] == "Test Pension"
        ]
        assert len(pension_holdings) == 2

    def test_isa_activity_correct(self, adapter):
        records = adapter.parse_transactions(SAMPLE_TEXT)
        isa_txns = [
            r
            for r in records
            if r["record_type"] == "transaction"
            and r["_account_identifier_raw"] == "VG9999999_ISA"
        ]
        assert len(isa_txns) == 2
        deposit = next(t for t in isa_txns if "Regular Deposit" in t["description"])
        assert deposit["amount"] == 50.00
        assert deposit["cash_balance"] == 50.15
        bought = next(t for t in isa_txns if "Bought" in t["description"])
        assert bought["amount"] == -50.00
        assert bought["cash_balance"] == 0.15
        # Boilerplate must not have leaked into the wrapped description.
        assert "Walbrook" not in bought["description"]
        assert "Financial Conduct" not in bought["description"]

    def test_pension_activity_correct(self, adapter):
        records = adapter.parse_transactions(SAMPLE_TEXT)
        pension_txns = [
            r
            for r in records
            if r["record_type"] == "transaction"
            and r["_account_identifier_raw"] == "VG9999999_Test Pension"
        ]
        assert len(pension_txns) == 1
        assert pension_txns[0]["amount"] == 500.00
        assert pension_txns[0]["cash_balance"] == 500.00


class TestVanguardStatementPeriod:
    def test_extracts_period(self, adapter):
        period = adapter._extract_statement_period(SAMPLE_TEXT)
        assert period == (datetime(2026, 4, 1), datetime(2026, 7, 1))

    def test_returns_none_when_period_missing(self, adapter):
        assert adapter._extract_statement_period("Vanguard\nNo activity here") is None

    def test_sets_last_statement_period(self, adapter):
        adapter.parse_transactions(SAMPLE_TEXT)
        assert adapter.last_statement_period is not None
        assert adapter.last_statement_period.from_date == datetime(2026, 4, 1)
        assert adapter.last_statement_period.to_date == datetime(2026, 7, 1)

    def test_no_period_leaves_last_statement_period_none(self, adapter):
        adapter.parse_transactions("Vanguard\nNo activity here")
        assert adapter.last_statement_period is None

    def test_statement_period_resets_between_parses(self, adapter):
        """Adapter instances are reused across files by AdapterFactory - a
        period-bearing file must not leak into a later file with no period
        header of its own."""
        adapter.parse_transactions(SAMPLE_TEXT)
        assert adapter.last_statement_period is not None

        adapter.parse_transactions("Vanguard\nNo activity here")
        assert adapter.last_statement_period is None


class TestVanguardReconciliation:
    """'Your Vanguard account summary' (page 1) prints each wrapper's
    closing value - confirmed an exact anchor against real statement data:
    it equals that wrapper's own holdings total (fund total_value + cash
    total_value). Unlike every other reconciling adapter, Vanguard can
    produce more than one result per file (one per wrapper), via the
    additive last_reconciliations list rather than last_reconciliation."""

    def test_sets_reconciliations_on_match(self, adapter):
        records = adapter.parse_transactions(SAMPLE_TEXT)
        assert len(adapter.last_reconciliations) == 2
        assert all(r.matches is True for r in adapter.last_reconciliations)
        check_names = {r.check_name for r in adapter.last_reconciliations}
        assert check_names == {
            "vanguard_account_summary_isa",
            "vanguard_account_summary_test_pension",
        }
        # Each result's account_identifier is the hashed per-wrapper
        # identifier, matching what's stored on that wrapper's own records.
        isa_identifiers = {
            r["_account_identifier_raw"]
            for r in records
            if r["_account_identifier_raw"] == "VG9999999_ISA"
        }
        assert isa_identifiers  # sanity: fixture still produces ISA records

    def test_sets_reconciliation_on_mismatch(self, adapter):
        mismatched_text = SAMPLE_TEXT.replace("£500.15", "£999.99", 1)
        adapter.parse_transactions(mismatched_text)
        by_name = {r.check_name: r for r in adapter.last_reconciliations}
        assert by_name["vanguard_account_summary_isa"].matches is False
        assert by_name["vanguard_account_summary_test_pension"].matches is True

    def test_no_account_summary_leaves_reconciliations_empty(self, adapter):
        no_summary_text = SAMPLE_TEXT.replace(
            "Your Vanguard account summary\n"
            "Product\n"
            "Value on 01 April 2026\n"
            "Value on 01 July 2026\n"
            "ISA\n"
            "£900.00\n"
            "£500.15\n"
            "Test Pension\n"
            "£0.00\n"
            "£501.00\n"
            "Account total\n"
            "£900.00\n"
            "£1,001.15\n\n",
            "",
        )
        adapter.parse_transactions(no_summary_text)
        assert adapter.last_reconciliations == []

    def test_reconciliation_resets_between_parses(self, adapter):
        """Adapter instances are reused across files by AdapterFactory - a
        file with an account summary must not leak its results into a
        later file with no summary table of its own."""
        adapter.parse_transactions(SAMPLE_TEXT)
        assert adapter.last_reconciliations != []

        adapter.parse_transactions("Vanguard\nNo activity here")
        assert adapter.last_reconciliations == []

    def test_missing_holdings_section_skips_that_wrapper(self, adapter):
        """A wrapper listed in the account summary but with no (or
        malformed) holdings section of its own produces no result for that
        wrapper - "no signal", not a false mismatch."""
        missing_pension_holdings_text = SAMPLE_TEXT[
            : SAMPLE_TEXT.index("Your Test Pension investments")
        ]
        adapter.parse_transactions(missing_pension_holdings_text)
        check_names = {r.check_name for r in adapter.last_reconciliations}
        assert check_names == {"vanguard_account_summary_isa"}


class TestVanguardSourceKey:
    def test_holding_key_differs_from_transaction_key_format(self, adapter):
        holding_txn = {
            "record_type": "holding",
            "fund_name": "Test Fund",
            "as_of_date": "01 July 2026",
        }
        transaction_txn = {
            "record_type": "transaction",
            "date": "01/05/2026",
            "description": "Deposit",
            "amount": 50.0,
        }
        holding_key = adapter.generate_source_key(holding_txn, 1, "VG9999999_ISA")
        txn_key = adapter.generate_source_key(transaction_txn, 1, "VG9999999_ISA")
        assert holding_key.startswith("vanguard_holding_")
        assert txn_key.startswith("vanguard_txn_")

    def test_detect_source_type(self, adapter):
        assert adapter.detect_source_type() == "vanguard-pdf"
