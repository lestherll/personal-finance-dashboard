"""American Express credit card PDF statement adapter."""

import re
from typing import Any, Dict, List, Optional

from adapters.pdf_adapter import PdfAdapter


class AmexPdfAdapter(PdfAdapter):
    """Parse American Express PDF statements."""

    def validate_text(self, text: str) -> bool:
        """Check if text is from American Express statement."""
        return (
            "American Express" in text
            and ("Preferred Rewards" in text or "Credit Card" in text)
        )

    def parse_transactions(self, text: str) -> List[Dict[str, Any]]:
        """
        Extract transactions from American Express statement.

        AmEx format:
        Transaction Date | Process Date | Transaction Details | Amount £
        """
        transactions = []

        lines = text.split("\n")

        # Find transaction section
        in_transactions = False
        for line in lines:
            # Look for lines that start a transaction (date pattern at start)
            if in_transactions:
                txn = self._parse_transaction_row(line)
                if txn:
                    transactions.append(txn)
                    continue

            # Start of transactions section
            if "Merchant" in line or ("Apr" in line and "May" in line):
                # This heuristic looks for month names which indicate transaction area
                in_transactions = True

            # Alternative: look for detailed transaction markers
            if "Transaction Details" in line:
                in_transactions = True

        return transactions

    def _parse_transaction_row(self, line: str) -> Optional[Dict[str, Any]]:
        """
        Parse a single transaction row.

        Format: DD Mmm [DD Mmm] | Merchant details | £amount
        """
        line = line.strip()
        if not line or len(line) < 10:
            return None

        # Match date at start (e.g., "Apr 28", "May 1")
        date_match = re.match(r"^([A-Z][a-z]{2})\s+(\d{1,2})", line)
        if not date_match:
            return None

        month = date_match.group(1)
        day = date_match.group(2)
        date_str = f"{day} {month}"

        # Find amount at end (£ format)
        amount_match = re.search(r"£\s*([0-9,]+\.?\d*)", line)
        if not amount_match:
            return None

        amount_str = amount_match.group(1).replace(",", "")
        try:
            amount = -float(amount_str)  # AmEx shows debit amounts
        except ValueError:
            return None

        # Description is between date and amount
        rest = line[len(date_match.group(0)):].strip()
        amount_start = rest.rfind("£")
        if amount_start > 0:
            description = rest[:amount_start].strip()
        else:
            description = rest

        # Skip empty descriptions
        if not description:
            return None

        return {
            "date": date_str,
            "description": description,
            "amount": amount,
        }

    def generate_source_key(self, txn: Dict[str, Any], line_num: int) -> str:
        """Generate deterministic key from date + description + amount."""
        date_str = txn.get("date", "").replace(" ", "")
        description = txn.get("description", "")[:10].replace(" ", "_")
        amount = str(abs(txn.get("amount", 0))).replace(".", "_")

        return f"amex_txn_{date_str}_{description}_{amount}"

    def detect_source_type(self) -> str:
        """Return source type."""
        return "amex"
