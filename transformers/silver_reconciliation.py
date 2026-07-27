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

from typing import Dict, Optional

import pandas as pd

from models.money import parse_money_minor, MoneyParseError

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
    plan_it_adjustments: Optional[Dict[str, int]] = None,
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

    `plan_it_adjustments` (ingestion_id -> minor units, see
    `amex_plan_it_adjustment_by_ingestion` below): Amex's own Bronze-level
    rollforward (adapters/amex_pdf_adapter.py::parse()) adds a "Plan It
    Instalments Due" component beyond the plain transaction sum whenever a
    Plan-It plan is active - without this, an Amex statement with an
    active plan would show a spurious mismatch here even though Bronze's
    own check (and the real statement) reconciles perfectly.
    """
    if bronze_anchors.empty or transaction_sources.empty or transactions.empty:
        return pd.DataFrame(columns=_BREAKS_COLUMNS)

    anchored = bronze_anchors.dropna(subset=["expected_opening_minor"])
    if anchored.empty:
        return pd.DataFrame(columns=_BREAKS_COLUMNS)

    contributions = _contribution_by_ingestion(transaction_sources, transactions)
    plan_it_adjustments = plan_it_adjustments or {}

    rows = []
    for row in anchored.itertuples():
        sign = _ROLLFORWARD_SIGN.get(row.source_type)
        if sign is None:
            continue
        contributed = contributions.get(row.ingestion_id, 0)
        derived = row.expected_opening_minor + sign * contributed
        derived += plan_it_adjustments.get(row.ingestion_id, 0)
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

    Joined on bronze_record_id, not silver_transaction_id: transaction_sources
    carries one row per original Bronze record (including cross-source-
    absorbed rows whose own bronze_record_id never made it into `transactions`
    at all, since only the preferred source's row survives there - those
    simply drop out of an inner join, correctly not contributing, since the
    absorbed side is always a non-anchored source_type in the one declared
    cross-source policy today). bronze_record_id is the one thing guaranteed
    unique on both sides; silver_transaction_id is NOT reliably unique in
    `transactions` - two genuinely distinct same-day/same-amount/same-
    description transactions can share one fingerprint-derived ID (matching.py
    doesn't fold the occurrence number into it), which would silently
    undercount a real transaction's contribution if joined on that column
    instead (confirmed against real data: two real, distinct -£8.90 same-day
    charges collapsed to one via a silver_transaction_id join).
    """
    merged = transaction_sources.merge(
        transactions[["bronze_record_id", "amount_minor"]],
        on="bronze_record_id",
        how="inner",
    )
    return merged.groupby("ingestion_id")["amount_minor"].sum()


def amex_plan_it_adjustment_by_ingestion(
    amex_bronze: Optional[pd.DataFrame],
) -> Dict[str, int]:
    """Sum each ingestion's active Plan-It plans' `due_this_month_plan`
    figure - the same adjustment adapters/amex_pdf_adapter.py::parse()
    rolls into its own closing-balance derivation (`due_this_month_plan`
    already nets out that plan's own instalment fee, which is separately
    counted once via the ordinary "INSTALMENT PLAN FEE" transaction row -
    see that method's comments).

    Computed directly from Bronze's raw `plan_it_instalment` rows, not the
    Silver `plan_it_instalments` table - deliberately independent of
    whatever that table's own normalization does.
    """
    if amex_bronze is None or amex_bronze.empty or "record_type" not in amex_bronze.columns:
        return {}
    plan_it = amex_bronze[amex_bronze["record_type"] == "plan_it_instalment"]
    if plan_it.empty:
        return {}

    adjustments: Dict[str, int] = {}
    for row in plan_it.itertuples():
        raw = row.raw_data
        try:
            due_plan_minor = parse_money_minor(raw.get("due_this_month_plan", ""))
        except MoneyParseError:
            continue
        adjustments[row.ingestion_id] = adjustments.get(row.ingestion_id, 0) + due_plan_minor
    return adjustments
