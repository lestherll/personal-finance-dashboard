"""Base class for PDF statement adapters."""

import re
from abc import abstractmethod
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Union

import fitz

from adapters.base import (
    DataSourceAdapter,
    RawRecord,
    hash_account_identifier,
    make_bronze_record_id,
)

_DECIMAL_RE = re.compile(r"[-+]?[\d,]+\.\d{2}")

# Real statements can list a transaction a day or two outside the declared
# "From X to Y" period (e.g. a purchase processed right at the boundary) -
# a few days' tolerance absorbs that without risking the wrong year, since
# the alternative candidate year is ~365 days away, not a few days away.
_PERIOD_BOUNDARY_TOLERANCE = timedelta(days=3)


def resolve_year_in_period(
    day_month: str, from_date: datetime, to_date: datetime
) -> Optional[str]:
    """Attach a year to a 'DD Mon' string using a known statement period.

    Tries both the period's start and end year, preferring whichever places
    the date inside [from_date, to_date] (with a small boundary tolerance) -
    handles a statement period that crosses a year boundary (e.g. "20 Dec"
    to "19 Jan 2026"). Returns None if neither year places the date in
    range (caller should fall back to upload-timestamp inference, e.g.
    silver_transformer._infer_dated_with_year).
    """
    window_start = from_date - _PERIOD_BOUNDARY_TOLERANCE
    window_end = to_date + _PERIOD_BOUNDARY_TOLERANCE
    for year in {from_date.year, to_date.year}:
        try:
            candidate = datetime.strptime(f"{day_month} {year}", "%d %b %Y")
        except ValueError:
            continue
        if window_start <= candidate <= window_end:
            return f"{day_month} {year}"
    return None


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
            occurrence_counts: Dict[str, int] = {}
            for idx, txn in enumerate(transactions, start=1):
                raw_identifier = txn.pop("_account_identifier_raw", None)
                account_identifier = (
                    hash_account_identifier(raw_identifier) if raw_identifier else None
                )

                # generate_source_key runs before popping record_type, so
                # adapters that produce multiple record types (e.g. Vanguard's
                # holdings vs transactions) can still branch key format on it.
                source_key = self.generate_source_key(txn, idx, account_identifier)

                # Two transactions that look identical (same date/description/
                # amount) within one statement are always two real, distinct
                # transactions - never a parsing duplicate - so the Nth
                # occurrence of a given content-key within this file gets a
                # disambiguating suffix. The *first* occurrence keeps the key
                # unsuffixed, so a genuinely unique transaction that also
                # appears (once) in an overlapping-period statement still
                # dedupes correctly across files - only same-file repeats are
                # disambiguated.
                occurrence = occurrence_counts.get(source_key, 0)
                occurrence_counts[source_key] = occurrence + 1
                if occurrence > 0:
                    source_key = f"{source_key}_dup{occurrence}"

                record_type = txn.pop("record_type", "transaction")
                records.append(
                    RawRecord(
                        source_key=source_key,
                        source_type=self.detect_source_type(),
                        raw_data=txn,
                        filename=filename,
                        file_hash=file_hash,
                        upload_timestamp=datetime.now(),
                        line_number=idx,
                        account_identifier=account_identifier,
                        record_type=record_type,
                        bronze_record_id=make_bronze_record_id(
                            file_hash, record_type, idx
                        ),
                        source_ordinal=idx,
                    )
                )

            return records

        except Exception as e:
            raise ValueError(f"Failed to parse PDF: {e}")

    @staticmethod
    def _extract_text(file_content: bytes) -> str:
        """Extract text from PDF using PyMuPDF.

        Pages are joined with a form-feed ("\\x0c") sentinel so adapters that
        need per-page structure (e.g. Amex's column-flattened tables) can
        split on it. It's whitespace, so str.strip() reduces it to "" and
        existing line-based adapters that ignore page boundaries are unaffected.
        """
        doc = fitz.open(stream=file_content, filetype="pdf")
        text = ""
        for page in doc:
            text += page.get_text() + "\n\x0c\n"
        doc.close()
        return text

    @staticmethod
    def _parse_decimal(text: str) -> Optional[Decimal]:
        """Parse a £/comma-formatted amount to Decimal, e.g. "£1,234.56", "-£47.22".

        Strips "£" and thousands-commas and keeps an optional leading sign.
        Any trailing marker such as "CR" is stripped and ignored - direction
        (credit vs debit) is context-specific and left to the caller, the
        same way existing float-based amount parsing already handles it.
        """
        stripped = (
            text.upper().replace("CR", "").replace("£", "").replace(",", "").strip()
        )
        match = _DECIMAL_RE.search(stripped)
        if not match:
            return None
        try:
            return Decimal(match.group(0))
        except InvalidOperation:
            return None

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
        - amount_minor: int (transaction amount in minor units, signed)

        May also include:
        - balance_minor: int (running balance in minor units)
        - amount_text: str - original source text for the amount
        - _account_identifier_raw: str - unmasked account/card identifier,
          extracted from the statement text. Hashed by parse() before storage.
        - record_type: str - "transaction" (default) or "holding", for
          adapters that can produce both (e.g. Vanguard PDF).

        Returns:
            List of transaction dictionaries
        """

    @abstractmethod
    def generate_source_key(
        self,
        txn: Dict[str, Any],
        line_num: int,
        account_identifier: Optional[str] = None,
    ) -> str:
        """Generate deterministic source key for transaction."""

    def detect_source_type(self) -> str:
        """Return source type (e.g., 'kroo', 'natwest-transactions', 'firstdirect', 'amex')."""
        # Default implementation: lowercase class name without 'Adapter'
        class_name = self.__class__.__name__
        return class_name.replace("Adapter", "").lower()
