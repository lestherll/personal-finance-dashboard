"""Tests for Kroo PDF adapter."""

import pytest

from adapters.kroo_pdf_adapter import KrooPdfAdapter

SAMPLE_TEXT = """Your Current Account
Statement number 0001
Kroo Current Account
Sort code: 01-02-03
Account number: 12345678
Overview
Total opening balance
£100.00
Account transactions
01 June 2026
Deposit interest
June interest earned
£1.23
£101.23
02 June 2026
To Test Merchant (Faster Payment Out)
Sent from Kroo
£25.00
£76.23
03 June 2026
From Employer Ltd (Faster Payment In)
SALARY
£500.00
£576.23
Closing balance
£576.23
"""


@pytest.fixture
def adapter():
    return KrooPdfAdapter()


class TestKrooValidation:
    def test_validates_kroo_statement(self, adapter):
        assert adapter.validate_text(SAMPLE_TEXT)

    def test_rejects_non_kroo_text(self, adapter):
        assert not adapter.validate_text("Some other bank statement")


class TestKrooIdentifierExtraction:
    def test_extracts_sort_code_and_account_number(self, adapter):
        identifier = adapter._extract_account_identifier(SAMPLE_TEXT)
        assert identifier == "01-02-03_12345678"

    def test_returns_none_when_missing(self, adapter):
        assert adapter._extract_account_identifier("no identifying info here") is None


class TestKrooParsing:
    def test_parses_transactions_and_stamps_identifier(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        assert len(txns) == 3
        assert all(t["_account_identifier_raw"] == "01-02-03_12345678" for t in txns)

    def test_interest_deposit_is_positive(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        interest = next(t for t in txns if "interest" in t["description"].lower())
        assert interest["amount"] == 1.23

    def test_faster_payment_out_is_negative(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        payment = next(t for t in txns if "Test Merchant" in t["description"])
        assert payment["amount"] == -25.00

    def test_faster_payment_in_is_positive(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        salary = next(t for t in txns if "SALARY" in t["description"])
        assert salary["amount"] == 500.00


class TestKrooSourceKey:
    def test_source_key_includes_account_identifier(self, adapter):
        txn = {"date": "1June2026", "description": "Test", "amount": -1.0}
        key_with = adapter.generate_source_key(txn, 1, "01-02-03_12345678")
        key_without = adapter.generate_source_key(txn, 1, None)
        assert key_with != key_without
        assert "01-02-03_12345678" in key_with

    def test_detect_source_type(self, adapter):
        assert adapter.detect_source_type() == "kroo"
