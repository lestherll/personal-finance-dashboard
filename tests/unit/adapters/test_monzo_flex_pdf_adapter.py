"""Tests for Monzo Flex PDF adapter."""

from datetime import datetime

import pytest

from adapters.monzo_flex_pdf_adapter import MonzoFlexPdfAdapter

# Synthetic fixture (fake merchants/amounts, not the user's real statement)
# but structurally faithful to a real Flex export, including a deliberate
# reproduction of the page-7/8-boundary description scramble (see the
# adapter's docstring): "Old Purchase"'s buffer swallows the trailing
# "Corner Bakery" line before the next date, and the final "01/04/2026" row
# is left with no merchant name at all.
SAMPLE_TEXT = """Flex statement
The amounts you'll see below are for these dates
01/04/2026 - 30/06/2026
Jane Doe
1 Example Street
Sometown
AB1 2CD
United Kingdom
£1217.07
Balance at start
Including interest
£0.00
Interest you paid across all
your plans
£150.00
Balance at end
Includes interest to 01/07/2026
29%
Interest rate
£2000.00
Available to spend
£0.00
Missed payments
Based on your minimum
payment
£50.00
Amount you paid back
£30.00 minimum monthly
payment*
Date
Description
Debit
Credit
Balance
30/06/2026
Corner Shop
5.00
0.00
150.00
28/06/2026
Foreign Cafe
Amount: EUR 12.00. Conversion rate:
0.85.
10.20
0.00
145.00
01/06/2026
Monthly payment
0.00
30.00
134.80
15/04/2026
Old Purchase
Amount: EUR 5.00. Conversion rate:
0.85.
4.25
0.00
164.80
Corner Bakery
01/04/2026
Amount: EUR 3.00. Conversion rate:
0.85.
2.55
0.00
160.55
 Some of the wording below is legally prescribed, which means we can't change it.
"""

TEXT_WITHOUT_PERIOD = SAMPLE_TEXT.replace("01/04/2026 - 30/06/2026\n", "")
TEXT_WITHOUT_BALANCE_AT_END = SAMPLE_TEXT.replace("£150.00\nBalance at end\n", "")
TEXT_MISMATCHED_BALANCE_AT_END = SAMPLE_TEXT.replace(
    "£150.00\nBalance at end", "£999.99\nBalance at end"
)


@pytest.fixture
def adapter():
    return MonzoFlexPdfAdapter()


class TestMonzoFlexValidation:
    def test_validates_flex_statement(self, adapter):
        assert adapter.validate_text(SAMPLE_TEXT)

    def test_rejects_non_flex_text(self, adapter):
        assert not adapter.validate_text("Some other bank statement")

    def test_rejects_bare_flex_mention_without_statement_header(self, adapter):
        """A bare "Flex" mention (e.g. as it appears inside a Monzo Personal
        Account statement's own transaction description, since Flex
        repayments show up there too) must not be mistaken for a Flex
        statement itself."""
        assert not adapter.validate_text(
            "Personal Account statement\n30/06/2026\nFlex\n-651.12\n2,375.37\n"
        )


class TestMonzoFlexNoAccountIdentifier:
    def test_transactions_have_no_account_identifier(self, adapter):
        """No sort code, account number, IBAN, BIC, or masked digits appear
        anywhere in a real Flex statement - parse_transactions must never
        set _account_identifier_raw."""
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        assert txns
        assert all("_account_identifier_raw" not in t for t in txns)


class TestMonzoFlexParsing:
    def test_parses_expected_number_of_transactions(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        assert len(txns) == 5

    def test_debit_only_purchase_is_negative(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        txn = next(t for t in txns if "Corner Shop" in t["description"])
        assert txn["amount"] == -5.00
        assert txn["balance"] == 150.00

    def test_credit_only_payment_is_positive(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        txn = next(t for t in txns if t["description"] == "Monthly payment")
        assert txn["amount"] == 30.00
        assert txn["balance"] == 134.80

    def test_foreign_currency_description_is_joined(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        txn = next(t for t in txns if "Foreign Cafe" in t["description"])
        assert (
            txn["description"]
            == "Foreign Cafe Amount: EUR 12.00. Conversion rate: 0.85."
        )
        assert txn["amount"] == -10.20
        assert txn["balance"] == 145.00

    def test_stops_before_legal_footer(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        assert not any("legally prescribed" in t["description"] for t in txns)

    def test_page_split_description_is_numerically_correct_but_scrambled(self, adapter):
        """Known, accepted limitation: a description split across a page
        boundary can land on the wrong row. Date/debit/credit/balance stay
        correct for both affected rows regardless - only `description` text
        is scrambled. See the adapter's docstring / CLAUDE.md."""
        txns = adapter.parse_transactions(SAMPLE_TEXT)

        old_purchase = next(t for t in txns if "Old Purchase" in t["description"])
        assert old_purchase["amount"] == -4.25
        assert old_purchase["balance"] == 164.80
        # The next row's trailing merchant name leaks into this description.
        assert "Corner Bakery" in old_purchase["description"]

        last_row = next(t for t in txns if t["date"] == "01/04/2026")
        assert last_row["amount"] == -2.55
        assert last_row["balance"] == 160.55
        # Its own merchant name was lost to the row above.
        assert last_row["description"] == "Amount: EUR 3.00. Conversion rate: 0.85."


class TestMonzoFlexStatementPeriod:
    def test_extracts_period(self, adapter):
        period = adapter._extract_statement_period(SAMPLE_TEXT)
        assert period == (datetime(2026, 4, 1), datetime(2026, 6, 30))

    def test_returns_none_when_period_missing(self, adapter):
        assert adapter._extract_statement_period(TEXT_WITHOUT_PERIOD) is None

    def test_sets_last_statement_period(self, adapter):
        adapter.parse_transactions(SAMPLE_TEXT)
        assert adapter.last_statement_period is not None
        assert adapter.last_statement_period.from_date == datetime(2026, 4, 1)
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


class TestMonzoFlexReconciliation:
    """Flex's table is newest-first (unlike Kroo's oldest-first), so the
    FIRST parsed transaction's own printed balance is the one that should
    match the statement's "Balance at end" anchor - this only confirms the
    table was read through to its start, not that the transaction
    arithmetic itself reconciles (direct-read balance, nothing rolled
    forward)."""

    def test_sets_last_reconciliation_on_match(self, adapter):
        adapter.parse_transactions(SAMPLE_TEXT)
        assert adapter.last_reconciliation is not None
        assert adapter.last_reconciliation.matches is True
        assert adapter.last_reconciliation.check_name == "monzo_flex_balance_at_end"

    def test_sets_last_reconciliation_on_mismatch(self, adapter):
        adapter.parse_transactions(TEXT_MISMATCHED_BALANCE_AT_END)
        assert adapter.last_reconciliation is not None
        assert adapter.last_reconciliation.matches is False

    def test_no_balance_at_end_leaves_last_reconciliation_none(self, adapter):
        adapter.parse_transactions(TEXT_WITHOUT_BALANCE_AT_END)
        assert adapter.last_reconciliation is None

    def test_reconciliation_resets_between_parses(self, adapter):
        """Adapter instances are reused across files by AdapterFactory - a
        mismatching file must not leak its result into a later file with no
        balance-at-end anchor of its own."""
        adapter.parse_transactions(TEXT_MISMATCHED_BALANCE_AT_END)
        assert adapter.last_reconciliation is not None

        adapter.parse_transactions(TEXT_WITHOUT_BALANCE_AT_END)
        assert adapter.last_reconciliation is None


class TestMonzoFlexSourceKey:
    def test_source_key_format(self, adapter):
        txn = {"date": "30/06/2026", "description": "Test", "amount": -1.0}
        key = adapter.generate_source_key(txn, 1, None)
        assert key.startswith("monzo_flex_txn_")

    def test_detect_source_type(self, adapter):
        assert adapter.detect_source_type() == "monzo-flex"
