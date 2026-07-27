"""Tests for Kroo PDF adapter."""

from datetime import datetime

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
1 June 2026 to 30 June 2026
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
        assert interest["amount_minor"] == 123

    def test_faster_payment_out_is_negative(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        payment = next(t for t in txns if "Test Merchant" in t["description"])
        assert payment["amount_minor"] == -2500

    def test_faster_payment_in_is_positive(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        salary = next(t for t in txns if "SALARY" in t["description"])
        assert salary["amount_minor"] == 50000

    def test_running_balance_captured(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        interest = next(t for t in txns if "interest" in t["description"].lower())
        payment = next(t for t in txns if "Test Merchant" in t["description"])
        salary = next(t for t in txns if "SALARY" in t["description"])
        assert interest["balance_minor"] == 10123
        assert payment["balance_minor"] == 7623
        assert salary["balance_minor"] == 57623


TEXT_WITHOUT_CLOSING_BALANCE = SAMPLE_TEXT.replace("Closing balance\n£576.23\n", "")
TEXT_WITHOUT_PERIOD = SAMPLE_TEXT.replace("1 June 2026 to 30 June 2026\n", "")


class TestKrooStatementPeriod:
    def test_extracts_period(self, adapter):
        period = adapter._extract_statement_period(SAMPLE_TEXT)
        assert period == (datetime(2026, 6, 1), datetime(2026, 6, 30))

    def test_returns_none_when_period_missing(self, adapter):
        assert adapter._extract_statement_period(TEXT_WITHOUT_PERIOD) is None

    def test_sets_last_statement_period(self, adapter):
        adapter.parse_transactions(SAMPLE_TEXT)
        assert adapter.last_statement_period is not None
        assert adapter.last_statement_period.from_date == datetime(2026, 6, 1)
        assert adapter.last_statement_period.to_date == datetime(2026, 6, 30)

    def test_no_period_leaves_last_statement_period_none(self, adapter):
        adapter.parse_transactions(TEXT_WITHOUT_PERIOD)
        assert adapter.last_statement_period is None

    def test_statement_period_resets_between_parses(self, adapter):
        """Adapter instances are reused across files by AdapterFactory - a
        period-bearing file must not leak into a later file with no period
        header of its own."""
        adapter.parse_transactions(SAMPLE_TEXT)
        assert adapter.last_statement_period is not None

        adapter.parse_transactions(TEXT_WITHOUT_PERIOD)
        assert adapter.last_statement_period is None


class TestKrooReconciliation:
    """Kroo's per-transaction balance is a direct read of a printed column,
    not rolled forward - so this check only confirms the last transaction's
    printed balance matches the statement's separate "Closing balance"
    anchor (previously skipped over during parsing, never captured), not
    that the transaction arithmetic itself reconciles."""

    def test_sets_last_reconciliation_on_match(self, adapter):
        adapter.parse_transactions(SAMPLE_TEXT)
        assert adapter.last_reconciliation is not None
        assert adapter.last_reconciliation.matches is True
        assert adapter.last_reconciliation.check_name == "kroo_closing_balance"

    def test_sets_last_reconciliation_on_mismatch(self, adapter):
        mismatched_text = SAMPLE_TEXT.replace(
            "Closing balance\n£576.23", "Closing balance\n£999.99"
        )
        adapter.parse_transactions(mismatched_text)
        assert adapter.last_reconciliation is not None
        assert adapter.last_reconciliation.matches is False

    def test_no_closing_balance_leaves_last_reconciliation_none(self, adapter):
        adapter.parse_transactions(TEXT_WITHOUT_CLOSING_BALANCE)
        assert adapter.last_reconciliation is None

    def test_reconciliation_resets_between_parses(self, adapter):
        """Adapter instances are reused across files by AdapterFactory - a
        mismatching file must not leak its result into a later file with no
        closing-balance anchor of its own."""
        mismatched_text = SAMPLE_TEXT.replace(
            "Closing balance\n£576.23", "Closing balance\n£999.99"
        )
        adapter.parse_transactions(mismatched_text)
        assert adapter.last_reconciliation is not None

        adapter.parse_transactions(TEXT_WITHOUT_CLOSING_BALANCE)
        assert adapter.last_reconciliation is None


class TestKrooSourceKey:
    def test_source_key_includes_account_identifier(self, adapter):
        txn = {"date": "1June2026", "description": "Test", "amount_minor": -100}
        key_with = adapter.generate_source_key(txn, 1, "01-02-03_12345678")
        key_without = adapter.generate_source_key(txn, 1, None)
        assert key_with != key_without
        assert "01-02-03_12345678" in key_with

    def test_detect_source_type(self, adapter):
        assert adapter.detect_source_type() == "kroo"
