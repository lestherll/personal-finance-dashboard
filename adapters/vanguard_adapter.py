"""Vanguard holdings export adapter."""

import csv
from datetime import datetime
from io import StringIO
from typing import Any, Dict, List

from adapters.base import DataSourceAdapter, RawRecord


class VanguardAdapter(DataSourceAdapter):
    """Vanguard holdings export (not transactions; separate domain)."""

    EXPECTED_COLUMNS = ["ISIN", "Fund Name", "Quantity", "Price", "Value"]

    def validate(self, file_content: str) -> tuple[bool, float]:
        """Check first row contains Vanguard headers."""
        lines = file_content.split("\n")
        if not lines:
            return False, 0.0

        headers = [h.strip() for h in lines[0].split(",")]
        matches = sum(1 for col in self.EXPECTED_COLUMNS if col in headers)
        score = matches / len(self.EXPECTED_COLUMNS)

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
                    source_type="vanguard",
                    raw_data=dict(row),
                    filename=filename,
                    file_hash=file_hash,
                    upload_timestamp=datetime.now(),
                    line_number=idx,
                )
            )

        return records

    def detect_source_type(self) -> str:
        return "vanguard"

    def generate_source_key(self, row_data: Dict[str, Any], line_num: int) -> str:
        """Holdings change over time; key by ISIN and quantity."""
        isin = row_data.get("ISIN", "").strip()
        quantity = row_data.get("Quantity", "").strip()

        if not isin:
            return f"vanguard_unknown_{line_num}"

        return f"vanguard_holding_{isin}_{quantity}"
