"""Personal finance dashboard CLI.

Usage:
    uv run python cli.py accounts list-unmapped
    uv run python cli.py accounts register <account_identifier> <account_id> <display_name> <account_type>
    uv run python cli.py accounts register-fallback <source_type> <account_id> <display_name> <account_type>
    uv run python cli.py accounts coverage
    uv run python cli.py accounts reconciliation
    uv run python cli.py ingest <file> [<file> ...]
"""

import hashlib
import sys
from pathlib import Path
from typing import List, Optional

import click
import pandas as pd

from adapters.base import ReconciliationResult, StatementPeriod
from adapters.factory import AdapterDetectionError, AdapterFactory
from models.datalake import get_datalake
from models.money import format_minor
from models.ingestion import (
    STATUS_BRONZE_FAILED,
    STATUS_COMPLETE,
    STATUS_PARSE_FAILED,
    start_ingestion,
    write_manifest,
)
from transformers.account_config import (
    find_unmapped_accounts,
    register_account,
    register_source_type_fallback,
)
from transformers.balance import get_net_worth_breakdown
from transformers.coverage import find_coverage_gaps, find_statement_periods
from transformers.reconciliation_status import find_reconciliation_status
from transformers.silver_transformer import run_bronze_to_silver
from models.build import list_builds, current_build_id

ACCOUNT_TYPE_CHOICES = click.Choice(["current", "credit", "investment", "savings"])


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
    if result is None or result.matches is None:
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
        if result.matches is None:
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
        raw_bytes = path.read_bytes()
        file_hash = hashlib.sha256(raw_bytes).hexdigest()
        manifest = start_ingestion(path, file_hash)
        if manifest.status == STATUS_COMPLETE:
            click.echo(f"✓ {path.name}: already ingested ({file_hash})")
            continue
        content = (
            raw_bytes.decode("utf-8-sig")
            if path.suffix.lower() == ".csv"
            else raw_bytes
        )

        try:
            result = factory.ingest(content, path.name, file_hash)
        except AdapterDetectionError as e:
            manifest.status = STATUS_PARSE_FAILED
            manifest.error = str(e)
            write_manifest(manifest)
            click.echo(f"✗ {path.name}: {e}")
            had_failure = True
            continue
        except ValueError as e:
            manifest.status = STATUS_PARSE_FAILED
            manifest.error = str(e)
            write_manifest(manifest)
            click.echo(
                f"✗ {path.name}: recognized the format but failed to parse "
                f"this file ({e})"
            )
            had_failure = True
            continue

        records = result.records
        manifest.source_type = result.source_type or records[0].source_type
        manifest.adapter = result.adapter or "test/legacy-adapter"
        manifest.parser_version = result.parser_version
        if not records:
            manifest.status = STATUS_PARSE_FAILED
            manifest.error = "Adapter parsed zero records"
            write_manifest(manifest)
            click.echo(f"⚠ {path.name}: parsed 0 records")
            had_failure = True
            continue

        df = pd.DataFrame(
            [
                {
                    "source_key": r.source_key,
                    "raw_data": r.raw_data,
                    "account_identifier": r.account_identifier,
                    "record_type": r.record_type,
                    "file_hash": r.file_hash,
                    "line_number": r.line_number,
                    "bronze_record_id": r.bronze_record_id,
                    "source_ordinal": r.source_ordinal,
                }
                for r in records
            ]
        )
        try:
            filepath = datalake.write_bronze(
                manifest,
                df,
                reconciliation=result.reconciliation,
                statement_period=result.statement_period,
                reconciliations=result.reconciliations,
            )
        except Exception as e:
            manifest.status = STATUS_BRONZE_FAILED
            manifest.error = str(e)
            write_manifest(manifest)
            click.echo(f"✗ {path.name}: failed to publish Bronze ({e})")
            had_failure = True
            continue

        manifest.status = STATUS_COMPLETE
        manifest.record_count = len(records)
        manifest.bronze_path = filepath
        manifest.error = None
        write_manifest(manifest)
        click.echo(f"✓ {path.name}: {len(records)} record(s) -> {filepath}")
        click.echo(f"  archived raw file -> {manifest.raw_artifact_path}")
        _echo_reconciliation(result.reconciliation)
        _echo_reconciliations(result.reconciliations)
        _echo_statement_period(result.statement_period)

        if result.reconciliation is not None and result.reconciliation.matches is False:
            had_failure = True
        if any(r.matches is False for r in result.reconciliations):
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
def reconciliation():
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

    for account_id, group in statuses.groupby("account_id"):
        click.echo(f"{account_id}:")
        for row in group.itertuples():
            if row.matches:
                click.echo(
                    f"  ✓ {row.filename}: reconciles ({format_minor(row.expected_closing)})"
                )
            else:
                click.echo(
                    f"  ⚠ {row.filename}: mismatch - derived {format_minor(row.derived_closing)} "
                    f"vs printed {format_minor(row.expected_closing)}"
                )

@cli.group()
def silver():
    """Manage Silver layer rebuilds."""


@silver.command("rebuild")
def silver_rebuild():
    """Rebuild Silver from the current immutable Bronze set."""
    result = run_bronze_to_silver()
    build_id = result["build_id"]
    click.echo(f"Published Silver build {build_id}")
    click.echo(
        f"  {result['transactions'].shape[0] if hasattr(result['transactions'], 'shape') else len(result['transactions'])} transactions, "
        f"{len(result['account_ledger'])} ledger entries, "
        f"{len(result['holdings'])} holdings"
    )


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


@accounts.command("breakdown")
def get_breakdown():
    dl = get_datalake()
    breakdown = get_net_worth_breakdown(dl)

    import pandas as pd

    pd.set_option("display.max_rows", None)
    pd.set_option("display.max_columns", None)

    click.echo(breakdown[["account_id", "as_of_date", "contribution_to_net_worth"]])

    click.echo("\nTotal contribution to net worth:")
    #click.echo(breakdown[["contribution_to_net_worth"]].aggregate({"contribution_to_net_worth": np.sum}, axis=0))
    click.echo(f"£{breakdown[["contribution_to_net_worth"]].sum().values[0]}")



if __name__ == "__main__":
    cli()
