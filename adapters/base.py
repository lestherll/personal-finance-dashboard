"""Base adapter interface for all data sources."""

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional


def hash_account_identifier(raw_identifier: str) -> str:
    """Deterministically mask a sensitive account identifier (card/account number).

    Not a cryptographic secret - just avoids persisting a raw card/account
    number in Bronze Parquet or config files. Same physical account always
    hashes to the same value, so it's still usable as a lookup key.
    """
    normalized = re.sub(r"\s+", "", raw_identifier).upper()
    return hashlib.sha256(normalized.encode()).hexdigest()[:12]


@dataclass
class RawRecord:
    """Minimal wrapper for raw data from a file."""

    source_key: str  # Deterministic ID: "monzo_txn_abc123"
    source_type: str  # "monzo", "natwest", "amex", "vanguard"
    raw_data: Dict[str, Any]  # Entire row as dict
    filename: str
    file_hash: str  # SHA256 of uploaded file
    upload_timestamp: datetime
    line_number: int
    account_identifier: Optional[
        str
    ] = None  # hashed; distinguishes multiple accounts of the same source_type
    record_type: str = "transaction"  # "transaction" | "holding"


@dataclass
class ReconciliationResult:
    """Per-file self-check: does a rolled-forward/derived balance match a
    printed anchor (e.g. Closing/New Balance) on the statement itself?"""

    check_name: str
    expected_closing: Optional[Decimal]
    derived_closing: Optional[Decimal]
    matches: Optional[bool]  # None = anchor not found in this file, inconclusive


@dataclass
class StatementPeriod:
    """Per-file statement coverage period, as printed on the statement."""

    from_date: datetime
    to_date: datetime


class DataSourceAdapter(ABC):
    """All adapters inherit from this."""

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

    @abstractmethod
    def validate(self, file_content: str) -> tuple[bool, float]:
        """
        Check if file format matches this adapter.

        Returns:
            (is_valid: bool, confidence: float 0.0-1.0)

        Confidence allows multiple adapters to compete; highest wins.
        """

    @abstractmethod
    def parse(
        self, file_content: str, filename: str, file_hash: str
    ) -> List[RawRecord]:
        """Parse file, return raw records (minimal transformation)."""

    @abstractmethod
    def detect_source_type(self) -> str:
        """Return: 'monzo', 'natwest', 'amex', 'vanguard'"""

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
