"""Personal finance dashboard CLI.

Usage:
    uv run python cli.py accounts list-unmapped
    uv run python cli.py accounts register <account_identifier> <account_id> <display_name> <account_type>
    uv run python cli.py accounts register-fallback <source_type> <account_id> <display_name> <account_type>
"""

import click

from models.datalake import get_datalake
from transformers.account_config import (
    find_unmapped_accounts,
    register_account,
    register_source_type_fallback,
)

ACCOUNT_TYPE_CHOICES = click.Choice(["current", "credit", "investment"])


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
        "<display_name> <current|credit|investment>"
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


if __name__ == "__main__":
    cli()
