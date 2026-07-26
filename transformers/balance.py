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

# Confirmed forward-chronological (ascending line_number = ascending time)
# via each source's own reconciliation logic depending on that order to
# pass against real statements: kroo (last transaction's balance must match
# the closing anchor), amex/natwest-statement (rolling the balance forward
# through transactions in parse order must land on the printed closing
# figure). Monzo PDF is confirmed reverse-chronological the same rigorous
# way (see module docstring). First Direct has only ever been seen with a
# single real transaction per statement, so its direction is unverified -
# revisit if a multi-transaction statement ever produces a wrong balance.
_REVERSE_CHRONOLOGICAL_SOURCE_TYPES = {"monzo-pdf"}


def get_current_balances(datalake: Optional[DataLake] = None) -> pd.DataFrame:
    """One row per account_id: its balance as of the most recent
    (as_of_date, statement_period_to-or-upload_timestamp, line_number)."""
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
    latest = ordered.groupby("account_id", as_index=False).tail(1)
    return latest[_BALANCES_COLUMNS].reset_index(drop=True)


def get_net_worth(
    datalake: Optional[DataLake] = None, path: Optional[PathLike] = None
) -> Decimal:
    """Sum of current balances across accounts, sign-adjusted for
    account_type: credit accounts are a liability (subtracted), everything
    else - current/savings/investment - is an asset (added).

    Per CLAUDE.md Gotcha #6, a credit account's `balance` is already stored
    as a positive "amount owed" figure, not a negative one.
    """
    balances = get_current_balances(datalake)
    if balances.empty:
        return Decimal("0")

    accounts = build_accounts_table(path=path)
    merged = balances.merge(
        accounts[["account_id", "account_type"]], on="account_id", how="left"
    )

    total = Decimal("0")
    for row in merged.itertuples():
        amount = Decimal(str(row.balance))
        if row.account_type == "credit":
            total -= amount
        else:
            total += amount
    return total
