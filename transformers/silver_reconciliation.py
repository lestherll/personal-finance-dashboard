"""Silver-side reconciliation: re-verify Bronze's own B1 self-check still
holds after transaction matching/dedup.

Bronze's reconciliation check (transformers/reconciliation_status.py) rolls
forward through the transactions the *adapter* parsed, before any
cross-file matching or same-source dedup runs (transformers/matching.py::
match_transactions()). Nothing currently re-verifies that the *surviving*
Silver transactions - after a cross-source duplicate is absorbed, or a
same-source occurrence is canonicalized - still roll forward to the same
anchor. A bug in match_transactions() that silently drops or duplicates a
genuine (non-duplicate) transaction would break this without Bronze's own
per-file check ever catching it, since that check only ever sees the
adapter's raw, pre-matching parse.
"""

from typing import Dict

import pandas as pd

_BREAKS_COLUMNS = [
    "account_id",
    "source_type",
    "ingestion_id",
    "filename",
    "expected_opening_minor",
    "expected_closing_minor",
    "silver_derived_closing_minor",
    "matches",
]

# Mirrors each adapter's own rollforward sign convention (adapters/
# reconciliation.py, CLAUDE.md Gotcha #6/#12): liability accounts move
# opposite to the signed, cash-received `amount` convention; asset/cash
# accounts move the same way. Only source_types that roll an opening
# anchor forward at all reach this dict - see the module docstring on
# adapters/reconciliation.py for which of the 7 anchored adapters those are.
_ROLLFORWARD_SIGN: Dict[str, int] = {
    "amex": -1,
    "firstdirect": -1,
    "chase": 1,
    "natwest-statement": 1,
}


def find_silver_reconciliation_breaks(
    bronze_anchors: pd.DataFrame,
    transaction_sources: pd.DataFrame,
    transactions: pd.DataFrame,
) -> pd.DataFrame:
    """Re-derive each anchored ingestion's closing balance from the
    surviving Silver transactions it contributed to, and compare against
    the same printed closing anchor Bronze already validated pre-dedup.

    Only ingestions with a captured opening anchor (`expected_opening_minor`
    not null - see adapters/reconciliation.py) are checked: an ingestion
    with no opening anchor (Kroo/Monzo Flex's lighter direct-read checks
    when the opening figure wasn't printed, or Monzo PDF's genuine absence
    of one) has no independent arithmetic to re-verify here - it was never
    rolled forward in the first place, so there's nothing for Silver's
    matching step to have broken.
    """
    if bronze_anchors.empty or transaction_sources.empty or transactions.empty:
        return pd.DataFrame(columns=_BREAKS_COLUMNS)

    anchored = bronze_anchors.dropna(subset=["expected_opening_minor"])
    if anchored.empty:
        return pd.DataFrame(columns=_BREAKS_COLUMNS)

    contributions = _contribution_by_ingestion(transaction_sources, transactions)

    rows = []
    for row in anchored.itertuples():
        sign = _ROLLFORWARD_SIGN.get(row.source_type)
        if sign is None:
            continue
        contributed = contributions.get(row.ingestion_id, 0)
        derived = row.expected_opening_minor + sign * contributed
        rows.append(
            {
                "account_id": row.account_id,
                "source_type": row.source_type,
                "ingestion_id": row.ingestion_id,
                "filename": row.filename,
                "expected_opening_minor": row.expected_opening_minor,
                "expected_closing_minor": row.expected_closing_minor,
                "silver_derived_closing_minor": derived,
                "matches": derived == row.expected_closing_minor,
            }
        )

    return pd.DataFrame(rows, columns=_BREAKS_COLUMNS)


def _contribution_by_ingestion(
    transaction_sources: pd.DataFrame, transactions: pd.DataFrame
) -> "pd.Series[int]":
    """Net amount_minor each ingestion's Bronze rows contribute to the
    final Silver transaction set.

    A cross-source-absorbed duplicate still appears in transaction_sources,
    mapped to the *surviving* row's silver_transaction_id - so counting each
    distinct (ingestion_id, silver_transaction_id) pair once, not once per
    bronze_record_id, correctly attributes an absorbed duplicate's amount to
    whichever ingestion it came from without double-counting it against the
    surviving row's own ingestion. Two rows sharing one silver_transaction_id
    necessarily share the same amount_minor too (it's part of the fingerprint
    that produced that id), so which specific occurrence the join picks
    doesn't affect the sum.
    """
    merged = transaction_sources.merge(
        transactions[["silver_transaction_id", "amount_minor"]],
        on="silver_transaction_id",
        how="inner",
    )
    unique = merged.drop_duplicates(["ingestion_id", "silver_transaction_id"])
    return unique.groupby("ingestion_id")["amount_minor"].sum()
