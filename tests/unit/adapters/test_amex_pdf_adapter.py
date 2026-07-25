"""Tests for Amex PDF adapter.

Real Amex statements extract via PyMuPDF as column-flattened tables (all
dates print first, then all descriptions, then all amounts as a separate
block) rather than one line per transaction - these fixtures mirror that
structure, including a boilerplate paragraph containing decimal numbers
that must NOT be mistaken for transaction amounts.
"""

from datetime import datetime

import pytest

from adapters.amex_pdf_adapter import AmexPdfAdapter

# Mirrors the real column-block layout: date/date/description triples,
# then unrelated remark lines, then a boilerplate "worked example" with
# amount-shaped numbers, then the real amount block, then footer text.
SAMPLE_PAGE = """Mr Test Cardholder
xxxx-xxxxxx-12345
19/05/26
Page 2 of 3
CR
May 1
May 1
PAYMENT RECEIVED - THANK YOU
Apr 19
Apr 19
COFFEE SHOP LONDON
Apr 20
Apr 20
GROCERY STORE LONDON
GOODS
GOODS
Some interest rate worked example paragraph mentioning numbers like
100.00
50.00
+
25.00
=
75.00
that are not real transaction amounts.
Prepared for
Membership Number
Date
Transaction
Date
Process
Date
Transaction Details
Amount  £
50.00
3.85
8.00
Statement of Account
American Express®
Preferred Rewards Gold Credit Card
"""


# Mirrors real statements, which print the period once as e.g.
# "From  20 April to 19 May 2026" - the source text never puts a year on
# individual transaction dates ("Apr 19"), only on this summary line.
SAMPLE_PAGE_WITH_PERIOD = (
    "Statement includes payments and charges received by 19 May 2026\n"
    "From  20 April to 19 May 2026\n" + SAMPLE_PAGE
)


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


class TestAmexColumnReconstruction:
    def test_ignores_boilerplate_amounts_and_uses_real_block(self, adapter):
        """The real amount block is the run matching the transaction count,
        not the interest-example numbers that appear earlier on the page."""
        txns = adapter.parse_transactions(SAMPLE_PAGE)

        assert len(txns) == 3
        amounts = {round(t["amount"], 2) for t in txns}
        # None of the boilerplate's numbers (100.00, 25.00, 75.00) should appear.
        assert 100.00 not in amounts
        assert 75.00 not in amounts

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

    def test_page_with_no_triples_yields_no_transactions(self, adapter):
        assert adapter._parse_page("just some random unrelated text\n42.00\n") == []


class TestAmexMultiPage:
    def test_splits_on_page_boundary_sentinel(self, adapter):
        two_pages = SAMPLE_PAGE + "\x0c" + SAMPLE_PAGE
        txns = adapter.parse_transactions(two_pages)
        assert len(txns) == 6


class TestAmexStatementPeriod:
    def test_extracts_period_with_inferred_from_year(self, adapter):
        period = adapter._extract_statement_period(SAMPLE_PAGE_WITH_PERIOD)
        assert period == (datetime(2026, 4, 20), datetime(2026, 5, 19))

    def test_returns_none_when_period_missing(self, adapter):
        assert adapter._extract_statement_period(SAMPLE_PAGE) is None

    def test_period_crossing_year_boundary_infers_prior_year_for_from_date(
        self, adapter
    ):
        text = "From  20 December to 19 January 2026\n"
        period = adapter._extract_statement_period(text)
        assert period == (datetime(2025, 12, 20), datetime(2026, 1, 19))

    def test_parse_transactions_attaches_year_from_period(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_PAGE_WITH_PERIOD)
        assert len(txns) == 3
        assert all(len(t["date"].split()) == 3 for t in txns)
        coffee = next(t for t in txns if "COFFEE SHOP" in t["description"])
        assert coffee["date"] == "19 Apr 2026"

    def test_parse_transactions_without_period_leaves_date_year_less(self, adapter):
        """Backward compatible: no period header -> date stays 'DD Mon' and
        the Silver-layer upload-timestamp fallback takes over instead."""
        txns = adapter.parse_transactions(SAMPLE_PAGE)
        assert all(len(t["date"].split()) == 2 for t in txns)


class TestAmexSourceKey:
    def test_source_key_includes_account_identifier(self, adapter):
        txn = {"date": "19 Apr", "description": "Test", "amount": -1.0}
        key_with = adapter.generate_source_key(txn, 1, "xxxx-xxxxxx-12345")
        key_without = adapter.generate_source_key(txn, 1, None)
        assert key_with != key_without

    def test_detect_source_type(self, adapter):
        assert adapter.detect_source_type() == "amex"
