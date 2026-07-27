"""Tests for transformers/silver_reconciliation.py (item 3: re-verifying
Bronze's B1 self-check against surviving Silver transactions post-matching)."""

import pandas as pd

from transformers.silver_reconciliation import (
    amex_plan_it_adjustment_by_ingestion,
    find_silver_reconciliation_breaks,
)


def _anchor_row(
    account_id="acc_amex",
    source_type="amex",
    ingestion_id="ing1",
    filename="jan.pdf",
    expected_opening_minor=10000,
    expected_closing_minor=12000,
):
    return {
        "account_id": account_id,
        "source_type": source_type,
        "ingestion_id": ingestion_id,
        "filename": filename,
        "expected_opening_minor": expected_opening_minor,
        "expected_closing_minor": expected_closing_minor,
    }


def _source_row(silver_transaction_id, bronze_record_id, ingestion_id, source_type="amex"):
    return {
        "silver_transaction_id": silver_transaction_id,
        "bronze_record_id": bronze_record_id,
        "ingestion_id": ingestion_id,
        "source_type": source_type,
        "match_policy": "fingerprint",
    }


class TestFindSilverReconciliationBreaks:
    def test_nothing_dropped_matches(self):
        """Two transactions survive matching untouched - amex rolls
        forward with subtraction (liability): 10000 - (500 + 1500) = 8000
        wouldn't reconcile, so pick amounts that land on 12000: opening
        10000 minus amounts summing to -2000 (i.e. two credits)."""
        anchors = pd.DataFrame([_anchor_row()])
        sources = pd.DataFrame(
            [
                _source_row("fp1", "br1", "ing1"),
                _source_row("fp2", "br2", "ing1"),
            ]
        )
        transactions = pd.DataFrame(
            [
                {"bronze_record_id": "br1", "silver_transaction_id": "fp1", "amount_minor": -1000},
                {"bronze_record_id": "br2", "silver_transaction_id": "fp2", "amount_minor": -1000},
            ]
        )

        result = find_silver_reconciliation_breaks(anchors, sources, transactions)

        assert len(result) == 1
        row = result.iloc[0]
        # opening 10000 - (-1000 + -1000) = 10000 + 2000 = 12000
        assert row["silver_derived_closing_minor"] == 12000
        assert bool(row["matches"]) is True

    def test_dedup_bug_dropping_a_genuine_transaction_causes_mismatch(self):
        """Simulates match_transactions() incorrectly absorbing a genuine,
        non-duplicate transaction: only one of the two Bronze rows survives
        into transaction_sources/transactions, so the re-derived total no
        longer reconciles - exactly the bug class this check exists for."""
        anchors = pd.DataFrame([_anchor_row()])
        # Only fp1 survives - fp2's bronze row (br2) is missing entirely,
        # as if matching.py wrongly dropped it.
        sources = pd.DataFrame([_source_row("fp1", "br1", "ing1")])
        transactions = pd.DataFrame(
            [{"bronze_record_id": "br1", "silver_transaction_id": "fp1", "amount_minor": -1000}]
        )

        result = find_silver_reconciliation_breaks(anchors, sources, transactions)

        assert len(result) == 1
        row = result.iloc[0]
        # opening 10000 - (-1000) = 11000, not the expected 12000
        assert row["silver_derived_closing_minor"] == 11000
        assert bool(row["matches"]) is False

    def test_absorbed_cross_source_duplicate_not_double_counted(self):
        """A non-preferred source's bronze row (br_b) gets absorbed by the
        preferred source's own row (br_a) during cross-source dedup - both
        share a provenance entry pointing at the same silver_transaction_id,
        but only br_a's own row actually survives into `transactions`. The
        join (on bronze_record_id, not silver_transaction_id) must count
        that amount once, via br_a alone - br_b's source row simply finds
        no match and contributes nothing, which is correct: the absorbed
        side is never an anchored source_type in the one declared
        cross-source policy today."""
        anchors = pd.DataFrame(
            [_anchor_row(source_type="chase", ingestion_id="ing_chase")]
        )
        sources = pd.DataFrame(
            [
                _source_row("fp1", "br_a", "ing_chase", source_type="chase"),
                _source_row("fp1", "br_b", "ing_chase", source_type="chase"),
            ]
        )
        transactions = pd.DataFrame(
            [{"bronze_record_id": "br_a", "silver_transaction_id": "fp1", "amount_minor": 2000}]
        )

        result = find_silver_reconciliation_breaks(anchors, sources, transactions)

        assert len(result) == 1
        row = result.iloc[0]
        # Chase is asset/cash: opening 10000 + 2000 = 12000 (matches,
        # not 14000 which would mean the 2000 got counted twice)
        assert row["silver_derived_closing_minor"] == 12000
        assert bool(row["matches"]) is True

    def test_genuine_same_fingerprint_repeat_both_counted(self):
        """Two genuinely distinct transactions (e.g. two real same-day,
        same-amount, same-description charges) can share one
        silver_transaction_id, since matching.py's fingerprint doesn't
        fold in the occurrence number - but each keeps its OWN
        bronze_record_id in `transactions`. Both must still be counted,
        proving the join is robust to that ID collision (regression test
        for a real bug this surfaced against production Amex data: two
        £8.90 same-day charges were undercounted by one when joined on
        the non-unique silver_transaction_id instead)."""
        anchors = pd.DataFrame(
            [_anchor_row(source_type="chase", ingestion_id="ing_chase")]
        )
        sources = pd.DataFrame(
            [
                _source_row("fp1", "br_a", "ing_chase", source_type="chase"),
                _source_row("fp1", "br_b", "ing_chase", source_type="chase"),
            ]
        )
        transactions = pd.DataFrame(
            [
                {"bronze_record_id": "br_a", "silver_transaction_id": "fp1", "amount_minor": 890},
                {"bronze_record_id": "br_b", "silver_transaction_id": "fp1", "amount_minor": 890},
            ]
        )

        result = find_silver_reconciliation_breaks(anchors, sources, transactions)

        assert len(result) == 1
        row = result.iloc[0]
        # opening 10000 + 890 + 890 = 11780, not 10890 (which would mean
        # one of the two genuine repeats got silently dropped)
        assert row["silver_derived_closing_minor"] == 11780

    def test_anchor_with_no_opening_minor_excluded_not_false_mismatch(self):
        """Monzo PDF-style anchors (no opening figure ever printed) must be
        skipped entirely, never reported as a break."""
        anchors = pd.DataFrame(
            [
                _anchor_row(
                    account_id="acc_monzo",
                    source_type="monzo-pdf",
                    ingestion_id="ing_monzo",
                    expected_opening_minor=None,
                )
            ]
        )
        sources = pd.DataFrame([_source_row("fp1", "br1", "ing_monzo", source_type="monzo-pdf")])
        transactions = pd.DataFrame(
            [{"silver_transaction_id": "fp1", "amount_minor": -500}]
        )

        result = find_silver_reconciliation_breaks(anchors, sources, transactions)
        assert result.empty

    def test_source_type_with_no_rollforward_sign_excluded(self):
        """A source_type with an opening anchor captured (e.g. Kroo/Monzo
        Flex, per adapters/reconciliation.py) but no declared rollforward
        sign here (they were never rolled forward to begin with - direct-
        read balances) must be skipped, not silently treated as sign=1."""
        anchors = pd.DataFrame(
            [_anchor_row(account_id="acc_kroo", source_type="kroo", ingestion_id="ing_kroo")]
        )
        sources = pd.DataFrame([_source_row("fp1", "br1", "ing_kroo", source_type="kroo")])
        transactions = pd.DataFrame(
            [{"bronze_record_id": "br1", "silver_transaction_id": "fp1", "amount_minor": -500}]
        )

        result = find_silver_reconciliation_breaks(anchors, sources, transactions)
        assert result.empty

    def test_empty_inputs_return_empty(self):
        empty = pd.DataFrame()
        result = find_silver_reconciliation_breaks(empty, empty, empty)
        assert result.empty

    def test_amex_plan_it_adjustment_prevents_false_mismatch(self):
        """A real Amex statement with an active Plan-It plan reconciles at
        Bronze level via an extra "Plan It Instalments Due" adjustment
        beyond the plain transaction sum (adapters/amex_pdf_adapter.py::
        parse()). Without plan_it_adjustments, this would show as a false
        mismatch even though nothing was actually dropped/duplicated."""
        anchors = pd.DataFrame(
            [_anchor_row(expected_opening_minor=10000, expected_closing_minor=15521)]
        )
        sources = pd.DataFrame([_source_row("fp1", "br1", "ing1")])
        transactions = pd.DataFrame(
            [{"bronze_record_id": "br1", "silver_transaction_id": "fp1", "amount_minor": -0}]
        )
        # opening 10000 - 0 = 10000, but the real closing is 15521 (10000 +
        # a 5521 Plan-It adjustment) - without the adjustment this mismatches.
        result_without = find_silver_reconciliation_breaks(anchors, sources, transactions)
        assert bool(result_without.iloc[0]["matches"]) is False

        result_with = find_silver_reconciliation_breaks(
            anchors, sources, transactions, plan_it_adjustments={"ing1": 5521}
        )
        assert result_with.iloc[0]["silver_derived_closing_minor"] == 15521
        assert bool(result_with.iloc[0]["matches"]) is True


class TestAmexPlanItAdjustmentByIngestion:
    def test_sums_due_this_month_plan_for_active_plans(self):
        bronze = pd.DataFrame(
            [
                {
                    "ingestion_id": "ing1",
                    "record_type": "plan_it_instalment",
                    "raw_data": {"due_this_month_plan": "552.13"},
                },
                {
                    "ingestion_id": "ing1",
                    "record_type": "transaction",
                    "raw_data": {"amount_minor": -100},
                },
            ]
        )
        result = amex_plan_it_adjustment_by_ingestion(bronze)
        assert result == {"ing1": 55213}

    def test_sums_multiple_concurrent_plans_for_same_ingestion(self):
        bronze = pd.DataFrame(
            [
                {
                    "ingestion_id": "ing1",
                    "record_type": "plan_it_instalment",
                    "raw_data": {"due_this_month_plan": "100.00"},
                },
                {
                    "ingestion_id": "ing1",
                    "record_type": "plan_it_instalment",
                    "raw_data": {"due_this_month_plan": "50.00"},
                },
            ]
        )
        result = amex_plan_it_adjustment_by_ingestion(bronze)
        assert result == {"ing1": 15000}

    def test_no_plan_it_rows_returns_empty(self):
        bronze = pd.DataFrame(
            [
                {
                    "ingestion_id": "ing1",
                    "record_type": "transaction",
                    "raw_data": {"amount_minor": -100},
                }
            ]
        )
        assert amex_plan_it_adjustment_by_ingestion(bronze) == {}

    def test_none_or_empty_bronze_returns_empty(self):
        assert amex_plan_it_adjustment_by_ingestion(None) == {}
        assert amex_plan_it_adjustment_by_ingestion(pd.DataFrame()) == {}
