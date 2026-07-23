"""Monzo CSV export adapter."""

import csv
from datetime import datetime
from io import StringIO
from typing import Any, Dict, List

from adapters.base import DataSourceAdapter, RawRecord


class MonzoAdapter(DataSourceAdapter):
    """Monzo CSV export format."""

    EXPECTED_COLUMNS = [
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

    def validate(self, file_content: str) -> tuple[bool, float]:
        """Check first row contains Monzo headers."""
        lines = file_content.split("\n")
        if not lines:
            return False, 0.0

        headers = [h.strip() for h in lines[0].split(",")]

        # Monzo has very specific columns; high confidence if most present
        matches = sum(1 for col in self.EXPECTED_COLUMNS[:5] if col in headers)
        score = matches / 5

        return score >= 0.8, score

    def parse(
        self, file_content: str, filename: str, file_hash: str
    ) -> List[RawRecord]:
        """Convert CSV to RawRecord list."""
        records = []
        reader = csv.DictReader(StringIO(file_content))

        if not reader or not reader.fieldnames:
            return records

        for idx, row in enumerate(reader, start=2):
            if not row or not any(row.values()):
                continue

            source_key = self.generate_source_key(row, idx)

            records.append(
                RawRecord(
                    source_key=source_key,
                    source_type="monzo",
                    raw_data=dict(row),
                    filename=filename,
                    file_hash=file_hash,
                    upload_timestamp=datetime.now(),
                    line_number=idx,
                )
            )

        return records

    def detect_source_type(self) -> str:
        return "monzo"

    def generate_source_key(self, row_data: Dict[str, Any], line_num: int) -> str:
        """Use Transaction ID (unique within Monzo) as key."""
        txn_id = row_data.get("Transaction ID", "").strip()
        if not txn_id:
            return f"monzo_unknown_{line_num}"
        return f"monzo_txn_{txn_id}"
