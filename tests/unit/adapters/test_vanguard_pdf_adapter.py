"""Tests for Vanguard PDF adapter.

Real Vanguard statements cover one account number but multiple product
wrappers (e.g. ISA and Personal Pension), each with its own holdings table
and its own activity section - these fixtures mirror that structure.
"""

import pytest

from adapters.vanguard_pdf_adapter import VanguardPdfAdapter

SAMPLE_TEXT = """Vanguard
Client name: Test Person
Account number: VG9999999
Your Vanguard account summary
Product
ISA
Test Pension
Account total

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
