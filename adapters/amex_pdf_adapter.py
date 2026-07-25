"""American Express credit card PDF statement adapter."""

import logging
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple, Union

import fitz

from adapters.pdf_adapter import PdfAdapter, resolve_year_in_period

logger = logging.getLogger(__name__)

_CARD_RE = re.compile(r"[xX]{4}-[xX]{6}-\d+")
# A transaction row, once extracted in reading order (see _extract_text):
# "Feb 19   Feb 19   TESCO STORE 5312 5312TE GLASGOW          2.15". The
# process date is captured but unused - only the transaction date is kept.
_TXN_LINE_RE = re.compile(
    r"^([A-Z][a-z]{2}\s+\d{1,2})\s+[A-Z][a-z]{2}\s+\d{1,2}\s+(.+?)\s+([\d,]+\.\d{2})$"
)
# A "New Plan It Instalments Created" row: single date, e.g.
# "Apr 12   INSTALMENT PLAN   1,656.39" - see _parse_plan_it_created.
_PLAN_IT_CREATED_LINE_RE = re.compile(
    r"^([A-Z][a-z]{2}\s+\d{1,2})\s+(.+?)\s+([\d,]+\.\d{2})$"
)
# Captures the whole "Previous - Credits + Debits [+ Plan It Due] = Closing"
# values block: every consecutive line containing a "£" right after the
# header, stopping at the first line that doesn't (usually a blank line,
# but not always - see _extract_account_summary). The exact column count
# varies, so counting/positioning happens there instead of in this regex.
_ACCOUNT_SUMMARY_RE = re.compile(
    r"Previous Closing Balance.*?Closing Balance\s*\n+((?:[^\n]*£[^\n]*\n)+)",
    re.DOTALL,
)
_SUMMARY_AMOUNT_RE = re.compile(r"£\s*([\d,]+\.\d{2})")
# e.g. "From  20 April to 19 May 2026" - the year only appears once, on the
# closing ("to") date; the opening date's year is inferred (see
# _extract_statement_period), since the period can cross a year boundary.
_PERIOD_RE = re.compile(
    r"From\s+(\d{1,2}\s+[A-Za-z]+)\s+to\s+(\d{1,2}\s+[A-Za-z]+)\s+(\d{4})"
)


class AmexPdfAdapter(PdfAdapter):
    """Parse American Express PDF statements.

    PyMuPDF's *default* text extraction flattens real Amex statements into
    column-major blocks - all Transaction/Process Date pairs first, then
    unrelated remark lines, then every Amount as a separate block - which
    makes reconstructing a row (matching a date pair to its amount) an
    unreliable positional-guessing exercise: real statements were found to
    split the amount block across multiple runs (a "Total ..." summary line
    or a new section like "OTHER ACCOUNT TRANSACTIONS" interrupts it), and
    to separate a "CR" credit marker from the row it belongs to whenever a
    PDF page boundary or block-ordering quirk lands between them - both
    silently produce wrong amounts, wrong signs, or dropped transactions.

    `_extract_text` is overridden below to use PyMuPDF's `sort=True` mode
    instead, which reorders text spans by position and reliably keeps each
    transaction as one line - "Feb 19   Feb 19   TESCO STORE ...   2.15",
    optionally followed by a "CR" line for credits - so rows are parsed with
    a single-line regex (`_TXN_LINE_RE`) rather than column reconstruction.
    """

    def validate_text(self, text: str) -> bool:
        """Check if text is from American Express statement."""
        return "American Express" in text and (
            "Preferred Rewards" in text or "Credit Card" in text
        )

    def parse(
        self, file_content: Union[str, bytes], filename: str, file_hash: str
    ) -> List[Any]:
        """Parse transactions as usual, then attach a derived running balance.

        Amex statements don't print a per-transaction balance - only a
        single "Previous Closing Balance" anchor in the Account Summary box.
        That box sits side-by-side with an unrelated box (Direct Debit info)
        on the page, and PyMuPDF's default plain-text extraction interleaves
        the two unpredictably (verified against a real statement). Extracting
        it needs `sort=True`, which reorders spans by position and reliably
        produces "Previous Closing Balance ... Closing Balance" immediately
        followed by "£769.58 - £779.58 + £1,437.34 = £1,427.34" - used only
        for this one lookup, not for transaction parsing, which continues to
        rely on the default flattened order handled elsewhere in this file.
        """
        records = super().parse(file_content, filename, file_hash)
        if isinstance(file_content, str) or not records:
            return records

        anchors = self._extract_account_summary(file_content)
        if anchors is None:
            return records
        previous_balance, statement_closing_balance, plan_it_instalments_due = anchors

        running = previous_balance
        for record in records:
            # Amex's `amount` follows a cash-received convention (spend
            # negative, payments/credits positive), but Closing Balance is
            # a liability that moves the opposite way - spend increases what
            # you owe, payments decrease it - so it's subtracted, not added.
            running -= Decimal(str(record.raw_data["amount"]))
            record.raw_data["balance"] = float(running.quantize(Decimal("0.01")))

        if plan_it_instalments_due is not None:
            # Months with an active Plan It installment plan add a further
            # "Plan It Instalments Due" component to what's owed that never
            # appears as a line in the transaction table at all (it lives in
            # a separate "Plan It Instalments Summary" table, not parsed
            # here) - Closing = Previous - Credits + Debits + Plan It Due.
            # Not tied to any specific transaction, so it's folded into the
            # last record's balance rather than left unaccounted for.
            running += plan_it_instalments_due
            records[-1].raw_data["balance"] = float(running.quantize(Decimal("0.01")))

        if running.quantize(Decimal("0.01")) != statement_closing_balance:
            logger.warning(
                "Amex %s: derived closing balance %.2f does not match "
                "statement's printed Closing Balance %.2f - transaction "
                "amounts don't fully reconcile with the Account Summary box. "
                "Balance fields on this statement's records may be "
                "inaccurate.",
                filename,
                running,
                statement_closing_balance,
            )

        return records

    @staticmethod
    def _extract_account_summary(
        file_content: bytes,
    ) -> Optional[tuple[Decimal, Decimal, Optional[Decimal]]]:
        """Extract (previous_closing_balance, closing_balance,
        plan_it_instalments_due) from the Account Summary box on the
        statement's first page.

        The values line normally reads "Previous - Credits + Debits =
        Closing" (4 £ amounts), but any statement with an active Plan It
        installment plan gets a 5th column inserted before Closing: "Previous
        - Credits + Debits + Plan It Instalments Due = Closing". Rather than
        hardcode a column count, every £ amount on the line is extracted and
        the count (4 vs 5) determines whether a Plan It Due figure is
        present. Amex also isn't consistent about a space after "£" (seen
        both "£1,427.34" and "£ 769.58" on real statements), hence `£\\s*`.
        """
        doc = fitz.open(stream=file_content, filetype="pdf")
        try:
            text = doc[0].get_text(sort=True)
        finally:
            doc.close()

        match = _ACCOUNT_SUMMARY_RE.search(text)
        if not match:
            return None
        values = _SUMMARY_AMOUNT_RE.findall(match.group(1))
        if len(values) not in (4, 5):
            return None
        try:
            previous_balance = Decimal(values[0].replace(",", ""))
            closing_balance = Decimal(values[-1].replace(",", ""))
            plan_it_instalments_due = (
                Decimal(values[3].replace(",", "")) if len(values) == 5 else None
            )
        except InvalidOperation:
            return None
        return previous_balance, closing_balance, plan_it_instalments_due

    @staticmethod
    def _extract_text(file_content: bytes) -> str:
        """Extract text page-by-page using `sort=True` (see class docstring).

        Overrides `PdfAdapter._extract_text`'s default flattened extraction.
        Pages are still joined with the same "\\x0c" sentinel other adapters
        rely on, and `validate()`/`_extract_account_identifier` (both of
        which just substring-search the whole text) are unaffected by the
        different ordering.
        """
        doc = fitz.open(stream=file_content, filetype="pdf")
        text = ""
        for page in doc:
            text += page.get_text(sort=True) + "\n\x0c\n"
        doc.close()
        return text

    def _extract_account_identifier(self, text: str) -> Optional[str]:
        """Extract the masked card number, e.g. 'xxxx-xxxxxx-82009'."""
        match = _CARD_RE.search(text)
        return match.group(0) if match else None

    def _extract_statement_period(
        self, text: str
    ) -> Optional[Tuple[datetime, datetime]]:
        """Extract the statement period, e.g. 'From 20 April to 19 May 2026'.

        Only the closing date carries a year in the source text - the
        opening date's year is inferred from it (same year, unless the
        opening month is numerically after the closing month, which means
        the period crossed a year boundary, e.g. Dec -> Jan).
        """
        match = _PERIOD_RE.search(text)
        if not match:
            return None
        from_str, to_str, year_str = match.groups()
        to_year = int(year_str)
        try:
            to_date = datetime.strptime(f"{to_str} {to_year}", "%d %B %Y")
            # Placeholder leap year so "29 February" parses without needing
            # (and without pandas warning about) an implicit default year.
            from_no_year = datetime.strptime(f"{from_str} 2000", "%d %B %Y")
        except ValueError:
            return None
        from_year = to_year if from_no_year.month <= to_date.month else to_year - 1
        from_date = from_no_year.replace(year=from_year)
        return from_date, to_date

    def parse_transactions(self, text: str) -> List[Dict[str, Any]]:
        """Extract transactions from an American Express statement."""
        account_identifier = self._extract_account_identifier(text)
        period = self._extract_statement_period(text)

        transactions = []
        for page_text in text.split("\x0c"):
            transactions.extend(self._parse_page(page_text))

        if period:
            from_date, to_date = period
            for txn in transactions:
                dated = resolve_year_in_period(txn["date"], from_date, to_date)
                if dated:
                    txn["date"] = dated

        if account_identifier:
            for txn in transactions:
                txn["_account_identifier_raw"] = account_identifier

        return transactions

    def _parse_page(self, page_text: str) -> List[Dict[str, Any]]:
        """Extract transactions from one page's `sort=True` text.

        Skips anything that isn't a "date  date  description  amount" line -
        this naturally excludes "Total ..." summary lines, and is why the
        "New Plan It Instalments Created" section (see below) needs its own
        matching: its rows have only one leading date, not the two-date
        pair `_TXN_LINE_RE` requires.
        """
        lines = [line.strip() for line in page_text.split("\n")]
        lines = [line for line in lines if line]

        transactions = self._parse_main_table(lines)
        transactions.extend(self._parse_plan_it_created(lines))
        return transactions

    @staticmethod
    def _is_credit_marked(lines: List[str], idx: int) -> bool:
        """Check whether the line after `lines[idx]` marks it as a credit.

        Amex prints "CR" two different ways depending on the section: a
        standalone "CR" line right after the transaction (e.g. "PAYMENT
        RECEIVED - THANK YOU"), or appended as a trailing token on an
        annotation line that follows it (e.g. "DeliverooGoldBenefit ... CR"
        under "OTHER ACCOUNT TRANSACTIONS"). Section membership alone isn't
        a reliable signal - that same section can also hold plain debits
        (e.g. a "MEMBERSHIP FEE" line with no adjacent "CR" at all) - so
        this "CR"-marker check is the only signal used, uniformly, for both
        the main table and "OTHER ACCOUNT TRANSACTIONS".
        """
        next_line = lines[idx + 1] if idx + 1 < len(lines) else None
        if next_line is None:
            return False
        return next_line == "CR" or next_line.endswith(" CR")

    def _parse_main_table(self, lines: List[str]) -> List[Dict[str, Any]]:
        """Parse the "date date description amount" rows (spend table +
        "OTHER ACCOUNT TRANSACTIONS")."""
        transactions = []
        for idx, line in enumerate(lines):
            match = _TXN_LINE_RE.match(line)
            if not match:
                continue
            txn_date, description, amount_str = match.groups()
            month, day = txn_date.split()
            try:
                amount = float(amount_str.replace(",", ""))
            except ValueError:
                continue

            if not self._is_credit_marked(lines, idx):
                amount = -amount  # Amex lists spend as positive; we use signed debits

            transactions.append(
                {
                    "date": f"{day} {month}",
                    "description": description,
                    "amount": amount,
                }
            )

        return transactions

    def _parse_plan_it_created(self, lines: List[str]) -> List[Dict[str, Any]]:
        """Parse "New Plan It Instalments Created" rows as credits.

        These reflect an existing "Standard transaction" (already parsed as
        a normal debit by `_parse_main_table`, on its own earlier date)
        being moved into an installment plan - printed with only a single
        date, e.g. "Apr 12   INSTALMENT PLAN   1,656.39", so they need their
        own matching rather than `_TXN_LINE_RE`. Amex's own "New Credits"
        total on the Account Summary box includes this amount, so it must
        be added back as a real (dated) credit or the derived running
        balance permanently overstates what's owed from that date on - see
        Bug 4 in AMEX_BUG_HANDOFF.md. Stops at the section's own "Total of
        New Plan It Instalments Created" line, which doesn't match the
        row pattern either (no amount immediately follows a date).
        """
        try:
            start = lines.index("New Plan It Instalments Created") + 1
        except ValueError:
            return []

        transactions = []
        for line in lines[start:]:
            if line.startswith("Total of New Plan It Instalments Created"):
                break
            match = _PLAN_IT_CREATED_LINE_RE.match(line)
            if not match:
                continue
            txn_date, description, amount_str = match.groups()
            month, day = txn_date.split()
            try:
                amount = float(amount_str.replace(",", ""))
            except ValueError:
                continue
            transactions.append(
                {
                    "date": f"{day} {month}",
                    "description": description,
                    "amount": amount,  # always a credit
                }
            )

        return transactions

    def generate_source_key(
        self,
        txn: Dict[str, Any],
        line_num: int,
        account_identifier: Optional[str] = None,
    ) -> str:
        """Generate deterministic key from account + date + description + amount."""
        date_str = txn.get("date", "").replace(" ", "")
        description = txn.get("description", "")[:10].replace(" ", "_")
        amount = str(abs(txn.get("amount", 0))).replace(".", "_")
        account_part = f"{account_identifier}_" if account_identifier else ""

        return f"amex_txn_{account_part}{date_str}_{description}_{amount}"

    def detect_source_type(self) -> str:
        """Return source type."""
        return "amex"
