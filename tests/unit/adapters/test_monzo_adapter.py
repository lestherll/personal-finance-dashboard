"""Tests for Monzo adapter."""

import pytest
from adapters.monzo_adapter import MonzoAdapter
from adapters.base import RawRecord


@pytest.fixture
def adapter():
    return MonzoAdapter()


class TestMonzoValidation:
    """Tests for Monzo CSV validation."""

    def test_validate_correct_format(self, adapter, sample_monzo_csv):
        """Monzo CSV with correct headers validates."""
        is_valid, confidence = adapter.validate(sample_monzo_csv)
        assert is_valid
        assert confidence >= 0.8

    def test_validate_wrong_format(self, adapter, invalid_csv):
        """Non-Monzo CSV fails validation."""
        is_valid, confidence = adapter.validate(invalid_csv)
        assert not is_valid

    def test_validate_empty_file(self, adapter):
        """Empty file fails validation."""
        is_valid, confidence = adapter.validate("")
        assert not is_valid


class TestMonzoParsing:
    """Tests for Monzo CSV parsing."""

    def test_parse_records(self, adapter, sample_monzo_csv):
        """Monzo CSV parsed correctly."""
        records = adapter.parse(sample_monzo_csv, "test.csv", "abc123")

        assert len(records) == 3
        assert all(isinstance(r, RawRecord) for r in records)
        assert all(r.source_type == "monzo" for r in records)

    def test_source_key_generation(self, adapter, sample_monzo_csv):
        """Source keys generated from Transaction IDs."""
        records = adapter.parse(sample_monzo_csv, "test.csv", "abc123")

        assert records[0].source_key == "monzo_txn_tx_abc123"
        assert records[1].source_key == "monzo_txn_tx_abc124"

    def test_deterministic_source_key(self, adapter):
        """Source key is deterministic."""
        raw_data = {"Transaction ID": "tx_abc123"}

        key1 = adapter.generate_source_key(raw_data, 1)
        key2 = adapter.generate_source_key(raw_data, 1)

        assert key1 == key2

    def test_parse_handles_empty_file(self, adapter):
        """Empty Monzo file handled gracefully."""
        records = adapter.parse("", "test.csv", "abc123")
        assert len(records) == 0

    def test_parse_preserves_raw_data(self, adapter, sample_monzo_csv):
        """All fields preserved in raw_data."""
        records = adapter.parse(sample_monzo_csv, "test.csv", "abc123")

        first = records[0]
        assert first.raw_data["Name"] == "Tesco Groceries"
        assert first.raw_data["Amount"] == "-25.50"
        assert first.raw_data["Category"] == "Groceries"

    def test_parse_missing_transaction_id(self, adapter):
        """Handles missing Transaction ID gracefully."""
        csv_content = """Transaction ID,Date,Time,Type,Name,Emoji,Category,Amount,Currency,Local Amount,Local Currency,Notes,Receipt,Description
,15/01/2024,14:30:00,card_payment,Tesco,🛒,Groceries,-25.50,GBP,-25.50,GBP,,0,Tesco
"""
        records = adapter.parse(csv_content, "test.csv", "abc123")

        assert len(records) == 1
        assert "monzo_unknown_" in records[0].source_key


class TestMonzoDetection:
    """Tests for Monzo source type detection."""

    def test_detect_source_type(self, adapter):
        """Returns correct source type."""
        assert adapter.detect_source_type() == "monzo"
