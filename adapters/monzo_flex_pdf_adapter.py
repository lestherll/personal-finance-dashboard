"""Monzo Flex (BNPL/credit) PDF statement adapter."""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from models.money import parse_money_minor, MoneyParseError

from adapters.base import StatementPeriod, make_transaction_source_key
from adapters.pdf_adapter import PdfAdapter
from adapters.reconciliation import build_reconciliation_result

# e.g. "01/04/2026 - 30/06/2026" - printed once directly under "The amounts
# you'll see below are for these dates", itself under the "Flex statement"
# header. Same shape as MonzoPdfAdapter._PERIOD_RE.
_PERIOD_RE = re.compile(r"^(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})$")

_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
# Exactly 2 decimal digits, no trailing text - deliberately excludes
# conversion-rate lines like "22318.84058." or "1.146245." (variable,
# usually more than 2, decimal digits, often a trailing sentence-ending
# period) even though those are pure numerics too.
_AMOUNT_RE = re.compile(r"^-?[\d,]+\.\d{2}$")

# "£1101.37\nBalance at end" - amount precedes its own label here (opposite
# order from Kroo's "Closing balance\n£..."). "Balance at start" appears
# earlier in the same summary block - requiring the literal "end" is what
# keeps this from matching that anchor instead.
_BALANCE_AT_END_RE = re.compile(r"£\s*([\d,]+\.\d{2})\s*\n\s*Balance at end\b")
# "£1217.07\nBalance at start" - same amount-precedes-label order as
# _BALANCE_AT_END_RE, captured purely for cross-file continuity checking
# (this adapter's own `matches` check only ever compares against the "end"
# anchor, since the table is a direct read, nothing rolled forward).
_BALANCE_AT_START_RE = re.compile(r"£\s*([\d,]+\.\d{2})\s*\n\s*Balance at start\b")


class MonzoFlexPdfAdapter(PdfAdapter):
    """Parse Monzo Flex (BNPL/credit) statement PDFs.

    Unlike the Monzo Personal Account statement, this document contains no
    account identifier anywhere (no sort code, account number, IBAN, BIC,
    masked digits - not even the literal word "Monzo") - `parse_transactions`
    never sets `_account_identifier_raw`, so every transaction's
    `account_identifier` resolves to None. Account linking relies entirely
    on `cli.py accounts register-fallback monzo-flex <id> <name> credit`
    (the same single-account-per-source_type pattern already used for Monzo
    CSV) - this assumes exactly one Flex account in practice.
    """

    PARSER_VERSION = "2"

    def validate_text(self, text: str) -> bool:
        """ "Flex statement" is the page-1 header; "Balance at end" confirms
        the BNPL summary block rather than a stray mention of the word
        "Flex" elsewhere (e.g. inside a Monzo Personal Account statement's
        own transaction descriptions)."""
        return "Flex statement" in text and "Balance at end" in text

    def _extract_statement_period(
        self, text: str
    ) -> Optional[Tuple[datetime, datetime]]:
        """Extract the statement period, e.g. '01/04/2026 - 30/06/2026'."""
        for line in text.split("\n"):
            match = _PERIOD_RE.match(line.strip())
            if not match:
                continue
            from_str, to_str = match.groups()
            try:
                return (
                    datetime.strptime(from_str, "%d/%m/%Y"),
                    datetime.strptime(to_str, "%d/%m/%Y"),
                )
            except ValueError:
                return None
        return None

    def parse_transactions(self, text: str) -> List[Dict[str, Any]]:
        """
        Extract transactions from a Monzo Flex statement.

        Table format (multi-line per row, newest-first):
        Date (DD/MM/YYYY)
        Description (1 or 3 lines - foreign-currency purchases add an
        "Amount: <CCY> <amt>. Conversion rate:" line + a "<rate>." line,
        occasionally wrapping into a 3rd fragment)
        Debit (always printed, 0.00 if not a purchase)
        Credit (always printed, 0.00 if not a payment/refund)
        Balance (always positive - direct read, like Monzo PDF/Kroo, not
        rolled forward from an opening anchor)

        The 5 bare column-header lines ("Date"/"Description"/"Debit"/
        "Credit"/"Balance") appear exactly once, only on page 1 - the bare
        "Balance" line is used as the unique "table starts here" marker
        (safe: it never recurs, unlike "Balance at start"/"Balance at end"
        which are two-word lines and don't collide with this equality
        check). The table ends where the legal-footer paragraph starts.
        """
        self.last_reconciliation = None
        self.last_statement_period = None
        period = self._extract_statement_period(text)

        transactions: List[Dict[str, Any]] = []
        in_transactions = False
        current_txn_lines: List[str] = []

        for line in text.split("\n"):
            line = line.strip()

            if line == "Balance":
                in_transactions = True
                continue

            if not in_transactions:
                continue

            if "legally prescribed" in line:
                break

            if not line:
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

        self._check_reconciliation(text, transactions)

        return transactions

    def _parse_transaction_lines(self, lines: List[str]) -> Optional[Dict[str, Any]]:
        """Parse a transaction that spans multiple lines.

        Exactly 3 numeric lines are expected, always in this order: Debit,
        Credit, Balance - a direct read of Balance (see silver_transformer
        Gotcha #6), not derived from an anchor. Signed
        `amount = credit - debit` gives the same convention already used by
        adapters/amex_pdf_adapter.py (spend negative, payment/refund
        positive); the printed Balance is already a positive "amount owed"
        figure, so no sign flip is needed for account_ledger either.

        Known cosmetic limitation (accepted, not special-cased): the last
        transaction's description can be split across a page boundary in a
        way that reorders extracted lines - the merchant name lands on the
        *previous* row's buffer instead of its own. Date/debit/credit/
        balance stay numerically correct for both affected rows; only the
        `description` text for those two rows is scrambled. See CLAUDE.md.
        """
        if not lines:
            return None

        date_str = lines[0]
        if not _DATE_RE.match(date_str):
            return None

        description_parts = []
        numbers = []
        for line in lines[1:]:
            if _AMOUNT_RE.match(line):
                numbers.append(line)
            else:
                description_parts.append(line)

        if not description_parts or len(numbers) != 3:
            return None

        try:
            debit, credit, balance = (parse_money_minor(n) for n in numbers)
        except MoneyParseError:
            return None

        return {
            "date": date_str,
            "description": " ".join(description_parts),
            "amount_minor": credit - debit,
            "balance_minor": balance,
        }

    def _check_reconciliation(
        self, text: str, transactions: List[Dict[str, Any]]
    ) -> None:
        """Mirror of KrooPdfAdapter._check_reconciliation, but checking the
        FIRST parsed transaction, not the last - Flex's table is newest-
        first (Kroo's is oldest-first), so the most-recently-posted row is
        the one whose own printed balance should equal "Balance at end".
        Like Kroo, this only confirms the table was read through to its
        start, not that the arithmetic itself reconciles (direct-read
        balance, nothing rolled forward)."""
        if not transactions:
            return
        match = _BALANCE_AT_END_RE.search(text)
        if not match:
            return
        try:
            expected = parse_money_minor(match.group(1))
            derived = transactions[0]["balance_minor"]
        except (MoneyParseError, KeyError):
            return

        start_match = _BALANCE_AT_START_RE.search(text)
        opening = None
        if start_match:
            try:
                opening = parse_money_minor(start_match.group(1))
            except MoneyParseError:
                opening = None

        self.last_reconciliation = build_reconciliation_result(
            check_name="monzo_flex_balance_at_end",
            expected_closing_minor=expected,
            derived_closing_minor=derived,
            expected_opening_minor=opening,
        )

    def generate_source_key(
        self,
        txn: Dict[str, Any],
        line_num: int,
        account_identifier: Optional[str] = None,
    ) -> str:
        """Generate deterministic key from date + description + amount.

        account_identifier will always be None in practice (see class
        docstring) but is still folded in for interface conformance /
        future-proofing, same as every other adapter's generate_source_key.
        """
        return make_transaction_source_key(
            "monzo_flex_txn",
            txn.get("date", ""),
            txn.get("description", ""),
            int(txn.get("amount_minor", 0)),
            account_identifier,
        )

    def detect_source_type(self) -> str:
        """Return source type."""
        return "monzo-flex"
