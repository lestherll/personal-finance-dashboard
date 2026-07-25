"""Natwest quarterly Statement PDF adapter.

Distinct from `natwest_pdf_adapter.py`, which targets the "Transactions"
export downloadable from online banking. Natwest issues a separate, much
richer document every ~3 months (a proper statement, not an export dump):
different section markers, a `Date | Description | Paid In(£) | Withdrawn(£)
| Balance(£)` table instead of a single signed-amount-per-line format, and a
full (unmasked) account number instead of a masked one. The two formats
share almost nothing structurally, so this is a separate adapter/source_type
rather than a branch inside the existing one.
"""

import logging
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from adapters.base import ReconciliationResult, StatementPeriod
from adapters.pdf_adapter import PdfAdapter

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^\d{1,2}\s+[A-Z]{3}(\s+\d{4})?$")
_NUMBER_RE = re.compile(r"^[\d,]+\.\d{2}$")
_HEADER_TOKENS = {
    "Date",
    "Description",
    "Paid In(£)",
    "Withdrawn(£)",
    "Balance(£)",
}
_IDENTIFIER_RE = re.compile(
    r"Account No\s*\n\s*Sort Code\s*\n\s*Page No\s*\n\s*.+?\n\s*"
    r"(\d{6,10})\s*\n\s*(\d{2}-\d{2}-\d{2})",
    re.DOTALL,
)
_PREVIOUS_BALANCE_RE = re.compile(r"Previous Balance\s*\n\s*£([\d,]+\.\d{2})")
_NEW_BALANCE_RE = re.compile(r"New Balance\s*\n\s*£([\d,]+\.\d{2})")
_FOOTER_MARKERS = ("Interest (variable)", "RETSTMT")
# e.g. "Period Covered\n14 FEB 2026 to 13 MAY 2026" - unlike Amex/natwest-pdf,
# this document prints an explicit year on both ends already.
_PERIOD_COVERED_RE = re.compile(
    r"Period Covered\s*\n\s*(\d{1,2}\s+[A-Z]{3}\s+\d{4})\s+to\s+"
    r"(\d{1,2}\s+[A-Z]{3}\s+\d{4})"
)


class NatwestStatementPdfAdapter(PdfAdapter):
    """Parse Natwest quarterly Statement PDFs."""

    def validate_text(self, text: str) -> bool:
        """Check if text is from a Natwest quarterly Statement.

        "BROUGHT FORWARD" and "Previous Balance" are specific to this
        document type - the online "Transactions" export
        (`natwest_pdf_adapter.py`) has neither.
        """
        return "BROUGHT FORWARD" in text and "Previous Balance" in text

    def _extract_account_identifier(self, text: str) -> Optional[str]:
        """Extract the full account number + sort code, e.g. '97386227_54-10-04'.

        Unlike the online export's masked identifier, this document prints
        the full account number - still hashed immediately by
        `PdfAdapter.parse()`, never stored raw.
        """
        match = _IDENTIFIER_RE.search(text)
        if not match:
            return None
        return f"{match.group(1)}_{match.group(2)}"

    @staticmethod
    def _extract_period_covered(text: str) -> Optional[Tuple[datetime, datetime]]:
        """Extract the statement's own "Period Covered" range.

        Unlike Amex/natwest-pdf, both dates already carry an explicit year
        here, so no year-inference is needed - this is purely for B3
        coverage tracking, not for dating transactions (this format's
        per-line dates already carry years where printed).
        """
        match = _PERIOD_COVERED_RE.search(text)
        if not match:
            return None
        from_str, to_str = match.groups()
        try:
            return (
                datetime.strptime(from_str, "%d %b %Y"),
                datetime.strptime(to_str, "%d %b %Y"),
            )
        except ValueError:
            return None

    def parse_transactions(self, text: str) -> List[Dict[str, Any]]:
        """Extract transactions from the Date/Description/Paid In/Withdrawn/
        Balance table.

        Plain-text extraction loses column position, so a lone number after
        a description is ambiguous - it could be either the Paid In or
        Withdrawn value. Rather than disambiguate that, the trailing number
        in each row (always the unambiguous Balance(£) figure) is treated as
        the primary parsed value, and the transaction's signed amount is
        derived as `balance - previous_balance` - correct by construction,
        and sidesteps the column ambiguity entirely.

        The date is only printed once per calendar day; a second same-day
        transaction omits the date line, so the last-seen date carries
        forward.
        """
        self.last_reconciliation = None
        self.last_statement_period = None
        period = self._extract_period_covered(text)
        if period:
            self.last_statement_period = StatementPeriod(period[0], period[1])

        account_identifier = self._extract_account_identifier(text)
        lines = [line.strip() for line in text.split("\n")]
        lines = [line for line in lines if line]

        transactions: List[Dict[str, Any]] = []
        last_date: Optional[str] = None
        running_balance: Optional[Decimal] = None

        i = 0
        n = len(lines)
        while i < n:
            line = lines[i]

            if any(line.startswith(marker) for marker in _FOOTER_MARKERS):
                break

            if line in _HEADER_TOKENS:
                i += 1
                continue

            if _DATE_RE.match(line):
                last_date = line
                i += 1
                continue

            if _NUMBER_RE.match(line):
                # Stray number outside a transaction row - defensively skip.
                i += 1
                continue

            # Anything else starts a description block for the current
            # (possibly carried-forward) date.
            description_parts = []
            while (
                i < n
                and not _NUMBER_RE.match(lines[i])
                and not _DATE_RE.match(lines[i])
            ):
                description_parts.append(lines[i])
                i += 1

            numbers = []
            while i < n and _NUMBER_RE.match(lines[i]):
                numbers.append(lines[i])
                i += 1

            if not numbers:
                continue

            try:
                balance = Decimal(numbers[-1].replace(",", ""))
            except InvalidOperation:
                continue

            description = " ".join(description_parts)

            if description == "BROUGHT FORWARD":
                running_balance = balance
                continue

            if running_balance is None:
                # No opening anchor seen yet - can't derive a signed amount.
                running_balance = balance
                continue

            amount = balance - running_balance
            running_balance = balance

            txn: Dict[str, Any] = {
                "date": last_date or "",
                "description": description,
                "amount": float(amount),
                "balance": float(balance),
            }
            if account_identifier:
                txn["_account_identifier_raw"] = account_identifier
            transactions.append(txn)

        self._check_reconciliation(text, running_balance)

        return transactions

    def _check_reconciliation(
        self, text: str, computed_final: Optional[Decimal]
    ) -> None:
        """Non-blocking sanity check against the statement's own summary figures."""
        if computed_final is None:
            return

        new_balance_match = _NEW_BALANCE_RE.search(text)
        if not new_balance_match:
            return
        try:
            expected = Decimal(new_balance_match.group(1).replace(",", ""))
        except InvalidOperation:
            return

        matches = computed_final == expected
        self.last_reconciliation = ReconciliationResult(
            check_name="natwest_statement_new_balance",
            expected_closing=expected,
            derived_closing=computed_final,
            matches=matches,
        )
        if not matches:
            logger.warning(
                "Natwest statement: final parsed balance %.2f does not "
                "match statement's printed New Balance %.2f - the "
                "transaction table may not have parsed completely.",
                computed_final,
                expected,
            )

    def generate_source_key(
        self,
        txn: Dict[str, Any],
        line_num: int,
        account_identifier: Optional[str] = None,
    ) -> str:
        """Generate deterministic key from account + date + description + amount."""
        date_str = txn.get("date", "").replace(" ", "")
        description = txn.get("description", "")[:10].replace(" ", "_")
        amount = str(abs(txn.get("amount", 0))).replace(".", "_")
        account_part = f"{account_identifier}_" if account_identifier else ""

        return f"natwest_statement_txn_{account_part}{date_str}_{description}_{amount}"

    def detect_source_type(self) -> str:
        """Return source type."""
        return "natwest-statement"
