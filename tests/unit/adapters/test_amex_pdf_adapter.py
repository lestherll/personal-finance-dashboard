"""Tests for Amex PDF adapter.

Real Amex statements extract cleanly with PyMuPDF's `sort=True` mode - each
transaction stays on one line ("date date description amount"), optionally
followed by a "CR" marker line for credits - which is what `AmexPdfAdapter`
overrides `_extract_text` to use (see AMEX_BUG_HANDOFF.md for why the
default flattened extraction mode is unreliable for this bank's layout).
These fixtures mirror that single-line structure.
"""

from datetime import datetime
from decimal import Decimal

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


class TestAmexTransactionParsing:
    def test_parses_all_transactions(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_PAGE)
        assert len(txns) == 3

    def test_credit_marked_payment_is_positive(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_PAGE)
        payment = next(t for t in txns if "PAYMENT RECEIVED" in t["description"])
        assert payment["amount_minor"] == 5000

    def test_purchases_are_negative(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_PAGE)
        coffee = next(t for t in txns if "COFFEE SHOP" in t["description"])
        grocery = next(t for t in txns if "GROCERY STORE" in t["description"])
        assert coffee["amount_minor"] == -385
        assert grocery["amount_minor"] == -800

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
        assert deliveroo["amount_minor"] == 500
        assert fee["amount_minor"] == -19500

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
        assert created["amount_minor"] == 165639

    def test_stops_plan_it_parsing_at_total_line(self, adapter):
        page = """xxxx-xxxxxx-12345
New Plan It Instalments Created
Apr 12   INSTALMENT PLAN                                               1656.39
Total of New Plan It Instalments Created                               1656.39
Statement of Account
"""
        txns = adapter.parse_transactions(page)
        assert len(txns) == 1

    def test_instalment_plan_fee_parsed_as_debit(self, adapter):
        """A "New Plan It Instalments and Fees" section is distinct from
        "New Plan It Instalments Created" - it restates the plan (a bare
        "INSTALMENT PLAN" line, no new transaction) and adds a genuine new
        "INSTALMENT PLAN FEE" debit, charged every month the plan is
        active."""
        page = """xxxx-xxxxxx-12345
New Plan It Instalments and Fees
Apr 12         INSTALMENT PLAN                                            1656.39
Apr 19         INSTALMENT PLAN FEE                                              17.23
Total of New Instalment Plans and Fees                                       1673.62
"""
        txns = adapter.parse_transactions(page)
        assert len(txns) == 1
        fee = txns[0]
        assert fee["description"] == "INSTALMENT PLAN FEE"
        assert fee["date"] == "19 Apr"
        assert fee["amount_minor"] == -1723

    def test_instalment_plan_fee_parsed_without_new_plan_created(self, adapter):
        """An ongoing plan (no new plan created this month) still bills a
        fee - matches the real May-Jun 2026 statement, which has only a fee
        line in this section, no restated "INSTALMENT PLAN" row."""
        page = """xxxx-xxxxxx-12345
New Plan It Instalments and Fees
Jun 19         INSTALMENT PLAN FEE                                              17.22
Total of New Instalment Plans and Fees                                          17.22
"""
        txns = adapter.parse_transactions(page)
        assert len(txns) == 1
        assert txns[0]["amount_minor"] == -1722

    def test_stops_plan_it_fees_parsing_at_total_line(self, adapter):
        page = """xxxx-xxxxxx-12345
New Plan It Instalments and Fees
Jun 19         INSTALMENT PLAN FEE                                              17.22
Total of New Instalment Plans and Fees                                          17.22
Statement of Account
"""
        txns = adapter.parse_transactions(page)
        assert len(txns) == 1


class TestAmexPlanItSummary:
    """The "Plan It Instalments Summary" table: per-plan detail, separate
    from the aggregate "Plan It Instalments Due" figure used for balance
    reconciliation. Fixture mirrors the real Mar-Apr 2026 statement."""

    PAGE = """xxxx-xxxxxx-12345
Plan It Instalments Summary
Instalments due this month are included in your Minimum Payment.
Plan Amount /    Remaining Plan               Instalments Due This Month
Start Date    Details                  Total Fee £         Balance £        Plan £     Fee £      Total Amount £     Instalment
Apr 12 2026   MALAYSIA AIRLINES KUALA KUALA LUMPUR
1,656.39             1,104.26        552.13      17.23            569.36           1 OF 3
51.68
Total                                   1,656.39            1,104.26       552.13      17.23           569.36
Total Fees                                 51.68
"""

    def test_parses_single_plan(self, adapter):
        txns = adapter.parse_transactions(self.PAGE)
        plans = [t for t in txns if t.get("record_type") == "plan_it_instalment"]
        assert len(plans) == 1
        plan = plans[0]
        assert plan["start_date"] == "Apr 12 2026"
        assert plan["description"] == "MALAYSIA AIRLINES KUALA KUALA LUMPUR"
        assert plan["plan_total"] == "1,656.39"
        assert plan["remaining_balance"] == "1,104.26"
        assert plan["due_this_month_plan"] == "552.13"
        assert plan["due_this_month_fee"] == "17.23"
        assert plan["due_this_month_total"] == "569.36"
        assert plan["instalment_progress"] == "1 OF 3"
        assert plan["plan_lifetime_fee"] == "51.68"

    def test_as_of_date_uses_statement_period_closing_date(self, adapter):
        page = "From  20 March to 19 April 2026\n" + self.PAGE
        txns = adapter.parse_transactions(page)
        plan = next(t for t in txns if t.get("record_type") == "plan_it_instalment")
        assert plan["as_of_date"] == "19 Apr 2026"

    def test_no_summary_section_yields_no_plan_records(self, adapter):
        txns = adapter.parse_transactions(SAMPLE_PAGE)
        assert not [t for t in txns if t.get("record_type") == "plan_it_instalment"]

    def test_stops_at_total_line(self, adapter):
        txns = adapter.parse_transactions(self.PAGE)
        plans = [t for t in txns if t.get("record_type") == "plan_it_instalment"]
        assert len(plans) == 1  # the two "Total"/"Total Fees" rows are not plans

    def test_plan_it_summary_records_excluded_from_balance_roll(self, adapter):
        """plan_it_instalment dicts have no `amount_minor` field - parse_transactions
        must not choke iterating them alongside real transactions."""
        page = SAMPLE_PAGE + "\n" + self.PAGE
        txns = adapter.parse_transactions(page)
        assert any(t.get("record_type") == "plan_it_instalment" for t in txns)
        assert any("amount_minor" in t for t in txns if t.get("record_type") is None)


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
        assert previous == 10000
        assert closing == 11000
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
        assert previous == 10000
        assert closing == 12500
        assert plan_it_due == 1500

    def test_balance_rolls_forward_through_transactions(self, adapter):
        content = self._statement_bytes()
        records = adapter.parse(content, "test.pdf", "fakehash")
        payment = next(
            r for r in records if "PAYMENT RECEIVED" in r.raw_data["description"]
        )
        coffee = next(r for r in records if "COFFEE SHOP" in r.raw_data["description"])
        # Previous Closing Balance 100.00; payment (credit, +20.00) reduces
        # what's owed, coffee purchase (debit, -3.85) increases it.
        assert payment.raw_data["balance_minor"] == 8000
        assert coffee.raw_data["balance_minor"] == 8385

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

    def test_plan_it_due_not_double_counted_with_parsed_fee(self, adapter):
        """The "Plan It Instalments Due" lump (£30.00) includes the same
        £10.00 fee that's now also parsed as its own dated transaction from
        "New Plan It Instalments and Fees" - the derived closing balance
        must still land on the printed Closing Balance (£130.00), not
        £140.00 from counting the fee twice."""
        content = _build_pdf_bytes(
            [
                ("American Express", 50),
                ("Preferred Rewards Gold Credit Card", 70),
                ("xxxx-xxxxxx-12345", 100),
                ("Account Summary", 120),
                (
                    "Previous Closing Balance New Credits New Debits "
                    "Plan It Instalments Due Closing Balance",
                    140,
                ),
                ("£100.00 - £0.00 + £0.00 + £30.00 = £130.00", 160),
                ("New Plan It Instalments and Fees", 200),
                (
                    "Apr 19         INSTALMENT PLAN FEE                17.23",
                    220,
                ),
                ("Total of New Instalment Plans and Fees        17.23", 240),
            ]
        )
        records = adapter.parse(content, "test.pdf", "fakehash")
        fee = next(
            r for r in records if r.raw_data["description"] == "INSTALMENT PLAN FEE"
        )
        assert fee.raw_data["amount_minor"] == -1723
        # Previous 100.00 - fee (a debit, +17.23 owed) then + remaining Plan
        # It Due (30.00 - 17.23 = 12.77 principal-only) = 130.00, matching
        # the printed Closing Balance exactly - not 100 + 17.23 + 30 = 147.23.
        assert fee.raw_data["balance_minor"] == 13000
        assert adapter.last_reconciliation.matches is True
        assert adapter.last_reconciliation.derived_closing_minor == 13000


class TestAmexReconciliation:
    """See TestAmexDerivedBalance for the same Account Summary extraction
    mechanics - these tests check the resulting self.last_reconciliation
    attribute (B1), not the derived-balance numbers themselves."""

    def test_sets_last_reconciliation_on_match(self, adapter):
        content = _build_pdf_bytes(
            [
                ("American Express", 50),
                ("Preferred Rewards Gold Credit Card", 70),
                ("xxxx-xxxxxx-12345", 100),
                ("Account Summary", 120),
                (
                    "Previous Closing Balance New Credits New Debits Closing Balance",
                    140,
                ),
                ("£100.00 - £0.00 + £3.85 = £103.85", 160),
                (
                    "Apr 19   Apr 19   COFFEE SHOP LONDON                     3.85",
                    200,
                ),
                ("Amount  £", 460),
            ]
        )
        adapter.parse(content, "test.pdf", "fakehash")
        assert adapter.last_reconciliation is not None
        assert adapter.last_reconciliation.matches is True
        assert adapter.last_reconciliation.expected_closing_minor == 10385
        assert adapter.last_reconciliation.derived_closing_minor == 10385

    def test_sets_last_reconciliation_on_mismatch(self, adapter):
        content = _build_pdf_bytes(
            [
                ("American Express", 50),
                ("Preferred Rewards Gold Credit Card", 70),
                ("xxxx-xxxxxx-12345", 100),
                ("Account Summary", 120),
                (
                    "Previous Closing Balance New Credits New Debits Closing Balance",
                    140,
                ),
                ("£100.00 - £20.00 + £30.00 = £110.00", 160),
                (
                    "May 1   May 1   PAYMENT RECEIVED - THANK YOU            20.00",
                    200,
                ),
                ("CR", 220),
                (
                    "Apr 19   Apr 19   COFFEE SHOP LONDON                     3.85",
                    260,
                ),
                ("Amount  £", 460),
            ]
        )
        adapter.parse(content, "test.pdf", "fakehash")
        assert adapter.last_reconciliation is not None
        assert adapter.last_reconciliation.matches is False

    def test_no_account_summary_leaves_last_reconciliation_none(self, adapter):
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
        adapter.parse(content, "test.pdf", "fakehash")
        assert adapter.last_reconciliation is None

    def test_reconciliation_and_period_reset_between_parses(self, adapter):
        """Adapter instances are reused across files by AdapterFactory - a
        mismatching/period-bearing file must not leak its results into a
        later file that has neither."""
        mismatching_content = _build_pdf_bytes(
            [
                ("American Express", 50),
                ("Preferred Rewards Gold Credit Card", 70),
                ("xxxx-xxxxxx-12345", 100),
                ("Account Summary", 120),
                (
                    "Previous Closing Balance New Credits New Debits Closing Balance",
                    140,
                ),
                ("£100.00 - £20.00 + £30.00 = £110.00", 160),
                (
                    "May 1   May 1   PAYMENT RECEIVED - THANK YOU            20.00",
                    200,
                ),
                ("CR", 220),
                ("From  20 April to 19 May 2026", 240),
                (
                    "Apr 19   Apr 19   COFFEE SHOP LONDON                     3.85",
                    260,
                ),
                ("Amount  £", 460),
            ]
        )
        adapter.parse(mismatching_content, "test.pdf", "fakehash")
        assert adapter.last_reconciliation is not None
        assert adapter.last_statement_period is not None

        bare_content = _build_pdf_bytes(
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
        adapter.parse(bare_content, "test2.pdf", "fakehash")
        assert adapter.last_reconciliation is None
        assert adapter.last_statement_period is None


class TestAmexStatementPeriod:
    def test_extracts_period_with_inferred_from_year(self, adapter):
        period = adapter._extract_statement_period(SAMPLE_PAGE_WITH_PERIOD)
        assert period == (datetime(2026, 4, 20), datetime(2026, 5, 19))

    def test_sets_last_statement_period(self, adapter):
        adapter.parse_transactions(SAMPLE_PAGE_WITH_PERIOD)
        assert adapter.last_statement_period is not None
        assert adapter.last_statement_period.from_date == datetime(2026, 4, 20)
        assert adapter.last_statement_period.to_date == datetime(2026, 5, 19)

    def test_no_period_leaves_last_statement_period_none(self, adapter):
        adapter.parse_transactions(SAMPLE_PAGE)
        assert adapter.last_statement_period is None

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
        txn = {"date": "19 Apr", "description": "Test", "amount_minor": -100}
        key_with = adapter.generate_source_key(txn, 1, "xxxx-xxxxxx-12345")
        key_without = adapter.generate_source_key(txn, 1, None)
        assert key_with != key_without

    def test_detect_source_type(self, adapter):
        assert adapter.detect_source_type() == "amex"

    def test_plan_it_instalment_key_distinguishes_by_as_of_date(self, adapter):
        """The same plan re-appears every month it's active, with the same
        start_date/description - only as_of_date (the statement's own
        closing date) tells two months' rows apart, mirroring Vanguard
        holdings' account + fund_name + as_of_date key."""
        base = {
            "record_type": "plan_it_instalment",
            "start_date": "Apr 12 2026",
            "description": "MALAYSIA AIRLINES KUALA KUALA LUMPUR",
        }
        month_one = adapter.generate_source_key(
            {**base, "as_of_date": "19 Apr 2026"}, 1, "xxxx-xxxxxx-12345"
        )
        month_two = adapter.generate_source_key(
            {**base, "as_of_date": "19 May 2026"}, 1, "xxxx-xxxxxx-12345"
        )
        assert month_one != month_two

    def test_plan_it_instalment_key_stable_on_reingest(self, adapter):
        txn = {
            "record_type": "plan_it_instalment",
            "start_date": "Apr 12 2026",
            "description": "MALAYSIA AIRLINES KUALA KUALA LUMPUR",
            "as_of_date": "19 Apr 2026",
        }
        first = adapter.generate_source_key(txn, 1, "xxxx-xxxxxx-12345")
        second = adapter.generate_source_key(dict(txn), 7, "xxxx-xxxxxx-12345")
        assert first == second
