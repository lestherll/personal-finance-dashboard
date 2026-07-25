"""Tests for Natwest PDF adapter."""

import pytest

from adapters.natwest_pdf_adapter import NatwestPdfAdapter

SAMPLE_TEXT = """NatWest
Account details
*****123 · 12-34-56
Your transactions
Date
Description
Type
Paid in
Paid out
15 May
TEST MERCHANT
Mobile/Online Transaction
-£25.00
16 May
SALARY PAYMENT
Automated Credit
£1000.00
Downloaded from the NatWest online transactions service.
"""


@pytest.fixture
def adapter():
    return NatwestPdfAdapter()


class TestNatwestPdfValidation:
    def test_validates_natwest_statement(self, adapter):
        assert adapter.validate_text(SAMPLE_TEXT)

    def test_rejects_non_natwest_text(self, adapter):
        assert not adapter.validate_text("Some other bank statement")


class TestNatwestPdfIdentifierExtraction:
    def test_extracts_masked_account_and_sort_code(self, adapter):
        assert adapter._extract_account_identifier(SAMPLE_TEXT) == "*****123_12-34-56"

    def test_returns_none_when_missing(self, adapter):
        assert adapter._extract_account_identifier("no identifying info") is None


class TestNatwestPdfParsing:
    def test_parses_transactions_and_stamps_identifier(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        assert len(txns) == 2
        assert all(t["_account_identifier_raw"] == "*****123_12-34-56" for t in txns)

    def test_debit_negative_credit_positive(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        debit = next(t for t in txns if "TEST MERCHANT" in t["description"])
        credit = next(t for t in txns if "SALARY" in t["description"])
        assert debit["amount"] == -25.00
        assert credit["amount"] == 1000.00

    def test_detect_source_type(self, adapter):
        assert adapter.detect_source_type() == "natwest-pdf"
