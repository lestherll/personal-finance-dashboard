"""American Express credit card PDF statement adapter."""

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

import fitz

from models.money import parse_money_minor, MoneyParseError

from adapters.base import StatementPeriod
from adapters.pdf_adapter import PdfAdapter, resolve_year_in_period
from adapters.reconciliation import build_reconciliation_result

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
# "Plan It Instalments Summary" table rows - see _parse_plan_it_summary.
# Each plan spans 3 physical lines once flattened by sort=True:
#   "Apr 12 2026   MALAYSIA AIRLINES KUALA KUALA LUMPUR"
#   "1,656.39   1,104.26   552.13   17.23   569.36   1 OF 3"
#   "51.68"
_PLAN_IT_SUMMARY_START_RE = re.compile(r"^([A-Z][a-z]{2}\s+\d{1,2}\s+\d{4})\s+(.+)$")
_PLAN_IT_SUMMARY_VALUES_RE = re.compile(
    r"^([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+"
    r"([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+(\d+\s+OF\s+\d+)$"
)
_PLAN_IT_SUMMARY_FEE_RE = re.compile(r"^([\d,]+\.\d{2})$")


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

    PARSER_VERSION = "2"

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
        self.last_reconciliation = None
        records = super().parse(file_content, filename, file_hash)
        if isinstance(file_content, str) or not records:
            return records

        anchors = self._extract_account_summary(file_content)
        if anchors is None:
            return records
        previous_balance, statement_closing_balance, plan_it_instalments_due = anchors

        # Balance rolls forward through actual transactions only - the
        # "Plan It Instalments Summary" table (see _parse_plan_it_summary)
        # produces its own record_type ("plan_it_instalment") with no
        # `amount` field at all, since it's a per-plan snapshot, not a
        # cash-flow event.
        transaction_records = [r for r in records if r.record_type == "transaction"]

        running = previous_balance
        for record in transaction_records:
            # Amex's `amount` follows a cash-received convention (spend
            # negative, payments/credits positive), but Closing Balance is
            # a liability that moves the opposite way - spend increases what
            # you owe, payments decrease it - so it's subtracted, not added.
            running -= record.raw_data["amount_minor"]
            record.raw_data["balance_minor"] = running

        if plan_it_instalments_due is not None and transaction_records:
            # Months with an active Plan It installment plan add a further
            # "Plan It Instalments Due" component to what's owed - Closing =
            # Previous - Credits + Debits + Plan It Due. Part of that lump is
            # a dated "INSTALMENT PLAN FEE" line, already parsed by
            # _parse_plan_it_fees() and folded into `running` through the
            # normal per-record loop above like any other debit - subtract it
            # back out here so it isn't counted twice. What's left (the
            # month's principal-only portion) has no transaction of its own
            # anywhere on the statement, so it's folded into the last
            # transaction record's balance rather than left unaccounted for.
            fees_already_captured = sum(
                (
                    -record.raw_data["amount_minor"]
                    for record in transaction_records
                    if record.raw_data.get("description") == "INSTALMENT PLAN FEE"
                )
            )
            running += plan_it_instalments_due - fees_already_captured
            transaction_records[-1].raw_data["balance_minor"] = running

        derived_closing = running
        self.last_reconciliation = build_reconciliation_result(
            check_name="amex_closing_balance",
            expected_closing_minor=statement_closing_balance,
            derived_closing_minor=derived_closing,
            expected_opening_minor=previous_balance,
        )
        matches = self.last_reconciliation is not None and self.last_reconciliation.matches
        if not matches:
            logger.warning(
                "Amex %s: derived closing balance %d minor units does not "
                "match statement's printed Closing Balance %d minor units - "
                "transaction amounts don't fully reconcile with the Account "
                "Summary box. Balance fields on this statement's records "
                "may be inaccurate.",
                filename,
                running,
                statement_closing_balance,
            )

        return records

    @staticmethod
    def _extract_account_summary(
        file_content: bytes,
    ) -> Optional[tuple[int, int, Optional[int]]]:
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
            previous_balance = parse_money_minor(values[0])
            closing_balance = parse_money_minor(values[-1])
            plan_it_instalments_due = (
                parse_money_minor(values[3]) if len(values) == 5 else None
            )
        except MoneyParseError:
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
        self.last_statement_period = None
        account_identifier = self._extract_account_identifier(text)
        period = self._extract_statement_period(text)
        # The "Plan It Instalments Summary" table (_parse_plan_it_summary)
        # has no dated transaction of its own to anchor "as of" - it's a
        # snapshot, so it borrows the statement's own closing date.
        closing_date = period[1].strftime("%d %b %Y") if period else None

        transactions = []
        for page_text in text.split("\x0c"):
            transactions.extend(self._parse_page(page_text, closing_date))

        if period:
            from_date, to_date = period
            self.last_statement_period = StatementPeriod(from_date, to_date)
            for txn in transactions:
                if "date" not in txn:
                    continue  # e.g. plan_it_instalment records - see above
                dated = resolve_year_in_period(txn["date"], from_date, to_date)
                if dated:
                    txn["date"] = dated

        if account_identifier:
            for txn in transactions:
                txn["_account_identifier_raw"] = account_identifier

        return transactions

    def _parse_page(
        self, page_text: str, closing_date: Optional[str] = None
    ) -> List[Dict[str, Any]]:
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
        transactions.extend(self._parse_plan_it_fees(lines))
        transactions.extend(self._parse_plan_it_summary(lines, closing_date))
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
                amount_minor = parse_money_minor(amount_str)
            except MoneyParseError:
                continue

            if not self._is_credit_marked(lines, idx):
                amount_minor = -amount_minor  # Amex lists spend as positive; we use signed debits

            transactions.append(
                {
                    "date": f"{day} {month}",
                    "description": description,
                    "amount_minor": amount_minor,
                    "amount_text": amount_str,
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
                amount_minor = parse_money_minor(amount_str)
            except MoneyParseError:
                continue
            transactions.append(
                {
                    "date": f"{day} {month}",
                    "description": description,
                    "amount_minor": amount_minor,  # always a credit
                    "amount_text": amount_str,
                }
            )

        return transactions

    def _parse_plan_it_fees(self, lines: List[str]) -> List[Dict[str, Any]]:
        """Parse "INSTALMENT PLAN FEE" rows from a distinct, later section,
        "New Plan It Instalments and Fees", as debits.

        This is a different section from "New Plan It Instalments Created"
        (see _parse_plan_it_created above) - it's part of "TRANSACTION
        DETAILS", not the earlier spend table, and appears every month an
        Plan It plan is active (not just the month it's created). It also
        restates the instalment plan itself whenever a new plan was created
        that month (a bare "INSTALMENT PLAN" line, no "FEE" suffix) - that's
        not a new transaction, just a reference back to the credit already
        parsed by _parse_plan_it_created, so only rows whose description is
        exactly "INSTALMENT PLAN FEE" are kept here. Stops at the section's
        own "Total of New Instalment Plans and Fees" line, which doesn't
        match the row pattern either (no amount immediately follows a date).
        """
        try:
            start = lines.index("New Plan It Instalments and Fees") + 1
        except ValueError:
            return []

        transactions = []
        for line in lines[start:]:
            if line.startswith("Total of New Instalment Plans and Fees"):
                break
            match = _PLAN_IT_CREATED_LINE_RE.match(line)
            if not match:
                continue
            txn_date, description, amount_str = match.groups()
            if description != "INSTALMENT PLAN FEE":
                continue
            month, day = txn_date.split()
            try:
                amount_minor = parse_money_minor(amount_str)
            except MoneyParseError:
                continue
            transactions.append(
                {
                    "date": f"{day} {month}",
                    "description": description,
                    "amount_minor": -amount_minor,  # a fee is always a debit
                    "amount_text": amount_str,
                }
            )

        return transactions

    def _parse_plan_it_summary(
        self, lines: List[str], closing_date: Optional[str]
    ) -> List[Dict[str, Any]]:
        """Parse the "Plan It Instalments Summary" table: one row per active
        instalment plan (start date, merchant, lifetime plan amount/fee,
        remaining balance, this month's plan/fee/total split, "N OF M"
        progress).

        Not tied to the aggregate "Plan It Instalments Due" figure used for
        balance reconciliation in parse() - this is purely for per-plan
        visibility (e.g. tracking a specific purchase's payoff progress),
        tagged with its own "plan_it_instalment" record_type the same way
        VanguardPdfAdapter tags "holding" records alongside "transaction"
        ones.

        Each plan prints across 3 physical lines once flattened by
        sort=True (the "Plan Amount / Total Fee £" column header wraps
        across two rows, so the lifetime fee total prints on its own line
        below the rest of that row's values) - see the module-level regex
        comments. Stops at the first "Total"/"Total Fees" aggregate row.
        Only a single-active-plan statement has been seen in practice across
        the 3 real statements validated against - if a real statement with
        two concurrent plans surfaces, re-verify this 3-line-per-plan
        grouping still holds back-to-back (see Gotcha #8/#15 in CLAUDE.md).
        """
        try:
            start = lines.index("Plan It Instalments Summary") + 1
        except ValueError:
            return []

        plans = []
        i = start
        while i < len(lines):
            line = lines[i]
            if line.startswith("Total"):
                break
            start_match = _PLAN_IT_SUMMARY_START_RE.match(line)
            if not start_match or i + 2 >= len(lines):
                i += 1
                continue
            values_match = _PLAN_IT_SUMMARY_VALUES_RE.match(lines[i + 1])
            fee_match = _PLAN_IT_SUMMARY_FEE_RE.match(lines[i + 2])
            if not values_match or not fee_match:
                i += 1
                continue

            start_date, description = start_match.groups()
            (
                plan_total,
                remaining_balance,
                due_plan,
                due_fee,
                due_total,
                instalment_progress,
            ) = values_match.groups()

            plans.append(
                {
                    "record_type": "plan_it_instalment",
                    "start_date": start_date,
                    "description": description,
                    "plan_total": plan_total,
                    "plan_lifetime_fee": fee_match.group(1),
                    "remaining_balance": remaining_balance,
                    "due_this_month_plan": due_plan,
                    "due_this_month_fee": due_fee,
                    "due_this_month_total": due_total,
                    "instalment_progress": instalment_progress,
                    "as_of_date": closing_date,
                }
            )
            i += 3

        return plans

    def generate_source_key(
        self,
        txn: Dict[str, Any],
        line_num: int,
        account_identifier: Optional[str] = None,
    ) -> str:
        """Generate deterministic key from account + date + description + amount."""
        account_part = f"{account_identifier}_" if account_identifier else ""

        if txn.get("record_type") == "plan_it_instalment":
            # Distinguished from other months' rows for the same plan by
            # as_of_date (the statement's own closing date) - mirrors
            # VanguardPdfAdapter's holding key (account + fund_name +
            # as_of_date), since each month's statement re-prints the same
            # plan with updated progress.
            start_date = txn.get("start_date", "").replace(" ", "_")
            description = txn.get("description", "")[:15].replace(" ", "_")
            as_of = (txn.get("as_of_date") or "").replace(" ", "_")
            return f"amex_planit_{account_part}{start_date}_{description}_{as_of}"

        date_str = txn.get("date", "").replace(" ", "")
        description = txn.get("description", "")[:10].replace(" ", "_")
        amount = str(abs(txn.get("amount_minor", 0)))

        return f"amex_txn_{account_part}{date_str}_{description}_{amount}"

    def detect_source_type(self) -> str:
        """Return source type."""
        return "amex"
