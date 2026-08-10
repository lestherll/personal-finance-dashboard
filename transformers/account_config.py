"""Bronze account_identifier -> canonical account resolution.

The actual account mapping is user data, not code - it lives in a JSON file
in the data store (config.ACCOUNT_MAP_PATH, default `data/account_map.json`),
never hardcoded here. This module only holds the resolution logic: hashed
identifier lookup first, source_type fallback second.

Keyed by the *hashed* `account_identifier` extracted by each PDF adapter
(see `adapters/base.py::hash_account_identifier`) - never a raw card/account
number - because a single source_type can cover multiple physical accounts
(e.g. two Amex cards, or Natwest current vs credit).

Hashes aren't human-predictable, so the file can't be pre-populated for an
account that hasn't been ingested yet. Workflow for a new account:
1. Ingest the statement once.
2. Read `account_identifier` off the resulting Bronze row.
3. Add an entry to the data file's "identifiers" section with a friendly
   account_id/display_name.

Expected file shape:
{
  "identifiers": {
    "<hashed_identifier>": {"account_id": "...", "display_name": "...", "account_type": "..."}
  },
  "source_type_fallback": {
    "<source_type>": {"account_id": "...", "display_name": "...", "account_type": "..."}
  }
}
"""

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

import pandas as pd

from adapters.factory import AdapterFactory
from config import ACCOUNT_MAP_PATH
from models.datalake import DataLake, get_datalake

PathLike = Union[str, Path]

_UNMAPPED_COLUMNS = [
    "source_type",
    "account_identifier",
    "sample_description",
    "record_count",
]

ACCOUNT_TYPE_CHOICES = ("current", "credit", "investment", "savings")


_CACHE: Dict[Path, Dict[str, Dict[str, Dict[str, str]]]] = {}


def _resolve_path(path: Optional[PathLike]) -> Path:
    return Path(path) if path is not None else ACCOUNT_MAP_PATH


def _read_account_map_file(
    resolved: Path,
) -> Dict[str, Dict[str, Dict[str, str]]]:
    """Actually parse the account map off disk. A missing file is treated
    as an empty (not-yet-configured) map rather than an error. This is the
    expensive part - _load() pays it at most once per resolved path per
    process."""
    if not resolved.exists():
        return {"identifiers": {}, "source_type_fallback": {}}

    with open(resolved) as f:
        data = json.load(f)
    return {
        "identifiers": data.get("identifiers", {}),
        "source_type_fallback": data.get("source_type_fallback", {}),
    }


def _load(path: Optional[PathLike] = None) -> Dict[str, Dict[str, Dict[str, str]]]:
    """Load the account map from the data store, cached per resolved path.

    Re-resolves `path or ACCOUNT_MAP_PATH` on every call (not once at
    import/cache-construction time), so a monkeypatched
    transformers.account_config.ACCOUNT_MAP_PATH in one test gets its own
    cache entry, never another test's stale one. register_account /
    register_source_type_fallback invalidate the entry for their resolved
    path immediately after writing, so a registration is visible to the
    very next _load() call in-process.

    One deliberate trade-off: only this module's own writes evict, so an
    edit made by *another* process is invisible for the rest of this
    process's lifetime. That is fine for the CLI (short-lived, one
    register-or-rebuild per invocation) but a long-lived worker (Phase 3
    Celery) would need to restart - or evict `_CACHE` itself - to see an
    external edit.

    The returned dict is the live cache entry: callers must not mutate it
    (registrations copy first - see _fresh_copy).
    """
    resolved = _resolve_path(path)
    if resolved not in _CACHE:
        _CACHE[resolved] = _read_account_map_file(resolved)
    return _CACHE[resolved]


def _fresh_copy(
    config: Dict[str, Dict[str, Dict[str, str]]],
) -> Dict[str, Dict[str, Dict[str, str]]]:
    """Per-section shallow copy of a cached config, for registration to
    mutate safely. Mutating the cached dict itself would leave a phantom
    entry behind if the subsequent file write failed halfway - the next
    _load() would serve an account that was never actually persisted."""
    return {section: dict(entries) for section, entries in config.items()}


def get_account_id(
    account_identifier: Optional[str],
    source_type: str,
    path: Optional[PathLike] = None,
) -> str:
    """Resolve a Bronze record to its canonical account_id.

    Looks up by the hashed account_identifier first (distinguishes multiple
    accounts of the same source_type); falls back to a source_type-level
    default when no identifier was extracted.
    """
    config = _load(path)

    if account_identifier is not None:
        entry = config["identifiers"].get(account_identifier)
        if entry is not None:
            return entry["account_id"]

    entry = config["source_type_fallback"].get(source_type)
    if entry is not None:
        return entry["account_id"]

    raise KeyError(
        f"No account mapping for account_identifier={account_identifier!r}, "
        f"source_type={source_type!r}. Add an entry to "
        f"{path or ACCOUNT_MAP_PATH} under 'identifiers' or 'source_type_fallback'."
    )


def build_accounts_table(path: Optional[PathLike] = None) -> pd.DataFrame:
    """Build the Silver accounts registry: one row per canonical account_id."""
    config = _load(path)
    by_account: Dict[str, Dict[str, Any]] = {}

    def _row(account_id: str, entry: Dict[str, str]) -> Dict[str, Any]:
        return by_account.setdefault(
            account_id,
            {
                "account_id": account_id,
                "display_name": entry["display_name"],
                "account_type": entry["account_type"],
                "account_identifiers": [],
                "source_types": [],
            },
        )

    for identifier, entry in config["identifiers"].items():
        row = _row(entry["account_id"], entry)
        row["account_identifiers"].append(identifier)

    for source_type, entry in config["source_type_fallback"].items():
        row = _row(entry["account_id"], entry)
        row["source_types"].append(source_type)

    return pd.DataFrame(list(by_account.values()))


def _write(
    config: Dict[str, Dict[str, Dict[str, str]]], path: Optional[PathLike]
) -> None:
    resolved = _resolve_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    with open(resolved, "w") as f:
        json.dump(config, f, indent=2, sort_keys=True)
        f.write("\n")
    _CACHE.pop(resolved, None)


def register_account(
    account_identifier: str,
    account_id: str,
    display_name: str,
    account_type: str,
    path: Optional[PathLike] = None,
) -> None:
    """Add (or overwrite) an identifier -> account mapping in the data store."""
    config = _fresh_copy(_load(path))
    config["identifiers"][account_identifier] = {
        "account_id": account_id,
        "display_name": display_name,
        "account_type": account_type,
    }
    _write(config, path)


def register_source_type_fallback(
    source_type: str,
    account_id: str,
    display_name: str,
    account_type: str,
    path: Optional[PathLike] = None,
) -> None:
    """Add (or overwrite) a source_type-level fallback (for sources with no
    extractable identifier at all, e.g. Monzo's CSV export)."""
    config = _fresh_copy(_load(path))
    config["source_type_fallback"][source_type] = {
        "account_id": account_id,
        "display_name": display_name,
        "account_type": account_type,
    }
    _write(config, path)


def _sample_description(raw: Dict[str, Any]) -> str:
    for key in ("description", "Transaction Narrative", "fund_name", "Name", "title"):
        value = raw.get(key)
        if value:
            return str(value)
    return str(raw)[:60]


def find_unmapped_accounts(
    datalake: Optional[DataLake] = None,
    path: Optional[PathLike] = None,
    bronze_frames: Optional[Dict[str, pd.DataFrame]] = None,
) -> pd.DataFrame:
    """Scan all Bronze data for (source_type, account_identifier) combos with
    no mapping yet.

    One row per unmapped combo, with a sample description and record count
    to help identify which real-world account it is before registering it.

    Pass an already-loaded `bronze_frames` dict (source_type -> DataFrame) to
    avoid re-reading Bronze from disk when the caller already has it in
    memory (e.g. run_bronze_to_silver's pre-flight check) - falls back to
    reading from `datalake` per source_type when omitted.
    """
    datalake = datalake or get_datalake()
    config = _load(path)
    known_identifiers = set(config["identifiers"])
    known_fallback_source_types = set(config["source_type_fallback"])

    all_source_types = AdapterFactory.CSV_SOURCE_TYPES | AdapterFactory.PDF_SOURCE_TYPES
    unmapped: Dict[Tuple[str, Optional[str]], Dict[str, Any]] = {}

    for source_type in all_source_types:
        df = (
            bronze_frames.get(source_type)
            if bronze_frames is not None
            else datalake.read_bronze(source_type)
        )
        if df is None or df.empty:
            continue

        for _, row in df.iterrows():
            identifier = row.get("account_identifier")
            if identifier is not None:
                if identifier in known_identifiers:
                    continue
            elif source_type in known_fallback_source_types:
                continue

            key = (source_type, identifier)
            if key not in unmapped:
                unmapped[key] = {
                    "source_type": source_type,
                    "account_identifier": identifier,
                    "sample_description": _sample_description(
                        row.get("raw_data") or {}
                    ),
                    "record_count": 0,
                }
            unmapped[key]["record_count"] += 1

    if not unmapped:
        return pd.DataFrame(columns=_UNMAPPED_COLUMNS)
    return pd.DataFrame(list(unmapped.values()))


class UnmappedAccountsError(Exception):
    """Raised when Bronze data contains accounts not yet in the account map."""

    def __init__(self, unmapped_df: pd.DataFrame):
        self.unmapped = unmapped_df
        lines = [
            f"  - source_type={row.source_type!r} account_identifier={row.account_identifier!r} "
            f"sample={row.sample_description!r} ({row.record_count} records)"
            for row in unmapped_df.itertuples()
        ]
        message = (
            f"{len(unmapped_df)} account(s) in Bronze data are not yet mapped in "
            f"{ACCOUNT_MAP_PATH}. Register them first:\n"
            "  uv run python cli.py accounts list-unmapped\n"
            "  uv run python cli.py accounts register <account_identifier> <account_id> "
            "<display_name> <current|credit|investment|savings>\n" + "\n".join(lines)
        )
        super().__init__(message)
