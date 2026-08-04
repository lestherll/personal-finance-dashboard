"""Data-access helpers for the Streamlit dashboard.

Thin wrappers around transformers/balance.py and a couple of new ad hoc
DuckDB queries for the spending time series. Every function is
error-tolerant (returns empty data + a message instead of raising) since a
fresh data lake has no Silver build yet, and app.py needs to degrade
gracefully rather than crash.
"""

from typing import List, Optional, Tuple

import duckdb
import pandas as pd
import streamlit as st

from models.datalake import StaleSilverError, get_datalake
from transformers.account_config import build_accounts_table
from transformers.balance import (
    MixedCurrencyError,
    get_current_balances,
    get_net_worth,
    get_net_worth_breakdown,
)

AVAILABLE_MONTHS_SQL = """
    SELECT DISTINCT date_trunc('month', transaction_date) AS month
    FROM transactions
    ORDER BY month
"""

AVAILABLE_YEARS_SQL = """
    SELECT DISTINCT date_part('year', transaction_date)::INTEGER AS year
    FROM transactions
    ORDER BY year
"""


def _weekly_spending_sql(month: pd.Timestamp) -> str:
    return f"""
        SELECT date_trunc('week', transaction_date) AS week,
               sum(-amount_minor) AS spend_minor
        FROM transactions
        WHERE amount_minor < 0
          AND date_trunc('month', transaction_date) = DATE '{month.strftime("%Y-%m-%d")}'
        GROUP BY week
        ORDER BY week
    """


def _monthly_spending_sql(year: int) -> str:
    return f"""
        SELECT date_trunc('month', transaction_date) AS month,
               sum(-amount_minor) AS spend_minor
        FROM transactions
        WHERE amount_minor < 0
          AND date_part('year', transaction_date) = {int(year)}
        GROUP BY month
        ORDER BY month
    """


def _max_weekly_spend_for_year_sql(year: int) -> str:
    return f"""
        SELECT max(week_spend_minor) AS max_spend_minor
        FROM (
            SELECT sum(-amount_minor) AS week_spend_minor
            FROM transactions
            WHERE amount_minor < 0
              AND date_part('year', transaction_date) = {int(year)}
            GROUP BY date_trunc('week', transaction_date)
        )
    """


_MAX_MONTHLY_SPEND_SQL = """
    SELECT max(month_spend_minor) AS max_spend_minor
    FROM (
        SELECT sum(-amount_minor) AS month_spend_minor
        FROM transactions
        WHERE amount_minor < 0
        GROUP BY date_trunc('month', transaction_date)
    )
"""


@st.cache_data(ttl=300)
def get_net_worth_summary() -> Tuple[Optional[int], Optional[int], Optional[str]]:
    """(current_net_worth_minor, delta_vs_one_month_before_latest_data, error).

    The comparison point is one month before the *latest data actually on
    file* (max as_of_date across current balances), not wall-clock "today" -
    statements are ingested in batches well after the fact, so a comparison
    anchored to real time would compare the same one or two known statements
    against themselves and always show a misleading £0 change whenever
    nothing has been ingested in the last few days. Returns delta=None (no
    error) when there isn't yet a balance to anchor from at all.
    """
    try:
        datalake = get_datalake()
        current = get_net_worth(datalake)
        balances = get_current_balances(datalake)
        if balances.empty:
            return current, None, None
        latest_date = pd.to_datetime(balances["as_of_date"]).max()
        one_month_before = latest_date - pd.DateOffset(months=1)
        previous = get_net_worth(datalake, as_of=one_month_before)
        return current, current - previous, None
    except (MixedCurrencyError, StaleSilverError) as e:
        return None, None, str(e)


@st.cache_data(ttl=300)
def get_breakdown_table() -> Tuple[pd.DataFrame, Optional[str]]:
    """Net worth breakdown with display_name/account_type merged in."""
    try:
        datalake = get_datalake()
        breakdown = get_net_worth_breakdown(datalake)
        if breakdown.empty:
            return breakdown, None
        accounts = build_accounts_table()[
            ["account_id", "display_name", "account_type"]
        ]
        return breakdown.merge(accounts, on="account_id", how="left"), None
    except (MixedCurrencyError, StaleSilverError) as e:
        return pd.DataFrame(), str(e)


@st.cache_data(ttl=300)
def get_available_months() -> Tuple[List[pd.Timestamp], Optional[str]]:
    """Calendar months (first-of-month timestamps) that have transactions."""
    try:
        datalake = get_datalake()
        months = datalake.query(AVAILABLE_MONTHS_SQL)
        return list(pd.to_datetime(months["month"])), None
    except (StaleSilverError, duckdb.Error) as e:
        return [], str(e)


@st.cache_data(ttl=300)
def get_available_years() -> Tuple[List[int], Optional[str]]:
    """Calendar years that have transactions."""
    try:
        datalake = get_datalake()
        years = datalake.query(AVAILABLE_YEARS_SQL)
        return [int(y) for y in years["year"]], None
    except (StaleSilverError, duckdb.Error) as e:
        return [], str(e)


@st.cache_data(ttl=300)
def get_weekly_spending(month: pd.Timestamp) -> Tuple[pd.DataFrame, Optional[str]]:
    """Week-by-week spend (money out) for the given calendar month."""
    try:
        datalake = get_datalake()
        return datalake.query(_weekly_spending_sql(month)), None
    except (StaleSilverError, duckdb.Error) as e:
        return pd.DataFrame(columns=["week", "spend_minor"]), str(e)


@st.cache_data(ttl=300)
def get_monthly_spending(year: int) -> Tuple[pd.DataFrame, Optional[str]]:
    """Month-by-month spend (money out) for the given calendar year."""
    try:
        datalake = get_datalake()
        return datalake.query(_monthly_spending_sql(year)), None
    except (StaleSilverError, duckdb.Error) as e:
        return pd.DataFrame(columns=["month", "spend_minor"]), str(e)


@st.cache_data(ttl=300)
def get_max_weekly_spend_for_year(year: int) -> Tuple[int, Optional[str]]:
    """Highest single week's spend anywhere in the given year, in minor
    units - fixes the weekly chart's y-axis scale so switching between
    months within a year stays visually comparable instead of rescaling
    per month."""
    try:
        datalake = get_datalake()
        result = datalake.query(_max_weekly_spend_for_year_sql(year))
        max_spend = result["max_spend_minor"].iloc[0]
        return int(max_spend) if pd.notna(max_spend) else 0, None
    except (StaleSilverError, duckdb.Error) as e:
        return 0, str(e)


@st.cache_data(ttl=300)
def get_max_monthly_spend() -> Tuple[int, Optional[str]]:
    """Highest single month's spend across all years on file, in minor
    units - fixes the monthly chart's y-axis scale so switching between
    years stays visually comparable instead of rescaling per year."""
    try:
        datalake = get_datalake()
        result = datalake.query(_MAX_MONTHLY_SPEND_SQL)
        max_spend = result["max_spend_minor"].iloc[0]
        return int(max_spend) if pd.notna(max_spend) else 0, None
    except (StaleSilverError, duckdb.Error) as e:
        return 0, str(e)
