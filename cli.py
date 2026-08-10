"""Personal finance dashboard CLI.

Usage:
    uv run python cli.py accounts list-unmapped
    uv run python cli.py accounts register <account_identifier> <account_id> <display_name> <account_type>
    uv run python cli.py accounts register-fallback <source_type> <account_id> <display_name> <account_type>
    uv run python cli.py accounts coverage
    uv run python cli.py accounts reconciliation [--fail-on-mismatch]
    uv run python cli.py accounts continuity [--fail-on-mismatch]
    uv run python cli.py accounts reconciliation-log
    uv run python cli.py ingest <file> [<file> ...]
    uv run python cli.py silver rebuild [--strict]
    uv run python cli.py silver builds
"""

import sys
from pathlib import Path
from typing import List, Optional

import click
import pandas as pd

from adapters.base import ReconciliationResult, StatementPeriod
from adapters.factory import AdapterFactory
from ingestion_service import (
    STAGE_ALREADY_INGESTED,
    STAGE_BRONZE_FAILED,
    STAGE_COMPLETE,
    STAGE_DETECTION_FAILED,
    STAGE_PARSE_FAILED,
    STAGE_ZERO_RECORDS,
    IngestOutcome,
    ingest_file,
)
from models.datalake import get_datalake
from models.money import format_minor
from models.ingestion import load_manifest, start_ingestion, write_manifest
from transformers.account_config import (
    ACCOUNT_TYPE_CHOICES as _ACCOUNT_TYPE_CHOICES,
    build_accounts_table,
    find_unmapped_accounts,
    register_account,
    register_source_type_fallback,
)
from transformers.balance import get_net_worth_breakdown
from transformers.continuity import find_balance_continuity
from transformers.coverage import find_coverage_gaps, find_statement_periods
from transformers.reconciliation_log import ReconciliationMismatchError
from transformers.reconciliation_status import find_reconciliation_status
from transformers.silver_transformer import run_bronze_to_silver
from models.build import list_builds, current_build_id

ACCOUNT_TYPE_CHOICES = click.Choice(_ACCOUNT_TYPE_CHOICES)


@click.group()
def cli():
    """Personal finance dashboard CLI."""


@cli.group()
def accounts():
    """Manage the account_identifier -> canonical account mapping."""


@accounts.command("list-unmapped")
def list_unmapped():
    """List Bronze accounts that have no entry in the account map yet."""
    datalake = get_datalake()
    df = find_unmapped_accounts(datalake)

    if df.empty:
        click.echo("No unmapped accounts found.")
        return

    click.echo(f"{len(df)} unmapped account(s):\n")
    for row in df.itertuples():
        click.echo(
            f"  source_type={row.source_type!r}\n"
            f"  account_identifier={row.account_identifier!r}\n"
            f"  sample: {row.sample_description!r} ({row.record_count} records)\n"
        )
    click.echo(
        "Register with:\n"
        "  uv run python cli.py accounts register <account_identifier> <account_id> "
        "<display_name> <current|credit|investment|savings>"
    )


@accounts.command("register")
@click.argument("account_identifier")
@click.argument("account_id")
@click.argument("display_name")
@click.argument("account_type", type=ACCOUNT_TYPE_CHOICES)
def register(account_identifier, account_id, display_name, account_type):
    """Map a hashed account_identifier (from list-unmapped) to a canonical account."""
    register_account(account_identifier, account_id, display_name, account_type)
    click.echo(f"Registered {account_identifier!r} -> {account_id} ({display_name})")


@accounts.command("register-fallback")
@click.argument("source_type")
@click.argument("account_id")
@click.argument("display_name")
@click.argument("account_type", type=ACCOUNT_TYPE_CHOICES)
def register_fallback(source_type, account_id, display_name, account_type):
    """Map a source_type with no extractable identifier (e.g. Monzo CSV) to an account."""
    register_source_type_fallback(source_type, account_id, display_name, account_type)
    click.echo(
        f"Registered fallback for {source_type!r} -> {account_id} ({display_name})"
    )


def _echo_reconciliation(result: Optional[ReconciliationResult]) -> None:
    if (
        result is None
        or result.matches is None
        or result.expected_closing_minor is None
        or result.derived_closing_minor is None
    ):
        return
    if result.matches:
        click.echo(
            f"  ✓ reconciles against printed closing balance "
            f"({format_minor(result.expected_closing_minor)})"
        )
    else:
        click.echo(
            f"  ⚠ balance mismatch: derived {format_minor(result.derived_closing_minor)} vs "
            f"statement's printed {format_minor(result.expected_closing_minor)} - balance "
            "figures on this statement may be inaccurate, check manually"
        )


def _echo_reconciliations(results: List[ReconciliationResult]) -> None:
    """Echo per-account reconciliation results (e.g. Vanguard's per-wrapper
    checks) - a separate function from _echo_reconciliation since a file can
    carry more than one result here, each needing its check_name to stay
    distinguishable."""
    for result in results:
        if (
            result.matches is None
            or result.expected_closing_minor is None
            or result.derived_closing_minor is None
        ):
            continue
        if result.matches:
            click.echo(
                f"  ✓ {result.check_name} reconciles "
                f"({format_minor(result.expected_closing_minor)})"
            )
        else:
            click.echo(
                f"  ⚠ {result.check_name} mismatch: derived "
                f"{format_minor(result.derived_closing_minor)} vs statement's printed "
                f"{format_minor(result.expected_closing_minor)} - balance figures on this "
                "statement may be inaccurate, check manually"
            )


def _echo_statement_period(period: Optional[StatementPeriod]) -> None:
    if period is None:
        return
    click.echo(
        f"  statement period: {period.from_date.date()} to {period.to_date.date()}"
    )


def _echo_ingest_outcome(path: Path, outcome: IngestOutcome) -> None:
    if outcome.stage == STAGE_ALREADY_INGESTED:
        click.echo(f"✓ {path.name}: already ingested ({outcome.file_hash})")
    elif outcome.stage == STAGE_DETECTION_FAILED:
        click.echo(f"✗ {path.name}: {outcome.error}")
    elif outcome.stage == STAGE_PARSE_FAILED:
        click.echo(
            f"✗ {path.name}: recognized the format but failed to parse "
            f"this file ({outcome.error})"
        )
    elif outcome.stage == STAGE_ZERO_RECORDS:
        click.echo(f"⚠ {path.name}: parsed 0 records")
    elif outcome.stage == STAGE_BRONZE_FAILED:
        click.echo(f"✗ {path.name}: failed to publish Bronze ({outcome.error})")
    elif outcome.stage == STAGE_COMPLETE:
        click.echo(
            f"✓ {path.name}: {outcome.record_count} record(s) -> {outcome.bronze_path}"
        )
        click.echo(f"  archived raw file -> {outcome.manifest.raw_artifact_path}")
        _echo_reconciliation(outcome.reconciliation)
        _echo_reconciliations(outcome.reconciliations)
        _echo_statement_period(outcome.statement_period)


@cli.command("ingest")
@click.argument(
    "files", nargs=-1, required=True, type=click.Path(exists=True, dir_okay=False)
)
def ingest(files):
    """Parse statement files and write raw records to the Bronze layer."""
    datalake = get_datalake()
    factory = AdapterFactory()
    had_failure = False

    for file_arg in files:
        path = Path(file_arg)
        outcome = ingest_file(
            path,
            datalake,
            factory,
            start_ingestion_fn=start_ingestion,
            write_manifest_fn=write_manifest,
        )
        _echo_ingest_outcome(path, outcome)
        if outcome.is_failure():
            had_failure = True

    if had_failure:
        sys.exit(1)


@accounts.command("coverage")
def coverage():
    """List ingested statement periods per account and flag gaps between them."""
    datalake = get_datalake()
    periods = find_statement_periods(datalake)

    if periods.empty:
        click.echo(
            "No statement-period data found yet (PDF sources track periods; "
            "CSV sources - monzo - don't print one)."
        )
        return

    gaps = find_coverage_gaps(periods)

    for account_id, group in periods.groupby("account_id"):
        click.echo(f"{account_id}:")
        for row in group.sort_values("period_from").itertuples():
            click.echo(
                f"  {row.period_from.date()} to {row.period_to.date()}  "
                f"({row.filename})"
            )
        account_gaps = gaps[gaps["account_id"] == account_id]
        for gap in account_gaps.itertuples():
            click.echo(
                f"  ⚠ gap: {gap.gap_start.date()} to {gap.gap_end.date()} "
                f"({gap.days} days uncovered)"
            )


@accounts.command("reconciliation")
@click.option(
    "--fail-on-mismatch",
    is_flag=True,
    default=False,
    help="Exit non-zero if any file's reconciliation mismatches - for "
    "scripting/CI use, independent of the Silver-build strict gate.",
)
def reconciliation(fail_on_mismatch):
    """Show balance-reconciliation status per account."""
    datalake = get_datalake()
    statuses = find_reconciliation_status(datalake)

    if statuses.empty:
        click.echo(
            "No reconciliation data found yet (only amex, firstdirect, kroo, "
            "natwest-statement, chase, monzo-flex, monzo-pdf, and "
            "vanguard-pdf sources self-check a balance anchor)."
        )
        return

    any_mismatch = False
    for account_id, group in statuses.groupby("account_id"):
        click.echo(f"{account_id}:")
        for row in group.itertuples():
            if row.matches:
                click.echo(
                    f"  ✓ {row.filename}: reconciles ({format_minor(row.expected_closing_minor)})"
                )
            else:
                any_mismatch = True
                click.echo(
                    f"  ⚠ {row.filename}: mismatch - derived {format_minor(row.derived_closing_minor)} "
                    f"vs printed {format_minor(row.expected_closing_minor)}"
                )

    if fail_on_mismatch and any_mismatch:
        sys.exit(1)


@accounts.command("continuity")
@click.option(
    "--fail-on-mismatch",
    is_flag=True,
    default=False,
    help="Exit non-zero if any consecutive pair of statements breaks "
    "continuity - for scripting/CI use, independent of the Silver-build "
    "strict gate.",
)
def continuity(fail_on_mismatch):
    """Check that each account's consecutive statements connect (closing
    balance of one file matches the opening balance of the next)."""
    datalake = get_datalake()
    breaks = find_balance_continuity(datalake)

    if breaks.empty:
        click.echo(
            "No continuity data found yet (needs at least two anchored "
            "statements for the same account - see 'accounts reconciliation')."
        )
        return

    any_mismatch = False
    for account_id, group in breaks.groupby("account_id"):
        click.echo(f"{account_id}:")
        for row in group.itertuples():
            if row.matches is True:
                click.echo(f"  ✓ {row.filename} -> {row.next_filename}: continuous")
            elif row.gap_related:
                click.echo(
                    f"  ? {row.filename} -> {row.next_filename}: known coverage "
                    "gap between statements, not comparable"
                )
            elif row.matches is None:
                click.echo(
                    f"  ? {row.filename} -> {row.next_filename}: no opening/"
                    "closing anchor on one side, not comparable"
                )
            else:
                any_mismatch = True
                click.echo(
                    f"  ⚠ {row.filename} -> {row.next_filename}: mismatch - "
                    f"closing {format_minor(row.expected_closing_minor)} vs next "
                    f"opening {format_minor(row.expected_opening_minor)}"
                )

    if fail_on_mismatch and any_mismatch:
        sys.exit(1)


@accounts.command("reconciliation-log")
def reconciliation_log():
    """Show the persisted, build-versioned reconciliation history (all
    three check types: bronze self-check, continuity, Silver rollforward) -
    distinct from 'accounts reconciliation'/'accounts continuity', which
    query live Bronze and don't require a Silver build to have run."""
    datalake = get_datalake()
    log = datalake.read_silver("reconciliation_log")

    if log is None or log.empty:
        click.echo(
            "No reconciliation log found yet - run 'cli.py silver rebuild' "
            "at least once."
        )
        return

    for check_type, group in log.groupby("check_type"):
        click.echo(f"{check_type}:")
        for matches, sub_group in group.groupby("matches", dropna=False):
            if pd.isna(matches):
                label = "inconclusive"
            else:
                label = "match" if bool(matches) else "mismatch"
            click.echo(f"  {label}: {len(sub_group)}")


@cli.group()
def silver():
    """Manage Silver layer rebuilds."""


@silver.command("rebuild")
@click.option(
    "--strict",
    "strict_reconciliation",
    is_flag=True,
    default=False,
    help=(
        "Refuse to publish a new build if any reconciliation check "
        "(bronze self-check/continuity/silver rollforward) mismatches. "
        "Gates on every historical mismatch in the current Bronze set, "
        "not just newly-ingested files."
    ),
)
def silver_rebuild(strict_reconciliation):
    """Rebuild Silver from the current immutable Bronze set."""
    try:
        result = run_bronze_to_silver(strict_reconciliation=strict_reconciliation)
    except ReconciliationMismatchError as e:
        click.echo(f"✗ {e}")
        sys.exit(1)

    build_id = result["build_id"]
    click.echo(f"Published Silver build {build_id}")
    click.echo(
        f"  {result['transactions'].shape[0] if hasattr(result['transactions'], 'shape') else len(result['transactions'])} transactions, "
        f"{len(result['account_ledger'])} ledger entries, "
        f"{len(result['holdings'])} holdings"
    )

    log = result["reconciliation_log"]
    if not log.empty:
        click.echo("  reconciliation summary:")
        for (check_type, matches), count in (
            log.groupby(["check_type", "matches"], dropna=False).size().items()
        ):
            if pd.isna(matches):
                label = "inconclusive"
            else:
                label = "match" if bool(matches) else "mismatch"
            click.echo(f"    {check_type}: {count} {label}")


@silver.command("builds")
def silver_builds():
    """List published Silver builds."""
    current = current_build_id()
    builds = list_builds()
    if not builds:
        click.echo("No Silver builds published yet.")
        return

    for b in builds:
        bid = b["build_id"]
        marker = " *" if bid == current else ""
        rows = b.get("row_counts", {})
        txn = rows.get("transactions", 0)
        led = rows.get("account_ledger", 0)
        hold = rows.get("holdings", 0)
        click.echo(f"  {bid}{marker}  {txn}t / {led}l / {hold}h")


def _holding_label(display_name: str) -> str:
    """Wrapper-level label for a non-cash holding row, e.g. 'Vanguard ISA'
    -> 'Stocks & Shares ISA'. Individual funds within an account aren't
    broken out - holdings can change over time, the wrapper doesn't."""
    lowered = display_name.lower()
    if "isa" in lowered:
        return "Stocks & Shares ISA"
    if "pension" in lowered:
        return "Pension"
    return "Holdings"


@accounts.command("breakdown")
def get_breakdown():
    dl = get_datalake()
    breakdown = get_net_worth_breakdown(dl)

    if breakdown.empty:
        click.echo("No account data yet.")
        return

    account_info = build_accounts_table()[
        ["account_id", "display_name", "account_type"]
    ]
    breakdown = breakdown.merge(account_info, on="account_id", how="left")
    breakdown["display_name"] = breakdown["display_name"].fillna(
        breakdown["account_id"]
    )

    is_ledger_row = breakdown["source"] == breakdown["account_id"]
    is_cash_row = breakdown["source"] == "Cash account"
    is_fund_row = ~is_ledger_row & ~is_cash_row

    ledger_rows = breakdown[is_ledger_row].copy()
    ledger_rows["Detail"] = ledger_rows["account_type"].str.capitalize()
    cash_rows = breakdown[is_cash_row].assign(Detail="Cash account")

    fund_rows = breakdown[is_fund_row].copy()
    fund_rows["Detail"] = fund_rows["display_name"].map(_holding_label)
    fund_rows = fund_rows.groupby(
        ["account_id", "display_name", "Detail"], as_index=False
    ).agg(
        balance_or_value=("balance_or_value", "sum"),
        contribution_to_net_worth=("contribution_to_net_worth", "sum"),
        as_of_date=("as_of_date", "max"),
    )

    combined = pd.concat([ledger_rows, cash_rows, fund_rows], ignore_index=True)
    combined = combined.sort_values(
        ["as_of_date", "contribution_to_net_worth"], ascending=[False, False]
    )

    table = pd.DataFrame(
        {
            "Account": combined["display_name"],
            "Detail": combined["Detail"],
            "Balance/Value": combined["balance_or_value"].map(_fmt_minor),
            "As Of": pd.to_datetime(combined["as_of_date"]).dt.strftime("%Y-%m-%d"),
            "Contribution": combined["contribution_to_net_worth"].map(_fmt_minor),
        }
    )

    pd.set_option("display.max_rows", None)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", None)

    click.echo(table.to_string(index=False))

    click.echo("\nTotal contribution to net worth:")
    click.echo(_fmt_minor(int(breakdown["contribution_to_net_worth"].sum())))


@cli.group()
def ingestions():
    """Manage individual ingestions (quarantine, override)."""


@ingestions.command("list")
def ingestions_list():
    """List all known ingestion manifests."""
    import json
    from models.ingestion import INGESTIONS_DIR

    manifests = sorted(
        [p for p in INGESTIONS_DIR.glob("*.json") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not manifests:
        click.echo("No ingestion manifests found.")
        return

    for mp in manifests:
        try:
            m = json.loads(mp.read_text())
        except json.JSONDecodeError:
            click.echo(f"  {mp.stem}: unparseable manifest")
            continue
        iid = m.get("ingestion_id", mp.stem)[:12]
        status = m.get("status", "?")
        st = m.get("source_type") or "?"
        rec_match = m.get("reconciliation_matches")
        flagged = "!"
        if rec_match is True:
            flagged = "✓"
        elif rec_match is None:
            flagged = " "
        elif rec_match is False:
            override_flag = ""
            if m.get("promotion_override", {}).get("decision") == "allow":
                override_flag = " (overridden)"
            flagged = "⚠"
            flagged += override_flag
        click.echo(f"  {iid}  {st:20s}  {status:14s}  {flagged}")


@ingestions.command("show")
@click.argument("ingestion_id_prefix")
def ingestions_show(ingestion_id_prefix):
    """Show a full ingestion manifest."""
    from models.ingestion import INGESTIONS_DIR

    for mp in INGESTIONS_DIR.glob("*.json"):
        if mp.stem.startswith(ingestion_id_prefix):
            click.echo(mp.read_text())
            return
    click.echo(f"No manifest found with id prefix {ingestion_id_prefix!r}")


@ingestions.command("quarantined")
def ingestions_quarantined():
    """List quarantined ingestions (reconciliation mismatch, no override)."""
    import json
    from models.ingestion import INGESTIONS_DIR

    found = False
    for mp in sorted(
        INGESTIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    ):
        m = json.loads(mp.read_text())
        rec_match = m.get("reconciliation_matches")
        recs = m.get("reconciliations", [])
        any_mismatch = (rec_match is False) or any(
            r.get("matches") is False for r in recs
        )
        if not any_mismatch:
            continue
        override = m.get("promotion_override", {})
        if override.get("decision") == "allow":
            continue
        found = True
        iid = m.get("ingestion_id", mp.stem)[:12]
        st = m.get("source_type") or "?"
        check = m.get("reconciliation_check_name") or ""
        expected = m.get("reconciliation_expected_minor")
        derived = m.get("reconciliation_derived_minor")
        click.echo(f"  {iid}  {st}  {check}")
        if expected is not None and derived is not None:
            click.echo(
                f"    expected: {_fmt_minor(expected)}  derived: {_fmt_minor(derived)}"
            )
        for rec in recs:
            if rec.get("matches") is False:
                click.echo(
                    f"    {rec['check_name']}: expected {_fmt_minor(rec.get('expected_closing_minor'))}  derived {_fmt_minor(rec.get('derived_closing_minor'))}"
                )
    if not found:
        click.echo("No quarantined ingestions.")


@ingestions.command("override")
@click.argument("ingestion_id_prefix")
@click.option(
    "--allow",
    "decision",
    flag_value="allow",
    help="Allow this ingestion into Silver despite reconciliation mismatch",
)
@click.option("--reason", default="manual override", help="Reason for the override")
def ingestions_override(ingestion_id_prefix, decision, reason):
    """Override a quarantined ingestion's promotion decision."""
    from models.ingestion import INGESTIONS_DIR

    for mp in INGESTIONS_DIR.glob("*.json"):
        if mp.stem.startswith(ingestion_id_prefix):
            manifest = load_manifest(mp.stem)
            if manifest is None:
                click.echo(f"Failed to load manifest {mp.stem}")
                return
            from datetime import datetime, timezone

            manifest.promotion_override = {
                "decision": decision,
                "reason": reason,
                "at": datetime.now(timezone.utc).isoformat(),
            }
            write_manifest(manifest)
            click.echo(
                f"Overridden {manifest.ingestion_id[:12]}: {decision} "
                f"(reason: {reason})"
            )
            return
    click.echo(f"No manifest found with id prefix {ingestion_id_prefix!r}")


def _fmt_minor(minor: int) -> str:
    """Format minor units for CLI display."""
    if minor is None:
        return "None"
    from models.money import format_minor

    return format_minor(minor)


if __name__ == "__main__":
    cli()
