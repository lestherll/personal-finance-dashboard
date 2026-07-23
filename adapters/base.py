"""Base adapter interface for all data sources."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List


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


class DataSourceAdapter(ABC):
    """All adapters inherit from this."""

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
    def generate_source_key(self, row_data: Dict[str, Any], line_num: int) -> str:
        """
        Generate deterministic source key.

        Same record from re-uploaded file has same key (no duplicates).
        """
