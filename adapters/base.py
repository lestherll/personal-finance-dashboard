"""Base adapter interface for all data sources."""

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Union


def hash_account_identifier(raw_identifier: str) -> str:
    """Deterministically mask a sensitive account identifier (card/account number).

    Not a cryptographic secret - just avoids persisting a raw card/account
    number in Bronze Parquet or config files. Same physical account always
    hashes to the same value, so it's still usable as a lookup key.
    """
    normalized = re.sub(r"\s+", "", raw_identifier).upper()
    return hashlib.sha256(normalized.encode()).hexdigest()[:12]


def make_bronze_record_id(
    ingestion_id: str, record_type: str, source_ordinal: int
) -> str:
    """Return an immutable identity for one parsed row in one raw artifact."""
    material = f"{ingestion_id}:{record_type}:{source_ordinal}".encode()
    return hashlib.sha256(material).hexdigest()


def _readable_slug(text: str, limit: int = 10) -> str:
    """Short human-readable fragment for the readable part of a source key."""
    return re.sub(r"\s+", "_", (text or "").strip())[:limit]


def _normalize_key_date(date_str: str) -> str:
    """Strip the separators PDF adapters variously use ('15 May 2026', '15/05/2026')."""
    return re.sub(r"[\s/]", "", date_str or "")


def make_transaction_source_key(
    prefix: str,
    date_str: str,
    description: str,
    amount_minor: int,
    account_identifier: Optional[str] = None,
) -> str:
    """Deterministic content key for one parsed transaction.

    Two properties this must have, both of which an earlier hand-rolled
    version in each adapter silently lacked:

    - **Sign-preserving.** The old key used `abs(amount_minor)`, so paying
      someone £50 and being repaid £50 by them on the same day produced an
      *identical* key. Only `PdfAdapter.parse()`'s `_dup1/_dup2` same-file
      suffixing (Gotcha #13) kept those rows apart, and that disambiguates
      by position, not by meaning.
    - **Collision-free on long descriptions.** The old key truncated the
      description to 10 characters, so two different merchants sharing a
      10-char prefix on the same day for the same amount also collided.

    The readable `slug` is kept purely for debuggability; correctness comes
    from `digest`, which covers the *full* description and the *signed*
    amount. Callers pass their own `prefix` so keys stay source-attributable.
    """
    account_part = f"{account_identifier}_" if account_identifier else ""
    slug = _readable_slug(description)
    digest = hashlib.sha256(f"{description or ''}|{amount_minor}".encode()).hexdigest()[
        :8
    ]
    return (
        f"{prefix}_{account_part}{_normalize_key_date(date_str)}_"
        f"{slug}_{amount_minor}_{digest}"
    )


def make_snapshot_source_key(
    prefix: str,
    label: str,
    as_of_date: str,
    account_identifier: Optional[str] = None,
    extra: Optional[str] = None,
) -> str:
    """Deterministic content key for a re-printed snapshot row (a Vanguard
    holding, an Amex Plan-It plan) - identified by *what* it is plus the
    statement date it was printed on, with no amount involved.

    Same truncation hazard as `make_transaction_source_key`: `label` (a fund
    name or plan description) was previously cut to 15 characters, so two
    funds sharing a 15-char prefix on one statement collided. The digest
    covers the full label.
    """
    account_part = f"{account_identifier}_" if account_identifier else ""
    extra_part = f"{_normalize_key_date(extra)}_" if extra else ""
    digest = hashlib.sha256(f"{label or ''}".encode()).hexdigest()[:8]
    return (
        f"{prefix}_{account_part}{extra_part}{_readable_slug(label, 15)}_"
        f"{_normalize_key_date(as_of_date)}_{digest}"
    )


@dataclass
class RawRecord:
    """Minimal wrapper for raw data from a file."""

    source_key: str  # Deterministic ID: "monzo_txn_abc123"
    source_type: str  # "monzo", "kroo", "amex", "chase"
    raw_data: Dict[str, Any]  # Entire row as dict
    filename: str
    file_hash: str  # SHA256 of uploaded file
    upload_timestamp: datetime
    line_number: int
    account_identifier: Optional[
        str
    ] = None  # hashed; distinguishes multiple accounts of the same source_type
    record_type: str = "transaction"  # "transaction" | "holding"
    bronze_record_id: Optional[str] = None
    source_ordinal: Optional[int] = None


@dataclass
class ReconciliationResult:
    """Per-file self-check: does a rolled-forward/derived balance match a
    printed anchor (e.g. Closing/New Balance) on the statement itself?

    Monetary values are in integer minor units (e.g. GBP pence), never float."""

    check_name: str
    expected_closing_minor: Optional[int]
    derived_closing_minor: Optional[int]
    matches: Optional[bool]  # None = anchor not found in this file, inconclusive
    # Hashed account_identifier this result applies to (same value stored in
    # Bronze's account_identifier column). None means "applies to the whole
    # file" - the implicit meaning for every single-result adapter. Only set
    # for adapters that emit multiple results per file via
    # self.last_reconciliations (see DataSourceAdapter.__init__ below).
    account_identifier: Optional[str] = None
    # The statement's own printed opening/previous-balance anchor, when one
    # exists - independent of whether this check's mechanism actually rolls
    # forward from it (Natwest Statement/Kroo/Monzo Flex capture it purely
    # for cross-file continuity checking, without using it in `matches`).
    # None when the statement genuinely prints no opening anchor at all
    # (Monzo PDF), not when one exists but wasn't looked for.
    expected_opening_minor: Optional[int] = None


@dataclass
class StatementPeriod:
    """Per-file statement coverage period, as printed on the statement."""

    from_date: datetime
    to_date: datetime


class DataSourceAdapter(ABC):
    """All adapters inherit from this."""

    PARSER_VERSION = "1"

    def __init__(self) -> None:
        # Per-file, whole-statement facts a subclass's parse() may set -
        # not part of the RawRecord/parse() contract since they describe
        # the file as a whole, not an individual record. Must be reset to
        # None at the top of whatever method computes them: adapter
        # instances are reused across files by AdapterFactory, so a stale
        # value would otherwise leak into a file that has no anchor of its
        # own to check/extract.
        self.last_reconciliation: Optional[ReconciliationResult] = None
        self.last_statement_period: Optional[StatementPeriod] = None
        # Additive multi-result channel, for a source where one file can
        # cover multiple distinct accounts (e.g. Vanguard's ISA + Personal
        # Pension wrappers) and therefore needs more than one
        # ReconciliationResult per file. Adapters that only ever need one
        # result keep using last_reconciliation and never touch this list.
        # Same reset-to-empty-at-top discipline applies (Gotcha #14).
        self.last_reconciliations: List[ReconciliationResult] = []

    @abstractmethod
    def validate(self, file_content: Union[str, bytes]) -> tuple[bool, float]:
        """
        Check if file format matches this adapter.

        Returns:
            (is_valid: bool, confidence: float 0.0-1.0)

        Confidence allows multiple adapters to compete; highest wins.

        `file_content` is `str` for CSV adapters and `bytes` for PDF
        adapters - the factory routes by isinstance, so subclasses can
        narrow the type via their own override.
        """

    @abstractmethod
    def parse(
        self, file_content: Union[str, bytes], filename: str, file_hash: str
    ) -> List[RawRecord]:
        """Parse file, return raw records (minimal transformation).

        `file_content` is `str` for CSV adapters and `bytes` for PDF
        adapters.
        """

    @abstractmethod
    def detect_source_type(self) -> str:
        """Return: 'monzo', 'kroo', 'amex', 'chase'"""

    @abstractmethod
    def generate_source_key(
        self,
        row_data: Dict[str, Any],
        line_num: int,
        account_identifier: Optional[str] = None,
    ) -> str:
        """
        Generate deterministic source key.

        Same record from re-uploaded file has same key (no duplicates).
        account_identifier should be folded in wherever a source_type can
        cover multiple physical accounts (e.g. two Amex cards), otherwise
        same-day/same-amount transactions on different accounts could collide.
        """
