"""Kroo bank PDF statement adapter."""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from models.money import parse_money_minor, MoneyParseError

from adapters.base import ReconciliationResult, StatementPeriod
from adapters.pdf_adapter import PdfAdapter

_CLOSING_BALANCE_RE = re.compile(r"Closing balance\s*\n\s*£\s*([\d,]+\.\d{2})")
# e.g. "1 June 2026 to 30 June 2026" - printed once right under the "Account
# transactions" header, unpadded day (no leading zero, unlike the per-
# transaction "01 June 2026"-style dates below). Anchored to a whole line so
# it can't span across newlines or get mistaken for a transaction row.
_PERIOD_RE = re.compile(
    r"^(\d{1,2}\s+[A-Za-z]+\s+\d{4})\s+to\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})$"
)


class KrooPdfAdapter(PdfAdapter):
    """Parse Kroo PDF statements."""

    PARSER_VERSION = "2"

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

    def _extract_statement_period(
        self, text: str
    ) -> Optional[Tuple[datetime, datetime]]:
        """Extract the statement period, e.g. '1 June 2026 to 30 June 2026'."""
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
        Extract transactions from Kroo statement.

        Kroo format (multi-line per transaction):
        Date | Description | Out | In | Balance
        """
        self.last_reconciliation = None
        self.last_statement_period = None
        period = self._extract_statement_period(text)
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

        if period:
            self.last_statement_period = StatementPeriod(period[0], period[1])

        if account_identifier:
            for txn in transactions:
                txn["_account_identifier_raw"] = account_identifier

        self._check_reconciliation(text, transactions)

        return transactions

    def _check_reconciliation(
        self, text: str, transactions: List[Dict[str, Any]]
    ) -> None:
        """Compare the last parsed transaction's printed balance against the
        statement's own "Closing balance" anchor (previously skipped over,
        never captured).

        Unlike Amex/First Direct/Natwest Statement, Kroo's per-transaction
        `balance` is a *direct read* of a printed column, not rolled forward
        from an opening anchor - so this doesn't confirm the arithmetic
        reconciles, only that the transaction table was read through to its
        end (a truncated/incompletely-parsed table would show a mismatch
        here just the same as a genuine parsing bug would).
        """
        if not transactions:
            return
        match = _CLOSING_BALANCE_RE.search(text)
        if not match:
            return
        try:
            expected = parse_money_minor(match.group(1))
            derived = transactions[-1]["balance_minor"]
        except (MoneyParseError, KeyError):
            return

        self.last_reconciliation = ReconciliationResult(
            check_name="kroo_closing_balance",
            expected_closing_minor=expected,
            derived_closing_minor=derived,
            matches=derived == expected,
        )

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
        amounts: List[int] = []
        amount_texts: List[str] = []

        for line in lines[1:]:
            line = line.strip()
            if "£" in line:
                # Extract amount
                match = re.search(r"£\s*([0-9,]+\.?\d*)", line)
                if match:
                    amount_str = match.group(1)
                    try:
                        amounts.append(parse_money_minor("£" + amount_str))
                        amount_texts.append(amount_str)
                    except MoneyParseError:
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
        balance_minor = amounts[-1]

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
                amount_minor = -txn_amount
            elif has_in_indicator:
                # It's incoming
                amount_minor = txn_amount
            else:
                # Default: assume outgoing for "To" and incoming for "From"
                if desc_lower.startswith("from"):
                    amount_minor = txn_amount
                else:
                    amount_minor = -txn_amount
        else:
            # 3+ amounts: Out, In, Balance...
            out_amount = amounts[0]
            in_amount = amounts[1]

            # Determine net transaction
            if out_amount > 0 and in_amount == 0:
                amount_minor = -out_amount
            elif in_amount > 0 and out_amount == 0:
                amount_minor = in_amount
            else:
                # Both present (shouldn't normally happen for a single transaction)
                amount_minor = in_amount - out_amount

        return {
            "date": date_str,
            "description": description,
            "amount_minor": amount_minor,
            "balance_minor": balance_minor,
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
        amount = str(abs(txn.get("amount_minor", 0)))
        account_part = f"{account_identifier}_" if account_identifier else ""

        return f"kroo_txn_{account_part}{date_str}_{description}_{amount}"

    def detect_source_type(self) -> str:
        """Return source type."""
        return "kroo"
