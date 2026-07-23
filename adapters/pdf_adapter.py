"""Base class for PDF statement adapters."""

from abc import abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Union

import fitz

from adapters.base import DataSourceAdapter, RawRecord


class PdfAdapter(DataSourceAdapter):
    """Base class for all PDF statement adapters."""

    def validate(self, file_content: Union[str, bytes]) -> tuple[bool, float]:
        """
        Validate PDF content.

        Args:
            file_content: PDF bytes (will be str from factory, but handle both)

        Returns:
            (is_valid, confidence)
        """
        if isinstance(file_content, str):
            # Fallback if string passed - won't work for PDFs
            return False, 0.0

        try:
            text = self._extract_text(file_content)
            if not text or len(text) < 50:
                return False, 0.0

            # Delegate to subclass for bank-specific validation
            is_match = self.validate_text(text)
            confidence = 0.95 if is_match else 0.0
            return is_match, confidence

        except Exception:
            return False, 0.0

    def parse(
        self, file_content: Union[str, bytes], filename: str, file_hash: str
    ) -> List[RawRecord]:
        """
        Parse PDF and extract transactions.

        Args:
            file_content: PDF bytes
            filename: Original filename
            file_hash: SHA256 hash of file

        Returns:
            List of RawRecord objects
        """
        if isinstance(file_content, str):
            return []

        try:
            text = self._extract_text(file_content)
            transactions = self.parse_transactions(text)

            records = []
            for idx, txn in enumerate(transactions, start=1):
                source_key = self.generate_source_key(txn, idx)
                records.append(
                    RawRecord(
                        source_key=source_key,
                        source_type=self.detect_source_type(),
                        raw_data=txn,
                        filename=filename,
                        file_hash=file_hash,
                        upload_timestamp=datetime.now(),
                        line_number=idx,
                    )
                )

            return records

        except Exception as e:
            raise ValueError(f"Failed to parse PDF: {e}")

    @staticmethod
    def _extract_text(file_content: bytes) -> str:
        """Extract text from PDF using PyMuPDF."""
        doc = fitz.open(stream=file_content, filetype="pdf")
        text = ""
        for page in doc:
            text += page.get_text() + "\n"
        doc.close()
        return text

    @abstractmethod
    def validate_text(self, text: str) -> bool:
        """
        Check if text matches this bank's format.

        Subclasses should look for bank-specific markers (bank name, account type, etc).
        """

    @abstractmethod
    def parse_transactions(self, text: str) -> List[Dict[str, Any]]:
        """
        Parse transaction rows from extracted text.

        Should return list of dicts with at minimum:
        - date: str (transaction date)
        - description: str (merchant/transaction description)
        - amount: float (transaction amount, signed)

        Returns:
            List of transaction dictionaries
        """

    @abstractmethod
    def generate_source_key(self, txn: Dict[str, Any], line_num: int) -> str:
        """Generate deterministic source key for transaction."""

    def detect_source_type(self) -> str:
        """Return source type (e.g., 'kroo', 'natwest-pdf', 'firstdirect', 'amex')."""
        # Default implementation: lowercase class name without 'Adapter'
        class_name = self.__class__.__name__
        return class_name.replace("Adapter", "").lower()
