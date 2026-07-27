"""Silver transaction matching and deduplication.

Two-tier policy: same-source dedup uses a full content fingerprint (account,
date, amount, normalized description, occurrence); declared cross-source pairs
match on a looser key (account, date, amount) with a stated preference for one
source over the other.

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
    """Deterministic content fingerprint for same-source dedup."""
    normalized = _normalize_description(description)
    material = f"{account_id}|{date}|{amount_minor}|{normalized}".encode()
    return hashlib.sha256(material).hexdigest()


# Declared cross-source pairs with explicit match policy.
# Key: frozenset of source_types; Value: (match_key_columns, prefer_source)
_CROSS_SOURCE_POLICIES = {
    frozenset(["natwest-transactions", "natwest-statement"]): (
        ["account_id", "transaction_date", "amount_minor"],
        "natwest-statement",
    ),
}


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

    # 1. Same-source fingerprint (source_type included so different banks
    #    with the same description/amount don't collide).
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

        # Stable canonical id: fingerprint + occurrence so two genuinely
        # distinct same-day/same-amount/same-merchant repeats get unique ids.
        silver_id = f"{group.name[1]}_{group.name[2]}"
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

    # 5. Clean up internal columns.
    canonical["silver_transaction_id"] = (
        canonical["_fingerprint"].astype(str)
        + "_"
        + canonical["_occurrence"].astype(str)
    )
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
        survivor_id = f"{row['_fingerprint']}_{row['_occurrence']}"
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
            absorbed_id = f"{row['_fingerprint']}_{row['_occurrence']}"
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
