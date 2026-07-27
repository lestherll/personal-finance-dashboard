"""Adapter factory for auto-detecting and routing to the right adapter."""

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Set, Union

from adapters.base import (
    DataSourceAdapter,
    RawRecord,
    ReconciliationResult,
    StatementPeriod,
)
from adapters.monzo_adapter import MonzoAdapter
from adapters.kroo_pdf_adapter import KrooPdfAdapter
from adapters.natwest_transactions_pdf_adapter import NatwestTransactionsPdfAdapter
from adapters.natwest_statement_pdf_adapter import NatwestStatementPdfAdapter
from adapters.first_direct_pdf_adapter import FirstDirectPdfAdapter
from adapters.amex_pdf_adapter import AmexPdfAdapter
from adapters.vanguard_pdf_adapter import VanguardPdfAdapter
from adapters.monzo_pdf_adapter import MonzoPdfAdapter
from adapters.chase_pdf_adapter import ChasePdfAdapter
from adapters.monzo_flex_pdf_adapter import MonzoFlexPdfAdapter

logger = logging.getLogger(__name__)


class AdapterDetectionError(ValueError):
    """Base class for AdapterFactory.detect_adapter() failures."""


class UnrecognizedFormatError(AdapterDetectionError):
    """Raised when no adapter recognizes the uploaded file at all."""

    def __init__(self, content_type: str, enabled: str):
        self.content_type = content_type
        message = (
            f"File format not recognized ({content_type}): we don't support "
            f"this {content_type} statement format yet. Supported "
            f"{content_type} adapters: {enabled}."
        )
        super().__init__(message)


class AmbiguousFormatError(AdapterDetectionError):
    """Raised when two or more adapters match with indistinguishable confidence."""

    def __init__(
        self, best_type: str, best_conf: float, second_type: str, second_conf: float
    ):
        self.best_type = best_type
        self.second_type = second_type
        message = (
            f"File format ambiguous: this could be {best_type} "
            f"({best_conf:.1%}) or {second_type} ({second_conf:.1%}) and "
            "we're not confident enough to pick automatically."
        )
        super().__init__(message)


@dataclass
class IngestResult:
    """Return value of AdapterFactory.ingest(): the parsed records plus any
    whole-file facts (reconciliation, statement period) the adapter set on
    itself while parsing - see DataSourceAdapter.last_reconciliation /
    last_statement_period."""

    records: List[RawRecord]
    reconciliation: Optional[ReconciliationResult]
    statement_period: Optional[StatementPeriod]
    # Additive multi-result channel - see DataSourceAdapter.last_reconciliations.
    # Empty for every adapter except ones that need more than one
    # reconciliation result per file (e.g. Vanguard's per-wrapper checks).
    reconciliations: List[ReconciliationResult] = field(default_factory=list)
    source_type: str = ""
    adapter: str = ""
    parser_version: str = "1"


class AdapterFactory:
    """Auto-detect and route to the right adapter (CSV and PDF)."""

    CSV_SOURCE_TYPES: Set[str] = {"monzo"}
    PDF_SOURCE_TYPES: Set[str] = {
        "kroo",
        "natwest-transactions",
        "natwest-statement",
        "firstdirect",
        "amex",
        "vanguard-pdf",
        "monzo-pdf",
        "chase",
        "monzo-flex",
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
        ]

        # PDF adapters (handle bytes content)
        all_pdf_adapters: List[DataSourceAdapter] = [
            KrooPdfAdapter(),
            NatwestTransactionsPdfAdapter(),
            NatwestStatementPdfAdapter(),
            FirstDirectPdfAdapter(),
            AmexPdfAdapter(),
            VanguardPdfAdapter(),
            MonzoPdfAdapter(),
            ChasePdfAdapter(),
            MonzoFlexPdfAdapter(),
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
            logger.info(
                "Unsupported format attempted: no %s adapter matched (enabled: %s)",
                content_type,
                enabled,
            )
            raise UnrecognizedFormatError(content_type, enabled)

        # Sort by confidence
        valid_matches.sort(key=lambda x: x[1], reverse=True)
        best_adapter, best_conf = valid_matches[0]

        # Check for ambiguity (tie at top)
        if len(valid_matches) > 1:
            second_conf = valid_matches[1][1]
            if abs(best_conf - second_conf) < 0.05:
                second_type = valid_matches[1][0].detect_source_type()
                logger.info(
                    "Unsupported format attempted: ambiguous match between %s "
                    "(%.1f%%) and %s (%.1f%%)",
                    best_adapter.detect_source_type(),
                    best_conf * 100,
                    second_type,
                    second_conf * 100,
                )
                raise AmbiguousFormatError(
                    best_adapter.detect_source_type(),
                    best_conf,
                    second_type,
                    second_conf,
                )

        return best_adapter

    def ingest(
        self, file_content: Union[str, bytes], filename: str, file_hash: str
    ) -> IngestResult:
        """Single entry point: detect + parse (CSV or PDF)."""
        adapter = self.detect_adapter(file_content)
        records = adapter.parse(file_content, filename, file_hash)

        is_valid, confidence = adapter.validate(file_content)
        logger.info(
            f"✓ Detected {adapter.detect_source_type()} format "
            f"(confidence {confidence:.1%})"
        )
        logger.info(f"✓ Parsed {len(records)} records from {filename}")

        return IngestResult(
            records=records,
            reconciliation=getattr(adapter, "last_reconciliation", None),
            statement_period=getattr(adapter, "last_statement_period", None),
            reconciliations=getattr(adapter, "last_reconciliations", None) or [],
            source_type=adapter.detect_source_type(),
            adapter=adapter.__class__.__name__,
            parser_version=adapter.PARSER_VERSION,
        )
