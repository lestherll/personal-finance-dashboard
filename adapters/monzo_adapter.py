"""Monzo CSV export adapter."""

import csv
from datetime import datetime
from io import StringIO
from typing import Any, Dict, List, Optional, Union

from models.money import try_parse_money_minor

from adapters.base import DataSourceAdapter, RawRecord, make_bronze_record_id


class MonzoAdapter(DataSourceAdapter):
    """Monzo CSV export format (supports both full and search export)."""

    PARSER_VERSION = "2"

    FULL_EXPORT_COLUMNS = [
        "Transaction ID",
        "Date",
        "Time",
        "Type",
        "Name",
        "Emoji",
        "Category",
        "Amount",
        "Currency",
        "Local Amount",
        "Local Currency",
        "Notes",
        "Receipt",
        "Description",
    ]

    SEARCH_EXPORT_COLUMNS = [
        "id",
        "created",
        "title",
        "subtitle",
        "amount",
        "currency",
        "categories",
    ]

    def validate(self, file_content: Union[str, bytes]) -> tuple[bool, float]:
        """Check if file contains Monzo headers (full or search format)."""
        if isinstance(file_content, bytes):
            return False, 0.0
        lines = file_content.split("\n")
        if not lines:
            return False, 0.0

        headers = [h.strip() for h in lines[0].split(",")]

        # Check for full export format
        full_matches = sum(1 for col in self.FULL_EXPORT_COLUMNS[:5] if col in headers)
        full_score = full_matches / 5

        # Check for search export format
        search_matches = sum(
            1 for col in self.SEARCH_EXPORT_COLUMNS[:5] if col in headers
        )
        search_score = search_matches / 5

        # Use highest score
        score = max(full_score, search_score)
        return score >= 0.8, score

    def parse(
        self, file_content: Union[str, bytes], filename: str, file_hash: str
    ) -> List[RawRecord]:
        """Convert CSV to RawRecord list (handles both full and search format)."""
        records: list[RawRecord] = []
        if isinstance(file_content, bytes):
            return records
        reader = csv.DictReader(StringIO(file_content))

        if not reader or not reader.fieldnames:
            return records

        for idx, row in enumerate(reader, start=2):
            if not row or not any(row.values()):
                continue

            raw_data = dict(row)

            amount_field = raw_data.get("Amount") or raw_data.get("amount") or ""
            if amount_field:
                amount_minor = try_parse_money_minor(amount_field)
                if amount_minor is not None:
                    raw_data["amount_minor"] = amount_minor
                    raw_data["amount_text"] = amount_field

            source_key = self.generate_source_key(row, idx)

            records.append(
                RawRecord(
                    source_key=source_key,
                    source_type="monzo",
                    raw_data=raw_data,
                    filename=filename,
                    file_hash=file_hash,
                    upload_timestamp=datetime.now(),
                    line_number=idx,
                    bronze_record_id=make_bronze_record_id(
                        file_hash, "transaction", idx
                    ),
                    source_ordinal=idx,
                )
            )

        return records

    def detect_source_type(self) -> str:
        return "monzo"

    def generate_source_key(
        self,
        row_data: Dict[str, Any],
        line_num: int,
        account_identifier: Optional[str] = None,
    ) -> str:
        """Generate deterministic source key based on format.

        Monzo only ever has a single account per user, so
        `account_identifier` is accepted to satisfy the base contract but
        isn't folded into the key. Format (full vs. search export) is
        detected from the row's own keys.
        """
        if "id" in row_data and "created" in row_data:
            # Search format: use 'id' field
            txn_id = row_data.get("id", "").strip()
            if not txn_id:
                return f"monzo_unknown_{line_num}"
            return f"monzo_txn_{txn_id}"
        # Full export format: use 'Transaction ID' field
        txn_id = row_data.get("Transaction ID", "").strip()
        if not txn_id:
            return f"monzo_unknown_{line_num}"
        return f"monzo_txn_{txn_id}"
