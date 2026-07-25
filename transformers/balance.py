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
"""

from decimal import Decimal
from pathlib import Path
from typing import Optional, Union

import pandas as pd

from models.datalake import DataLake, get_datalake
from transformers.account_config import build_accounts_table

PathLike = Union[str, Path]

_BALANCES_COLUMNS = ["account_id", "balance", "as_of_date"]


def get_current_balances(datalake: Optional[DataLake] = None) -> pd.DataFrame:
    """One row per account_id: its balance as of the most recent
    (as_of_date, statement_period_to-or-upload_timestamp, line_number)."""
    datalake = datalake or get_datalake()
    ledger = datalake.read_silver("account_ledger")
    if ledger is None or ledger.empty:
        return pd.DataFrame(columns=_BALANCES_COLUMNS)

    sort_key = ledger["statement_period_to"].fillna(ledger["upload_timestamp"])
    ordered = ledger.assign(_sort_key=sort_key).sort_values(
        ["as_of_date", "_sort_key", "line_number"]
    )
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
