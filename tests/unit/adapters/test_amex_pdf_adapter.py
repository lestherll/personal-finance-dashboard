"""Tests for Amex PDF adapter.

Real Amex statements extract cleanly with PyMuPDF's `sort=True` mode - each
transaction stays on one line ("date date description amount"), optionally
followed by a "CR" marker line for credits - which is what `AmexPdfAdapter`
overrides `_extract_text` to use (see AMEX_BUG_HANDOFF.md for why the
default flattened extraction mode is unreliable for this bank's layout).
These fixtures mirror that single-line structure.
"""

import fitz
import pytest

from adapters.amex_pdf_adapter import AmexPdfAdapter

SAMPLE_PAGE = """Mr Test Cardholder
xxxx-xxxxxx-12345
19/05/26
Page 2 of 3
Transaction   Process
Date         Date          Transaction Details                        Amount  £
May 1   May 1   PAYMENT RECEIVED - THANK YOU                              50.00
CR
Apr 19   Apr 19   COFFEE SHOP LONDON                                       3.85
Apr 20   Apr 20   GROCERY STORE LONDON                                     8.00
GOODS
Statement of Account
American Express®
Preferred Rewards Gold Credit Card
"""


@pytest.fixture
def adapter():
    return AmexPdfAdapter()


class TestAmexValidation:
    def test_validates_amex_statement(self, adapter):
        assert adapter.validate_text(
            "American Express Preferred Rewards Gold Credit Card"
        )

    def test_rejects_non_amex_text(self, adapter):
        assert not adapter.validate_text("Some other bank statement")


class TestAmexIdentifierExtraction:
    def test_extracts_masked_card_number(self, adapter):
        assert adapter._extract_account_identifier(SAMPLE_PAGE) == "xxxx-xxxxxx-12345"

    def test_returns_none_when_missing(self, adapter):
        assert adapter._extract_account_identifier("no card info here") is None


class TestAmexTransactionParsing:
    def test_parses_all_transactions(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_PAGE)
        assert len(txns) == 3

    def test_credit_marked_payment_is_positive(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_PAGE)
        payment = next(t for t in txns if "PAYMENT RECEIVED" in t["description"])
        assert payment["amount"] == 50.00

    def test_purchases_are_negative(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_PAGE)
        coffee = next(t for t in txns if "COFFEE SHOP" in t["description"])
        grocery = next(t for t in txns if "GROCERY STORE" in t["description"])
        assert coffee["amount"] == -3.85
        assert grocery["amount"] == -8.00

    def test_stamps_account_identifier_on_every_txn(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_PAGE)
        assert all(t["_account_identifier_raw"] == "xxxx-xxxxxx-12345" for t in txns)

    def test_page_with_no_transaction_lines_yields_no_transactions(self, adapter):
        assert adapter._parse_page("just some random unrelated text\n42.00\n") == []

    def test_ignores_boilerplate_lines_with_decimal_numbers(self, adapter):
        """A stray line with decimal-shaped numbers but no leading date pair
        (e.g. an interest-rate worked example) must not be parsed as a
        transaction - `_TXN_LINE_RE` requires two leading dates."""
        page = SAMPLE_PAGE + "\nSome example paragraph: 100.00 + 25.00 = 75.00\n"
        txns = adapter.parse_transactions(page)
        assert len(txns) == 3

    def test_other_account_transactions_credit_needs_cr_suffix(self, adapter):
        """ "OTHER ACCOUNT TRANSACTIONS" isn't a blanket-credit section - it
        can hold real debits too (e.g. a membership fee), so only rows
        followed by a "CR" marker (standalone or as a trailing suffix on the
        next line) are treated as credits."""
        page = """xxxx-xxxxxx-12345
Amount  £
Jul 19   Jul 19   SOME PURCHASE                                            5.00
Total new spend transactions for Test                                      5.00
OTHER ACCOUNT TRANSACTIONS
Jul 7   Jul 7   DELIVEROO                                                  5.00
DeliverooGoldBenefit                                          CR
Jul 19   Jul 19   MEMBERSHIP FEE                                         195.00
Total of other account transactions                                      190.00
"""
        txns = adapter.parse_transactions(page)
        deliveroo = next(t for t in txns if "DELIVEROO" in t["description"])
        fee = next(t for t in txns if "MEMBERSHIP FEE" in t["description"])
        assert deliveroo["amount"] == 5.00
        assert fee["amount"] == -195.00

    def test_plan_it_instalments_created_parsed_as_credit(self, adapter):
        """A "New Plan It Instalments Created" row has only one date (not
        the two-date pair of a normal transaction row) and must be parsed
        as a positive credit - it reflects an existing purchase (already
        parsed elsewhere as its own debit) being moved into an installment
        plan. See Bug 4 in AMEX_BUG_HANDOFF.md."""
        page = """xxxx-xxxxxx-12345
Amount  £
Mar 25   Mar 25   BIG PURCHASE                                          1656.39
Total new spend transactions for Test                                  1656.39
New Plan It Instalments Created
Apr 12   INSTALMENT PLAN                                               1656.39
Total of New Plan It Instalments Created                               1656.39
"""
        txns = adapter.parse_transactions(page)
        assert len(txns) == 2
        created = next(t for t in txns if t["date"] == "12 Apr")
        assert created["amount"] == 1656.39

    def test_stops_plan_it_parsing_at_total_line(self, adapter):
        page = """xxxx-xxxxxx-12345
New Plan It Instalments Created
Apr 12   INSTALMENT PLAN                                               1656.39
Total of New Plan It Instalments Created                               1656.39
Statement of Account
"""
        txns = adapter.parse_transactions(page)
        assert len(txns) == 1


class TestAmexMultiPage:
    def test_splits_on_page_boundary_sentinel(self, adapter):
        two_pages = SAMPLE_PAGE + "\x0c" + SAMPLE_PAGE
        txns = adapter.parse_transactions(two_pages)
        assert len(txns) == 6


def _build_pdf_bytes(lines_with_y):
    """Build a minimal real PDF for tests that need actual bytes (balance
    extraction reopens the file with PyMuPDF's sort=True mode, so it can't
    be tested against a plain extracted-text string like the other tests
    in this file)."""
    doc = fitz.open()
    page = doc.new_page()
    for text, y in lines_with_y:
        page.insert_text((50, y), text)
    return doc.tobytes()


class TestAmexDerivedBalance:
    """The Account Summary box (Previous Closing Balance / Closing Balance)
    is column-flattened like the transaction table, verified against a real
    statement to need sort=True extraction - see AmexPdfAdapter.parse()."""

    def _statement_bytes(self, extra_lines=None):
        lines = [
            ("American Express", 50),
            ("Preferred Rewards Gold Credit Card", 70),
            ("xxxx-xxxxxx-12345", 100),
            ("Account Summary", 120),
            (
                "Previous Closing Balance New Credits New Debits Closing Balance",
                140,
            ),
            ("£100.00 - £20.00 + £30.00 = £110.00", 160),
            ("May 1   May 1   PAYMENT RECEIVED - THANK YOU              20.00", 200),
            ("CR", 220),
            ("Apr 19   Apr 19   COFFEE SHOP LONDON                       3.85", 260),
            ("Prepared for", 380),
            ("Membership Number", 390),
            ("Amount  £", 460),
        ]
        if extra_lines:
            lines.extend(extra_lines)
        return _build_pdf_bytes(lines)

    def test_extracts_previous_and_closing_balance(self, adapter):
        content = self._statement_bytes()
        anchors = adapter._extract_account_summary(content)
        assert anchors is not None
        previous, closing, plan_it_due = anchors
        assert previous == 100.00
        assert closing == 110.00
        assert plan_it_due is None

    def test_extracts_plan_it_instalments_due_when_present(self, adapter):
        content = _build_pdf_bytes(
            [
                ("American Express", 50),
                ("Preferred Rewards Gold Credit Card", 70),
                ("xxxx-xxxxxx-12345", 100),
                (
                    "Previous Closing Balance New Credits New Debits "
                    "Plan It Instalments Due Closing Balance",
                    140,
                ),
                ("£100.00 - £20.00 + £30.00 + £15.00 = £125.00", 160),
                ("CR", 180),
            ]
        )
        anchors = adapter._extract_account_summary(content)
        assert anchors is not None
        previous, closing, plan_it_due = anchors
        assert previous == 100.00
        assert closing == 125.00
        assert plan_it_due == 15.00

    def test_balance_rolls_forward_through_transactions(self, adapter):
        content = self._statement_bytes()
        records = adapter.parse(content, "test.pdf", "fakehash")
        payment = next(
            r for r in records if "PAYMENT RECEIVED" in r.raw_data["description"]
        )
        coffee = next(r for r in records if "COFFEE SHOP" in r.raw_data["description"])
        # Previous Closing Balance 100.00; payment (credit, +20.00) reduces
        # what's owed, coffee purchase (debit, -3.85) increases it.
        assert payment.raw_data["balance"] == 80.00
        assert coffee.raw_data["balance"] == 83.85

    def test_no_account_summary_block_skips_balance_silently(self, adapter):
        content = _build_pdf_bytes(
            [
                ("American Express", 50),
                ("Preferred Rewards Gold Credit Card", 70),
                ("xxxx-xxxxxx-12345", 100),
                ("CR", 180),
                (
                    "May 1   May 1   PAYMENT RECEIVED - THANK YOU        20.00",
                    220,
                ),
            ]
        )
        records = adapter.parse(content, "test.pdf", "fakehash")
        assert len(records) == 1
        assert "balance" not in records[0].raw_data


class TestAmexSourceKey:
    def test_source_key_includes_account_identifier(self, adapter):
        txn = {"date": "19 Apr", "description": "Test", "amount": -1.0}
        key_with = adapter.generate_source_key(txn, 1, "xxxx-xxxxxx-12345")
        key_without = adapter.generate_source_key(txn, 1, None)
        assert key_with != key_without

    def test_detect_source_type(self, adapter):
        assert adapter.detect_source_type() == "amex"
