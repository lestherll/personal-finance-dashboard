"""Kroo bank PDF statement adapter."""

import re
from typing import Any, Dict, List, Optional

from adapters.pdf_adapter import PdfAdapter


class KrooPdfAdapter(PdfAdapter):
    """Parse Kroo PDF statements."""

    def validate_text(self, text: str) -> bool:
        """Check if text is from Kroo statement.

        Must match the literal statement header, not just "Kroo" + "Sort code"
        anywhere in the text - other banks' statements can incidentally contain
        both (e.g. a transaction description referencing a Kroo account, plus
        the statement's own "Sort code:" line), causing a false positive.
        """
        return "Kroo Current Account" in text

    def _extract_account_identifier(self, text: str) -> Optional[str]:
        """Extract 'Sort code: XX-XX-XX' + 'Account number: XXXXXXXX' from the header."""
        sort_code_match = re.search(r"Sort code:\s*([\d-]+)", text)
        account_match = re.search(r"Account number:\s*(\d+)", text)
        if not sort_code_match or not account_match:
            return None
        return f"{sort_code_match.group(1)}_{account_match.group(1)}"

    def parse_transactions(self, text: str) -> List[Dict[str, Any]]:
        """
        Extract transactions from Kroo statement.

        Kroo format (multi-line per transaction):
        Date | Description | Out | In | Balance
        """
        account_identifier = self._extract_account_identifier(text)
        transactions = []
        lines = text.split("\n")

        # Find transaction section
        in_transactions = False
        current_txn_lines = []

        for i, line in enumerate(lines):
            line = line.strip()

            # Start of transactions section
            if "Account transactions" in line:
                in_transactions = True
                continue

            if not in_transactions:
                continue

            # Stop at closing balance or footer
            if "Closing balance" in line or "If something doesn't look right" in line:
                break

            # Skip empty lines and headers
            if not line or line in [
                "Date",
                "Description",
                "Out",
                "In",
                "Balance",
                "Page",
            ]:
                continue

            # Check if this line starts a new transaction (matches date pattern)
            is_new_txn = re.match(r"^\d{2}\s+\w+\s+\d{4}", line)

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

        if account_identifier:
            for txn in transactions:
                txn["_account_identifier_raw"] = account_identifier

        return transactions

    def _parse_transaction_lines(self, lines: List[str]) -> Optional[Dict[str, Any]]:
        """
        Parse a transaction that spans multiple lines.

        Kroo PDF format (after text extraction):
        Date
        Description line 1
        Description line 2 (optional)
        £out_amount (optional)
        £in_amount (optional)
        £balance
        """
        if not lines or not lines[0]:
            return None

        # First line is the date
        date_str = lines[0].strip()
        date_match = re.match(r"^(\d{2}\s+\w+\s+\d{4})", date_str)
        if not date_match:
            return None

        date_str = date_match.group(1)

        # Collect description and amounts
        description_parts = []
        amounts = []

        for line in lines[1:]:
            line = line.strip()
            if "£" in line:
                # Extract amount
                match = re.search(r"£\s*([0-9,]+\.?\d*)", line)
                if match:
                    amount_str = match.group(1).replace(",", "")
                    try:
                        amounts.append(float(amount_str))
                    except ValueError:
                        pass
            else:
                description_parts.append(line)

        if not description_parts or len(amounts) == 0:
            return None

        description = " ".join(description_parts)

        # Kroo format has: Out, In, Balance columns (any can be empty in PDF)
        # After text extraction, we get amounts in sequence
        # Need to determine which amounts are Out, In, Balance

        # Balance is always the last amount
        balance = amounts[-1]

        # If only 1 amount, it's opening/closing balance (skip these)
        if len(amounts) == 1:
            return None

        # If 2 amounts: one is transaction (Out or In), one is Balance
        if len(amounts) == 2:
            # Determine if it's Out or In by looking at description
            txn_amount = amounts[0]
            desc_lower = description.lower()

            # Check for explicit Out/In indicators first
            has_out_indicator = any(
                re.search(rf"\b{word}\b", desc_lower)
                for word in [
                    "payment out",
                    "faster payment out",
                    "sent to",
                    "sent",
                    " out",
                ]
            )
            has_in_indicator = any(
                re.search(rf"\b{word}\b", desc_lower)
                for word in [
                    "from",
                    "deposit",
                    "interest",
                    "salary",
                    "paid in",
                    "credit",
                    "payment in",
                ]
            )

            # If both present or only "From" at start, it's incoming
            # "Sent from Kroo" is ambiguous so check full pattern
            if has_out_indicator or ("to " in desc_lower and not has_in_indicator):
                # It's outgoing
                amount = -txn_amount
                out_amount = txn_amount
                in_amount = 0
            elif has_in_indicator:
                # It's incoming
                amount = txn_amount
                out_amount = 0
                in_amount = txn_amount
            else:
                # Default: assume outgoing for "To" and incoming for "From"
                if desc_lower.startswith("from"):
                    amount = txn_amount
                    out_amount = 0
                    in_amount = txn_amount
                else:
                    amount = -txn_amount
                    out_amount = txn_amount
                    in_amount = 0
        else:
            # 3+ amounts: Out, In, Balance...
            out_amount = amounts[0]
            in_amount = amounts[1]

            # Determine net transaction
            if out_amount > 0 and in_amount == 0:
                amount = -out_amount
            elif in_amount > 0 and out_amount == 0:
                amount = in_amount
            else:
                # Both present (shouldn't normally happen for a single transaction)
                amount = in_amount - out_amount

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
        amount = str(txn.get("amount", 0)).replace(".", "_")
        account_part = f"{account_identifier}_" if account_identifier else ""

        return f"kroo_txn_{account_part}{date_str}_{description}_{amount}"

    def detect_source_type(self) -> str:
        """Return source type."""
        return "kroo"
