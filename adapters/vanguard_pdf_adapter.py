"""Vanguard investment account PDF statement adapter."""

import re
from typing import Any, Dict, List, Optional

from adapters.pdf_adapter import PdfAdapter


class VanguardPdfAdapter(PdfAdapter):
    """Parse Vanguard investment statement PDFs."""

    def validate_text(self, text: str) -> bool:
        """Check if text is from Vanguard statement."""
        return (
            "Vanguard" in text
            and ("Statement" in text or "Your Regular Statement" in text)
            and "Activity" in text
        )

    def parse_transactions(self, text: str) -> List[Dict[str, Any]]:
        """
        Extract transactions from Vanguard statement.

        Vanguard format (multi-line per transaction after PDF extraction):
        Date (DD/MM/YYYY)
        Description (can be multiple lines)
        Cash amount (£ value)
        Cash balance (£ value)
        """
        transactions = []
        lines = text.split("\n")

        # Find activity section
        in_transactions = False
        current_txn_lines = []

        for line in lines:
            line = line.strip()

            # Start of activity section
            if "Activity" in line or "The transaction date is the date" in line:
                in_transactions = True
                continue

            if not in_transactions:
                continue

            # Skip empty lines and headers
            if not line or line in ["Transaction date", "Transaction details", "Cash amount", "Cash balance"]:
                continue

            # Stop at footer or end of document
            if "important information" in line.lower() or "vanguard" in line.lower() and "fund" not in line.lower():
                if current_txn_lines:
                    break

            # Check if this line starts a new transaction (matches date pattern DD/MM/YYYY)
            is_new_txn = re.match(r"^\d{2}/\d{2}/\d{4}", line)

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

        return transactions

    def _parse_transaction_lines(self, lines: List[str]) -> Optional[Dict[str, Any]]:
        """
        Parse a transaction that spans multiple lines.

        Format:
        Date (DD/MM/YYYY)
        Description (can be multiple lines)
        Cash amount (£ value, can be +/-)
        Cash balance (£ value)
        """
        if not lines or not lines[0]:
            return None

        # First line is the date
        date_str = lines[0].strip()
        date_match = re.match(r"^(\d{2}/\d{2}/\d{4})", date_str)
        if not date_match:
            return None

        date_str = date_match.group(1)

        # Collect description and amounts
        description_parts = []
        amounts = []

        for line in lines[1:]:
            line = line.strip()
            # Look for £ amounts
            if re.match(r"^[-+]?£", line):
                # Extract amount
                match = re.search(r"([-+])?£\s*([0-9,]+\.?\d*)", line)
                if match:
                    sign = match.group(1)
                    amount_str = match.group(2).replace(",", "")
                    try:
                        amount = float(amount_str)
                        if sign == "-":
                            amount = -amount
                        amounts.append(amount)
                    except ValueError:
                        pass
            else:
                description_parts.append(line)

        if not description_parts or len(amounts) < 1:
            return None

        description = " ".join(description_parts)

        # Vanguard format: Cash amount is the transaction, Cash balance is the running total
        # We only care about the cash amount (first amount)
        transaction_amount = amounts[0] if amounts else 0.0

        return {
            "date": date_str,
            "description": description,
            "amount": transaction_amount,
        }

    def generate_source_key(self, txn: Dict[str, Any], line_num: int) -> str:
        """Generate deterministic key from date + description + amount."""
        date_str = txn.get("date", "").replace("/", "")
        description = txn.get("description", "")[:15].replace(" ", "_")
        amount = str(abs(txn.get("amount", 0))).replace(".", "_")

        return f"vanguard_txn_{date_str}_{description}_{amount}"

    def detect_source_type(self) -> str:
        """Return source type."""
        return "vanguard-pdf"
