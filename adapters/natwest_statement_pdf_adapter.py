"""Natwest quarterly Statement PDF adapter.

Distinct from `natwest_transactions_pdf_adapter.py` (`natwest-transactions`),
which targets the "Transactions" export downloadable from online banking on
demand. This adapter (`natwest-statement`) handles the real Statement:
generated automatically by Natwest every ~3 months, covering a fixed
historical period once. The two exist because the Statement is the
authoritative, complete-looking document, but it lags - the transactions
export is what covers whatever's happened *since* the last Statement was
generated, so both are needed to have continuous coverage up to "now". See
the fuller rationale in `natwest_transactions_pdf_adapter.py`'s module
docstring.

The two documents also share almost nothing structurally: different section
markers, a `Date | Description | Paid In(£) | Withdrawn(£) | Balance(£)`
table instead of a single signed-amount-per-line format, and a full
(unmasked) account number instead of a masked one - hence a separate
adapter/source_type rather than a branch inside the other one. Because they
can cover overlapping dates for the same account, the same real transaction
can appear under both source_types with two different `bronze_source_key`s -
`transformers/silver_transformer.py::_dedupe_natwest_cross_format()` is what
prevents that from double-counting (see CLAUDE.md Gotcha #11).
"""

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from models.money import parse_money_minor, MoneyParseError

from adapters.base import StatementPeriod, make_transaction_source_key
from adapters.pdf_adapter import PdfAdapter, resolve_year_in_period
from adapters.reconciliation import build_reconciliation_result

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^\d{1,2}\s+[A-Z]{3}(\s+\d{4})?$")
_NUMBER_RE = re.compile(r"^[\d,]+\.\d{2}$")
_HEADER_TOKENS = {
    "Date",
    "Description",
    "Paid In(£)",
    "Withdrawn(£)",
    "Balance(£)",
}
_IDENTIFIER_RE = re.compile(
    r"Account No\s*\n\s*Sort Code\s*\n\s*Page No\s*\n\s*.+?\n\s*"
    r"(\d{6,10})\s*\n\s*(\d{2}-\d{2}-\d{2})",
    re.DOTALL,
)
_PREVIOUS_BALANCE_RE = re.compile(r"Previous Balance\s*\n\s*£([\d,]+\.\d{2})")
_NEW_BALANCE_RE = re.compile(r"New Balance\s*\n\s*£([\d,]+\.\d{2})")
_FOOTER_MARKERS = ("Interest (variable)", "RETSTMT")
# e.g. "Period Covered\n14 FEB 2026 to 13 MAY 2026" - unlike Amex/
# natwest-transactions, this document prints an explicit year on both ends
# already (this range itself has a year; it's the *per-transaction* dates
# below that don't - see resolve_year_in_period() usage in parse_transactions).
_PERIOD_COVERED_RE = re.compile(
    r"Period Covered\s*\n\s*(\d{1,2}\s+[A-Z]{3}\s+\d{4})\s+to\s+"
    r"(\d{1,2}\s+[A-Z]{3}\s+\d{4})"
)


class NatwestStatementPdfAdapter(PdfAdapter):
    """Parse Natwest quarterly Statement PDFs."""

    PARSER_VERSION = "2"

    def validate_text(self, text: str) -> bool:
        """Check if text is from a Natwest quarterly Statement.

        "BROUGHT FORWARD" and "Previous Balance" are specific to this
        document type - the online "Transactions" export
        (`natwest_transactions_pdf_adapter.py`) has neither.
        """
        return "BROUGHT FORWARD" in text and "Previous Balance" in text

    def _extract_account_identifier(self, text: str) -> Optional[str]:
        """Extract the full account number + sort code, e.g. '97386227_54-10-04'.

        Unlike the online export's masked identifier, this document prints
        the full account number - still hashed immediately by
        `PdfAdapter.parse()`, never stored raw.
        """
        match = _IDENTIFIER_RE.search(text)
        if not match:
            return None
        return f"{match.group(1)}_{match.group(2)}"

    @staticmethod
    def _extract_period_covered(text: str) -> Optional[Tuple[datetime, datetime]]:
        """Extract the statement's own "Period Covered" range.

        Unlike Amex/natwest-transactions, both dates already carry an
        explicit year here. This range is used for two purposes: B3
        coverage tracking (see `self.last_statement_period` below), and -
        despite an earlier assumption to the contrary - also for dating
        transactions: this format's per-line dates do *not* carry a year
        (e.g. "26 FEB"), the same ambiguity Amex/natwest-transactions have,
        so `parse_transactions` now resolves it via
        `resolve_year_in_period()` exactly like they do.
        """
        match = _PERIOD_COVERED_RE.search(text)
        if not match:
            return None
        from_str, to_str = match.groups()
        try:
            return (
                datetime.strptime(from_str, "%d %b %Y"),
                datetime.strptime(to_str, "%d %b %Y"),
            )
        except ValueError:
            return None

    def parse_transactions(self, text: str) -> List[Dict[str, Any]]:
        """Extract transactions from the Date/Description/Paid In/Withdrawn/
        Balance table.

        Plain-text extraction loses column position, so a lone number after
        a description is ambiguous - it could be either the Paid In or
        Withdrawn value. Rather than disambiguate that, the trailing number
        in each row (always the unambiguous Balance(£) figure) is treated as
        the primary parsed value, and the transaction's signed amount is
        derived as `balance - previous_balance` - correct by construction,
        and sidesteps the column ambiguity entirely.

        The date is only printed once per calendar day; a second same-day
        transaction omits the date line, so the last-seen date carries
        forward.
        """
        self.last_reconciliation = None
        self.last_statement_period = None
        period = self._extract_period_covered(text)
        if period:
            self.last_statement_period = StatementPeriod(period[0], period[1])

        account_identifier = self._extract_account_identifier(text)
        lines = [line.strip() for line in text.split("\n")]
        lines = [line for line in lines if line]

        transactions: List[Dict[str, Any]] = []
        last_date: Optional[str] = None
        running_balance: Optional[int] = None

        i = 0
        n = len(lines)
        while i < n:
            line = lines[i]

            if any(line.startswith(marker) for marker in _FOOTER_MARKERS):
                break

            if line in _HEADER_TOKENS:
                i += 1
                continue

            if _DATE_RE.match(line):
                last_date = line
                i += 1
                continue

            if _NUMBER_RE.match(line):
                # Stray number outside a transaction row - defensively skip.
                i += 1
                continue

            # Anything else starts a description block for the current
            # (possibly carried-forward) date.
            description_parts = []
            while (
                i < n
                and not _NUMBER_RE.match(lines[i])
                and not _DATE_RE.match(lines[i])
            ):
                description_parts.append(lines[i])
                i += 1

            numbers = []
            while i < n and _NUMBER_RE.match(lines[i]):
                numbers.append(lines[i])
                i += 1

            if not numbers:
                continue

            try:
                balance_minor = parse_money_minor(numbers[-1])
            except MoneyParseError:
                continue

            description = " ".join(description_parts)

            if description == "BROUGHT FORWARD":
                running_balance = balance_minor
                continue

            if running_balance is None:
                # No opening anchor seen yet - can't derive a signed amount.
                running_balance = balance_minor
                continue

            amount_minor = balance_minor - running_balance
            running_balance = balance_minor

            txn: Dict[str, Any] = {
                "date": last_date or "",
                "description": description,
                "amount_minor": amount_minor,
                "balance_minor": balance_minor,
            }
            if account_identifier:
                txn["_account_identifier_raw"] = account_identifier
            transactions.append(txn)

        if period:
            from_date, to_date = period
            for txn in transactions:
                if re.search(r"\d{4}", txn["date"]):
                    # _DATE_RE's year group is optional - if a real statement
                    # ever does print one, don't overwrite it.
                    continue
                dated = resolve_year_in_period(txn["date"], from_date, to_date)
                if dated:
                    txn["date"] = dated

        self._check_reconciliation(text, running_balance)

        return transactions

    def _check_reconciliation(self, text: str, computed_final: Optional[int]) -> None:
        """Non-blocking sanity check against the statement's own summary figures.

        Also captures the statement's "Previous Balance" anchor (previously
        parsed by _PREVIOUS_BALANCE_RE but never referenced anywhere) purely
        for cross-file continuity checking - `amount` here is already
        derived from balance deltas during parsing, so unlike every other
        adapter this opening figure plays no part in computing `matches`.
        """
        if computed_final is None:
            return

        new_balance_match = _NEW_BALANCE_RE.search(text)
        if not new_balance_match:
            return
        try:
            expected = parse_money_minor(new_balance_match.group(1))
        except MoneyParseError:
            return

        previous_match = _PREVIOUS_BALANCE_RE.search(text)
        opening = None
        if previous_match:
            try:
                opening = parse_money_minor(previous_match.group(1))
            except MoneyParseError:
                opening = None

        self.last_reconciliation = build_reconciliation_result(
            check_name="natwest_statement_new_balance",
            expected_closing_minor=expected,
            derived_closing_minor=computed_final,
            expected_opening_minor=opening,
        )
        if not self.last_reconciliation.matches:
            logger.warning(
                "Natwest statement: final parsed balance %d minor units "
                "does not match statement's printed New Balance %d minor "
                "units - the transaction table may not have parsed "
                "completely.",
                computed_final,
                expected,
            )

    def generate_source_key(
        self,
        txn: Dict[str, Any],
        line_num: int,
        account_identifier: Optional[str] = None,
    ) -> str:
        """Generate deterministic key from account + date + description + amount."""
        return make_transaction_source_key(
            "natwest_statement_txn",
            txn.get("date", ""),
            txn.get("description", ""),
            int(txn.get("amount_minor", 0)),
            account_identifier,
        )

    def detect_source_type(self) -> str:
        """Return source type."""
        return "natwest-statement"
