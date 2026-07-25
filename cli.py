"""Personal finance dashboard CLI.

Usage:
    uv run python cli.py accounts list-unmapped
    uv run python cli.py accounts register <account_identifier> <account_id> <display_name> <account_type>
    uv run python cli.py accounts register-fallback <source_type> <account_id> <display_name> <account_type>
    uv run python cli.py ingest <file> [<file> ...]
"""

import hashlib
import shutil
from pathlib import Path

import click
import pandas as pd

from adapters.factory import AdapterFactory
from config import RAW_DIR
from models.datalake import get_datalake
from transformers.account_config import (
    find_unmapped_accounts,
    register_account,
    register_source_type_fallback,
)

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


def _archive_raw_file(path: Path, source_type: str, file_hash: str) -> str:
    """Copy the original statement file into data/raw/{source_type}/, so all
    uploaded statements live in one place instead of scattered wherever the
    user downloaded them from. Never overwrites a differently-named file;
    if the same filename was already archived with different content, the
    new copy is suffixed with a short hash to avoid clobbering it.

    Returns the destination path (as a string) - a repeat of the exact
    same file (same name, same hash) is a no-op and just returns the
    existing path.
    """
    dest_dir = RAW_DIR / source_type
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name

    if dest.exists():
        existing_hash = hashlib.sha256(dest.read_bytes()).hexdigest()
        if existing_hash == file_hash:
            return str(dest)
        dest = dest_dir / f"{path.stem}_{file_hash[:8]}{path.suffix}"

    shutil.copy2(path, dest)
    return str(dest)


@cli.command("ingest")
@click.argument(
    "files", nargs=-1, required=True, type=click.Path(exists=True, dir_okay=False)
)
def ingest(files):
    """Parse statement files and write raw records to the Bronze layer."""
    datalake = get_datalake()
    factory = AdapterFactory()

    for file_arg in files:
        path = Path(file_arg)
        raw_bytes = path.read_bytes()
        file_hash = hashlib.sha256(raw_bytes).hexdigest()
        content = (
            raw_bytes.decode("utf-8-sig")
            if path.suffix.lower() == ".csv"
            else raw_bytes
        )

        try:
            records = factory.ingest(content, path.name, file_hash)
        except ValueError as e:
            click.echo(f"✗ {path.name}: {e}")
            continue

        if not records:
            click.echo(f"⚠ {path.name}: parsed 0 records")
            continue

        source_type = records[0].source_type
        df = pd.DataFrame(
            [
                {
                    "source_key": r.source_key,
                    "raw_data": r.raw_data,
                    "account_identifier": r.account_identifier,
                    "record_type": r.record_type,
                    "file_hash": r.file_hash,
                    "line_number": r.line_number,
                }
                for r in records
            ]
        )
        filepath = datalake.write_bronze(source_type, path.name, df)
        raw_path = _archive_raw_file(path, source_type, file_hash)
        click.echo(f"✓ {path.name}: {len(records)} record(s) -> {filepath}")
        click.echo(f"  archived raw file -> {raw_path}")


if __name__ == "__main__":
    cli()
