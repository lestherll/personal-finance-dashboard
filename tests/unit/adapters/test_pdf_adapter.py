"""Tests for the shared PdfAdapter.parse() behavior (base class)."""

import pytest

from adapters.kroo_pdf_adapter import KrooPdfAdapter
from adapters.pdf_adapter import resolve_year_in_period
from datetime import datetime

# Two genuinely distinct real transactions that happen to look identical
# (same date, description, amount) - e.g. two identical coffee purchases at
# the same shop on the same day. These must never be treated as a parsing
# duplicate of each other.
SAMPLE_WITH_REPEATED_TXN = """Kroo Current Account
Sort code: 01-02-03
Account number: 12345678
Account transactions
01 June 2026
Coffee Shop
To Test Merchant
£3.50
£96.50
01 June 2026
Coffee Shop
To Test Merchant
£3.50
£93.00
02 June 2026
Salary
From Employer Ltd
£500.00
£593.00
Closing balance
£593.00
"""


@pytest.fixture
def adapter():
    return KrooPdfAdapter()


class TestSameFileDuplicateDisambiguation:
    def test_identical_looking_transactions_get_distinct_source_keys(
        self, adapter, monkeypatch
    ):
        monkeypatch.setattr(
            adapter, "_extract_text", lambda content: SAMPLE_WITH_REPEATED_TXN
        )
        records = adapter.parse(b"fake pdf bytes", "test.pdf", "hash123")

        coffee_records = [
            r for r in records if "Coffee Shop" in r.raw_data["description"]
        ]
        assert len(coffee_records) == 2
        assert coffee_records[0].source_key != coffee_records[1].source_key

    def test_first_occurrence_key_is_unsuffixed(self, adapter, monkeypatch):
        """The first occurrence keeps the plain content-based key, so a
        transaction that appears once here and once in an overlapping-period
        statement still dedupes correctly across files."""
        monkeypatch.setattr(
            adapter, "_extract_text", lambda content: SAMPLE_WITH_REPEATED_TXN
        )
        records = adapter.parse(b"fake pdf bytes", "test.pdf", "hash123")

        salary = next(r for r in records if "Salary" in r.raw_data["description"])
        plain_key = adapter.generate_source_key(
            {
                "date": "02 June 2026",
                "description": "Salary From Employer Ltd",
                "amount": 500.0,
            },
            3,
            salary.account_identifier,
        )
        assert salary.source_key == plain_key

    def test_non_duplicated_file_is_unaffected(self, adapter, monkeypatch):
        text = """Kroo Current Account
Sort code: 01-02-03
Account number: 12345678
Account transactions
01 June 2026
Salary
From Employer Ltd
£500.00
£500.00
Closing balance
£500.00
"""
        monkeypatch.setattr(adapter, "_extract_text", lambda content: text)
        records = adapter.parse(b"fake pdf bytes", "test.pdf", "hash123")
        assert len(records) == 1
        assert not records[0].source_key.endswith("_dup1")


class TestResolveYearInPeriod:
    def test_within_period_resolves(self):
        result = resolve_year_in_period(
            "15 Jan", datetime(2026, 1, 1), datetime(2026, 1, 31)
        )
        assert result == "15 Jan 2026"

    def test_outside_period_and_tolerance_returns_none(self):
        result = resolve_year_in_period(
            "15 Jun", datetime(2026, 1, 1), datetime(2026, 1, 31)
        )
        assert result is None
