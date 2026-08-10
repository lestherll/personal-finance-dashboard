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
    statement_period_from=None,
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
        "statement_period_from": statement_period_from,
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

        # Both ids follow the source_type:fingerprint_occurrence shape.
        for stid in ids:
            assert stid.startswith("kroo:")
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
        # canonical row (the survivor): the absorbed row's own fingerprint
        # provenance was redirected to the survivor id, so nothing is left
        # pointing at the dropped row.
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


class TestDedupeCrossSourceRedirectsAllSharers:
    """P2.3 regression: the O(n^2) provenance rescan was replaced with an
    index keyed by silver_transaction_id. If an absorbed row's id already
    had multiple provenance rows pointing at it, the indexed redirect must
    move all of them to the new survivor - not just the first one a linear
    scan happened to find first."""

    def test_all_provenance_rows_sharing_absorbed_id_get_redirected(self):
        from transformers.matching import _dedupe_cross_source

        preferred_row = _txn(
            source_type="natwest-statement",
            description="CARD PURCHASE SHOP A",
            bronze_record_id="br_stmt_a",
        )
        preferred_row["_fingerprint"] = "fp_stmt"
        preferred_row["_occurrence"] = 1

        absorbed_row = _txn(
            source_type="natwest-transactions",
            description="SHOP A",
            bronze_record_id="br_txn_a",
        )
        absorbed_row["_fingerprint"] = "fp_txn"
        absorbed_row["_occurrence"] = 1

        canonical = _make_frame([preferred_row, absorbed_row])
        absorbed_id = "natwest-transactions:fp_txn_1"
        survivor_id = "natwest-statement:fp_stmt_1"

        provenance_rows = [
            {
                "silver_transaction_id": absorbed_id,
                "bronze_record_id": f"br_dup_{i}",
                "ingestion_id": f"ingest_dup_{i}",
                "source_type": "natwest-transactions",
                "match_policy": "fingerprint",
            }
            for i in range(20)
        ]

        result = _dedupe_cross_source(
            canonical,
            {"natwest-transactions", "natwest-statement"},
            ["account_id", "transaction_date", "amount_minor"],
            "natwest-statement",
            provenance_rows,
        )

        assert len(result) == 1
        assert result.iloc[0]["source_type"] == "natwest-statement"

        redirected = [
            p for p in provenance_rows if p["bronze_record_id"].startswith("br_dup_")
        ]
        assert len(redirected) == 20
        assert all(p["silver_transaction_id"] == survivor_id for p in redirected)


class TestSilverIdIsScopedBySourceType:
    """Regression: the content fingerprint deliberately excludes source_type
    (it hashes content only), and occurrence numbers count per (source_type,
    fingerprint) group - so two source_types producing identical normalized
    content for one account used to mint the *same* silver_transaction_id
    for two distinct canonical rows (duplicate primary key, fan-out joins).
    The Monzo CSV x Monzo PDF overlap is the real case: same account_id,
    no declared cross-source policy."""

    def test_identical_content_across_source_types_gets_distinct_ids(self):
        csv_row = _txn(
            source_type="monzo",
            account_id="acc_monzo_current",
            description="TESCO STORES",
            bronze_record_id="br_csv",
        )
        pdf_row = _txn(
            source_type="monzo-pdf",
            account_id="acc_monzo_current",
            description="TESCO STORES",
            bronze_record_id="br_pdf",
        )

        canonical, sources = match_transactions(_make_frame([csv_row, pdf_row]))

        # Both survive (no cross-source policy pairs these source_types)...
        assert len(canonical) == 2
        # ...with genuinely distinct ids, and provenance agrees with the
        # canonical frame.
        ids = set(canonical["silver_transaction_id"])
        assert len(ids) == 2
        assert any(i.startswith("monzo:") for i in ids)
        assert any(i.startswith("monzo-pdf:") for i in ids)
        assert set(sources["silver_transaction_id"]) == ids


class TestOverlapWindowDedup:
    """A partial-month statement (e.g. day 1-15) later re-covered by a
    full-month statement for the same account/source_type re-reports the
    same real transactions. Without this tier, step 2's occurrence
    numbering treats the second file's copies as distinct repeats (see
    TestSameFingerprintUniqueSilverId) and every aggregate double-counts
    the overlapping days."""

    def test_matching_transaction_in_overlap_window_collapses(self):
        partial = _txn(
            description="COFFEE SHOP",
            transaction_date="2026-01-10",
            bronze_record_id="br_partial_coffee",
            ingestion_id="ingest_partial",
            statement_period_from=pd.Timestamp("2026-01-01"),
            statement_period_to=pd.Timestamp("2026-01-15"),
            line_number=1,
        )
        full_coffee = _txn(
            description="COFFEE SHOP",
            transaction_date="2026-01-10",
            bronze_record_id="br_full_coffee",
            ingestion_id="ingest_full",
            statement_period_from=pd.Timestamp("2026-01-01"),
            statement_period_to=pd.Timestamp("2026-01-31"),
            line_number=1,
        )
        # Present only in the full statement, dated after the partial
        # statement's coverage ended - must survive untouched.
        full_only_grocery = _txn(
            description="GROCERY STORE",
            amount_minor=-2000,
            transaction_date="2026-01-20",
            bronze_record_id="br_full_grocery",
            ingestion_id="ingest_full",
            statement_period_from=pd.Timestamp("2026-01-01"),
            statement_period_to=pd.Timestamp("2026-01-31"),
            line_number=2,
        )

        canonical, sources = match_transactions(
            _make_frame([partial, full_coffee, full_only_grocery])
        )

        assert len(canonical) == 2
        assert sorted(canonical["description"].tolist()) == [
            "COFFEE SHOP",
            "GROCERY STORE",
        ]

        overlap_prov = sources[sources["match_policy"].str.startswith("overlap-window")]
        assert len(overlap_prov) == 1
        assert overlap_prov.iloc[0]["bronze_record_id"] in {
            "br_partial_coffee",
            "br_full_coffee",
        }
        # Both bronze rows are still traceable via provenance, even though
        # only one canonical row remains.
        assert set(sources["bronze_record_id"]) >= {
            "br_partial_coffee",
            "br_full_coffee",
        }

    def test_asymmetric_occurrence_counts_keep_the_excess(self):
        """The full statement reports the coffee shop twice in one day, the
        partial statement only once (e.g. its cutoff missed the second
        purchase's line, or it's a genuine second purchase). Multiset
        pairing must collapse only one pair and leave the excess distinct -
        max(1, 2) = 2 canonical rows, never 3 (sum) or 1 (over-collapse)."""
        partial = _txn(
            description="COFFEE SHOP",
            transaction_date="2026-01-10",
            bronze_record_id="br_partial_1",
            ingestion_id="ingest_partial",
            statement_period_from=pd.Timestamp("2026-01-01"),
            statement_period_to=pd.Timestamp("2026-01-15"),
            line_number=1,
        )
        full_1 = _txn(
            description="COFFEE SHOP",
            transaction_date="2026-01-10",
            bronze_record_id="br_full_1",
            ingestion_id="ingest_full",
            statement_period_from=pd.Timestamp("2026-01-01"),
            statement_period_to=pd.Timestamp("2026-01-31"),
            line_number=1,
        )
        full_2 = _txn(
            description="COFFEE SHOP",
            transaction_date="2026-01-10",
            bronze_record_id="br_full_2",
            ingestion_id="ingest_full",
            statement_period_from=pd.Timestamp("2026-01-01"),
            statement_period_to=pd.Timestamp("2026-01-31"),
            line_number=2,
        )

        canonical, sources = match_transactions(_make_frame([partial, full_1, full_2]))

        assert len(canonical) == 2
        # Every bronze row is still accounted for in provenance.
        assert set(sources["bronze_record_id"]) == {
            "br_partial_1",
            "br_full_1",
            "br_full_2",
        }

    def test_non_overlapping_periods_do_not_collapse(self):
        """Two ingestions whose declared periods don't overlap at all are
        never compared, even if a stray row coincidentally shares a full
        fingerprint - the period gate short-circuits before any date/content
        comparison."""
        jan = _txn(
            description="COFFEE SHOP",
            transaction_date="2026-01-10",
            bronze_record_id="br_jan",
            ingestion_id="ingest_jan",
            statement_period_from=pd.Timestamp("2026-01-01"),
            statement_period_to=pd.Timestamp("2026-01-15"),
            line_number=1,
        )
        feb = _txn(
            description="COFFEE SHOP",
            transaction_date="2026-01-10",
            bronze_record_id="br_feb",
            ingestion_id="ingest_feb",
            statement_period_from=pd.Timestamp("2026-02-01"),
            statement_period_to=pd.Timestamp("2026-02-28"),
            line_number=1,
        )

        canonical, sources = match_transactions(_make_frame([jan, feb]))

        assert len(canonical) == 2
        assert not sources["match_policy"].str.startswith("overlap-window").any()

    def test_matching_row_outside_the_overlap_window_is_not_collapsed(self):
        """A fingerprint shared by both ingestions but dated outside the
        overlap *intersection* (even though it's inside one file's own
        declared period) must not be collapsed - the window is the
        intersection of the two periods, not either one alone."""
        partial = _txn(
            description="COFFEE SHOP",
            transaction_date="2026-01-20",
            bronze_record_id="br_partial_late",
            ingestion_id="ingest_partial",
            statement_period_from=pd.Timestamp("2026-01-01"),
            statement_period_to=pd.Timestamp("2026-01-15"),
            line_number=1,
        )
        full = _txn(
            description="COFFEE SHOP",
            transaction_date="2026-01-20",
            bronze_record_id="br_full_late",
            ingestion_id="ingest_full",
            statement_period_from=pd.Timestamp("2026-01-01"),
            statement_period_to=pd.Timestamp("2026-01-31"),
            line_number=1,
        )

        canonical, sources = match_transactions(_make_frame([partial, full]))

        assert len(canonical) == 2
        assert not sources["match_policy"].str.startswith("overlap-window").any()


class TestOverlapWindowDateDrift:
    """Real-world bug: two natwest-transactions "on-demand" downloads whose
    declared statement periods overlap can report the *same* real transaction
    under two different dates (a pending-vs-cleared date shift in the online
    export between download sessions) - e.g. one download shows a KROO
    transfer on 2026-07-25, a later download of an overlapping window shows
    the same transfer on 2026-07-27. Because the exact-date fingerprint used
    by the plain overlap-window tier treats these as two distinct
    transactions, both survive and get double-counted downstream (e.g. in
    the derived account_ledger rollforward for a source with no balance
    anchor of its own - see NATWEST_TRANSACTIONS_BALANCE_DESIGN.md)."""

    def test_date_shifted_duplicate_within_tolerance_collapses(self):
        partial = _txn(
            account_id="acc_natwest_current",
            description="KROO ACCOUNT Mobile/Online Transaction",
            amount_minor=-274575,
            transaction_date="2026-07-25",
            source_type="natwest-transactions",
            bronze_record_id="br_partial_kroo",
            ingestion_id="ingest_partial",
            statement_period_from=pd.Timestamp("2026-04-26"),
            statement_period_to=pd.Timestamp("2026-07-26"),
            line_number=1,
        )
        full = _txn(
            account_id="acc_natwest_current",
            description="KROO ACCOUNT Mobile/Online Transaction",
            amount_minor=-274575,
            transaction_date="2026-07-27",
            source_type="natwest-transactions",
            bronze_record_id="br_full_kroo",
            ingestion_id="ingest_full",
            statement_period_from=pd.Timestamp("2026-05-04"),
            statement_period_to=pd.Timestamp("2026-08-04"),
            line_number=1,
        )

        canonical, sources = match_transactions(_make_frame([partial, full]))

        assert len(canonical) == 1
        drift_prov = sources[
            sources["match_policy"].str.startswith("overlap-window-date-shifted")
        ]
        assert len(drift_prov) == 1
        assert drift_prov.iloc[0]["bronze_record_id"] in {
            "br_partial_kroo",
            "br_full_kroo",
        }
        assert set(sources["bronze_record_id"]) == {"br_partial_kroo", "br_full_kroo"}

    def test_date_gap_beyond_tolerance_does_not_collapse(self):
        """A gap wider than the tolerance is left as two distinct
        transactions - erring on the side of not merging rather than
        guessing across too wide a gap."""
        partial = _txn(
            account_id="acc_natwest_current",
            description="KROO ACCOUNT Mobile/Online Transaction",
            amount_minor=-274575,
            transaction_date="2026-07-20",
            source_type="natwest-transactions",
            bronze_record_id="br_partial_far",
            ingestion_id="ingest_partial",
            statement_period_from=pd.Timestamp("2026-04-26"),
            statement_period_to=pd.Timestamp("2026-07-26"),
            line_number=1,
        )
        full = _txn(
            account_id="acc_natwest_current",
            description="KROO ACCOUNT Mobile/Online Transaction",
            amount_minor=-274575,
            transaction_date="2026-07-27",
            source_type="natwest-transactions",
            bronze_record_id="br_full_far",
            ingestion_id="ingest_full",
            statement_period_from=pd.Timestamp("2026-05-04"),
            statement_period_to=pd.Timestamp("2026-08-04"),
            line_number=1,
        )

        canonical, sources = match_transactions(_make_frame([partial, full]))

        assert len(canonical) == 2
        assert not sources["match_policy"].str.startswith("overlap-window").any()

    def test_ambiguous_multiple_candidates_are_not_collapsed(self):
        """Two same-amount/description rows on each side within the window
        is ambiguous (which pairs with which?) - left alone rather than
        guessed at, even though each side's count matches."""
        partial_1 = _txn(
            account_id="acc_natwest_current",
            description="KROO ACCOUNT Mobile/Online Transaction",
            amount_minor=-274575,
            transaction_date="2026-07-24",
            source_type="natwest-transactions",
            bronze_record_id="br_partial_1",
            ingestion_id="ingest_partial",
            statement_period_from=pd.Timestamp("2026-04-26"),
            statement_period_to=pd.Timestamp("2026-07-26"),
            line_number=1,
        )
        partial_2 = _txn(
            account_id="acc_natwest_current",
            description="KROO ACCOUNT Mobile/Online Transaction",
            amount_minor=-274575,
            transaction_date="2026-07-25",
            source_type="natwest-transactions",
            bronze_record_id="br_partial_2",
            ingestion_id="ingest_partial",
            statement_period_from=pd.Timestamp("2026-04-26"),
            statement_period_to=pd.Timestamp("2026-07-26"),
            line_number=2,
        )
        full_1 = _txn(
            account_id="acc_natwest_current",
            description="KROO ACCOUNT Mobile/Online Transaction",
            amount_minor=-274575,
            transaction_date="2026-07-26",
            source_type="natwest-transactions",
            bronze_record_id="br_full_1",
            ingestion_id="ingest_full",
            statement_period_from=pd.Timestamp("2026-05-04"),
            statement_period_to=pd.Timestamp("2026-08-04"),
            line_number=1,
        )
        full_2 = _txn(
            account_id="acc_natwest_current",
            description="KROO ACCOUNT Mobile/Online Transaction",
            amount_minor=-274575,
            transaction_date="2026-07-27",
            source_type="natwest-transactions",
            bronze_record_id="br_full_2",
            ingestion_id="ingest_full",
            statement_period_from=pd.Timestamp("2026-05-04"),
            statement_period_to=pd.Timestamp("2026-08-04"),
            line_number=2,
        )

        canonical, sources = match_transactions(
            _make_frame([partial_1, partial_2, full_1, full_2])
        )

        assert len(canonical) == 4
        assert not sources["match_policy"].str.startswith("overlap-window").any()


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
