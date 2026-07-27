"""First Direct credit card PDF statement adapter."""

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from dateutil.relativedelta import relativedelta

from models.money import parse_money_minor, MoneyParseError

from adapters.base import StatementPeriod
from adapters.pdf_adapter import PdfAdapter
from adapters.reconciliation import build_reconciliation_result

logger = logging.getLogger(__name__)

_PREVIOUS_BALANCE_RE = re.compile(r"Previous Balance\s*\n\s*([\d,]+\.\d{2})")
_NEW_BALANCE_RE = re.compile(r"New Balance\s*\n\s*([\d,]+\.\d{2})")
# e.g. "Statement Date 05 May 2026" - printed inline on one line (unlike
# Previous/New Balance above, which are label-then-newline-then-value, this
# is label-space-value on a real statement), and repeats once per sheet.
# Unlike every other adapter's period extraction, this is a single date, not
# a printed "from...to" range. First Direct's statements are always
# generated on a fixed monthly cycle (the 5th), so `from_date` isn't printed
# anywhere at all - it's derived as exactly one calendar month before the
# printed Statement Date, a hardcoded assumption specific to this adapter's
# known billing cycle. If that cycle ever changes (a different day-of-month,
# or a non-monthly cadence), this will silently produce a wrong `from_date` -
# unlike the other B4 adapters, which only ever echo a date range the
# statement prints itself.
_STATEMENT_DATE_RE = re.compile(r"Statement Date\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})")


class FirstDirectPdfAdapter(PdfAdapter):
    """Parse First Direct PDF statements."""

    PARSER_VERSION = "2"

    def validate_text(self, text: str) -> bool:
        """Check if text is from First Direct statement."""
        return "first direct" in text.lower() and (
            "Your Transaction Details" in text or "Credit Card" in text
        )

    def _extract_account_identifier(self, text: str) -> Optional[str]:
        """Extract the card number, e.g. '4543 1222 4201 2670'.

        Note: real First Direct statements show the full, unmasked card
        number in extracted text (unlike Amex/Natwest's masked forms) -
        hashed immediately by PdfAdapter.parse(), never stored raw.
        """
        match = re.search(r"\b(\d{4}\s\d{4}\s\d{4}\s\d{4})\b", text)
        return match.group(1) if match else None

    def _extract_statement_period(
        self, text: str
    ) -> Optional[Tuple[datetime, datetime]]:
        """Extract the statement period from the single printed "Statement
        Date", e.g. "05 May 2026".

        `from_date` is derived, not printed - see _STATEMENT_DATE_RE.
        """
        match = _STATEMENT_DATE_RE.search(text)
        if not match:
            return None
        try:
            to_date = datetime.strptime(match.group(1), "%d %B %Y")
        except ValueError:
            return None
        from_date = to_date - relativedelta(months=1)
        return from_date, to_date

    def parse_transactions(self, text: str) -> List[Dict[str, Any]]:
        """
        Extract transactions from First Direct statement.

        First Direct format (multi-line per transaction after PDF extraction):
        Date
        Details (can be multiple lines)
        Amount (e.g., "47.22CR" or "47.22")
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

        for line in lines:
            line = line.strip()

            # Start of transactions section
            if "Your Transaction Details" in line or "Transaction Date" in line:
                in_transactions = True
                continue

            if not in_transactions:
                continue

            # Stop at footer
            if "Outstanding Balance" in line or "Summary Of Interest" in line:
                break

            # Skip empty lines and headers
            if not line or line in [
                "Received By Us",
                "Transaction Date",
                "Details",
                "Amount",
            ]:
                continue

            # Check if this line starts a new transaction (matches date pattern DD Mmm YY)
            is_new_txn = re.match(r"^\d{1,2}\s+\w{3}\s+\d{2}", line)

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

        self._attach_derived_balances(text, transactions)

        return transactions

    def _attach_derived_balances(
        self, text: str, transactions: List[Dict[str, Any]]
    ) -> None:
        """Roll a "Previous Balance" anchor forward through transactions.

        First Direct statements don't print a per-transaction running
        balance - only a single "Previous Balance" / "New Balance" pair in
        the Account Summary block. Unlike Amex, that block isn't
        column-flattened, so it can be read straight from the same text
        used for transaction parsing.
        """
        if not transactions:
            return

        previous_match = _PREVIOUS_BALANCE_RE.search(text)
        if not previous_match:
            return

        try:
            opening = parse_money_minor(previous_match.group(1))
        except MoneyParseError:
            return

        running = opening
        for txn in transactions:
            # `amount` follows a cash-received convention (spend negative,
            # payments/credits positive), but the statement balance is a
            # liability that moves the opposite way - subtract, don't add.
            running -= txn["amount_minor"]
            txn["balance_minor"] = running

        new_balance_match = _NEW_BALANCE_RE.search(text)
        if not new_balance_match:
            return
        try:
            expected = parse_money_minor(new_balance_match.group(1))
        except MoneyParseError:
            return

        derived_closing = running
        self.last_reconciliation = build_reconciliation_result(
            check_name="first_direct_new_balance",
            expected_closing_minor=expected,
            derived_closing_minor=derived_closing,
            expected_opening_minor=opening,
        )
        if not self.last_reconciliation.matches:
            logger.warning(
                "First Direct statement: derived closing balance %d "
                "minor units does not match statement's printed New "
                "Balance %d minor units - transaction amounts don't fully "
                "reconcile with the Account Summary block. Balance fields "
                "may be inaccurate.",
                running,
                expected,
            )

    def _parse_transaction_lines(self, lines: List[str]) -> Optional[Dict[str, Any]]:
        """
        Parse a transaction that spans multiple lines.

        Format:
        Date (DD Mmm YY)
        Description (can be multiple lines)
        Amount (with optional CR suffix for credit)
        """
        if not lines or not lines[0]:
            return None

        # First line is the date
        date_str = lines[0].strip()
        date_match = re.match(r"^(\d{1,2}\s+\w{3}\s+\d{2})", date_str)
        if not date_match:
            return None

        date_str = date_match.group(1)

        # Find the amount (last line with £ or format like "47.22CR")
        amount_str = None
        is_credit = False
        description_parts = []

        for line in lines[1:]:
            line = line.strip()

            # Check for amount pattern
            if re.match(r"^[0-9,]+\.?\d*(CR)?$", line):
                # This is the amount line
                is_credit = line.endswith("CR")
                amount_str = line.replace("CR", "").strip()
                break
            elif "£" in line:
                # Amount with £ prefix
                match = re.search(r"£\s*([0-9,]+\.?\d*)\s*(CR)?", line)
                if match:
                    amount_str = match.group(1)
                    is_credit = match.group(2) == "CR"
                    break
            else:
                description_parts.append(line)

        if not amount_str or not description_parts:
            return None

        try:
            parsed = parse_money_minor(amount_str)
            amount_minor = parsed if is_credit else -parsed
        except MoneyParseError:
            return None

        description = " ".join(description_parts)

        return {
            "date": date_str,
            "description": description,
            "amount_minor": amount_minor,
            "amount_text": amount_str,
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

        return f"firstdirect_txn_{account_part}{date_str}_{description}_{amount}"

    def detect_source_type(self) -> str:
        """Return source type."""
        return "firstdirect"
