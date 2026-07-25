"""Monzo Personal Account PDF statement adapter."""

import re
from typing import Any, Dict, List, Optional

from adapters.pdf_adapter import PdfAdapter


class MonzoPdfAdapter(PdfAdapter):
    """Parse Monzo "Personal Account statement" PDFs.

    Only covers the Personal Account transaction table - a statement can
    also include a separate "Pot statement" section per Pot, which this
    adapter does not parse (stops at the first "Pot statement" marker).
    """

    def validate_text(self, text: str) -> bool:
        """Check if text is from a Monzo Personal Account statement."""
        return "Personal Account statement" in text and "Monzo" in text

    def _extract_account_identifier(self, text: str) -> Optional[str]:
        """Extract 'Sort code: XX-XX-XX' + 'Account number: XXXXXXXX' from the footer."""
        sort_code_match = re.search(r"Sort code:\s*([\d-]+)", text)
        account_match = re.search(r"Account number:\s*(\d+)", text)
        if not sort_code_match or not account_match:
            return None
        return f"{sort_code_match.group(1)}_{account_match.group(1)}"

    def parse_transactions(self, text: str) -> List[Dict[str, Any]]:
        """
        Extract transactions from a Monzo Personal Account statement.

        Monzo format (multi-line per transaction after PDF extraction):
        Date (DD/MM/YYYY)
        Description (can wrap across multiple lines, e.g. a "Reference: ..."
        continuation, or the description itself wrapping mid-phrase)
        (GBP) Amount (signed, e.g. "-44.43" or "91.00")
        (GBP) Balance (signed running balance)

        The "Sort code:" / "Account number:" / "BIC:" / "IBAN:" footer block
        appears once, interleaved mid-table between the first and second
        page - skipped explicitly rather than left to pollute the
        transaction either side of it.
        """
        account_identifier = self._extract_account_identifier(text)
        transactions = []
        lines = text.split("\n")

        in_transactions = False
        current_txn_lines: List[str] = []
        skip_prefixes = ("Sort code:", "Account number:", "BIC:", "IBAN:")

        for line in lines:
            line = line.strip()

            # Column headers appear once; last one marks the table start.
            if line == "(GBP) Balance":
                in_transactions = True
                continue

            if not in_transactions:
                continue

            # Stop at the Pot statement section or the legal footer.
            if line.startswith("Pot statement") or line.startswith(
                "Monzo Bank Limited"
            ):
                break

            if not line or line.startswith(skip_prefixes):
                continue

            is_new_txn = re.match(r"^\d{2}/\d{2}/\d{4}$", line)

            if is_new_txn and current_txn_lines:
                txn = self._parse_transaction_lines(current_txn_lines)
                if txn:
                    transactions.append(txn)
                current_txn_lines = [line]
            elif is_new_txn or current_txn_lines:
                current_txn_lines.append(line)

        if current_txn_lines:
            txn = self._parse_transaction_lines(current_txn_lines)
            if txn:
                transactions.append(txn)

        if account_identifier:
            for txn in transactions:
                txn["_account_identifier_raw"] = account_identifier

        return transactions

    def _parse_transaction_lines(self, lines: List[str]) -> Optional[Dict[str, Any]]:
        """Parse a transaction that spans multiple lines.

        Exactly two numeric lines are expected: signed amount, then signed
        running balance (dropped - see silver_transformer Gotcha #6, PDF
        adapters don't carry balance history into account_ledger).
        """
        if not lines:
            return None

        date_str = lines[0].strip()
        if not re.match(r"^\d{2}/\d{2}/\d{4}$", date_str):
            return None

        amount_re = re.compile(r"^-?[\d,]+\.\d{2}$")
        description_parts = []
        amounts = []

        for line in lines[1:]:
            line = line.strip()
            if amount_re.match(line):
                amounts.append(float(line.replace(",", "")))
            else:
                description_parts.append(line)

        if not description_parts or len(amounts) != 2:
            return None

        amount = amounts[0]
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
        date_str = txn.get("date", "").replace("/", "")
        description = txn.get("description", "")[:10].replace(" ", "_")
        amount = str(txn.get("amount", 0)).replace(".", "_")
        account_part = f"{account_identifier}_" if account_identifier else ""

        return f"monzo_pdf_txn_{account_part}{date_str}_{description}_{amount}"

    def detect_source_type(self) -> str:
        """Return source type."""
        return "monzo-pdf"
