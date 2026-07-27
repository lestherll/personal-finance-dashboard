"""Chase (J.P. Morgan) PDF statement adapter.

One adapter covers both a "Lesther Jr's Account statement" (current account)
and a "Chase Saver statement" (savings pot) - identical layout, distinguished
only by account_identifier (sort code + account number), same pattern as
two Amex cards under one source_type (see CLAUDE.md Gotcha #5).
"""

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from models.money import parse_money_minor, MoneyParseError

from adapters.base import StatementPeriod
from adapters.pdf_adapter import PdfAdapter
from adapters.reconciliation import build_reconciliation_result, roll_forward_balance

logger = logging.getLogger(__name__)

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
# Both labels also appear a second time, later, as a filtered-out single-
# amount summary row in the transaction table - but the anchor block always
# comes first in reading order, so the leftmost .search() match is always
# the real anchor.
_OPENING_BALANCE_RE = re.compile(r"Opening balance\s*\n\s*£\s*([\d,]+\.\d{2})")
_CLOSING_BALANCE_RE = re.compile(r"Closing balance\s*\n\s*£\s*([\d,]+\.\d{2})")


class ChasePdfAdapter(PdfAdapter):
    """Parse Chase PDF statements (current account or savings pot)."""

    PARSER_VERSION = "2"

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
        self.last_reconciliation = None
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

        self._check_reconciliation(text, transactions)

        return transactions

    def _check_reconciliation(
        self, text: str, transactions: List[Dict[str, Any]]
    ) -> None:
        """Roll the statement's own "Opening balance" anchor forward through
        parsed transactions and compare against the printed "Closing balance".

        Chase prints a genuine Opening/Money in/Money out/Closing balance
        block once per statement - same B1 anchor class as Amex/First
        Direct/Natwest Statement/Kroo (CLAUDE.md). Unlike those, Chase is a
        cash/asset account, not a credit card liability, so its balance
        moves the SAME direction as the signed, cash-received `amount`
        convention used elsewhere (spend/transfer-out negative, in
        positive): `running += amount_minor`, not `-=`. Verified against
        both real statements (Current: 0.00 + 200 - 200 = 0.00; Saver:
        0.00 + 2,550 + 200 = 2,750.00).
        """
        if not transactions:
            return
        opening_match = _OPENING_BALANCE_RE.search(text)
        closing_match = _CLOSING_BALANCE_RE.search(text)
        if not opening_match or not closing_match:
            return
        try:
            opening = parse_money_minor(opening_match.group(1))
            expected_closing = parse_money_minor(closing_match.group(1))
        except MoneyParseError:
            return

        derived_closing = roll_forward_balance(
            opening, (txn["amount_minor"] for txn in transactions), sign=1
        )
        self.last_reconciliation = build_reconciliation_result(
            check_name="chase_closing_balance",
            expected_closing_minor=expected_closing,
            derived_closing_minor=derived_closing,
            expected_opening_minor=opening,
        )
        if not self.last_reconciliation.matches:
            logger.warning(
                "Chase statement: derived closing balance %d minor units "
                "does not match statement's printed Closing balance %d "
                "minor units - transaction amounts don't fully reconcile "
                "with the Opening/Closing balance block. Balance fields "
                "may be inaccurate.",
                derived_closing,
                expected_closing,
            )

    def _parse_transaction_lines(self, lines: List[str]) -> Optional[Dict[str, Any]]:
        """Parse a transaction that spans multiple lines.

        Captures both `amount` (signed) and `balance` (unsigned, a direct
        read of the statement's own Balance column, not derived). `balance`
        feeds transformers/silver_transformer.py::_ledger_from_chase - Chase
        was the one PDF source of 8 that had a printed balance column with
        no account_ledger wiring at all. Not used by this adapter's own
        `_check_reconciliation` above, which independently verifies against
        the statement's separate Opening/Closing balance anchor by summing
        `amount` - two complementary checks, not a redundant one.
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
                try:
                    amounts.append(parse_money_minor(line))
                except MoneyParseError:
                    pass
            else:
                description_parts.append(line)

        if not description_parts or len(amounts) != 2:
            return None

        return {
            "date": date_str,
            "description": " ".join(description_parts),
            "amount_minor": amounts[0],
            "balance_minor": amounts[1],
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

        return f"chase_txn_{account_part}{date_str}_{description}_{amount}"

    def detect_source_type(self) -> str:
        """Return source type."""
        return "chase"
