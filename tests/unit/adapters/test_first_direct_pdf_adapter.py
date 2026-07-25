"""Tests for First Direct PDF adapter."""

import pytest

from adapters.first_direct_pdf_adapter import FirstDirectPdfAdapter

SAMPLE_TEXT = """first direct
Card number
Sheet number 1 of 1
1234 5678 9012 3456
Account Summary
Credit Limit
£ 5,000.00
APR
24.9%
Previous Balance
1000.00
Debits
15.50
Credits
47.22
New Balance
968.28
Principal Balance
968.28
Your Transaction Details
Received By Us
Transaction Date
Details
Amount
01 May 26
01 May 26
PAYMENT RECEIVED - THANK YOU
47.22CR
02 May 26
02 May 26
TEST MERCHANT PURCHASE
15.50
Outstanding Balance
"""


@pytest.fixture
def adapter():
    return FirstDirectPdfAdapter()


class TestFirstDirectValidation:
    def test_validates_first_direct_statement(self, adapter):
        assert adapter.validate_text("first direct Your Transaction Details")

    def test_rejects_non_first_direct_text(self, adapter):
        assert not adapter.validate_text("Some other bank statement")


class TestFirstDirectIdentifierExtraction:
    def test_extracts_card_number(self, adapter):
        assert adapter._extract_account_identifier(SAMPLE_TEXT) == "1234 5678 9012 3456"

    def test_returns_none_when_missing(self, adapter):
        assert adapter._extract_account_identifier("no card info here") is None


class TestFirstDirectParsing:
    def test_parses_transactions_and_stamps_identifier(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        assert len(txns) == 2
        assert all(t["_account_identifier_raw"] == "1234 5678 9012 3456" for t in txns)

    def test_cr_suffix_is_positive_credit(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        payment = next(t for t in txns if "PAYMENT RECEIVED" in t["description"])
        assert payment["amount"] == 47.22

    def test_no_suffix_is_negative_debit(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        purchase = next(t for t in txns if "TEST MERCHANT" in t["description"])
        assert purchase["amount"] == -15.50

    def test_detect_source_type(self, adapter):
        assert adapter.detect_source_type() == "firstdirect"


class TestFirstDirectDerivedBalance:
    def test_balance_rolls_forward_from_previous_balance_anchor(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        payment = next(t for t in txns if "PAYMENT RECEIVED" in t["description"])
        purchase = next(t for t in txns if "TEST MERCHANT" in t["description"])
        # Previous Balance 1000.00; payment (credit) reduces what's owed,
        # purchase (debit) increases it.
        assert payment["balance"] == 952.78
        assert purchase["balance"] == 968.28

    def test_no_account_summary_block_skips_balance_silently(self, adapter):
        text_without_summary = """first direct
Your Transaction Details
Received By Us
Transaction Date
Details
Amount
01 May 26
01 May 26
PAYMENT RECEIVED - THANK YOU
47.22CR
Outstanding Balance
"""
        txns = adapter.parse_transactions(text_without_summary)
        assert len(txns) == 1
        assert "balance" not in txns[0]
