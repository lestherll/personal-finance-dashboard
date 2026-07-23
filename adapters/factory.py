"""Adapter factory for auto-detecting and routing to the right adapter."""

import logging
from typing import List

from adapters.base import DataSourceAdapter, RawRecord
from adapters.monzo_adapter import MonzoAdapter
from adapters.natwest_adapter import NatwelstAdapter
from adapters.vanguard_adapter import VanguardAdapter

logger = logging.getLogger(__name__)


class AdapterFactory:
    """Auto-detect and route to the right adapter."""

    def __init__(self):
        # Registry of all adapters—add new ones here
        self.adapters: List[DataSourceAdapter] = [
            MonzoAdapter(),
            NatwelstAdapter(),
            VanguardAdapter(),
        ]

    def detect_adapter(self, file_content: str) -> DataSourceAdapter:
        """
        Try each adapter; highest confidence wins.

        Raises:
            ValueError if no adapter confidence >= 0.8 or tie at top
        """
        results = [
            (adapter, *adapter.validate(file_content)) for adapter in self.adapters
        ]

        # Filter to valid matches
        valid_matches = [(a, conf) for a, is_valid, conf in results if is_valid]

        if not valid_matches:
            raise ValueError(
                "File format not recognized. Supported: Monzo, Natwest, Vanguard"
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

    def ingest(self, file_content: str, filename: str, file_hash: str) -> List[RawRecord]:
        """Single entry point: detect + parse."""
        adapter = self.detect_adapter(file_content)
        records = adapter.parse(file_content, filename, file_hash)

        is_valid, confidence = adapter.validate(file_content)
        logger.info(
            f"✓ Detected {adapter.detect_source_type()} format "
            f"(confidence {confidence:.1%})"
        )
        logger.info(f"✓ Parsed {len(records)} records from {filename}")

        return records
