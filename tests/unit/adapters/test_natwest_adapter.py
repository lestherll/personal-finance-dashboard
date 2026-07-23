"""Tests for Natwest adapter."""

import pytest
from adapters.natwest_adapter import NatwelstAdapter
from adapters.base import RawRecord


@pytest.fixture
def adapter():
    return NatwelstAdapter()


class TestNatwelstValidation:
    """Tests for Natwest CSV validation."""

    def test_validate_correct_format(self, adapter, sample_natwest_csv):
        """Natwest CSV with correct headers validates."""
        is_valid, confidence = adapter.validate(sample_natwest_csv)
        assert is_valid
        assert confidence >= 0.7

    def test_validate_wrong_format(self, adapter, invalid_csv):
        """Non-Natwest CSV fails validation."""
        is_valid, confidence = adapter.validate(invalid_csv)
        assert not is_valid

    def test_validate_empty_file(self, adapter):
        """Empty file fails validation."""
        is_valid, confidence = adapter.validate("")
        assert not is_valid


class TestNatwelstParsing:
    """Tests for Natwest CSV parsing."""

    def test_parse_records(self, adapter, sample_natwest_csv):
        """Natwest CSV parsed correctly."""
        records = adapter.parse(sample_natwest_csv, "test.csv", "abc123")

        assert len(records) == 3
        assert all(isinstance(r, RawRecord) for r in records)
        assert all(r.source_type == "natwest" for r in records)

    def test_source_key_deterministic(self, adapter):
        """Source key deterministic (date + amount + narrative)."""
        raw_data = {
            "Transaction Date": "15/01/2024",
            "Transaction Amount": "-50.00",
            "Transaction Narrative": "FUEL SHELL PETROL",
        }

        key1 = adapter.generate_source_key(raw_data, 1)
        key2 = adapter.generate_source_key(raw_data, 2)

        # Same date/amount/narrative = same key (re-upload safe)
        assert key1 == key2

    def test_parse_handles_empty_file(self, adapter):
        """Empty Natwest file handled gracefully."""
        records = adapter.parse("", "test.csv", "abc123")
        assert len(records) == 0

    def test_parse_preserves_balance_data(self, adapter, sample_natwest_csv):
        """Balance data preserved in raw_data."""
        records = adapter.parse(sample_natwest_csv, "test.csv", "abc123")

        first = records[0]
        assert first.raw_data["Balance"] == "450.00"
        assert first.raw_data["Balance Date"] == "15/01/2024"

    def test_parse_missing_required_fields(self, adapter):
        """Handles missing required fields gracefully."""
        csv_content = """Transaction Type,Transaction Date,Transaction Amount,Transaction Narrative,Balance,Balance Date
DEBIT,,,-50.00,450.00,15/01/2024
"""
        records = adapter.parse(csv_content, "test.csv", "abc123")

        assert len(records) == 1
        assert "natwest_unknown_" in records[0].source_key


class TestNatwelstDetection:
    """Tests for Natwest source type detection."""

    def test_detect_source_type(self, adapter):
        """Returns correct source type."""
        assert adapter.detect_source_type() == "natwest"
