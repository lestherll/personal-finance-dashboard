"""Natwest bank PDF statement adapter."""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from adapters.base import StatementPeriod
from adapters.pdf_adapter import PdfAdapter, resolve_year_in_period

# e.g. "From  01/01/2026  To  31/05/2026" - full DD/MM/YYYY dates, unlike
# Amex's period (which only carries a year on the closing date).
_PERIOD_RE = re.compile(r"From\s+(\d{2}/\d{2}/\d{4})\s+To\s+(\d{2}/\d{2}/\d{4})")


class NatwestPdfAdapter(PdfAdapter):
    """Parse Natwest PDF statements."""

    def validate_text(self, text: str) -> bool:
        """Check if text is from the Natwest online "Transactions" export.

        Deliberately narrow: the generic "NatWest" + "Transaction" check
        this used to use also matched the quarterly Statement PDF (a
        structurally different document handled by
        `NatwestStatementPdfAdapter`), which produced an exact-confidence
        validation tie between the two adapters. "Your transactions" is the
        online export's section heading and doesn't appear in the Statement
        PDF.
        """
        return "NatWest" in text and "Your transactions" in text

    def _extract_account_identifier(self, text: str) -> Optional[str]:
        """Extract masked account + sort code, e.g. '*****227 · 54-10-04'."""
        match = re.search(r"(\*+\d+)\s*\xb7\s*([\d-]+)", text)
        if not match:
            return None
        return f"{match.group(1)}_{match.group(2)}"

    def _extract_statement_period(
        self, text: str
    ) -> Optional[Tuple[datetime, datetime]]:
        """Extract the statement period, e.g. 'From 01/01/2026 To 31/05/2026'."""
        match = _PERIOD_RE.search(text)
        if not match:
            return None
        from_str, to_str = match.groups()
        try:
            return (
                datetime.strptime(from_str, "%d/%m/%Y"),
                datetime.strptime(to_str, "%d/%m/%Y"),
            )
        except ValueError:
            return None

    def parse_transactions(self, text: str) -> List[Dict[str, Any]]:
        """
        Extract transactions from Natwest statement.

        Natwest format (multi-line per transaction after PDF extraction):
        Date
        Description (can be multiple lines)
        Amounts (Paid in and/or Paid out)
        """
        self.last_statement_period = None
        account_identifier = self._extract_account_identifier(text)
        period = self._extract_statement_period(text)
        transactions = []
        lines = text.split("\n")

        # Find transaction section
        in_transactions = False
        current_txn_lines = []

        for line in lines:
            line = line.strip()

            # Start of transactions section
            if "Your transactions" in line or "Transaction details" in line:
                in_transactions = True
                continue

            if not in_transactions:
                continue

            # Stop at footer
            if "Downloaded from" in line or "National Westminster" in line:
                break

            # Skip empty lines and headers
            if not line or line in [
                "Date",
                "Description",
                "Type",
                "Paid in",
                "Paid out",
            ]:
                continue

            # Check if this line starts a new transaction (matches date pattern DD Mmm)
            is_new_txn = re.match(r"^\d{1,2}\s+\w{3}", line)

            if is_new_txn and current_txn_lines:
                # Process the accumulated transaction
                txn = self._parse_transaction_lines(current_txn_lines)
                if txn:
                    transactions.append(txn)
                current_txn_lines = [line]
            elif is_new_txn or current_txn_lines:
                current_txn_lines.append(line)

        # Don't forget the last transaction
        if current_txn_lines:
            txn = self._parse_transaction_lines(current_txn_lines)
            if txn:
                transactions.append(txn)

        if period:
            from_date, to_date = period
            self.last_statement_period = StatementPeriod(from_date, to_date)
            for txn in transactions:
                dated = resolve_year_in_period(txn["date"], from_date, to_date)
                if dated:
                    txn["date"] = dated

        if account_identifier:
            for txn in transactions:
                txn["_account_identifier_raw"] = account_identifier

        return transactions

    def _parse_transaction_lines(self, lines: List[str]) -> Optional[Dict[str, Any]]:
        """
        Parse a transaction that spans multiple lines.

        Natwest format (after PDF extraction):
        Date (DD Mmm)
        Description (can be multiple lines)
        Type (optional)
        Amount (with -£ for debit, +£ or £ for credit)
        """
        if not lines or not lines[0]:
            return None

        # First line is the date
        date_str = lines[0].strip()
        date_match = re.match(r"^(\d{1,2}\s+\w{3})", date_str)
        if not date_match:
            return None

        date_str = date_match.group(1)

        # Collect description and amounts
        description_parts = []
        amount = None

        for line in lines[1:]:
            line = line.strip()
            # Look for amount (starts with optional +/- then £)
            if re.match(r"^[-+]?£", line):
                # This is the amount line
                match = re.search(r"([-+])?£\s*([0-9,]+\.?\d*)", line)
                if match:
                    sign = match.group(1)
                    amount_str = match.group(2).replace(",", "")
                    try:
                        amount = float(amount_str)
                        if sign == "-":
                            amount = -amount
                    except ValueError:
                        pass
                break
            else:
                # This is description or type
                description_parts.append(line)

        if not description_parts or amount is None:
            return None

        description = " ".join(description_parts)

        return {
            "date": date_str,
            "description": description,
            "amount": amount,
        }

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

        return f"natwest_pdf_txn_{account_part}{date_str}_{description}_{amount}"

    def detect_source_type(self) -> str:
        """Return source type."""
        return "natwest-pdf"
