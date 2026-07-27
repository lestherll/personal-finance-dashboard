"""Tests for Chase PDF adapter."""

from datetime import datetime

import pytest

from adapters.chase_pdf_adapter import ChasePdfAdapter

# Mirrors a real Chase Current statement: account-opening entry (time, no
# amount), opening/closing balance rows (one amount only - the balance
# itself), and two real transactions (amount + balance).
SAMPLE_TEXT = """Lesther Jr's Account statement
Account number:   18492643
Sort code:   60-84-07
02 June 2026 - 30 June 2026
Opening balance
£0.00
+
Money in
£200.00
−
Money out
£200.00
=
Closing balance
£0.00
Date
Transaction details
Amount
Balance
02 Jun 2026
You opened your account
20:58
02 Jun 2026
Opening balance
£0.00
02 Jun 2026
From LLACUNA L - Lesther NW
Payment
+£200.00
£200.00
02 Jun 2026
To Chase Saver
Transfer
-£200.00
£0.00
30 Jun 2026
Closing balance
£0.00
Page 1 of 2
Lesther Jr's Account statement
02 June 2026 - 30 June 2026
Account number:   18492643
Sort code:   60-84-07
Date
Transaction details
Amount
Balance
Some useful information
Some transactions may take a few days to finalise.
Chase is a registered trademark and trading name of J.P. Morgan Europe Limited. It is authorised by the Prudential
Regulation Authority.
Page 2 of 2
"""

# Mirrors the real Chase Saver statement for the same period - different
# account, different real transactions (a Kroo-originated deposit and the
# receiving side of the Current account's outgoing transfer above).
SAMPLE_SAVER_TEXT = """Chase Saver statement
Account number:   53932015
Sort code:   60-84-07
02 June 2026 - 30 June 2026
Opening balance
£0.00
+
Money in
£2,750.00
−
Money out
£0.00
=
Closing balance
£2,750.00
Date
Transaction details
Amount
Balance
02 Jun 2026
You opened your account
21:00
02 Jun 2026
Opening balance
£0.00
02 Jun 2026
From LESTHER JR LLACUNA - Sent from Kroo
Payment
+£2,550.00
£2,550.00
02 Jun 2026
From Lesther Jr's Account
Transfer
+£200.00
£2,750.00
30 Jun 2026
Closing balance
£2,750.00
Page 1 of 2
Chase Saver statement
02 June 2026 - 30 June 2026
Account number:   53932015
Sort code:   60-84-07
Date
Transaction details
Amount
Balance
Some useful information
Some transactions may take a few days to finalise.
Chase is a registered trademark and trading name of J.P. Morgan Europe Limited. It is authorised by the Prudential
Regulation Authority.
Page 2 of 2
"""


@pytest.fixture
def adapter():
    return ChasePdfAdapter()


class TestChaseValidation:
    def test_validates_chase_current_statement(self, adapter):
        assert adapter.validate_text(SAMPLE_TEXT)

    def test_validates_chase_saver_statement(self, adapter):
        text = SAMPLE_TEXT.replace(
            "Lesther Jr's Account statement", "Chase Saver statement"
        )
        assert adapter.validate_text(text)

    def test_rejects_non_chase_text(self, adapter):
        assert not adapter.validate_text("Some other bank statement")

    def test_rejects_missing_morgan_boilerplate(self, adapter):
        assert not adapter.validate_text("Lesther Jr's Account statement, Sort code")


class TestChaseIdentifierExtraction:
    def test_extracts_account_number_and_sort_code(self, adapter):
        identifier = adapter._extract_account_identifier(SAMPLE_TEXT)
        assert identifier == "18492643_60-84-07"

    def test_returns_none_when_missing(self, adapter):
        assert adapter._extract_account_identifier("no identifying info here") is None


class TestChaseParsing:
    def test_only_real_money_movements_are_transactions(self, adapter):
        """Account-opening (time, no amount) and opening/closing balance
        (single amount) rows must not be mistaken for transactions."""
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        assert len(txns) == 2

    def test_stamps_identifier_on_every_txn(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        assert all(t["_account_identifier_raw"] == "18492643_60-84-07" for t in txns)

    def test_incoming_payment_is_positive(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        payment = next(t for t in txns if "Lesther NW" in t["description"])
        assert payment["amount_minor"] == 20000
        assert payment["date"] == "02 Jun 2026"

    def test_outgoing_transfer_is_negative(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        transfer = next(t for t in txns if "Chase Saver" in t["description"])
        assert transfer["amount_minor"] == -20000

    def test_balance_captured(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        payment = next(t for t in txns if "Lesther NW" in t["description"])
        transfer = next(t for t in txns if "Chase Saver" in t["description"])
        assert payment["balance_minor"] == 20000
        assert transfer["balance_minor"] == 0

    def test_saver_statement_parses(self, adapter):
        """Genuinely Saver-shaped content (different account, different
        real transactions) - previously only exercised by string-replacing
        the header on the Current fixture, never parsed for real."""
        txns = adapter.parse_transactions(SAMPLE_SAVER_TEXT)
        assert len(txns) == 2
        assert all(
            t["_account_identifier_raw"] == "53932015_60-84-07" for t in txns
        )

        kroo_deposit = next(t for t in txns if "Sent from Kroo" in t["description"])
        assert kroo_deposit["amount_minor"] == 255000
        assert kroo_deposit["balance_minor"] == 255000

        transfer_in = next(
            t for t in txns if "From Lesther Jr's Account" in t["description"]
        )
        assert transfer_in["amount_minor"] == 20000
        assert transfer_in["balance_minor"] == 275000

    def test_stops_before_footer_disclaimer(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        assert not any("FSCS" in t["description"] for t in txns)
        assert not any("Prudential" in t["description"] for t in txns)

    def test_repeated_page_header_does_not_pollute_descriptions(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_TEXT)
        assert not any("Account number" in t["description"] for t in txns)
        assert not any("Sort code" in t["description"] for t in txns)


class TestChaseTwoAccountsDisambiguation:
    """Chase allows multiple accounts of the same or different type (e.g. a
    Current plus a Saver) - AdapterFactory reuses one adapter instance
    across every file in a batch, so parsing both in sequence must not
    leak state and must produce distinguishable identifiers/source keys."""

    def test_identifiers_differ_across_accounts(self, adapter):
        current_txns = adapter.parse_transactions(SAMPLE_TEXT)
        saver_txns = adapter.parse_transactions(SAMPLE_SAVER_TEXT)

        current_ids = {t["_account_identifier_raw"] for t in current_txns}
        saver_ids = {t["_account_identifier_raw"] for t in saver_txns}
        assert current_ids == {"18492643_60-84-07"}
        assert saver_ids == {"53932015_60-84-07"}
        assert current_ids != saver_ids

    def test_source_keys_differ_for_same_looking_transaction(self, adapter):
        txn = {"date": "02 Jun 2026", "description": "Payment", "amount_minor": 20000}
        current_key = adapter.generate_source_key(
            txn, 1, account_identifier="18492643_60-84-07"
        )
        saver_key = adapter.generate_source_key(
            txn, 1, account_identifier="53932015_60-84-07"
        )
        assert current_key != saver_key


class TestChaseStatementPeriod:
    def test_extracts_period(self, adapter):
        period = adapter._extract_statement_period(SAMPLE_TEXT)
        assert period == (datetime(2026, 6, 2), datetime(2026, 6, 30))

    def test_returns_none_when_period_missing(self, adapter):
        assert (
            adapter._extract_statement_period("Lesther Jr's Account statement") is None
        )

    def test_sets_last_statement_period(self, adapter):
        adapter.parse_transactions(SAMPLE_TEXT)
        assert adapter.last_statement_period is not None
        assert adapter.last_statement_period.from_date == datetime(2026, 6, 2)
        assert adapter.last_statement_period.to_date == datetime(2026, 6, 30)

    def test_no_period_leaves_last_statement_period_none(self, adapter):
        adapter.parse_transactions("Lesther Jr's Account statement")
        assert adapter.last_statement_period is None

    def test_statement_period_resets_between_parses(self, adapter):
        """Adapter instances are reused across files by AdapterFactory - a
        period-bearing file must not leak into a later file with no period
        header of its own."""
        adapter.parse_transactions(SAMPLE_TEXT)
        assert adapter.last_statement_period is not None

        adapter.parse_transactions("Lesther Jr's Account statement")
        assert adapter.last_statement_period is None


TEXT_WITHOUT_CLOSING_BALANCE = SAMPLE_TEXT.replace("Closing balance\n£0.00\n", "")


class TestChaseReconciliation:
    """Chase rolls its own "Opening balance" anchor forward through
    transactions and compares against the printed "Closing balance" - same
    B1 pattern as Amex/First Direct/Natwest Statement/Kroo, but summed
    (cash/asset account) rather than subtracted (credit card liability)."""

    def test_sets_last_reconciliation_on_match(self, adapter):
        adapter.parse_transactions(SAMPLE_TEXT)
        assert adapter.last_reconciliation is not None
        assert adapter.last_reconciliation.matches is True
        assert adapter.last_reconciliation.check_name == "chase_closing_balance"
        assert adapter.last_reconciliation.expected_closing_minor == 0
        assert adapter.last_reconciliation.derived_closing_minor == 0
        assert adapter.last_reconciliation.expected_opening_minor == 0

    def test_sets_last_reconciliation_on_match_saver(self, adapter):
        adapter.parse_transactions(SAMPLE_SAVER_TEXT)
        assert adapter.last_reconciliation is not None
        assert adapter.last_reconciliation.matches is True
        assert adapter.last_reconciliation.expected_closing_minor == 275000
        assert adapter.last_reconciliation.derived_closing_minor == 275000
        assert adapter.last_reconciliation.expected_opening_minor == 0

    def test_sets_last_reconciliation_on_mismatch(self, adapter):
        mismatched_text = SAMPLE_TEXT.replace(
            "Closing balance\n£0.00", "Closing balance\n£999.99"
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
            "Closing balance\n£0.00", "Closing balance\n£999.99"
        )
        adapter.parse_transactions(mismatched_text)
        assert adapter.last_reconciliation is not None

        adapter.parse_transactions(TEXT_WITHOUT_CLOSING_BALANCE)
        assert adapter.last_reconciliation is None


class TestChaseSourceKey:
    def test_source_key_includes_account_identifier(self, adapter):
        txn = {"date": "02 Jun 2026", "description": "Test", "amount_minor": -100}
        key_with = adapter.generate_source_key(txn, 1, "18492643_60-84-07")
        key_without = adapter.generate_source_key(txn, 1, None)
        assert key_with != key_without
        assert "18492643_60-84-07" in key_with

    def test_detect_source_type(self, adapter):
        assert adapter.detect_source_type() == "chase"
