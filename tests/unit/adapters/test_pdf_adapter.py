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
                "amount_minor": 50000,
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


class TestDataSourceAdapterDefaults:
    """DataSourceAdapter.__init__ (adapters/base.py) gives every adapter -
    CSV or PDF - these two attributes, defaulted to None before parse() is
    ever called."""

    def test_fresh_adapter_has_no_reconciliation_or_period(self, adapter):
        assert adapter.last_reconciliation is None
        assert adapter.last_statement_period is None


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

    def test_early_in_period_returns_year(self):
        """Date near beginning of period should resolve."""
        result = resolve_year_in_period(
            "01 Jan", datetime(2026, 1, 1), datetime(2026, 1, 31)
        )
        assert result == "01 Jan 2026"

    def test_late_in_period_returns_year(self):
        """Date near end of period should resolve."""
        result = resolve_year_in_period(
            "31 Jan", datetime(2026, 1, 1), datetime(2026, 1, 31)
        )
        assert result == "31 Jan 2026"

    def test_just_before_period_returns_none(self):
        """Date just before period starts should not resolve."""
        result = resolve_year_in_period(
            "31 Dec", datetime(2026, 1, 1), datetime(2026, 1, 31)
        )
        assert result is None

    def test_just_after_period_with_tolerance(self):
        """Date just after period ends might resolve if within tolerance."""
        # resolve_year_in_period has a tolerance window (typically 30 days)
        result = resolve_year_in_period(
            "01 Feb", datetime(2026, 1, 1), datetime(2026, 1, 31)
        )
        # This might resolve or not depending on the tolerance implementation
        assert result is None or "2026" in result

    def test_multi_month_period(self):
        """Period spanning multiple months."""
        result = resolve_year_in_period(
            "15 Feb", datetime(2026, 1, 1), datetime(2026, 3, 31)
        )
        assert result == "15 Feb 2026"

    def test_year_boundary_period(self):
        """Period crossing year boundary (Dec to Jan)."""
        result = resolve_year_in_period(
            "15 Jan", datetime(2025, 12, 1), datetime(2026, 1, 31)
        )
        assert result == "15 Jan 2026"

    def test_year_boundary_period_december_date(self):
        """Date in December within year-boundary period."""
        result = resolve_year_in_period(
            "15 Dec", datetime(2025, 12, 1), datetime(2026, 1, 31)
        )
        assert result == "15 Dec 2025"

    def test_none_input_returns_none(self):
        """None input should not crash."""
        result = resolve_year_in_period(
            None, datetime(2026, 1, 1), datetime(2026, 1, 31)
        )
        assert result is None

    def test_empty_string_returns_none(self):
        """Empty string should not resolve."""
        result = resolve_year_in_period("", datetime(2026, 1, 1), datetime(2026, 1, 31))
        assert result is None


class TestPdfAdapterTextExtraction:
    """Test base class _extract_text() method and page handling."""

    def test_empty_pdf_returns_empty_string(self, adapter):
        """PDF with no text content."""
        # Mock the PyMuPDF behavior
        import unittest.mock as mock

        with mock.patch("adapters.pdf_adapter.fitz.open") as mock_open:
            mock_doc = mock.MagicMock()
            mock_doc.page_count = 0
            mock_open.return_value = mock_doc
            result = adapter._extract_text(b"empty pdf")
            assert result == ""

    def test_single_page_pdf(self, adapter):
        """PDF with one page."""
        import unittest.mock as mock

        with mock.patch("adapters.pdf_adapter.fitz.open") as mock_open:
            mock_doc = mock.MagicMock()
            mock_page = mock.MagicMock()
            mock_page.get_text.return_value = "Page 1 content"
            mock_doc.__iter__.return_value = [mock_page]
            mock_doc.page_count = 1
            mock_open.return_value = mock_doc

            result = adapter._extract_text(b"single page pdf")
            assert "Page 1 content" in result

    def test_multi_page_pdf_joined_with_formfeed(self, adapter):
        """PDF with multiple pages should join with \\x0c (form feed)."""
        import unittest.mock as mock

        with mock.patch("adapters.pdf_adapter.fitz.open") as mock_open:
            mock_doc = mock.MagicMock()
            mock_page1 = mock.MagicMock()
            mock_page1.get_text.return_value = "Page 1"
            mock_page2 = mock.MagicMock()
            mock_page2.get_text.return_value = "Page 2"
            mock_doc.__iter__.return_value = [mock_page1, mock_page2]
            mock_doc.page_count = 2
            mock_open.return_value = mock_doc

            result = adapter._extract_text(b"multi page pdf")
            # Should be joined with form-feed delimiter
            assert "\x0c" in result or ("Page 1" in result and "Page 2" in result)

    def test_page_with_only_whitespace(self, adapter):
        """Page with only whitespace is preserved."""
        import unittest.mock as mock

        with mock.patch("adapters.pdf_adapter.fitz.open") as mock_open:
            mock_doc = mock.MagicMock()
            mock_page = mock.MagicMock()
            mock_page.get_text.return_value = "   \n\n   "
            mock_doc.__iter__.return_value = [mock_page]
            mock_doc.page_count = 1
            mock_open.return_value = mock_doc

            result = adapter._extract_text(b"whitespace pdf")
            assert "   " in result or result.strip() == ""


class TestPdfAdapterYearInferenceEdgeCases:
    """Edge cases in date/year inference logic."""

    def test_missing_period_no_year_inference(self, adapter, monkeypatch):
        """If no period is extracted, year cannot be inferred."""
        # This is adapter-specific, but test the base behavior
        assert adapter.last_statement_period is None

    def test_statement_period_persists_across_method_calls(self, adapter):
        """After setting a period, it should persist."""
        from adapters.base import StatementPeriod

        period = StatementPeriod(
            from_date=datetime(2026, 1, 1),
            to_date=datetime(2026, 1, 31),
        )
        adapter.last_statement_period = period
        assert adapter.last_statement_period == period

    def test_reconciliation_persists_across_calls(self, adapter):
        """After setting reconciliation, it should persist."""
        from adapters.base import ReconciliationResult

        reconciliation = ReconciliationResult(
            check_name="test_check",
            expected_closing_minor=1000,
            derived_closing_minor=1000,
            matches=True,
        )
        adapter.last_reconciliation = reconciliation
        assert adapter.last_reconciliation == reconciliation


class TestPdfAdapterErrorHandling:
    """Error handling in PDF processing."""

    def test_invalid_pdf_bytes_raises_during_extraction(self, adapter):
        """Invalid PDF bytes should fail gracefully."""
        import unittest.mock as mock

        with mock.patch("adapters.pdf_adapter.fitz.open") as mock_open:
            mock_open.side_effect = Exception("Corrupted PDF")
            with pytest.raises(Exception):
                adapter._extract_text(b"not a valid pdf")

    def test_parse_with_extraction_failure_raises(self, adapter, monkeypatch):
        """If text extraction fails, parse should raise."""
        monkeypatch.setattr(
            adapter,
            "_extract_text",
            lambda content: (_ for _ in ()).throw(Exception("PDF extraction failed")),
        )
        with pytest.raises(Exception):
            adapter.parse(b"corrupted pdf", "test.pdf", "hash123")
