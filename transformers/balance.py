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

import logging
from pathlib import Path
from typing import Optional, Union

import pandas as pd

from models.datalake import DataLake, get_datalake
from transformers.account_config import build_accounts_table

logger = logging.getLogger(__name__)

PathLike = Union[str, Path]

_BALANCES_COLUMNS = [
    "account_id",
    "balance_minor",
    "currency",
    "as_of_date",
    "balance_may_be_stale",
    "balance_source",
]
_BREAKDOWN_COLUMNS = [
    "account_id",
    "source",
    "balance_or_value",
    "as_of_date",
    "contribution_to_net_worth",
    "balance_may_be_stale",
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


class MixedCurrencyError(Exception):
    """Raised by get_net_worth/get_net_worth_breakdown when ledger balances
    and/or holdings span more than one currency - summing across currencies
    without FX conversion would produce a meaningless number. Mirrors
    transformers.account_config.UnmappedAccountsError's DataFrame-carrying
    shape."""

    def __init__(self, currency_breakdown: pd.DataFrame):
        self.currency_breakdown = currency_breakdown
        lines = [
            f"  - currency={row.currency!r}: {row.record_count} record(s), "
            f"e.g. account_id={row.sample_account_id!r}"
            for row in currency_breakdown.itertuples()
        ]
        message = (
            f"{len(currency_breakdown)} distinct currencies found across "
            "ledger balances/holdings - refusing to sum without FX "
            "conversion:\n" + "\n".join(lines)
        )
        super().__init__(message)


def _assert_single_currency(balances: pd.DataFrame, holdings: pd.DataFrame) -> None:
    """Raise MixedCurrencyError if ledger balances and holdings together
    span more than one currency. No-op (not an error) when neither frame
    has a currency column at all, or both are empty."""
    parts = []
    if not balances.empty and "currency" in balances.columns:
        parts.append(balances[["account_id", "currency"]])
    if not holdings.empty and "currency" in holdings.columns:
        parts.append(holdings[["account_id", "currency"]])
    if not parts:
        return

    records = pd.concat(parts, ignore_index=True)
    currencies = records["currency"].dropna().unique()
    if len(currencies) > 1:
        breakdown = (
            records.groupby("currency")
            .agg(
                record_count=("account_id", "size"),
                sample_account_id=("account_id", "first"),
            )
            .reset_index()
        )
        raise MixedCurrencyError(breakdown)


def get_latest_holdings_snapshot(
    datalake: Optional[DataLake] = None,
    as_of: Optional[pd.Timestamp] = None,
) -> pd.DataFrame:
    """Return only the latest complete holdings snapshot per account_id.

    Holdings are re-printed on every Vanguard statement. Summing all rows
    across all as_of_dates would double-count. This returns only the rows
    belonging to each account's most recent as_of_date, so get_net_worth
    uses exactly one complete snapshot per investment account.

    `as_of`, when given, restricts to snapshots dated on or before it first -
    so "latest" means "latest known as of that date", enabling historical
    net worth (see get_net_worth's `as_of` param).
    """
    datalake = datalake or get_datalake()
    holdings = datalake.read_silver("holdings")
    if holdings is None or holdings.empty:
        return pd.DataFrame()

    if as_of is not None:
        holdings = holdings[pd.to_datetime(holdings["as_of_date"]) <= as_of]
        if holdings.empty:
            return pd.DataFrame()

    latest_dates = holdings.groupby("account_id")["as_of_date"].max().reset_index()
    return holdings.merge(latest_dates, on=["account_id", "as_of_date"])


def get_current_balances(
    datalake: Optional[DataLake] = None,
    as_of: Optional[pd.Timestamp] = None,
) -> pd.DataFrame:
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

    Rows whose statement failed reconciliation (`reconciled is False`, see
    Gotcha #17/silver_transformer.py::_reconciled_flag) are excluded before
    the latest-row selection above, so a mismatched file's balance is never
    picked - the next-latest *reconciling* row wins instead ("stale but
    correct, rather than current but wrong"). If literally every row for an
    account has failed reconciliation, that account is silently absent from
    the result (logged via `logger.warning`, never fabricated). Any
    surviving row is flagged `balance_may_be_stale=True` when a newer,
    excluded statement exists for that account - i.e. the shown balance is
    real but not as current as what's actually on file.

    `as_of`, when given, restricts to rows dated on or before it before any
    other selection happens - "current" then means "as it stood on that
    date", giving a historical balance instead of today's. Used by
    get_net_worth's `as_of` param for month-over-month comparisons.
    """
    datalake = datalake or get_datalake()
    ledger = datalake.read_silver("account_ledger")
    if ledger is None or ledger.empty:
        return pd.DataFrame(columns=_BALANCES_COLUMNS)

    if as_of is not None:
        ledger = ledger[pd.to_datetime(ledger["as_of_date"]) <= as_of]
        if ledger.empty:
            return pd.DataFrame(columns=_BALANCES_COLUMNS)

    full_ledger = ledger
    if "reconciled" in ledger.columns:
        ledger = ledger[ledger["reconciled"] != False]  # noqa: E712

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

    latest["balance_may_be_stale"] = False
    if "reconciled" in full_ledger.columns:
        excluded_accounts = set(full_ledger["account_id"]) - set(latest["account_id"])
        if excluded_accounts:
            logger.warning(
                "get_current_balances: %d account(s) have zero reconciled "
                "rows and are excluded entirely: %s",
                len(excluded_accounts),
                sorted(excluded_accounts),
            )

        mismatched = full_ledger[full_ledger["reconciled"] == False]  # noqa: E712
        if not mismatched.empty:
            latest_mismatched = mismatched.groupby("account_id")["as_of_date"].max()
            newer_mismatch = latest["account_id"].map(latest_mismatched)
            latest["balance_may_be_stale"] = newer_mismatch.notna() & (
                newer_mismatch > latest["as_of_date"]
            )

    if "balance_source" not in latest.columns:
        latest["balance_source"] = "printed"

    # A build published before account_ledger gained a currency column
    # (P1.1) has none at all - tolerate that rather than KeyError on the
    # projection below, matching _assert_single_currency's deliberate
    # no-op when no currency information exists (an unknown currency is
    # never summed across, since dropna() excludes it there).
    if "currency" not in latest.columns:
        latest["currency"] = None

    return latest[_BALANCES_COLUMNS].reset_index(drop=True)


def get_net_worth(
    datalake: Optional[DataLake] = None,
    path: Optional[PathLike] = None,
    as_of: Optional[pd.Timestamp] = None,
) -> int:
    """Sum of current balances across accounts, sign-adjusted for
    account_type: credit accounts are a liability (subtracted), everything
    else - current/savings/investment - is an asset (added). Also includes
    holdings from the holdings table, which are always assets (never
    liabilities).

    Per CLAUDE.md Gotcha #6, a credit account's `balance` is already stored
    as a positive "amount owed" figure, not a negative one.

    `as_of`, when given, computes net worth as it stood on that date instead
    of today (see get_current_balances/get_latest_holdings_snapshot's `as_of`
    param) - e.g. for a month-over-month change indicator.

    Raises MixedCurrencyError if ledger balances/holdings span more than one
    currency - this function refuses to sum across currencies rather than
    silently returning a meaningless total.
    """
    datalake = datalake or get_datalake()
    balances = get_current_balances(datalake, as_of=as_of)
    latest_holdings = get_latest_holdings_snapshot(datalake, as_of=as_of)
    _assert_single_currency(balances, latest_holdings)

    total_minor = 0

    # Sum account ledger balances (with sign adjustment for credit accounts)
    if not balances.empty:
        accounts = build_accounts_table(path=path)
        merged = balances.merge(
            accounts[["account_id", "account_type"]], on="account_id", how="left"
        )

        for row in merged.itertuples():
            amount_minor = int(row.balance_minor)
            if row.account_type == "credit":
                total_minor -= amount_minor
            else:
                total_minor += amount_minor

    # Sum holdings (always assets, never liabilities).
    # Use the latest complete snapshot per account to avoid double-counting
    # across statements (holdings are re-printed on every statement).
    if not latest_holdings.empty:
        holdings_by_account = latest_holdings.groupby("account_id")[
            "total_value_minor"
        ].sum()
        for value_minor in holdings_by_account:
            total_minor += int(value_minor)

    return total_minor


def get_net_worth_breakdown(
    datalake: Optional[DataLake] = None, path: Optional[PathLike] = None
) -> pd.DataFrame:
    """Detailed breakdown of net worth by account/holding, including as_of_date
    and contribution to total net worth.

    Returns a DataFrame with columns: account_id, source, balance_or_value,
    as_of_date, contribution_to_net_worth, balance_may_be_stale. Each row is
    either a ledger balance or a holding fund. Rows are sorted by as_of_date
    (descending), then by contribution (descending for assets, ascending for
    liabilities). Holdings have no reconciliation concept, so their
    balance_may_be_stale is always False - see get_current_balances().

    Raises MixedCurrencyError if ledger balances/holdings span more than one
    currency - see get_net_worth().
    """
    datalake = datalake or get_datalake()
    rows = []

    balances = get_current_balances(datalake)
    latest_holdings = get_latest_holdings_snapshot(datalake)
    _assert_single_currency(balances, latest_holdings)

    # Add ledger-based rows (account balances)
    if not balances.empty:
        accounts = build_accounts_table(path=path)
        merged = balances.merge(
            accounts[["account_id", "account_type"]], on="account_id", how="left"
        )

        for row in merged.itertuples():
            amount_minor = int(row.balance_minor)
            contribution = (
                -amount_minor if row.account_type == "credit" else amount_minor
            )
            rows.append(
                {
                    "account_id": row.account_id,
                    "source": row.account_id,
                    "balance_or_value": amount_minor,
                    "as_of_date": row.as_of_date,
                    "contribution_to_net_worth": contribution,
                    "balance_may_be_stale": row.balance_may_be_stale,
                }
            )

    # Add holdings-based rows (latest snapshot per account).
    if not latest_holdings.empty:
        for holding_row in latest_holdings.itertuples():
            value_minor = int(holding_row.total_value_minor)
            rows.append(
                {
                    "account_id": holding_row.account_id,
                    "source": holding_row.fund_name,
                    "balance_or_value": value_minor,
                    "as_of_date": holding_row.as_of_date,
                    "contribution_to_net_worth": value_minor,
                    "balance_may_be_stale": False,
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
