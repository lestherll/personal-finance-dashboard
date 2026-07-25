"""Chase (J.P. Morgan) PDF statement adapter.

One adapter covers both a "Lesther Jr's Account statement" (current account)
and a "Chase Saver statement" (savings pot) - identical layout, distinguished
only by account_identifier (sort code + account number), same pattern as
two Amex cards under one source_type (see CLAUDE.md Gotcha #5).
"""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from adapters.base import StatementPeriod
from adapters.pdf_adapter import PdfAdapter

_AMOUNT_RE = re.compile(r"^[+-]?£[\d,]+\.\d{2}$")
_DATE_RE = re.compile(r"^(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})$")
# e.g. "02 June 2026 - 30 June 2026" - full month name, unlike the per-
# transaction "02 Jun 2026" dates above; repeats once per page, first match
# used. Anchored to a whole line so it can't span across newlines onto
# unrelated text, and doesn't collide with _DATE_RE (3-letter month, single
# date, no hyphen).
_PERIOD_RE = re.compile(
    r"^(\d{1,2}\s+[A-Za-z]+\s+\d{4})\s*-\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})$"
)


class ChasePdfAdapter(PdfAdapter):
    """Parse Chase PDF statements (current account or savings pot)."""

    def validate_text(self, text: str) -> bool:
        """Check if text is from a Chase statement."""
        return "J.P. Morgan Europe Limited" in text and (
            "Account statement" in text or "Chase Saver statement" in text
        )

    def _extract_account_identifier(self, text: str) -> Optional[str]:
        """Extract 'Account number: XXXXXXXX' + 'Sort code: XX-XX-XX'."""
        account_match = re.search(r"Account number:\s*(\d+)", text)
        sort_code_match = re.search(r"Sort code:\s*([\d-]+)", text)
        if not account_match or not sort_code_match:
            return None
        return f"{account_match.group(1)}_{sort_code_match.group(1)}"

    def _extract_statement_period(
        self, text: str
    ) -> Optional[Tuple[datetime, datetime]]:
        """Extract the statement period, e.g. '02 June 2026 - 30 June 2026'."""
        for line in text.split("\n"):
            match = _PERIOD_RE.match(line.strip())
            if not match:
                continue
            from_str, to_str = match.groups()
            try:
                return (
                    datetime.strptime(from_str, "%d %B %Y"),
                    datetime.strptime(to_str, "%d %B %Y"),
                )
            except ValueError:
                return None
        return None

    def parse_transactions(self, text: str) -> List[Dict[str, Any]]:
        """
        Extract transactions from a Chase statement.

        Chase format (multi-line per transaction after PDF extraction):
        Date (DD Mon YYYY - year always present, unlike Amex/Natwest PDF)
        Description (can be multiple lines, e.g. a "Payment"/"Transfer" type
        continuation line, or a time-of-day line for the account-opening
        entry)
        Amount (signed, "+£X.XX" or "-£X.XX" - absent for "You opened your
        account", "Opening balance", "Closing balance" rows)
        Balance (unsigned "£X.XX")

        Only rows with exactly two £-amount lines (amount + balance) are
        real transactions - this naturally filters out the account-opening,
        opening-balance and closing-balance rows without special-casing
        each one.
        """
        self.last_statement_period = None
        period = self._extract_statement_period(text)
        account_identifier = self._extract_account_identifier(text)
        transactions = []
        lines = text.split("\n")

        in_transactions = False
        current_txn_lines: List[str] = []
        skip_lines = {"Date", "Transaction details", "Amount", "Balance"}
        skip_prefixes = ("Account number:", "Sort code:", "Page ")

        for line in lines:
            line = line.strip()

            if line == "Balance" and not in_transactions:
                in_transactions = True
                continue

            if not in_transactions:
                continue

            if line.startswith("Some useful information"):
                break

            if not line or line in skip_lines or line.startswith(skip_prefixes):
                continue

            is_new_txn = bool(_DATE_RE.match(line))

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

        if period:
            self.last_statement_period = StatementPeriod(period[0], period[1])

        if account_identifier:
            for txn in transactions:
                txn["_account_identifier_raw"] = account_identifier

        return transactions

    def _parse_transaction_lines(self, lines: List[str]) -> Optional[Dict[str, Any]]:
        """Parse a transaction that spans multiple lines.

        Balance is dropped (see silver_transformer Gotcha #6 - PDF adapters
        don't carry balance history into account_ledger).
        """
        if not lines:
            return None

        date_str = lines[0].strip()
        if not _DATE_RE.match(date_str):
            return None

        description_parts = []
        amounts = []

        for line in lines[1:]:
            line = line.strip()
            if _AMOUNT_RE.match(line):
                amounts.append(float(line.replace("£", "").replace(",", "")))
            else:
                description_parts.append(line)

        if not description_parts or len(amounts) != 2:
            return None

        return {
            "date": date_str,
            "description": " ".join(description_parts),
            "amount": amounts[0],
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

        return f"chase_txn_{account_part}{date_str}_{description}_{amount}"

    def detect_source_type(self) -> str:
        """Return source type."""
        return "chase"
