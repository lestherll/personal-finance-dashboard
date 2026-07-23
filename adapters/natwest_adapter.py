"""Natwest CSV export adapter."""

import csv
from datetime import datetime
from io import StringIO
from typing import Any, Dict, List

from adapters.base import DataSourceAdapter, RawRecord


class NatwestAdapter(DataSourceAdapter):
    """Natwest CSV export (different format from Monzo)."""

    EXPECTED_COLUMNS = [
        "Transaction Type",
        "Transaction Date",
        "Transaction Amount",
        "Transaction Narrative",
        "Balance",
        "Balance Date",
    ]

    def validate(self, file_content: str) -> tuple[bool, float]:
        """Check first row contains Natwest headers."""
        lines = file_content.split("\n")
        if not lines:
            return False, 0.0

        headers = [h.strip() for h in lines[0].split(",")]
        matches = sum(1 for col in self.EXPECTED_COLUMNS[:3] if col in headers)
        score = matches / 3

        return score >= 0.7, score

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
                    source_type="natwest",
                    raw_data=dict(row),
                    filename=filename,
                    file_hash=file_hash,
                    upload_timestamp=datetime.now(),
                    line_number=idx,
                )
            )

        return records

    def detect_source_type(self) -> str:
        return "natwest"

    def generate_source_key(self, row_data: Dict[str, Any], line_num: int) -> str:
        """
        Deterministic key: date + amount + first 10 chars of narrative.

        Same transaction in re-uploaded file will have same key.
        """
        date_str = row_data.get("Transaction Date", "").replace("/", "")
        amount = row_data.get("Transaction Amount", "").strip()
        narrative = row_data.get("Transaction Narrative", "")[:10].replace(" ", "_")

        if not date_str or not amount:
            return f"natwest_unknown_{line_num}"

        return f"natwest_txn_{date_str}_{amount}_{narrative}"
