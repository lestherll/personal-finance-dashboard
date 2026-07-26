"""Current balance / net worth queries over the Silver account_ledger.

`account_ledger` has no natural single "latest balance" row per account -
multiple rows can share the same as_of_date (see BRONZE_SILVER_HARDENING_PLAN.md
S1/S2), so picking the wrong one silently produces a wrong "current balance".
The tie-break key here mirrors the one silver_transformer.py's
normalize_account_ledger() now threads onto every row:
(as_of_date, statement_period_to-or-upload_timestamp, line_number) -
statement_period_to (the statement's own printed cycle end) wins when
present since it reflects real-world recency better than upload_timestamp,
which only reflects when the user happened to upload a file.

`line_number` is only a valid same-day tiebreaker if ascending line_number
means ascending time within the file - true for most adapters, but NOT
Monzo PDF, which prints transactions newest-first (verified against a real
statement: balance_after_line_N - amount_N == balance_after_line_(N+1) for
every consecutive pair, i.e. line 1 is chronologically *last*, not first).
See _REVERSE_CHRONOLOGICAL_SOURCE_TYPES below - an earlier version of this
function didn't account for this and silently picked the oldest same-day
Monzo transaction instead of the newest.
"""

from decimal import Decimal
from pathlib import Path
from typing import Optional, Union

import pandas as pd

from models.datalake import DataLake, get_datalake
from transformers.account_config import build_accounts_table

PathLike = Union[str, Path]

_BALANCES_COLUMNS = ["account_id", "balance", "as_of_date"]
_BREAKDOWN_COLUMNS = [
    "account_id",
    "source",
    "balance_or_value",
    "as_of_date",
    "contribution_to_net_worth",
]

# Confirmed forward-chronological (ascending line_number = ascending time)
# via each source's own reconciliation logic depending on that order to
# pass against real statements: kroo (last transaction's balance must match
# the closing anchor), amex/natwest-statement (rolling the balance forward
# through transactions in parse order must land on the printed closing
# figure). Monzo PDF is confirmed reverse-chronological the same rigorous
# way (see module docstring). First Direct has only ever been seen with a
# single real transaction per statement, so its direction is unverified -
# revisit if a multi-transaction statement ever produces a wrong balance.
_REVERSE_CHRONOLOGICAL_SOURCE_TYPES = {"monzo-pdf", "monzo-flex"}


def get_current_balances(datalake: Optional[DataLake] = None) -> pd.DataFrame:
    """One row per account_id: its balance as of the most recent
    (as_of_date, statement_period_to-or-upload_timestamp, line_number).

    The returned `as_of_date` is bumped up to the winning row's
    `statement_period_to` when that's later than the row's own transaction
    date - a balance is still accurate through the end of the statement
    that reported it, even if the last real transaction happened earlier
    in the period (e.g. an account with a single transaction on day 1 of a
    28-day statement: nothing else was reported, so the balance genuinely
    held through day 28, not just day 1). Without this, an inactive
    account's "current balance" looks stale by however many days were left
    in its last statement, even though it's the true up-to-date figure.
    """
    datalake = datalake or get_datalake()
    ledger = datalake.read_silver("account_ledger")
    if ledger is None or ledger.empty:
        return pd.DataFrame(columns=_BALANCES_COLUMNS)

    sort_key = ledger["statement_period_to"].fillna(ledger["upload_timestamp"])
    is_reverse = ledger["source_type"].isin(_REVERSE_CHRONOLOGICAL_SOURCE_TYPES)
    effective_line_number = ledger["line_number"].where(
        ~is_reverse, -ledger["line_number"]
    )

    ordered = ledger.assign(
        _sort_key=sort_key, _line=effective_line_number
    ).sort_values(["as_of_date", "_sort_key", "_line"])
    latest = ordered.groupby("account_id", as_index=False).tail(1).copy()
    as_of = pd.to_datetime(latest["as_of_date"])
    period_to = pd.to_datetime(latest["statement_period_to"])
    latest["as_of_date"] = as_of.where(as_of >= period_to.fillna(as_of), period_to)
    return latest[_BALANCES_COLUMNS].reset_index(drop=True)


def get_net_worth(
    datalake: Optional[DataLake] = None, path: Optional[PathLike] = None
) -> Decimal:
    """Sum of current balances across accounts, sign-adjusted for
    account_type: credit accounts are a liability (subtracted), everything
    else - current/savings/investment - is an asset (added). Also includes
    holdings from the holdings table, which are always assets (never
    liabilities).

    Per CLAUDE.md Gotcha #6, a credit account's `balance` is already stored
    as a positive "amount owed" figure, not a negative one.
    """
    datalake = datalake or get_datalake()
    balances = get_current_balances(datalake)

    total = Decimal("0")

    # Sum account ledger balances (with sign adjustment for credit accounts)
    if not balances.empty:
        accounts = build_accounts_table(path=path)
        merged = balances.merge(
            accounts[["account_id", "account_type"]], on="account_id", how="left"
        )

        for row in merged.itertuples():
            amount = Decimal(str(row.balance))
            if row.account_type == "credit":
                total -= amount
            else:
                total += amount

    # Sum holdings (always assets, never liabilities)
    holdings = datalake.read_silver("holdings")
    if holdings is not None and not holdings.empty:
        holdings_by_account = (
            holdings.groupby("account_id")["total_value"].sum()
        )
        for value in holdings_by_account:
            total += Decimal(str(value))

    return total


def get_net_worth_breakdown(
    datalake: Optional[DataLake] = None, path: Optional[PathLike] = None
) -> pd.DataFrame:
    """Detailed breakdown of net worth by account/holding, including as_of_date
    and contribution to total net worth.

    Returns a DataFrame with columns: account_id, source, balance_or_value,
    as_of_date, contribution_to_net_worth. Each row is either a ledger balance
    or a holding fund. Rows are sorted by as_of_date (descending), then by
    contribution (descending for assets, ascending for liabilities).
    """
    datalake = datalake or get_datalake()
    rows = []

    # Add ledger-based rows (account balances)
    balances = get_current_balances(datalake)
    if not balances.empty:
        accounts = build_accounts_table(path=path)
        merged = balances.merge(
            accounts[["account_id", "account_type"]], on="account_id", how="left"
        )

        for row in merged.itertuples():
            amount = Decimal(str(row.balance))
            contribution = -amount if row.account_type == "credit" else amount
            rows.append(
                {
                    "account_id": row.account_id,
                    "source": row.account_id,
                    "balance_or_value": row.balance,
                    "as_of_date": row.as_of_date,
                    "contribution_to_net_worth": contribution,
                }
            )

    # Add holdings-based rows
    holdings = datalake.read_silver("holdings")
    if holdings is not None and not holdings.empty:
        for holding_row in holdings.itertuples():
            value = Decimal(str(holding_row.total_value))
            rows.append(
                {
                    "account_id": holding_row.account_id,
                    "source": holding_row.fund_name,
                    "balance_or_value": holding_row.total_value,
                    "as_of_date": None,
                    "contribution_to_net_worth": value,
                }
            )

    if not rows:
        return pd.DataFrame(columns=_BREAKDOWN_COLUMNS)

    df = pd.DataFrame(rows)
    df = df[_BREAKDOWN_COLUMNS].sort_values(
        ["as_of_date", "contribution_to_net_worth"],
        ascending=[False, False],
        na_position="last",
    )
    return df.reset_index(drop=True)
