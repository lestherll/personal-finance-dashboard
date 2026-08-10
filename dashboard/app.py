"""Streamlit dashboard: an Overview tab (net worth breakdown, balance with a
month-over-month change indicator, and a spending time series you can slide
through by month or year), a Coverage tab (calendar-style statement-period
coverage per account), and an Upload tab (statement ingestion into Bronze,
unmapped-account registration, and a Silver rebuild trigger).

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

import hashlib  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402

import altair as alt  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from adapters.base import ReconciliationResult, StatementPeriod  # noqa: E402
from adapters.factory import AdapterFactory  # noqa: E402
from dashboard.data import (  # noqa: E402
    get_available_months,
    get_available_years,
    get_breakdown_table,
    get_coverage_calendar,
    get_coverage_gaps,
    get_max_monthly_spend,
    get_max_weekly_spend_for_year,
    get_monthly_spending,
    get_net_worth_summary,
    get_weekly_spending,
)
from ingestion_service import (  # noqa: E402
    STAGE_ALREADY_INGESTED,
    STAGE_BRONZE_FAILED,
    STAGE_COMPLETE,
    STAGE_DETECTION_FAILED,
    STAGE_PARSE_FAILED,
    STAGE_ZERO_RECORDS,
    ingest_file,
)
from models.datalake import get_datalake  # noqa: E402
from models.money import format_minor  # noqa: E402
from transformers.account_config import (  # noqa: E402
    ACCOUNT_TYPE_CHOICES,
    UnmappedAccountsError,
    find_unmapped_accounts,
    register_account,
    register_source_type_fallback,
)
from transformers.reconciliation_log import ReconciliationMismatchError  # noqa: E402
from transformers.silver_transformer import run_bronze_to_silver  # noqa: E402
from logging_config import setup_logging  # noqa: E402
from object_storage import log_s3_connectivity  # noqa: E402

setup_logging()


@st.cache_resource(show_spinner=False)
def _log_s3_connectivity_once() -> None:
    """Streamlit re-executes this whole module on every rerun (every widget
    interaction), not just once at process start - st.cache_resource is what
    makes this run exactly once per process instead of firing a HeadBucket
    call on every click. Logged, not surfaced in the UI: this is a startup
    diagnostic for `kubectl logs`, not something an end user needs to see.
    """
    log_s3_connectivity()


_log_s3_connectivity_once()


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


def _render_reconciliation(result: "ReconciliationResult | None") -> None:
    if (
        result is None
        or result.matches is None
        or result.expected_closing_minor is None
        or result.derived_closing_minor is None
    ):
        return
    if result.matches:
        st.success(
            "reconciles against printed closing balance "
            f"({format_minor(result.expected_closing_minor)})"
        )
    else:
        st.warning(
            f"balance mismatch: derived {format_minor(result.derived_closing_minor)} "
            f"vs statement's printed {format_minor(result.expected_closing_minor)} - "
            "balance figures on this statement may be inaccurate, check manually"
        )


def _render_reconciliations(results: "list[ReconciliationResult]") -> None:
    """Renders per-account reconciliation results (e.g. Vanguard's
    per-wrapper checks) - a file can carry more than one result here, each
    needing its check_name to stay distinguishable."""
    for result in results:
        if (
            result.matches is None
            or result.expected_closing_minor is None
            or result.derived_closing_minor is None
        ):
            continue
        if result.matches:
            st.success(
                f"{result.check_name} reconciles "
                f"({format_minor(result.expected_closing_minor)})"
            )
        else:
            st.warning(
                f"{result.check_name} mismatch: derived "
                f"{format_minor(result.derived_closing_minor)} vs statement's printed "
                f"{format_minor(result.expected_closing_minor)} - balance figures on "
                "this statement may be inaccurate, check manually"
            )


def _render_statement_period(period: "StatementPeriod | None") -> None:
    if period is None:
        return
    st.caption(
        f"statement period: {period.from_date.date()} to {period.to_date.date()}"
    )


def _render_ingest_outcome(display_name: str, outcome) -> None:
    has_mismatch = (
        outcome.reconciliation is not None and outcome.reconciliation.matches is False
    ) or any(r.matches is False for r in outcome.reconciliations)

    if outcome.stage in (
        STAGE_DETECTION_FAILED,
        STAGE_PARSE_FAILED,
        STAGE_BRONZE_FAILED,
    ):
        icon = "✗"
    elif outcome.stage == STAGE_ZERO_RECORDS or has_mismatch:
        icon = "⚠"
    else:
        icon = "✓"
    expanded = outcome.stage != STAGE_COMPLETE or has_mismatch

    with st.expander(f"{icon} {display_name}", expanded=expanded):
        if outcome.stage == STAGE_ALREADY_INGESTED:
            st.info(
                f"Already ingested previously (hash {outcome.file_hash[:12]}…) "
                f"— skipped re-processing. Status on file: "
                f"{outcome.manifest.status}, {outcome.manifest.record_count} record(s)."
            )
        elif outcome.stage == STAGE_DETECTION_FAILED:
            st.error(str(outcome.error))
        elif outcome.stage == STAGE_PARSE_FAILED:
            st.error(
                f"Recognized the format but failed to parse this file ({outcome.error})"
            )
        elif outcome.stage == STAGE_ZERO_RECORDS:
            st.warning("Adapter parsed 0 records")
        elif outcome.stage == STAGE_BRONZE_FAILED:
            st.error(f"Failed to publish Bronze ({outcome.error})")
        elif outcome.stage == STAGE_COMPLETE:
            st.success(f"{outcome.record_count} record(s) → {outcome.bronze_path}")
            st.caption(f"archived raw file → {outcome.manifest.raw_artifact_path}")
            _render_reconciliation(outcome.reconciliation)
            _render_reconciliations(outcome.reconciliations)
            _render_statement_period(outcome.statement_period)


# Status colors (fixed, never themed) - same hex clears 3:1 contrast on both
# light and dark chart surfaces, so no separate dark-mode step is needed.
# "no_data" uses the mode-invariant muted-ink tone, at reduced opacity so
# untracked months recede rather than reading as a third "equal" status.
_COVERAGE_STATUS_COLORS = {
    "covered": "#0ca30c",
    "gap": "#d03b3b",
    "no_data": "#898781",
}
_COVERAGE_STATUS_OPACITY = {"covered": 1.0, "gap": 1.0, "no_data": 0.3}
_COVERAGE_STATUS_LABELS = {
    "covered": "Covered",
    "gap": "Gap (missing statement)",
    "no_data": "No data expected",
}


def _render_coverage_calendar(calendar_df: pd.DataFrame) -> None:
    chart_df = calendar_df.copy()
    chart_df["month_label"] = chart_df["month"].dt.strftime("%b %Y")
    chart_df["status_label"] = chart_df["status"].map(_COVERAGE_STATUS_LABELS)
    month_order = list(dict.fromkeys(chart_df.sort_values("month")["month_label"]))
    account_order = sorted(chart_df["display_name"].unique())

    chart = (
        alt.Chart(chart_df)
        .mark_rect(cornerRadius=3)
        .encode(
            x=alt.X("month_label:N", sort=month_order, title=None),
            y=alt.Y("display_name:N", sort=account_order, title=None),
            color=alt.Color(
                "status:N",
                scale=alt.Scale(
                    domain=list(_COVERAGE_STATUS_COLORS.keys()),
                    range=list(_COVERAGE_STATUS_COLORS.values()),
                ),
                legend=alt.Legend(
                    title="Status",
                    orient="bottom",
                    labelExpr=(
                        "{'covered': 'Covered', 'gap': 'Gap (missing statement)', "
                        "'no_data': 'No data expected'}[datum.label]"
                    ),
                ),
            ),
            opacity=alt.Opacity(
                "status:N",
                scale=alt.Scale(
                    domain=list(_COVERAGE_STATUS_OPACITY.keys()),
                    range=list(_COVERAGE_STATUS_OPACITY.values()),
                ),
                legend=None,
            ),
            tooltip=[
                alt.Tooltip("display_name:N", title="Account"),
                alt.Tooltip("month_label:N", title="Month"),
                alt.Tooltip("status_label:N", title="Status"),
            ],
        )
    )
    st.altair_chart(chart, width="stretch")


st.set_page_config(page_title="Personal Finance Dashboard", layout="wide")
st.title("Personal Finance Dashboard")

tab_overview, tab_coverage, tab_upload = st.tabs(["Overview", "Coverage", "Upload"])

with tab_overview:
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

with tab_coverage:
    st.header("Statement Coverage")
    st.caption(
        "Which calendar months have an ingested statement per account. "
        "Only source types that print a statement period are tracked here "
        "(CSV exports like Monzo don't print one)."
    )

    calendar_df, calendar_error = get_coverage_calendar()
    if calendar_error:
        st.warning(f"Coverage calendar unavailable: {calendar_error}")
    elif calendar_df.empty:
        st.info("No statement-period data found yet.")
    else:
        _render_coverage_calendar(calendar_df)

        with st.expander("View as table"):
            pivot = calendar_df.pivot(
                index="display_name", columns="month", values="status"
            )
            pivot.columns = [c.strftime("%b %Y") for c in pivot.columns]
            st.dataframe(pivot, width="stretch")

        gaps_df, gaps_error = get_coverage_gaps()
        st.subheader("Gaps")
        if gaps_error:
            st.warning(f"Gap detail unavailable: {gaps_error}")
        elif gaps_df.empty:
            st.caption("No gaps between ingested statements.")
        else:
            display_gaps = gaps_df.copy()
            display_gaps["gap_start"] = display_gaps["gap_start"].dt.date
            display_gaps["gap_end"] = display_gaps["gap_end"].dt.date
            st.dataframe(
                display_gaps[["display_name", "gap_start", "gap_end", "days"]],
                width="stretch",
            )

with tab_upload:
    st.header("Upload a Statement")
    uploaded_files = st.file_uploader(
        "Upload bank statement files (CSV or PDF)",
        type=["csv", "pdf"],
        accept_multiple_files=True,
        key="statement_uploader",
    )

    if "ingest_outcomes" not in st.session_state:
        st.session_state["ingest_outcomes"] = {}

    datalake = get_datalake()
    factory = AdapterFactory()

    if uploaded_files:
        st.subheader("Upload results")

    for uploaded_file in uploaded_files or []:
        raw_bytes = uploaded_file.getvalue()
        file_hash = hashlib.sha256(raw_bytes).hexdigest()

        # Streamlit reruns this whole script on every widget interaction, and
        # st.file_uploader keeps returning the same UploadedFile objects for
        # as long as they're listed in the widget - without this session-state
        # cache, every unrelated rerun (e.g. moving the Overview tab's year
        # slider) would re-run ingest_file for every still-listed upload.
        if file_hash not in st.session_state["ingest_outcomes"]:
            tmp_dir = tempfile.mkdtemp(prefix="dashboard-upload-")
            outcome = None
            try:
                # Use the real filename (not a random temp basename) so
                # start_ingestion's manifest.original_filename is correct and
                # suffix-based CSV/PDF detection still works.
                tmp_path = Path(tmp_dir) / uploaded_file.name
                tmp_path.write_bytes(raw_bytes)
                try:
                    outcome = ingest_file(tmp_path, datalake, factory)
                except Exception as e:
                    st.error(
                        f"{uploaded_file.name}: unexpected error during ingestion ({e})"
                    )
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            if outcome is not None:
                st.session_state["ingest_outcomes"][file_hash] = outcome

        outcome = st.session_state["ingest_outcomes"].get(file_hash)
        if outcome is not None:
            _render_ingest_outcome(uploaded_file.name, outcome)

    st.header("Unmapped Accounts")
    # Deliberately uncached (unlike dashboard/data.py's helpers) - this
    # section exists to reflect accounts that appeared seconds ago from an
    # upload, so freshness matters more than the cost of re-scanning Bronze.
    unmapped_df = find_unmapped_accounts(datalake)
    if unmapped_df.empty:
        st.caption("No unmapped accounts.")
    else:
        st.warning(
            f"{len(unmapped_df)} account(s) need mapping before Silver can rebuild."
        )
        for row in unmapped_df.itertuples():
            form_key = (
                f"register_{row.source_type}_{row.account_identifier or 'fallback'}"
            )
            with st.form(key=form_key):
                st.write(
                    f"**source_type**: `{row.source_type}`  "
                    f"**account_identifier**: `{row.account_identifier}`"
                )
                st.caption(
                    f"sample: {row.sample_description!r} ({row.record_count} records)"
                )
                account_id = st.text_input("Account ID", key=f"{form_key}_account_id")
                display_name = st.text_input(
                    "Display name", key=f"{form_key}_display_name"
                )
                account_type = st.selectbox(
                    "Account type", ACCOUNT_TYPE_CHOICES, key=f"{form_key}_account_type"
                )
                if st.form_submit_button("Register"):
                    if not account_id or not display_name:
                        st.error("Account ID and display name are required.")
                    elif row.account_identifier is not None:
                        register_account(
                            row.account_identifier,
                            account_id,
                            display_name,
                            account_type,
                        )
                        st.success(
                            f"Registered {row.account_identifier!r} -> {account_id}"
                        )
                        st.rerun()
                    else:
                        register_source_type_fallback(
                            row.source_type, account_id, display_name, account_type
                        )
                        st.success(
                            f"Registered fallback for {row.source_type!r} -> {account_id}"
                        )
                        st.rerun()

    st.header("Rebuild Silver")
    strict = st.checkbox(
        "Strict reconciliation (refuse to publish on any mismatch)",
        value=False,
        help="Mirrors `cli.py silver rebuild --strict`.",
    )
    if st.button("Rebuild Silver"):
        try:
            result = run_bronze_to_silver(strict_reconciliation=strict)
        except UnmappedAccountsError as e:
            st.error(f"Cannot rebuild — accounts still unmapped:\n\n{e}")
        except ReconciliationMismatchError as e:
            st.error(f"Rebuild refused (strict mode):\n\n{e}")
        except Exception as e:
            st.error(f"Silver rebuild failed: {e}")
        else:
            st.success(f"Published Silver build {result['build_id']}")
            st.write(
                f"{len(result['transactions'])} transactions, "
                f"{len(result['account_ledger'])} ledger entries, "
                f"{len(result['holdings'])} holdings"
            )
            st.cache_data.clear()
            st.rerun()
