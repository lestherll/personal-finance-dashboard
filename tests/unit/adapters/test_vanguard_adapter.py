"""Tests for Vanguard adapter."""

import pytest
from adapters.vanguard_adapter import VanguardAdapter
from adapters.base import RawRecord


@pytest.fixture
def adapter():
    return VanguardAdapter()


class TestVanguardValidation:
    """Tests for Vanguard CSV validation."""

    def test_validate_correct_format(self, adapter, sample_vanguard_csv):
        """Vanguard CSV with correct headers validates."""
        is_valid, confidence = adapter.validate(sample_vanguard_csv)
        assert is_valid
        assert confidence >= 0.8

    def test_validate_wrong_format(self, adapter, invalid_csv):
        """Non-Vanguard CSV fails validation."""
        is_valid, confidence = adapter.validate(invalid_csv)
        assert not is_valid

    def test_validate_empty_file(self, adapter):
        """Empty file fails validation."""
        is_valid, confidence = adapter.validate("")
        assert not is_valid


class TestVanguardParsing:
    """Tests for Vanguard CSV parsing."""

    def test_parse_records(self, adapter, sample_vanguard_csv):
        """Vanguard CSV parsed correctly."""
        records = adapter.parse(sample_vanguard_csv, "test.csv", "abc123")

        assert len(records) == 2
        assert all(isinstance(r, RawRecord) for r in records)
        assert all(r.source_type == "vanguard" for r in records)

    def test_source_key_by_isin_and_quantity(self, adapter):
        """Source key generated from ISIN and quantity."""
        raw_data = {
            "ISIN": "GB0009374884",
            "Quantity": "50.00",
        }

        key = adapter.generate_source_key(raw_data, 1)

        assert "GB0009374884" in key
        assert "50.00" in key

    def test_source_key_deterministic(self, adapter):
        """Source key is deterministic."""
        raw_data = {
            "ISIN": "GB0009374884",
            "Quantity": "50.00",
        }

        key1 = adapter.generate_source_key(raw_data, 1)
        key2 = adapter.generate_source_key(raw_data, 2)

        assert key1 == key2

    def test_parse_handles_empty_file(self, adapter):
        """Empty Vanguard file handled gracefully."""
        records = adapter.parse("", "test.csv", "abc123")
        assert len(records) == 0

    def test_parse_preserves_holding_data(self, adapter, sample_vanguard_csv):
        """Holding data preserved in raw_data."""
        records = adapter.parse(sample_vanguard_csv, "test.csv", "abc123")

        first = records[0]
        assert first.raw_data["Fund Name"] == "Vanguard FTSE All-World UCITS ETF"
        assert first.raw_data["Quantity"] == "50.00"
        assert first.raw_data["Price"] == "150.25"

    def test_parse_missing_isin(self, adapter):
        """Handles missing ISIN gracefully."""
        csv_content = """ISIN,Fund Name,Quantity,Price,Value,Account Reference,Portfolio Value,Time
,Vanguard FTSE All-World,50.00,150.25,7512.50,VA123456,50000.00,15/01/2024
"""
        records = adapter.parse(csv_content, "test.csv", "abc123")

        assert len(records) == 1
        assert "vanguard_unknown_" in records[0].source_key


class TestVanguardDetection:
    """Tests for Vanguard source type detection."""

    def test_detect_source_type(self, adapter):
        """Returns correct source type."""
        assert adapter.detect_source_type() == "vanguard"
