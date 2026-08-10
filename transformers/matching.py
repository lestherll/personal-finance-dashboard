"""Silver transaction matching and deduplication.

Three-tier policy: same-source dedup uses a full content fingerprint (account,
date, amount, normalized description, occurrence); a same-source **overlap-
window** tier collapses that same fingerprint across two ingestions of the
same source_type/account whose *declared* statement periods overlap (e.g. a
partial-month statement later re-covered by a full-month statement for the
same account) - restricted to the overlap window, matched via multiset
counting so genuine same-day repeats are preserved (excess occurrences on
either side are left distinct rather than force-merged); declared
cross-source pairs match on a looser key (account, date, amount) with a
stated preference for one source over the other.

There is deliberately **no** bank-transaction-id tier. An earlier version of
this docstring claimed one ("bank-provided IDs outrank both"), but the code
never implemented it - `_bank_txn_id` was assigned and then dropped unused.
It has been removed rather than built out, because the only source that
carries a genuine bank-issued id is the Monzo CSV export, which is
effectively unused in practice; every PDF source has no such id and never
will. Synthesising one at parse time would not restore the tier's value:
what earns a bank id top rank is being stable across *different files*
containing the same transaction, and any id derived from parsed content is
just a content fingerprint - which this module already computes, more
faithfully than an adapter-level key can (see
adapters/base.py::make_transaction_source_key). If a real bank-issued id
ever arrives (Open Banking), add the tier here then.

Designed after the plan doc's "explicit, traceable cross-file matching
policy" - the Natwest Transactions/Statement pair is the canonical
cross-source example, proven by real data to require loose-key matching
(descriptions differ materially between the two formats).
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Dict, List, Set, Tuple

import pandas as pd

logger = logging.getLogger(__name__)


def _normalize_description(desc: str) -> str:
    """Reduce a raw transaction description to a canonical form for
    fingerprinting: uppercase, collapse runs of non-alphanumeric/space
    characters to a single space, strip leading/trailing whitespace."""

    canonical = (
        desc.upper().replace("£", "").replace("'LL", " WILL").replace("N'T", " NOT")
    )
    canonical = re.sub(r"[^A-Z0-9\s]", " ", canonical)
    return re.sub(r"\s+", " ", canonical).strip()


def _compute_fingerprint(
    account_id: str, date: str, amount_minor: int, description: str
) -> str:
    """Deterministic content fingerprint for same-source dedup.

    Deliberately excludes source_type: it is a *content* hash. Scoping by
    source happens in the grouping keys and, for the canonical id, in
    _silver_id below."""
    normalized = _normalize_description(description)
    material = f"{account_id}|{date}|{amount_minor}|{normalized}".encode()
    return hashlib.sha256(material).hexdigest()


def _loose_fingerprint(account_id: str, amount_minor: int, description: str) -> str:
    """Same as _compute_fingerprint but without the date - used only by the
    overlap-window tier's date-drift fallback (see _DATE_DRIFT_TOLERANCE)."""
    normalized = _normalize_description(description)
    material = f"{account_id}|{amount_minor}|{normalized}".encode()
    return hashlib.sha256(material).hexdigest()


# Same-source overlap-window rows that share everything but the date can
# still be one real transaction reported under two different dates across
# two download sessions - observed with natwest-transactions, whose online
# "Transactions" export isn't stable about pending-vs-cleared dates between
# downloads (e.g. a KROO transfer reported as 25 Jul in one download and 27
# Jul in a later, overlapping one). Since natwest-transactions has no
# balance anchor of its own (see NATWEST_TRANSACTIONS_BALANCE_DESIGN.md),
# nothing else catches the resulting double-count. Kept deliberately small -
# wide enough for the observed drift, narrow enough that two genuinely
# separate same-amount/description transactions are unlikely to fall inside
# it by coincidence.
_DATE_DRIFT_TOLERANCE = pd.Timedelta(days=3)


def _silver_id(source_type: str, fingerprint: str, occurrence) -> str:
    """Canonical silver_transaction_id, scoped by source_type.

    Occurrence numbers are assigned per (source_type, fingerprint) group,
    so a bare `fingerprint_occurrence` id collides whenever two source
    types produce identical normalized content for the same account - the
    Monzo CSV x Monzo PDF overlap is the real case (both map to one
    account_id, and no cross-source policy pairs them). Two distinct
    canonical rows sharing one id makes the transactions table's primary
    key non-unique and turns every join on it into a fan-out, so the
    source_type is folded in here, at the single place ids are minted.
    """
    return f"{source_type}:{fingerprint}_{occurrence}"


# Declared cross-source pairs with explicit match policy.
# Key: frozenset of source_types; Value: (match_key_columns, prefer_source)
_CROSS_SOURCE_POLICIES = {
    frozenset(["natwest-transactions", "natwest-statement"]): (
        ["account_id", "transaction_date", "amount_minor"],
        "natwest-statement",
    ),
}

_PERIOD_COLUMNS = [
    "ingestion_id",
    "account_id",
    "source_type",
    "statement_period_from",
    "statement_period_to",
]


def _find_overlapping_ingestion_periods(df: pd.DataFrame) -> pd.DataFrame:
    """One row per ingestion_id with a declared statement period.

    Ingestions with no parsed statement_period_from/to (adapters that don't
    capture one, or CSV sources with no period concept at all) are dropped -
    the overlap-window tier only activates on declared periods; it never
    falls back to fingerprint-only matching when period metadata is
    missing.
    """
    if (
        "statement_period_from" not in df.columns
        or "statement_period_to" not in df.columns
    ):
        return pd.DataFrame(columns=_PERIOD_COLUMNS)
    periods = df.dropna(subset=["statement_period_from", "statement_period_to"])
    return periods.drop_duplicates(subset=["ingestion_id"])[_PERIOD_COLUMNS]


def _dedupe_overlapping_ingestions(
    canonical: pd.DataFrame,
    periods: pd.DataFrame,
    provenance_rows: List[Dict],
) -> pd.DataFrame:
    """Collapse same-source rows that two ingestions of the same
    account/source_type both re-report because their declared statement
    periods overlap.

    Only rows whose transaction_date falls inside the overlap window are
    considered, and matching is by full content fingerprint (not a loose
    key) with multiset pairing - if one ingestion reports a fingerprint more
    times than the other inside the window, the excess is left alone rather
    than force-merged, so nothing is ever discarded that only one side
    reported.
    """
    if periods.empty:
        return canonical

    for _, group in periods.groupby(["account_id", "source_type"]):
        records = group.to_dict("records")
        for i in range(len(records)):
            for j in range(i + 1, len(records)):
                a, b = records[i], records[j]
                lo = max(a["statement_period_from"], b["statement_period_from"])
                hi = min(a["statement_period_to"], b["statement_period_to"])
                if lo > hi:
                    continue
                canonical = _collapse_ingestion_pair(
                    canonical,
                    a["ingestion_id"],
                    b["ingestion_id"],
                    lo,
                    hi,
                    provenance_rows,
                )
    return canonical


def _collapse_ingestion_pair(
    df: pd.DataFrame,
    ingestion_a: str,
    ingestion_b: str,
    window_lo,
    window_hi,
    provenance_rows: List[Dict],
) -> pd.DataFrame:
    """Multiset-collapse matching fingerprints between two ingestions,
    restricted to rows dated inside [window_lo, window_hi] (buffered by
    _DATE_DRIFT_TOLERANCE on each side, so a date-shifted duplicate just
    past the declared-period intersection is still a reachable candidate).

    Which side survives as canonical is arbitrary (the match is already on
    full content, so either row is an equally valid representative) but
    picked deterministically - later _sort_key wins, ingestion_id breaks
    ties - so reruns are stable.
    """
    txn_dates = pd.to_datetime(df["transaction_date"])
    in_window = (txn_dates >= window_lo - _DATE_DRIFT_TOLERANCE) & (
        txn_dates <= window_hi + _DATE_DRIFT_TOLERANCE
    )
    mask = in_window & df["ingestion_id"].isin([ingestion_a, ingestion_b])
    if not mask.any():
        return df

    subset = df[mask]
    rest = df[~mask]

    a_rows = subset[subset["ingestion_id"] == ingestion_a]
    b_rows = subset[subset["ingestion_id"] == ingestion_b]
    if a_rows.empty or b_rows.empty:
        return df

    a_key = (a_rows["_sort_key"].max(), ingestion_a)
    b_key = (b_rows["_sort_key"].max(), ingestion_b)
    preferred, non_preferred = (b_rows, a_rows) if b_key > a_key else (a_rows, b_rows)

    def _survivor_id(row) -> str:
        return _silver_id(row["source_type"], row["_fingerprint"], row["_occurrence"])

    preferred_ids: Dict[str, List[str]] = {}
    for _, row in preferred.iterrows():
        preferred_ids.setdefault(row["_fingerprint"], []).append(_survivor_id(row))

    prov_index: Dict[str, List[Dict]] = {}
    for prov in provenance_rows:
        prov_index.setdefault(prov["silver_transaction_id"], []).append(prov)

    def _absorb(non_preferred_row, survivor_id: str, match_policy: str) -> None:
        absorbed_id = _survivor_id(non_preferred_row)
        new_entry = {
            "silver_transaction_id": survivor_id,
            "bronze_record_id": non_preferred_row.get("bronze_record_id", "") or "",
            "ingestion_id": non_preferred_row.get("ingestion_id", "") or "",
            "source_type": non_preferred_row.get("source_type", ""),
            "match_policy": match_policy,
        }
        provenance_rows.append(new_entry)
        prov_index.setdefault(survivor_id, []).append(new_entry)
        for prov in prov_index.pop(absorbed_id, []):
            prov["silver_transaction_id"] = survivor_id
            prov_index.setdefault(survivor_id, []).append(prov)

    drop_indices = []
    consumed_preferred_ids: Set[str] = set()
    for idx, row in non_preferred.iterrows():
        fp = row["_fingerprint"]
        if preferred_ids.get(fp):
            drop_indices.append(idx)
            survivor_id = preferred_ids[fp].pop(0)
            consumed_preferred_ids.add(survivor_id)
            _absorb(row, survivor_id, f"overlap-window({row['source_type']})")

    # Fallback pass: rows that didn't share an exact (date-inclusive)
    # fingerprint may still be the same real transaction under a
    # slightly-shifted date - see _DATE_DRIFT_TOLERANCE. Only collapsed when
    # unambiguous: exactly one leftover candidate on each side sharing
    # (account, amount, description); any ambiguity is left alone rather
    # than guessed at.
    leftover_non_preferred = non_preferred.drop(index=drop_indices)
    leftover_preferred = [
        (idx, row)
        for idx, row in preferred.iterrows()
        if _survivor_id(row) not in consumed_preferred_ids
    ]

    if not leftover_non_preferred.empty and leftover_preferred:
        preferred_by_loose: Dict[str, List[Tuple]] = {}
        for idx, row in leftover_preferred:
            loose_fp = _loose_fingerprint(
                row["account_id"], row["amount_minor"], row["description"]
            )
            preferred_by_loose.setdefault(loose_fp, []).append((idx, row))

        non_preferred_by_loose: Dict[str, List[Tuple]] = {}
        for idx, row in leftover_non_preferred.iterrows():
            loose_fp = _loose_fingerprint(
                row["account_id"], row["amount_minor"], row["description"]
            )
            non_preferred_by_loose.setdefault(loose_fp, []).append((idx, row))

        for loose_fp, non_pref_candidates in non_preferred_by_loose.items():
            pref_candidates = preferred_by_loose.get(loose_fp)
            if (
                not pref_candidates
                or len(pref_candidates) != 1
                or len(non_pref_candidates) != 1
            ):
                continue
            _, pref_row = pref_candidates[0]
            np_idx, np_row = non_pref_candidates[0]
            date_gap = abs(
                pd.Timestamp(np_row["transaction_date"])
                - pd.Timestamp(pref_row["transaction_date"])
            )
            if date_gap > _DATE_DRIFT_TOLERANCE:
                continue
            drop_indices.append(np_idx)
            survivor_id = _survivor_id(pref_row)
            _absorb(
                np_row,
                survivor_id,
                f"overlap-window-date-shifted({np_row['source_type']})",
            )

    deduped_non_preferred = non_preferred.drop(index=drop_indices)
    return pd.concat(
        [rest, preferred, deduped_non_preferred], ignore_index=False
    ).sort_index()


def match_transactions(
    transactions: pd.DataFrame,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Deduplicate a transactions DataFrame and produce a provenance table.

    Returns (canonical_transactions, transaction_sources) where
    transaction_sources maps each canonical transaction's
    silver_transaction_id to every bronze_record_id it subsumes.
    """
    if transactions.empty:
        sources = pd.DataFrame(
            columns=[
                "silver_transaction_id",
                "bronze_record_id",
                "ingestion_id",
                "source_type",
                "match_policy",
            ]
        )
        return transactions, sources

    df = transactions.copy()

    # 1. Same-source content fingerprint. source_type is NOT part of the
    #    hash - it scopes the grouping below and the canonical id (see
    #    _silver_id), so different banks with the same description/amount
    #    never merge.
    df["_fingerprint"] = [
        _compute_fingerprint(
            row.account_id,
            str(row.transaction_date),
            row.amount_minor,
            row.description,
        )
        for row in df.itertuples()
    ]

    # 2. Assign occurrence numbers within each (source_type, fingerprint)
    #    group. Sorting by (statement_period_to, upload_timestamp, line_number)
    #    gives a stable, sensible order in which to number occurrences.
    sort_key = df["statement_period_to"].fillna(df["upload_timestamp"])
    df["_sort_key"] = sort_key
    df = df.sort_values(["_sort_key", "line_number"]).reset_index(drop=True)
    df["_occurrence"] = df.groupby(["source_type", "_fingerprint"]).cumcount() + 1

    # 3. Canonicalize: for each (source_type, fingerprint, occurrence), keep
    #    the row with the most complete data and record all bronze_record_ids
    #    it absorbed.
    #    Drop duplicates within the same source_type + fingerprint +
    #    occurrence group, keeping the best row.
    group_cols = ["source_type", "_fingerprint", "_occurrence"]

    provenance_rows: List[Dict] = []

    def _pick_best_and_record_provenance(group: pd.DataFrame) -> pd.DataFrame:
        """Within one canonical group, record all ingested Bronze rows and
        return the single canonical row."""
        # group.name is (source_type, _fingerprint, _occurrence) - pandas 3.x
        # excludes group columns from the apply data.
        source_type = group.name[0]

        bronze_ids = group["bronze_record_id"].tolist()
        ingestion_ids = group["ingestion_id"].tolist()

        # Pick the canonical row: prefer the one with the most complete sort
        # metadata (statement_period_to beats upload_timestamp).
        group_sorted = group.sort_values(
            ["_sort_key", "line_number"], ascending=[False, True]
        )
        canonical = group_sorted.iloc[0].to_dict()

        # Stable canonical id: source_type + fingerprint + occurrence, so
        # two genuinely distinct same-day/same-amount/same-merchant repeats
        # - from one source or two - get unique ids.
        silver_id = _silver_id(group.name[0], group.name[1], group.name[2])
        for bi, ii in zip(bronze_ids, ingestion_ids):
            provenance_rows.append(
                {
                    "silver_transaction_id": silver_id,
                    "bronze_record_id": bi,
                    "ingestion_id": ii,
                    "source_type": source_type,
                    "match_policy": "fingerprint",
                }
            )

        canonical["_fingerprint"] = group.name[1]
        canonical["_occurrence"] = group.name[2]
        canonical["source_type"] = source_type
        return pd.DataFrame([canonical])

    grouped = df.groupby(group_cols, group_keys=False)
    canonical = grouped.apply(_pick_best_and_record_provenance).reset_index(drop=True)

    # 3b. Same-source, cross-ingestion overlap-window dedup: two ingestions
    #     of the same source_type/account whose *declared* statement periods
    #     overlap (e.g. a partial-month statement later re-covered by a
    #     full-month statement) re-report the same real transactions, but
    #     step 2's occurrence numbering treats them as distinct repeats
    #     because it counts per (source_type, fingerprint) across the whole
    #     dataset, not per ingestion. Collapse matching fingerprints back
    #     down, restricted to the overlap window only.
    periods = _find_overlapping_ingestion_periods(df)
    canonical = _dedupe_overlapping_ingestions(canonical, periods, provenance_rows)

    # 4. Cross-source dedup for declared policy pairs.
    #    A row from the non-preferred source that has a matching loose-key
    #    row in the preferred source is dropped - but only to the extent of
    #    the multiset count in the preferred source (genuine repeats
    #    within a single source are never dropped).
    for source_pair, (key_cols, prefer_source) in _CROSS_SOURCE_POLICIES.items():
        pair_set = set(source_pair)
        canonical = _dedupe_cross_source(
            canonical, pair_set, key_cols, prefer_source, provenance_rows
        )

    # 5. Clean up internal columns. Mint ids through the same helper the
    #    provenance/dedup steps used, so every id in both output frames is
    #    built identically by construction.
    canonical["silver_transaction_id"] = [
        _silver_id(st, fp, occ)
        for st, fp, occ in zip(
            canonical["source_type"],
            canonical["_fingerprint"],
            canonical["_occurrence"],
        )
    ]
    canonical = canonical.drop(
        columns=["_fingerprint", "_occurrence", "_sort_key"],
        errors="ignore",
    )

    sources_df = pd.DataFrame(provenance_rows).drop_duplicates()
    return canonical, sources_df


def _dedupe_cross_source(
    df: pd.DataFrame,
    source_pair: Set[str],
    key_cols: List[str],
    prefer_source: str,
    provenance_rows: List[Dict],
) -> pd.DataFrame:
    """Remove rows from non-preferred sources in a declared cross-source
    pair when they match rows in the preferred source by loose key,
    using multiset (count-based) removal."""
    mask = df["source_type"].isin(source_pair)
    if not mask.any():
        return df

    subset = df[mask]
    rest = df[~mask]
    preferred = subset[subset["source_type"] == prefer_source]
    non_preferred = subset[subset["source_type"] != prefer_source]

    # Map each loose key to the list of preferred survivor ids. We pop ids
    # as non-preferred rows are absorbed so provenance points at the actual
    # canonical row that subsumed the Bronze row (not the absorbed row's own
    # id, which would be dangling after it is dropped).
    preferred_ids: Dict[Tuple, List[str]] = {}
    for _, row in preferred.iterrows():
        key = tuple(row[col] for col in key_cols)
        survivor_id = _silver_id(
            row["source_type"], row["_fingerprint"], row["_occurrence"]
        )
        preferred_ids.setdefault(key, []).append(survivor_id)

    # Index existing provenance rows by silver_transaction_id, once, so the
    # redirect step below is a dict lookup instead of a full rescan of the
    # ever-growing provenance_rows list per absorbed row (was O(n^2) across
    # a rebuild). Maintained incrementally as rows are appended.
    prov_index: Dict[str, List[Dict]] = {}
    for prov in provenance_rows:
        prov_index.setdefault(prov["silver_transaction_id"], []).append(prov)

    drop_indices = []
    for idx, row in non_preferred.iterrows():
        key = tuple(row[col] for col in key_cols)
        if preferred_ids.get(key):
            drop_indices.append(idx)
            survivor_id = preferred_ids[key].pop(0)
            absorbed_id = _silver_id(
                row["source_type"], row["_fingerprint"], row["_occurrence"]
            )
            # Record provenance: this non-preferred row is absorbed
            # into the matching preferred row.
            new_entry = {
                "silver_transaction_id": survivor_id,
                "bronze_record_id": row.get("bronze_record_id", "") or "",
                "ingestion_id": row.get("ingestion_id", "") or "",
                "source_type": row.get("source_type", ""),
                "match_policy": f"cross-source({prefer_source}-preferred)",
            }
            provenance_rows.append(new_entry)
            prov_index.setdefault(survivor_id, []).append(new_entry)
            # Redirect any same-source provenance that pointed at the now-
            # absorbed canonical row so transaction_sources stays fully
            # joinable - every row that shared the absorbed id, not just one.
            for prov in prov_index.pop(absorbed_id, []):
                prov["silver_transaction_id"] = survivor_id
                prov_index.setdefault(survivor_id, []).append(prov)

    deduped_non_preferred = non_preferred.drop(index=drop_indices)
    return pd.concat(
        [rest, preferred, deduped_non_preferred], ignore_index=False
    ).sort_index()
