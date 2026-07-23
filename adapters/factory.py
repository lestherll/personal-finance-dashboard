"""Adapter factory for auto-detecting and routing to the right adapter."""

import logging
from typing import List, Union

from adapters.base import DataSourceAdapter, RawRecord
from adapters.monzo_adapter import MonzoAdapter
from adapters.natwest_adapter import NatwestAdapter
from adapters.vanguard_adapter import VanguardAdapter
from adapters.kroo_pdf_adapter import KrooPdfAdapter
from adapters.natwest_pdf_adapter import NatwestPdfAdapter
from adapters.first_direct_pdf_adapter import FirstDirectPdfAdapter
from adapters.amex_pdf_adapter import AmexPdfAdapter
from adapters.vanguard_pdf_adapter import VanguardPdfAdapter

logger = logging.getLogger(__name__)


class AdapterFactory:
    """Auto-detect and route to the right adapter (CSV and PDF)."""

    def __init__(self):
        # CSV adapters (handle string content)
        self.csv_adapters: List[DataSourceAdapter] = [
            MonzoAdapter(),
            NatwestAdapter(),
            VanguardAdapter(),
        ]

        # PDF adapters (handle bytes content)
        self.pdf_adapters: List[DataSourceAdapter] = [
            KrooPdfAdapter(),
            NatwestPdfAdapter(),
            FirstDirectPdfAdapter(),
            AmexPdfAdapter(),
            VanguardPdfAdapter(),
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
            raise ValueError(
                f"File format not recognized ({content_type}). "
                f"CSV: Monzo, Natwest, Vanguard | "
                f"PDF: Kroo, Natwest, First Direct, American Express, Vanguard"
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
