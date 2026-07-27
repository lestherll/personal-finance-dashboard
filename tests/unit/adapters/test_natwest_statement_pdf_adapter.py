"""Tests for the Natwest quarterly Statement PDF adapter.

Distinct from `test_natwest_transactions_pdf_adapter.py`, which covers the
online "Transactions" export - a structurally unrelated document. This fixture
mirrors the real quarterly statement's layout: a Date/Description/Paid
In(£)/Withdrawn(£)/Balance(£) table (not a single signed-amount-per-line
format), an opening "BROUGHT FORWARD" row, and a same-day continuation
transaction that omits its date line (only printed once per calendar day).
"""

from datetime import datetime

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
        assert credit["amount_minor"] == 10000
        assert credit["balance_minor"] == 111000

    def test_same_day_continuation_row_carries_forward_date(self, adapter):
        """The second '02 MAR' transaction omits its date line entirely -
        the statement only prints a date once per calendar day. (Date
        carries a stamped year here since SAMPLE_TEXT has a Period Covered
        header - see TestNatwestStatementYearInference.)"""
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        salary = next(t for t in txns if "SALARY" in t["description"])
        food = next(t for t in txns if "Food" in t["description"])
        assert salary["date"] == "02 MAR 2026"
        assert food["date"] == "02 MAR 2026"
        assert salary["amount_minor"] == -10000
        assert salary["balance_minor"] == 101000
        assert food["amount_minor"] == -5000
        assert food["balance_minor"] == 96000

    def test_stamps_account_identifier_on_every_txn(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        assert all(t["_account_identifier_raw"] == "12345678_11-22-33" for t in txns)

    def test_final_balance_reconciles_with_no_warning(self, adapter, caplog):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        assert txns[-1]["balance_minor"] == 96000
        assert "does not match" not in caplog.text

    def test_reconciliation_mismatch_is_logged_not_raised(self, adapter, caplog):
        mismatched_text = SAMPLE_TEXT.replace(
            "New Balance\n£960.00", "New Balance\n£999.99"
        )
        txns = adapter.parse_transactions(mismatched_text)
        assert len(txns) == 3  # doesn't raise or drop data
        assert "does not match" in caplog.text


BARE_TEXT = """Account Name
Account No
Sort Code
Page No
MR TEST PERSON
12345678
11-22-33
1 of 1
CURRENT ACCOUNT
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
"""


class TestNatwestStatementReconciliation:
    def test_sets_last_reconciliation_on_match(self, adapter):
        adapter.parse_transactions(SAMPLE_TEXT)
        assert adapter.last_reconciliation is not None
        assert adapter.last_reconciliation.matches is True
        assert adapter.last_reconciliation.check_name == "natwest_statement_new_balance"

    def test_sets_last_reconciliation_on_mismatch(self, adapter):
        mismatched_text = SAMPLE_TEXT.replace(
            "New Balance\n£960.00", "New Balance\n£999.99"
        )
        adapter.parse_transactions(mismatched_text)
        assert adapter.last_reconciliation is not None
        assert adapter.last_reconciliation.matches is False

    def test_no_new_balance_leaves_last_reconciliation_none(self, adapter):
        adapter.parse_transactions(BARE_TEXT)
        assert adapter.last_reconciliation is None

    def test_reconciliation_and_period_reset_between_parses(self, adapter):
        """Adapter instances are reused across files by AdapterFactory - a
        mismatching/period-bearing file must not leak into a later bare
        file with neither anchor."""
        mismatched_text = SAMPLE_TEXT.replace(
            "New Balance\n£960.00", "New Balance\n£999.99"
        )
        adapter.parse_transactions(mismatched_text)
        assert adapter.last_reconciliation is not None
        assert adapter.last_statement_period is not None

        adapter.parse_transactions(BARE_TEXT)
        assert adapter.last_reconciliation is None
        assert adapter.last_statement_period is None


class TestNatwestStatementPeriodCovered:
    def test_extracts_period_covered(self, adapter):
        period = adapter._extract_period_covered(SAMPLE_TEXT)
        assert period is not None
        from_date, to_date = period
        assert from_date == datetime(2026, 2, 14)
        assert to_date == datetime(2026, 5, 13)

    def test_returns_none_when_missing(self, adapter):
        assert adapter._extract_period_covered(BARE_TEXT) is None

    def test_sets_last_statement_period(self, adapter):
        adapter.parse_transactions(SAMPLE_TEXT)
        assert adapter.last_statement_period is not None
        assert adapter.last_statement_period.from_date == datetime(2026, 2, 14)
        assert adapter.last_statement_period.to_date == datetime(2026, 5, 13)


class TestNatwestStatementYearInference:
    """The per-transaction dates in this format never carry a year (e.g.
    "26 FEB") - same ambiguity as Amex/natwest-transactions. An earlier
    version of this adapter only used the extracted "Period Covered" range
    for B3 coverage tracking, not for dating transactions, leaving this
    adapter solely reliant on the Silver-layer upload-timestamp fallback.
    It now resolves the real year via resolve_year_in_period(), exactly
    like Amex/natwest-transactions already do."""

    def test_all_transaction_dates_get_a_year_stamped(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        assert len(txns) == 3
        assert all(t["date"].endswith("2026") for t in txns)
        credit = next(t for t in txns if "Automated Credit" in t["description"])
        assert credit["date"] == "26 FEB 2026"

    def test_no_period_leaves_dates_year_less(self, adapter):
        """Backward compatible: no Period Covered header -> date stays
        'DD MON' and the Silver-layer upload-timestamp fallback takes over
        instead."""
        txns = adapter.parse_transactions(BARE_TEXT)
        assert all(len(t["date"].split()) == 2 for t in txns)


class TestNatwestStatementSourceKey:
    def test_source_key_includes_account_identifier(self, adapter):
        txn = {"date": "26FEB", "description": "Test", "amount_minor": 10000}
        key_with = adapter.generate_source_key(txn, 1, "12345678_11-22-33")
        key_without = adapter.generate_source_key(txn, 1, None)
        assert key_with != key_without
        assert "12345678_11-22-33" in key_with

    def test_detect_source_type(self, adapter):
        assert adapter.detect_source_type() == "natwest-statement"
