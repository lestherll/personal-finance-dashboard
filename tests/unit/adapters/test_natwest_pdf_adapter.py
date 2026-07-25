"""Tests for Natwest PDF adapter."""

from datetime import datetime

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

SAMPLE_TEXT_WITH_PERIOD = """NatWest
Account details
*****123 · 12-34-56
From
01/01/2026
To
31/05/2026
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


class TestNatwestPdfStatementPeriod:
    def test_extracts_period(self, adapter):
        period = adapter._extract_statement_period(SAMPLE_TEXT_WITH_PERIOD)
        assert period == (datetime(2026, 1, 1), datetime(2026, 5, 31))

    def test_returns_none_when_period_missing(self, adapter):
        assert adapter._extract_statement_period(SAMPLE_TEXT) is None

    def test_parse_transactions_attaches_year_from_period(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT_WITH_PERIOD)
        assert len(txns) == 2
        assert all(t["date"].endswith("2026") for t in txns)
        debit = next(t for t in txns if "TEST MERCHANT" in t["description"])
        assert debit["date"] == "15 May 2026"

    def test_parse_transactions_without_period_leaves_date_year_less(self, adapter):
        """Backward compatible: no period header -> date stays 'DD Mon' and
        the Silver-layer upload-timestamp fallback takes over instead."""
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        assert all(len(t["date"].split()) == 2 for t in txns)
