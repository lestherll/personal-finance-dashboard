"""Tests for Monzo PDF adapter."""

import pytest

from adapters.monzo_pdf_adapter import MonzoPdfAdapter

SAMPLE_TEXT = """Personal Account statement
01/04/2026 - 30/06/2026
Lesther Jr Monsanto Llacuna
4/1 20 St. Andrews Square
Glasgow
G1 5PQ
United Kingdom
£2,256.39
Total balance
(Including all Pots and Cashback)
£2,255.37
Personal Account balance
(Excluding all Pots)
£0.00
Balance in Pots
(This includes both Regular Pots with Monzo and Savings
Pots with external providers)
£1.02
Cashback Balance
-£9,518.00
Total outgoings
+£10,141.12
Total deposits
Date
Description
(GBP) Amount
(GBP) Balance
30/06/2026
Lesther Llacuna (Faster Payments)
Reference: vrp2524950438141
-100.00
2,255.37
30/06/2026
Lesther Jr Llacuna & Ng Yinnee (P2P
Payment)
-20.00
2,355.37
30/06/2026
Flex
-651.12
2,375.37
Sort code: 04-00-04
Account number: 10562844
BIC: MONZGB2L
IBAN: GB39 MONZ 0400 0410 5628 44
29/06/2026
Jaylord Llacuna (P2P Payment)
197.00
3,026.49
Pot statement
01/04/2026 - 30/06/2026
Monzo Bank Limited (https://monzo.com) is a company registered in England No. 9446231.
"""


@pytest.fixture
def adapter():
    return MonzoPdfAdapter()


class TestMonzoValidation:
    def test_validates_monzo_statement(self, adapter):
        assert adapter.validate_text(SAMPLE_TEXT)

    def test_rejects_non_monzo_text(self, adapter):
        assert not adapter.validate_text("Some other bank statement")

    def test_rejects_missing_bank_name(self, adapter):
        assert not adapter.validate_text(
            "Personal Account statement\nNo bank named here"
        )


class TestMonzoIdentifierExtraction:
    def test_extracts_sort_code_and_account_number(self, adapter):
        identifier = adapter._extract_account_identifier(SAMPLE_TEXT)
        assert identifier == "04-00-04_10562844"

    def test_returns_none_when_missing(self, adapter):
        assert adapter._extract_account_identifier("no identifying info here") is None


class TestMonzoParsing:
    def test_parses_transactions_and_stamps_identifier(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        assert len(txns) == 4
        assert all(t["_account_identifier_raw"] == "04-00-04_10562844" for t in txns)

    def test_stops_before_pot_statement(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        assert all("Pot statement" not in t["description"] for t in txns)

    def test_single_line_description(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        payment = next(t for t in txns if "Jaylord" in t["description"])
        assert payment["amount"] == 197.00

    def test_reference_continuation_is_joined(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        txn = next(t for t in txns if "vrp2524950438141" in t["description"])
        assert (
            txn["description"]
            == "Lesther Llacuna (Faster Payments) Reference: vrp2524950438141"
        )
        assert txn["amount"] == -100.00

    def test_wrapped_description_without_reference_is_joined(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        txn = next(t for t in txns if "Ng Yinnee" in t["description"])
        assert txn["description"] == "Lesther Jr Llacuna & Ng Yinnee (P2P Payment)"
        assert txn["amount"] == -20.00

    def test_single_word_description(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        flex = next(t for t in txns if t["description"] == "Flex")
        assert flex["amount"] == -651.12

    def test_footer_block_does_not_pollute_descriptions(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        assert not any("Sort code" in t["description"] for t in txns)
        assert not any("BIC" in t["description"] for t in txns)


class TestMonzoSourceKey:
    def test_source_key_includes_account_identifier(self, adapter):
        txn = {"date": "30/06/2026", "description": "Test", "amount": -1.0}
        key_with = adapter.generate_source_key(txn, 1, "04-00-04_10562844")
        key_without = adapter.generate_source_key(txn, 1, None)
        assert key_with != key_without
        assert "04-00-04_10562844" in key_with

    def test_detect_source_type(self, adapter):
        assert adapter.detect_source_type() == "monzo-pdf"
