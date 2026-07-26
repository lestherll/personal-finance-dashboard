from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BronzeRecord: ...


@dataclass(frozen=True)
class SilverRecord: ...


@dataclass(frozen=True)
class RawData:
    """Required data when parsing statements/transactions files"""
    start_statement_date: str
    end_statement_date: str
    transactions: list[dict[str, Any]]
    extra_data: dict[str, Any] | None = None

