"""Adapter factory for auto-detecting and routing to the right adapter."""

import logging
from typing import List, Optional, Set, Union

from adapters.base import DataSourceAdapter, RawRecord
from adapters.monzo_adapter import MonzoAdapter
from adapters.natwest_adapter import NatwestAdapter
from adapters.vanguard_adapter import VanguardAdapter
from adapters.kroo_pdf_adapter import KrooPdfAdapter
from adapters.natwest_pdf_adapter import NatwestPdfAdapter
from adapters.natwest_statement_pdf_adapter import NatwestStatementPdfAdapter
from adapters.first_direct_pdf_adapter import FirstDirectPdfAdapter
from adapters.amex_pdf_adapter import AmexPdfAdapter
from adapters.vanguard_pdf_adapter import VanguardPdfAdapter
from adapters.monzo_pdf_adapter import MonzoPdfAdapter
from adapters.chase_pdf_adapter import ChasePdfAdapter

logger = logging.getLogger(__name__)


class AdapterFactory:
    """Auto-detect and route to the right adapter (CSV and PDF)."""

    CSV_SOURCE_TYPES: Set[str] = {"monzo", "natwest", "vanguard"}
    PDF_SOURCE_TYPES: Set[str] = {
        "kroo",
        "natwest-pdf",
        "natwest-statement",
        "firstdirect",
        "amex",
        "vanguard-pdf",
        "monzo-pdf",
        "chase",
    }

    def __init__(self, disabled_source_types: Optional[Set[str]] = None):
        """
        Args:
            disabled_source_types: source_type strings to exclude from
                detection/routing, e.g. AdapterFactory.CSV_SOURCE_TYPES to
                disable all CSV adapters (real exports are PDF-only, so
                CSV parsing correctness can't be verified against them).
        """
        self.disabled_source_types = disabled_source_types or set()

        unknown = self.disabled_source_types - (
            self.CSV_SOURCE_TYPES | self.PDF_SOURCE_TYPES
        )
        if unknown:
            raise ValueError(
                f"Unknown source_type(s) in disabled_source_types: {unknown}"
            )

        # CSV adapters (handle string content)
        all_csv_adapters: List[DataSourceAdapter] = [
            MonzoAdapter(),
            NatwestAdapter(),
            VanguardAdapter(),
        ]

        # PDF adapters (handle bytes content)
        all_pdf_adapters: List[DataSourceAdapter] = [
            KrooPdfAdapter(),
            NatwestPdfAdapter(),
            NatwestStatementPdfAdapter(),
            FirstDirectPdfAdapter(),
            AmexPdfAdapter(),
            VanguardPdfAdapter(),
            MonzoPdfAdapter(),
            ChasePdfAdapter(),
        ]

        self.csv_adapters = [
            a
            for a in all_csv_adapters
            if a.detect_source_type() not in self.disabled_source_types
        ]
        self.pdf_adapters = [
            a
            for a in all_pdf_adapters
            if a.detect_source_type() not in self.disabled_source_types
        ]

    def detect_adapter(self, file_content: Union[str, bytes]) -> DataSourceAdapter:
        """
        Auto-detect adapter based on content type (CSV or PDF).

        Tries CSV adapters first (faster), then PDF adapters.

        Raises:
            ValueError if no adapter confidence >= 0.8 or tie at top
        """
        # Determine content type
        is_pdf = isinstance(file_content, bytes)

        if is_pdf:
            adapters_to_try = self.pdf_adapters
            content_type = "PDF"
        else:
            adapters_to_try = self.csv_adapters
            content_type = "CSV"

        # Try all adapters and score them
        results = [
            (adapter, *adapter.validate(file_content)) for adapter in adapters_to_try
        ]

        # Filter to valid matches
        valid_matches = [(a, conf) for a, is_valid, conf in results if is_valid]

        if not valid_matches:
            enabled = (
                ", ".join(a.detect_source_type() for a in adapters_to_try) or "none"
            )
            raise ValueError(
                f"File format not recognized ({content_type}). "
                f"Enabled {content_type} adapters: {enabled}"
            )

        # Sort by confidence
        valid_matches.sort(key=lambda x: x[1], reverse=True)
        best_adapter, best_conf = valid_matches[0]

        # Check for ambiguity (tie at top)
        if len(valid_matches) > 1:
            second_conf = valid_matches[1][1]
            if abs(best_conf - second_conf) < 0.05:
                raise ValueError(
                    f"File format ambiguous: {best_adapter.detect_source_type()} "
                    f"({best_conf:.1%}) vs {valid_matches[1][0].detect_source_type()} "
                    f"({second_conf:.1%})"
                )

        return best_adapter

    def ingest(
        self, file_content: Union[str, bytes], filename: str, file_hash: str
    ) -> List[RawRecord]:
        """Single entry point: detect + parse (CSV or PDF)."""
        adapter = self.detect_adapter(file_content)
        records = adapter.parse(file_content, filename, file_hash)

        is_valid, confidence = adapter.validate(file_content)
        logger.info(
            f"✓ Detected {adapter.detect_source_type()} format "
            f"(confidence {confidence:.1%})"
        )
        logger.info(f"✓ Parsed {len(records)} records from {filename}")

        return records
