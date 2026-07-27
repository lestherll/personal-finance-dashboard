"""Tests for transformers/silver_reconciliation.py (item 3: re-verifying
Bronze's B1 self-check against surviving Silver transactions post-matching)."""

import pandas as pd

from transformers.silver_reconciliation import find_silver_reconciliation_breaks


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
                {"silver_transaction_id": "fp1", "amount_minor": -1000},
                {"silver_transaction_id": "fp2", "amount_minor": -1000},
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
            [{"silver_transaction_id": "fp1", "amount_minor": -1000}]
        )

        result = find_silver_reconciliation_breaks(anchors, sources, transactions)

        assert len(result) == 1
        row = result.iloc[0]
        # opening 10000 - (-1000) = 11000, not the expected 12000
        assert row["silver_derived_closing_minor"] == 11000
        assert bool(row["matches"]) is False

    def test_absorbed_cross_source_duplicate_not_double_counted(self):
        """Two bronze_record_ids (one from each side of a cross-source
        pair) both map to the same silver_transaction_id after the
        preferred source absorbs the duplicate - summing must count that
        amount once per ingestion, not twice."""
        anchors = pd.DataFrame(
            [_anchor_row(source_type="chase", ingestion_id="ing_chase")]
        )
        sources = pd.DataFrame(
            [
                _source_row("fp1", "br_a", "ing_chase", source_type="chase"),
                # A second bronze row for the SAME ingestion mapping to the
                # same silver_transaction_id (e.g. a genuine occurrence
                # collision) must not double the contribution.
                _source_row("fp1", "br_b", "ing_chase", source_type="chase"),
            ]
        )
        transactions = pd.DataFrame(
            [{"silver_transaction_id": "fp1", "amount_minor": 2000}]
        )

        result = find_silver_reconciliation_breaks(anchors, sources, transactions)

        assert len(result) == 1
        row = result.iloc[0]
        # Chase is asset/cash: opening 10000 + 2000 = 12000 (matches,
        # not 14000 which would mean the 2000 got counted twice)
        assert row["silver_derived_closing_minor"] == 12000
        assert bool(row["matches"]) is True

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
            [{"silver_transaction_id": "fp1", "amount_minor": -500}]
        )

        result = find_silver_reconciliation_breaks(anchors, sources, transactions)
        assert result.empty

    def test_empty_inputs_return_empty(self):
        empty = pd.DataFrame()
        result = find_silver_reconciliation_breaks(empty, empty, empty)
        assert result.empty
