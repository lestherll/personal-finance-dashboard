"""Monzo Personal Account PDF statement adapter."""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from models.money import parse_money_minor, MoneyParseError

from adapters.base import StatementPeriod, make_transaction_source_key
from adapters.pdf_adapter import PdfAdapter
from adapters.reconciliation import build_reconciliation_result

# e.g. "01/04/2026 - 30/06/2026" - printed once right under the "Personal
# Account statement" header, and again under "Pot statement" (same range in
# every real statement seen so far) - the first match is used. Anchored to a
# whole line (not searched across the full text with re.DOTALL-style \s+)
# so it can't accidentally span across newlines onto unrelated text.
_PERIOD_RE = re.compile(r"^(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})$")

# "£2,255.37\nPersonal Account balance" - amount precedes its own label
# (same order as Monzo Flex's "Balance at end", opposite Kroo's
# "Closing balance\n£..."). Requires the literal "Personal Account
# balance" text, not just "balance" - the summary block also prints a
# "Total balance" a few lines above (includes Pots/Cashback, which this
# adapter's transaction table excludes) that must not match instead.
_PERSONAL_ACCOUNT_BALANCE_RE = re.compile(
    r"£\s*([\d,]+\.\d{2})\s*\n\s*Personal Account balance\b"
)


class MonzoPdfAdapter(PdfAdapter):
    """Parse Monzo "Personal Account statement" PDFs.

    Only covers the Personal Account transaction table - a statement can
    also include a separate "Pot statement" section per Pot, which this
    adapter does not parse (stops at the first "Pot statement" marker).
    """

    PARSER_VERSION = "2"

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
        self.last_reconciliation = None
        self.last_statement_period = None
        period = self._extract_statement_period(text)
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
        """Mirror of KrooPdfAdapter._check_reconciliation / MonzoFlexPdfAdapter.
        _check_reconciliation - this table is newest-first (like Flex, unlike
        Kroo), so the FIRST parsed transaction's own printed balance is the
        one that should equal the summary block's "Personal Account balance"
        (not "Total balance", which also includes Pots/Cashback and isn't
        covered by this adapter's transaction table). Like Kroo/Flex, this
        only confirms the table was read through to its most recent row, not
        that the arithmetic itself reconciles (direct-read balance, nothing
        rolled forward) - see silver_transformer Gotcha #6."""
        if not transactions:
            return
        match = _PERSONAL_ACCOUNT_BALANCE_RE.search(text)
        if not match:
            return
        try:
            expected = parse_money_minor(match.group(1))
            derived = transactions[0]["balance_minor"]
        except (MoneyParseError, KeyError):
            return

        self.last_reconciliation = build_reconciliation_result(
            check_name="monzo_pdf_personal_account_balance",
            expected_closing_minor=expected,
            derived_closing_minor=derived,
        )

    def _parse_transaction_lines(self, lines: List[str]) -> Optional[Dict[str, Any]]:
        """Parse a transaction that spans multiple lines.

        Exactly two numeric lines are expected: signed amount, then signed
        running balance - a direct read, like Kroo/Vanguard PDF (see
        silver_transformer Gotcha #6), not derived from an anchor.
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
                try:
                    amounts.append(parse_money_minor(line))
                except MoneyParseError:
                    pass
            else:
                description_parts.append(line)

        if not description_parts or len(amounts) != 2:
            return None

        amount_minor, balance_minor = amounts
        description = " ".join(description_parts)

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
        return make_transaction_source_key(
            "monzo_pdf_txn",
            txn.get("date", ""),
            txn.get("description", ""),
            int(txn.get("amount_minor", 0)),
            account_identifier,
        )

    def detect_source_type(self) -> str:
        """Return source type."""
        return "monzo-pdf"
