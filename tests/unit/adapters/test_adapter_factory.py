"""Tests for Adapter Factory."""

import pytest
from adapters.factory import AdapterFactory


@pytest.fixture
def factory():
    return AdapterFactory()


class TestAdapterDetection:
    """Tests for adapter auto-detection."""

    def test_detect_monzo(self, factory, sample_monzo_csv):
        """Monzo adapter detected and selected."""
        adapter = factory.detect_adapter(sample_monzo_csv)
        assert adapter.detect_source_type() == "monzo"

    def test_detect_natwest(self, factory, sample_natwest_csv):
        """Natwest adapter detected and selected."""
        adapter = factory.detect_adapter(sample_natwest_csv)
        assert adapter.detect_source_type() == "natwest"

    def test_detect_vanguard(self, factory, sample_vanguard_csv):
        """Vanguard adapter detected and selected."""
        adapter = factory.detect_adapter(sample_vanguard_csv)
        assert adapter.detect_source_type() == "vanguard"

    def test_detect_unknown_format(self, factory, invalid_csv):
        """Unknown format raises error."""
        with pytest.raises(ValueError, match="File format not recognized"):
            factory.detect_adapter(invalid_csv)

    def test_detect_empty_file(self, factory):
        """Empty file raises error."""
        with pytest.raises(ValueError, match="File format not recognized"):
            factory.detect_adapter("")


class TestAdapterIngest:
    """Tests for ingestion pipeline."""

    def test_ingest_monzo(self, factory, sample_monzo_csv):
        """Ingest Monzo CSV end-to-end."""
        records = factory.ingest(sample_monzo_csv, "test.csv", "abc123")

        assert len(records) == 3
        assert all(r.source_type == "monzo" for r in records)

    def test_ingest_natwest(self, factory, sample_natwest_csv):
        """Ingest Natwest CSV end-to-end."""
        records = factory.ingest(sample_natwest_csv, "test.csv", "abc123")

        assert len(records) == 3
        assert all(r.source_type == "natwest" for r in records)

    def test_ingest_vanguard(self, factory, sample_vanguard_csv):
        """Ingest Vanguard CSV end-to-end."""
        records = factory.ingest(sample_vanguard_csv, "test.csv", "abc123")

        assert len(records) == 2
        assert all(r.source_type == "vanguard" for r in records)

    def test_ingest_preserves_file_hash(self, factory, sample_monzo_csv):
        """File hash preserved in records."""
        file_hash = "test_hash_123"
        records = factory.ingest(sample_monzo_csv, "test.csv", file_hash)

        assert all(r.file_hash == file_hash for r in records)

    def test_ingest_preserves_filename(self, factory, sample_monzo_csv):
        """Filename preserved in records."""
        records = factory.ingest(sample_monzo_csv, "export_2024.csv", "abc123")

        assert all(r.filename == "export_2024.csv" for r in records)


class TestAdapterDisabling:
    """Tests for disabling specific or all adapters."""

    def test_disable_single_source_type(self, sample_monzo_csv):
        """Disabling one CSV adapter excludes it from routing."""
        factory = AdapterFactory(disabled_source_types={"monzo"})
        with pytest.raises(ValueError, match="File format not recognized"):
            factory.detect_adapter(sample_monzo_csv)

    def test_disable_all_csv(
        self, sample_monzo_csv, sample_natwest_csv, sample_vanguard_csv
    ):
        """Disabling all CSV source types leaves csv_adapters empty."""
        factory = AdapterFactory(disabled_source_types=AdapterFactory.CSV_SOURCE_TYPES)
        assert factory.csv_adapters == []
        assert len(factory.pdf_adapters) == 5

        for content in (sample_monzo_csv, sample_natwest_csv, sample_vanguard_csv):
            with pytest.raises(ValueError, match="File format not recognized"):
                factory.detect_adapter(content)

    def test_no_disabling_by_default(self):
        """Default constructor enables every adapter."""
        factory = AdapterFactory()
        assert len(factory.csv_adapters) == 3
        assert len(factory.pdf_adapters) == 5

    def test_unknown_disabled_source_type_raises(self):
        """Typos in disabled_source_types are caught rather than silently ignored."""
        with pytest.raises(ValueError, match="Unknown source_type"):
            AdapterFactory(disabled_source_types={"not_a_real_source"})
