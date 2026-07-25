"""Tests for the Natwest quarterly Statement PDF adapter.

Distinct from `test_natwest_pdf_adapter.py`, which covers the online
"Transactions" export - a structurally unrelated document. This fixture
mirrors the real quarterly statement's layout: a Date/Description/Paid
In(£)/Withdrawn(£)/Balance(£) table (not a single signed-amount-per-line
format), an opening "BROUGHT FORWARD" row, and a same-day continuation
transaction that omits its date line (only printed once per calendar day).
"""

import pytest

from adapters.natwest_statement_pdf_adapter import NatwestStatementPdfAdapter

SAMPLE_TEXT = """Account Name
Account No
Sort Code
Page No
MR TEST PERSON
12345678
11-22-33
1 of 1
CURRENT ACCOUNT
Summary
Statement Date
13 MAY 2026
Period Covered
14 FEB 2026 to 13 MAY 2026
Previous Balance
£1,010.00
Paid In
£100.00
Withdrawn
£150.00
New Balance
£960.00
Date
Description
Paid In(£)
Withdrawn(£)
Balance(£)

14 FEB 2026

BROUGHT FORWARD


1,010.00

26 FEB

Automated Credit 3305 JPMCB

100.00

1,110.00

02 MAR

OnLine Transaction KROO ACCOUNT SALARY VIA MOBILE -
PYMT FP 28/02/26 10 58205043915108000N


100.00
1,010.00


OnLine Transaction LLACUNA J Food VIA MOBILE - PYMT

50.00

960.00
Interest (variable) you currently pay us on overdrawn balances
"""


@pytest.fixture
def adapter():
    return NatwestStatementPdfAdapter()


class TestNatwestStatementValidation:
    def test_validates_statement(self, adapter):
        assert adapter.validate_text(SAMPLE_TEXT)

    def test_rejects_non_matching_text(self, adapter):
        assert not adapter.validate_text("Some other bank statement")

    def test_rejects_online_export_format(self, adapter):
        """The online 'Transactions' export has neither marker this
        adapter looks for - the two Natwest formats must not collide."""
        online_export_text = "NatWest\nYour transactions\nDate\nDescription"
        assert not adapter.validate_text(online_export_text)


class TestNatwestStatementIdentifierExtraction:
    def test_extracts_full_account_number_and_sort_code(self, adapter):
        identifier = adapter._extract_account_identifier(SAMPLE_TEXT)
        assert identifier == "12345678_11-22-33"

    def test_returns_none_when_missing(self, adapter):
        assert adapter._extract_account_identifier("no identifying info here") is None


class TestNatwestStatementParsing:
    def test_brought_forward_excluded_as_non_transaction(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        assert all("BROUGHT FORWARD" not in t["description"] for t in txns)

    def test_parses_expected_transaction_count(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        assert len(txns) == 3

    def test_amount_derived_from_balance_delta(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        credit = next(t for t in txns if "Automated Credit" in t["description"])
        assert credit["amount"] == 100.00
        assert credit["balance"] == 1110.00

    def test_same_day_continuation_row_carries_forward_date(self, adapter):
        """The second '02 MAR' transaction omits its date line entirely -
        the statement only prints a date once per calendar day."""
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        salary = next(t for t in txns if "SALARY" in t["description"])
        food = next(t for t in txns if "Food" in t["description"])
        assert salary["date"] == "02 MAR"
        assert food["date"] == "02 MAR"
        assert salary["amount"] == -100.00
        assert salary["balance"] == 1010.00
        assert food["amount"] == -50.00
        assert food["balance"] == 960.00

    def test_stamps_account_identifier_on_every_txn(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        assert all(t["_account_identifier_raw"] == "12345678_11-22-33" for t in txns)

    def test_final_balance_reconciles_with_no_warning(self, adapter, caplog):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        assert txns[-1]["balance"] == 960.00
        assert "does not match" not in caplog.text

    def test_reconciliation_mismatch_is_logged_not_raised(self, adapter, caplog):
        mismatched_text = SAMPLE_TEXT.replace(
            "New Balance\n£960.00", "New Balance\n£999.99"
        )
        txns = adapter.parse_transactions(mismatched_text)
        assert len(txns) == 3  # doesn't raise or drop data
        assert "does not match" in caplog.text


class TestNatwestStatementSourceKey:
    def test_source_key_includes_account_identifier(self, adapter):
        txn = {"date": "26FEB", "description": "Test", "amount": 100.0}
        key_with = adapter.generate_source_key(txn, 1, "12345678_11-22-33")
        key_without = adapter.generate_source_key(txn, 1, None)
        assert key_with != key_without
        assert "12345678_11-22-33" in key_with

    def test_detect_source_type(self, adapter):
        assert adapter.detect_source_type() == "natwest-statement"
