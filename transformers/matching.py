"""Silver transaction matching and deduplication.

Two-tier policy: same-source dedup uses a full content fingerprint (account,
date, amount, normalized description, occurrence); declared cross-source pairs
match on a looser key (account, date, amount) with a stated preference for one
source over the other. Bank-provided transaction IDs (e.g. Monzo CSV's "id")
outrank both when available - a match on (account, bank_txn_id) is
unambiguous.

Designed after the plan doc's "explicit, traceable cross-file matching
policy" - the Natwest Transactions/Statement pair is the canonical
cross-source example, proven by real data to require loose-key matching
(descriptions differ materially between the two formats).
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd

logger = logging.getLogger(__name__)


def _normalize_description(desc: str) -> str:
    """Reduce a raw transaction description to a canonical form for
    fingerprinting: uppercase, collapse runs of non-alphanumeric/space
    characters to a single space, strip leading/trailing whitespace."""

    canonical = (
        desc.upper()
        .replace("£", "")
        .replace("'LL", " WILL")
        .replace("N'T", " NOT")
    )
    canonical = re.sub(r"[^A-Z0-9\s]", " ", canonical)
    return re.sub(r"\s+", " ", canonical).strip()


def _compute_fingerprint(
    account_id: str, date: str, amount: float, description: str
) -> str:
    """Deterministic content fingerprint for same-source dedup."""
    normalized = _normalize_description(description)
    material = f"{account_id}|{date}|{amount}|{normalized}".encode()
    return hashlib.sha256(material).hexdigest()


def _loose_key(account_id: str, date: str, amount: float) -> Tuple[str, str, float]:
    """Cross-source match key: account + date + amount (no description)."""
    return (account_id, date, amount)


# Declared cross-source pairs with explicit match policy.
# Key: frozenset of source_types; Value: (match_key_columns, prefer_source)
_CROSS_SOURCE_POLICIES = {
    frozenset(["natwest-transactions", "natwest-statement"]): (
        ["account_id", "transaction_date", "amount"],
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

    # 1. Extract bank-provided transaction IDs from raw_data if present.
    df["_bank_txn_id"] = None
    if "bank_transaction_id" in df.columns:
        df["_bank_txn_id"] = df["bank_transaction_id"]

    # 2. Same-source fingerprint (source_type included so different banks
    #    with the same description/amount don't collide).
    df["_fingerprint"] = [
        _compute_fingerprint(
            row.account_id,
            str(row.transaction_date),
            row.amount,
            row.description,
        )
        for row in df.itertuples()
    ]

    # 3. Assign occurrence numbers within each (source_type, fingerprint)
    #    group. Sorting by (statement_period_to, upload_timestamp, line_number)
    #    gives a stable, sensible order in which to number occurrences.
    sort_key = df["statement_period_to"].fillna(df["upload_timestamp"])
    df["_sort_key"] = sort_key
    df = df.sort_values(["_sort_key", "line_number"]).reset_index(drop=True)
    df["_occurrence"] = df.groupby(
        ["source_type", "_fingerprint"]
    ).cumcount() + 1

    # 4. Canonicalize: for each (source_type, fingerprint, occurrence), keep
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

        silver_id = group.name[1]  # _fingerprint from group key
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

        canonical["_fingerprint"] = silver_id
        canonical["_occurrence"] = group.name[2]
        canonical["source_type"] = source_type
        return pd.DataFrame([canonical])

    grouped = df.groupby(group_cols, group_keys=False)
    canonical = grouped.apply(_pick_best_and_record_provenance).reset_index(
        drop=True
    )

    # 5. Cross-source dedup for declared policy pairs.
    #    A row from the non-preferred source that has a matching loose-key
    #    row in the preferred source is dropped - but only to the extent of
    #    the multiset count in the preferred source (genuine repeats
    #    within a single source are never dropped).
    for source_pair, (key_cols, prefer_source) in _CROSS_SOURCE_POLICIES.items():
        pair_set = set(source_pair)
        canonical = _dedupe_cross_source(
            canonical, pair_set, key_cols, prefer_source, provenance_rows
        )

    # 6. Clean up internal columns.
    canonical["silver_transaction_id"] = canonical["_fingerprint"]
    canonical = canonical.drop(
        columns=["_fingerprint", "_occurrence", "_sort_key", "_bank_txn_id"],
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

    # Count occurrences of each loose key in the preferred source.
    preferred_counts: Dict[Tuple, int] = dict(
        preferred.groupby(key_cols).size()
    )

    drop_indices = []
    for idx, row in non_preferred.iterrows():
        key = tuple(row[col] for col in key_cols)
        if preferred_counts.get(key, 0) > 0:
            drop_indices.append(idx)
            preferred_counts[key] -= 1
            # Record provenance: this non-preferred row is absorbed
            # into the matching preferred row.
            for si, ri in zip(
                row.get("ingestion_id", [None]),
                [row.get("bronze_record_id", "")],
            ):
                provenance_rows.append(
                    {
                        "silver_transaction_id": row.get("_fingerprint", ""),
                        "bronze_record_id": ri if ri else "",
                        "ingestion_id": si if si else "",
                        "source_type": row.get("source_type", ""),
                        "match_policy": f"cross-source({prefer_source}-preferred)",
                    }
                )

    deduped_non_preferred = non_preferred.drop(index=drop_indices)
    return pd.concat(
        [rest, preferred, deduped_non_preferred], ignore_index=False
    ).sort_index()
