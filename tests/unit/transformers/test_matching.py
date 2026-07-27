"""Regression tests for transformers/matching.py."""

import pandas as pd

from transformers.matching import match_transactions


def _txn(
    account_id="acc_test",
    transaction_date="2026-01-15",
    amount_minor=-1000,
    description="COFFEE SHOP",
    source_type="kroo",
    bronze_record_id=None,
    ingestion_id=None,
    upload_timestamp=None,
    statement_period_to=None,
    line_number=1,
    bank_transaction_id=None,
    currency="GBP",
):
    """Build a single normalized-transaction-shaped row."""
    return {
        "account_id": account_id,
        "transaction_date": pd.Timestamp(transaction_date),
        "amount_minor": amount_minor,
        "description": description,
        "source_type": source_type,
        "bronze_record_id": bronze_record_id or f"br_{line_number}",
        "bronze_source_key": f"key_{line_number}",
        "ingestion_id": ingestion_id or "abc123",
        "upload_timestamp": upload_timestamp or pd.Timestamp("2026-01-20"),
        "statement_period_to": statement_period_to,
        "line_number": line_number,
        "bank_transaction_id": bank_transaction_id,
        "currency": currency,
        "category": None,
        "ingested_at": pd.Timestamp("2026-01-20"),
    }


def _make_frame(rows):
    return pd.DataFrame(rows)


class TestSameFingerprintUniqueSilverId:
    """Bug 1 regression: genuine same-content repeats within one source must
    not share a silver_transaction_id, or joins on that column become
    one-to-many instead of one-to-one."""

    def test_two_same_fingerprint_repeats_get_distinct_ids(self):
        rows = [
            _txn(
                bronze_record_id="br_repeat_1",
                ingestion_id="ingest_1",
                line_number=1,
            ),
            _txn(
                bronze_record_id="br_repeat_2",
                ingestion_id="ingest_2",
                line_number=2,
            ),
        ]
        canonical, sources = match_transactions(_make_frame(rows))

        assert len(canonical) == 2
        ids = set(canonical["silver_transaction_id"])
        assert len(ids) == 2

        # Both ids follow fingerprint_occurrence shape.
        for stid in ids:
            assert stid.endswith("_1") or stid.endswith("_2")

    def test_provenance_for_repeats_points_at_distinct_ids(self):
        rows = [
            _txn(
                bronze_record_id="br_repeat_1",
                ingestion_id="ingest_1",
                line_number=1,
            ),
            _txn(
                bronze_record_id="br_repeat_2",
                ingestion_id="ingest_2",
                line_number=2,
            ),
        ]
        canonical, sources = match_transactions(_make_frame(rows))

        # Each bronze row should map to exactly one canonical id, and the ids
        # should be distinct.
        by_bronze = sources.set_index("bronze_record_id")[
            "silver_transaction_id"
        ].to_dict()
        assert by_bronze["br_repeat_1"] != by_bronze["br_repeat_2"]
        assert set(by_bronze.values()) == set(canonical["silver_transaction_id"])


class TestCrossSourceProvenance:
    """Bug 2 regression: cross-source absorbed rows must record the full
    ingestion_id and point at the surviving canonical row."""

    def test_absorbed_row_records_full_ingestion_id_and_survivor(self):
        statement_id = "s" * 64  # real SHA-256-shaped ingestion id
        transactions_id = "t" * 64

        statement_row = _txn(
            source_type="natwest-statement",
            description="CARD PURCHASE COFFEE SHOP",
            bronze_record_id="br_statement",
            ingestion_id=statement_id,
            line_number=1,
        )
        transactions_row = _txn(
            source_type="natwest-transactions",
            description="COFFEE SHOP",
            bronze_record_id="br_transactions",
            ingestion_id=transactions_id,
            line_number=1,
        )

        canonical, sources = match_transactions(
            _make_frame([statement_row, transactions_row])
        )

        # Only the statement row survives.
        assert len(canonical) == 1
        survivor = canonical.iloc[0]
        assert survivor["source_type"] == "natwest-statement"

        # The cross-source provenance for the absorbed row points at the
        # survivor with the full ingestion_id (regression: was a single char
        # due to zip() over the ingestion_id string).
        cross_source = sources[
            (sources["bronze_record_id"] == "br_transactions")
            & (sources["match_policy"].str.startswith("cross-source"))
        ]
        assert len(cross_source) == 1
        cross_source = cross_source.iloc[0]
        assert (
            cross_source["silver_transaction_id"] == survivor["silver_transaction_id"]
        )
        assert cross_source["ingestion_id"] == transactions_id
        assert (
            cross_source["match_policy"] == "cross-source(natwest-statement-preferred)"
        )

        # Every silver_transaction_id in provenance must reference a real
        # canonical row (the survivor); the absorbed row's own fingerprint
        # provenance may still exist but now points at a dangling id - this
        # is pre-existing behaviour outside the two bugs being fixed here.
        canonical_ids = set(canonical["silver_transaction_id"])
        for stid in sources["silver_transaction_id"]:
            assert stid in canonical_ids, f"dangling provenance id: {stid}"

    def test_cross_source_multiset_pairs_each_absorbed_row_to_distinct_survivor(self):
        """If both sides have two matching loose-key rows, each non-preferred
        row is paired with a distinct preferred survivor."""
        rows = [
            _txn(
                source_type="natwest-statement",
                description="CARD PURCHASE SHOP A",
                bronze_record_id="br_stmt_a",
                ingestion_id="ingest_stmt",
                line_number=1,
            ),
            _txn(
                source_type="natwest-statement",
                description="CARD PURCHASE SHOP B",
                bronze_record_id="br_stmt_b",
                ingestion_id="ingest_stmt",
                line_number=2,
            ),
            _txn(
                source_type="natwest-transactions",
                description="SHOP A",
                bronze_record_id="br_txn_a",
                ingestion_id="ingest_txn",
                line_number=1,
            ),
            _txn(
                source_type="natwest-transactions",
                description="SHOP B",
                bronze_record_id="br_txn_b",
                ingestion_id="ingest_txn",
                line_number=2,
            ),
        ]
        canonical, sources = match_transactions(_make_frame(rows))

        # Two statement survivors; both transactions rows absorbed.
        assert len(canonical) == 2
        assert set(canonical["source_type"]) == {"natwest-statement"}

        absorbed = sources[sources["match_policy"].str.startswith("cross-source")]
        assert len(absorbed) == 2
        assert set(absorbed["silver_transaction_id"]) == set(
            canonical["silver_transaction_id"]
        )
        assert sorted(absorbed["bronze_record_id"].tolist()) == [
            "br_txn_a",
            "br_txn_b",
        ]


class TestMatchTransactionsEmpty:
    def test_empty_input_returns_schema(self):
        canonical, sources = match_transactions(pd.DataFrame())
        assert canonical.empty
        assert sources.empty
        assert list(sources.columns) == [
            "silver_transaction_id",
            "bronze_record_id",
            "ingestion_id",
            "source_type",
            "match_policy",
        ]
