"""American Express credit card PDF statement adapter."""

import re
from typing import Any, Dict, List, Optional

from adapters.pdf_adapter import PdfAdapter

_DATE_RE = re.compile(r"^([A-Z][a-z]{2})\s+(\d{1,2})$")
_AMOUNT_RE = re.compile(r"^[\d,]+\.\d{2}$")
_CARD_RE = re.compile(r"[xX]{4}-[xX]{6}-\d+")


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

    def parse_transactions(self, text: str) -> List[Dict[str, Any]]:
        """Extract transactions from an American Express statement."""
        account_identifier = self._extract_account_identifier(text)

        transactions = []
        for page_text in text.split("\x0c"):
            transactions.extend(self._parse_page(page_text))

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
