"""Basic Streamlit dashboard: net worth breakdown, balance (with a
month-over-month change indicator), a spending time series you can slide
through by month or year, and a placeholder file upload (not wired to
ingestion yet).

Run with:
    uv sync --extra dashboard
    uv run streamlit run dashboard/app.py
"""

import sys
from pathlib import Path

# Streamlit runs this script standalone, adding only its own directory
# (dashboard/) to sys.path - the repo root (needed for `dashboard.*` and
# `models`/`transformers`) has to be added explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import altair as alt  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from dashboard.data import (  # noqa: E402
    get_available_months,
    get_available_years,
    get_breakdown_table,
    get_max_monthly_spend,
    get_max_weekly_spend_for_year,
    get_monthly_spending,
    get_net_worth_summary,
    get_weekly_spending,
)
from models.money import format_minor  # noqa: E402


def _format_signed_minor(minor: int) -> str:
    """A leading +/- sign before the currency symbol, e.g. '+£12.34' /
    '-£12.34' - unlike format_minor's own '£-12.34', this is what
    st.metric's delta needs to detect direction and color correctly."""
    sign = "-" if minor < 0 else "+"
    return f"{sign}{format_minor(abs(minor))}"


def _render_spend_bar_chart(
    df: pd.DataFrame, x_col: str, y_col: str, y_max_minor: int
) -> None:
    """Bar chart with a fixed y-axis domain, so switching the slider between
    periods rescales the x-axis labels but never the y-axis - spend amounts
    stay visually comparable across periods instead of every period
    rescaling to fill the chart."""
    y_max_major = max(y_max_minor, 100) / 100  # at least £1 to avoid a zero-domain
    chart = (
        alt.Chart(df)
        .mark_bar()
        .encode(
            x=alt.X(f"{x_col}:N", sort=None, title=None),
            y=alt.Y(
                f"{y_col}:Q",
                title="Spend (£)",
                scale=alt.Scale(domain=[0, y_max_major * 1.1]),
            ),
        )
    )
    st.altair_chart(chart, width="stretch")


st.set_page_config(page_title="Personal Finance Dashboard", layout="wide")
st.title("Personal Finance Dashboard")

net_worth_minor, net_worth_delta_minor, net_worth_error = get_net_worth_summary()
if net_worth_error:
    st.warning(f"Net worth unavailable: {net_worth_error}")
elif net_worth_minor is None:
    st.info("No Silver build found yet — run `cli.py silver rebuild`.")
elif net_worth_delta_minor is None:
    st.metric("Net Worth", format_minor(net_worth_minor))
else:
    st.metric(
        "Net Worth",
        format_minor(net_worth_minor),
        delta=f"{_format_signed_minor(net_worth_delta_minor)} vs last month",
        help="Change vs net worth one month before the latest statement on file",
    )

st.header("Breakdown")
breakdown_df, breakdown_error = get_breakdown_table()
if breakdown_error:
    st.warning(f"Breakdown unavailable: {breakdown_error}")
elif breakdown_df.empty:
    st.info("No accounts to show yet.")
else:
    display_df = breakdown_df.copy()
    display_df["balance"] = display_df["balance_or_value"].map(format_minor)
    display_df["contribution"] = display_df["contribution_to_net_worth"].map(
        format_minor
    )
    st.dataframe(
        display_df[
            [
                "display_name",
                "account_type",
                "source",
                "balance",
                "contribution",
                "as_of_date",
                "balance_may_be_stale",
            ]
        ],
        width="stretch",
    )

st.header("Spending")

years, years_error = get_available_years()
if years_error:
    st.warning(f"Spending time series unavailable: {years_error}")
elif not years:
    st.info("No spending data yet.")
else:
    # Year is the outer constraint: the year slider only ever offers years
    # that actually have data, one step at a time.
    selected_year = st.select_slider("Year", options=years, value=years[-1])
    granularity = st.radio(
        "Group by",
        ["Week (within a month)", "Month (within the year)"],
        horizontal=True,
    )

    if granularity == "Week (within a month)":
        # The month slider is bounded to the selected year's own 12 months
        # (Jan-Dec), not the full multi-year history - one step = one month,
        # never crossing a year boundary.
        year_months = list(
            pd.date_range(f"{selected_year}-01-01", periods=12, freq="MS")
        )
        available_months, _ = get_available_months()
        months_with_data = [m for m in available_months if m.year == selected_year]
        default_month = months_with_data[-1] if months_with_data else year_months[0]

        selected_month = st.select_slider(
            "Month",
            options=year_months,
            value=default_month,
            format_func=lambda m: m.strftime("%b"),
        )
        spending_df, spending_error = get_weekly_spending(selected_month)
        max_spend_minor, max_error = get_max_weekly_spend_for_year(selected_year)
        if spending_error or max_error:
            st.warning(
                f"Spending time series unavailable: {spending_error or max_error}"
            )
        elif spending_df.empty:
            st.info(f"No spending recorded in {selected_month.strftime('%B %Y')}.")
        else:
            chart_df = spending_df.copy()
            chart_df["label"] = chart_df["week"].dt.strftime("Week of %b %d")
            chart_df["spend"] = chart_df["spend_minor"] / 100
            _render_spend_bar_chart(chart_df, "label", "spend", max_spend_minor)
    else:
        spending_df, spending_error = get_monthly_spending(selected_year)
        max_spend_minor, max_error = get_max_monthly_spend()
        if spending_error or max_error:
            st.warning(
                f"Spending time series unavailable: {spending_error or max_error}"
            )
        elif spending_df.empty:
            st.info(f"No spending recorded in {selected_year}.")
        else:
            chart_df = spending_df.copy()
            chart_df["label"] = chart_df["month"].dt.strftime("%b")
            chart_df["spend"] = chart_df["spend_minor"] / 100
            _render_spend_bar_chart(chart_df, "label", "spend", max_spend_minor)

st.header("Upload a Statement")
uploaded_files = st.file_uploader(
    "Upload bank statement files (CSV or PDF)",
    type=["csv", "pdf"],
    accept_multiple_files=True,
)
for uploaded_file in uploaded_files or []:
    st.success(f"Received {uploaded_file.name} — not processed yet.")
