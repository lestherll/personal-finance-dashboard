"""American Express credit card PDF statement adapter."""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from adapters.pdf_adapter import PdfAdapter, resolve_year_in_period

_DATE_RE = re.compile(r"^([A-Z][a-z]{2})\s+(\d{1,2})$")
_AMOUNT_RE = re.compile(r"^[\d,]+\.\d{2}$")
_CARD_RE = re.compile(r"[xX]{4}-[xX]{6}-\d+")
# e.g. "From  20 April to 19 May 2026" - the year only appears once, on the
# closing ("to") date; the opening date's year is inferred (see
# _extract_statement_period), since the period can cross a year boundary.
_PERIOD_RE = re.compile(
    r"From\s+(\d{1,2}\s+[A-Za-z]+)\s+to\s+(\d{1,2}\s+[A-Za-z]+)\s+(\d{4})"
)


class AmexPdfAdapter(PdfAdapter):
    """Parse American Express PDF statements.

    Real Amex exports extract via PyMuPDF as column-flattened tables, not
    one line per transaction: all Transaction Date/Process Date/Details
    triples print first (in order), followed by unrelated remark lines and
    stray header fragments, followed by every Amount value as a separate
    block (also in order). Rows are reconstructed by scanning for the date
    pair + description triples, then zipping them positionally against the
    amount block - not by regexing a single line per transaction.
    """

    def validate_text(self, text: str) -> bool:
        """Check if text is from American Express statement."""
        return "American Express" in text and (
            "Preferred Rewards" in text or "Credit Card" in text
        )

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
        """Reconstruct transactions from one page's column-flattened text."""
        lines = [line.strip() for line in page_text.split("\n")]
        lines = [line for line in lines if line]

        # Scan for (transaction_date, process_date, description) triples
        # wherever they occur - stray header/remark lines are just skipped.
        triples = []
        i = 0
        while i < len(lines):
            if (
                i + 2 < len(lines)
                and _DATE_RE.match(lines[i])
                and _DATE_RE.match(lines[i + 1])
                and not _DATE_RE.match(lines[i + 2])
                and not _AMOUNT_RE.match(lines[i + 2])
            ):
                is_credit = i > 0 and lines[i - 1] == "CR"
                triples.append((is_credit, lines[i], lines[i + 2]))
                i += 3
            else:
                i += 1

        if not triples:
            return []

        amounts = self._select_amount_block(lines, len(triples))

        transactions = []
        for (is_credit, txn_date, description), amount_str in zip(triples, amounts):
            month, day = txn_date.split()
            try:
                amount = float(amount_str.replace(",", ""))
            except ValueError:
                continue
            if not is_credit:
                amount = -amount  # Amex lists spend as positive; we use signed debits

            transactions.append(
                {
                    "date": f"{day} {month}",
                    "description": description,
                    "amount": amount,
                }
            )

        return transactions

    @staticmethod
    def _select_amount_block(lines: List[str], expected_count: int) -> List[str]:
        """Find the contiguous run of amount-shaped lines matching the transaction count.

        Statement boilerplate (e.g. an interest-rate worked example) can
        contain decimal numbers too, so we can't just take every amount-shaped
        line on the page - only a run whose length matches the transaction
        count is trustworthy. Falls back to the last run if none matches exactly.
        """
        runs: List[List[str]] = []
        current: List[str] = []
        for line in lines:
            if _AMOUNT_RE.match(line):
                current.append(line)
            elif current:
                runs.append(current)
                current = []
        if current:
            runs.append(current)

        for run in runs:
            if len(run) == expected_count:
                return run
        return runs[-1] if runs else []

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
