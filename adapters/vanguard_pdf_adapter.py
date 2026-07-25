"""Vanguard investment account PDF statement adapter."""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from adapters.base import StatementPeriod
from adapters.pdf_adapter import PdfAdapter

_ACCOUNT_NUMBER_RE = re.compile(r"Account number:\s*([A-Z0-9]+)")
_WRAPPER_INVESTMENTS_RE = re.compile(r"^Your (.+) investments at (.+)$")
_HOLDING_VALUE_RE = re.compile(r"^-$|^£[\d,]+\.\d{2}$|^\d+\.\d{2}$")
_ACTIVITY_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_ACTIVITY_AMOUNT_RE = re.compile(r"^[-+]?£[\d,]+\.\d{2}$")
_HOLDINGS_HEADER_TOKENS = {"Description", "Quantity", "Price", "Value"}
# e.g. "Activity from 01 April 2026 to 01 July 2026 for your ISA" - repeats
# once per product wrapper, same range every time on real statements seen so
# far (not treated as per-wrapper periods) - first match used. Matched
# per-line (not across the whole joined text) since the wrapper name after
# "for your " can itself wrap onto a following line.
_ACTIVITY_PERIOD_RE = re.compile(
    r"^Activity from (\d{1,2}\s+[A-Za-z]+\s+\d{4}) to (\d{1,2}\s+[A-Za-z]+\s+\d{4})"
)
_ACTIVITY_HEADER_NOISE = {
    "The transaction date is the date we carried out the activity.",
    "Transaction date Transaction details",
    "Cash amount",
    "Cash balance",
}
_PAGE_FOOTER_RE = re.compile(r"^Page \d+ of \d+$")
_ACCOUNT_LINE_RE = re.compile(r"^Account number:\s*[A-Z0-9]+$")
_ISSUED_BY_RE = re.compile(r"^Issued by Vanguard Asset Management")


class VanguardPdfAdapter(PdfAdapter):
    """Parse Vanguard investment statement PDFs.

    Real Vanguard statements cover one account number but multiple product
    wrappers (e.g. "ISA" and "Vanguard Personal Pension"), each with its own
    holdings table ("Your X investments at DATE") and its own activity
    section ("Activity from ... for your X"). This adapter tracks which
    wrapper it's currently inside and emits both holding-shaped and
    transaction-shaped dicts (tagged via "record_type"), unlike every other
    adapter which only produces transactions.
    """

    def validate_text(self, text: str) -> bool:
        """Check if text is from Vanguard statement."""
        return (
            "Vanguard" in text
            and ("Statement" in text or "Your Regular Statement" in text)
            and "Activity" in text
        )

    def _extract_account_number(self, text: str) -> Optional[str]:
        match = _ACCOUNT_NUMBER_RE.search(text)
        return match.group(1) if match else None

    def _extract_statement_period(
        self, text: str
    ) -> Optional[Tuple[datetime, datetime]]:
        """Extract the statement period, e.g. 'Activity from 01 April 2026 to
        01 July 2026 for your ISA' (first wrapper's range is used - see
        _ACTIVITY_PERIOD_RE)."""
        for line in text.split("\n"):
            match = _ACTIVITY_PERIOD_RE.match(line.strip())
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

    @staticmethod
    def _strip_page_boilerplate(lines: List[str]) -> List[str]:
        """Drop the repeating per-page header/footer block.

        Real statements repeat "{client name}\\nAccount number: X\\nPage N of M\\n
        Issued by Vanguard Asset Management...EC4N 8AF." on every page. Left in,
        it bleeds into whatever multi-line transaction description happens to
        fall on a page break. Detected structurally (anchored on the "Account
        number:" and "Issued by..." lines), not by hardcoding the client's name.
        """
        cleaned: List[str] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if _ACCOUNT_LINE_RE.match(line):
                if cleaned:
                    cleaned.pop()  # the client-name line just added
                i += 1
                if i < len(lines) and _PAGE_FOOTER_RE.match(lines[i]):
                    i += 1
                continue
            if _ISSUED_BY_RE.match(line):
                # This boilerplate paragraph wraps across multiple lines;
                # skip through to the closing address line, inclusive.
                while i < len(lines) and "EC4N 8AF." not in lines[i]:
                    i += 1
                if i < len(lines):
                    i += 1
                continue
            cleaned.append(line)
            i += 1
        return cleaned

    def parse_transactions(self, text: str) -> List[Dict[str, Any]]:
        """Extract holdings + activity across all product wrappers."""
        self.last_statement_period = None
        period = self._extract_statement_period(text)
        if period:
            self.last_statement_period = StatementPeriod(period[0], period[1])

        account_number = self._extract_account_number(text)
        lines = [line.strip() for line in text.split("\n")]
        lines = [line for line in lines if line]
        lines = self._strip_page_boilerplate(lines)

        records: List[Dict[str, Any]] = []
        current_wrapper: Optional[str] = None
        i = 0
        while i < len(lines):
            line = lines[i]

            wrapper_match = _WRAPPER_INVESTMENTS_RE.match(line)
            if wrapper_match:
                current_wrapper = wrapper_match.group(1).strip()
                as_of_date = wrapper_match.group(2).strip()
                i, holdings = self._parse_holdings_block(
                    lines, i + 1, current_wrapper, as_of_date, account_number
                )
                records.extend(holdings)
                continue

            if line.startswith("Activity from") and current_wrapper:
                i, activity = self._parse_activity_block(
                    lines, i + 1, current_wrapper, account_number
                )
                records.extend(activity)
                continue

            i += 1

        return records

    def _parse_holdings_block(
        self,
        lines: List[str],
        i: int,
        wrapper: str,
        as_of_date: str,
        account_number: Optional[str],
    ) -> Tuple[int, List[Dict[str, Any]]]:
        """Parse the 'Your X investments at DATE' table for one wrapper."""
        holdings = []
        description_parts: List[str] = []
        values: List[str] = []

        while i < len(lines) and lines[i] in _HOLDINGS_HEADER_TOKENS:
            i += 1

        while i < len(lines):
            line = lines[i]
            if line.startswith("Activity from") or _WRAPPER_INVESTMENTS_RE.match(line):
                break

            if _HOLDING_VALUE_RE.match(line):
                values.append(line)
            else:
                description_parts.append(line)

            if len(values) == 3:
                account_identifier = (
                    f"{account_number}_{wrapper}" if account_number else None
                )
                holdings.append(
                    {
                        "record_type": "holding",
                        "wrapper": wrapper,
                        "fund_name": " ".join(description_parts).strip(),
                        "quantity": values[0],
                        "unit_price": values[1],
                        "total_value": values[2],
                        "as_of_date": as_of_date,
                        "_account_identifier_raw": account_identifier,
                    }
                )
                description_parts = []
                values = []

            i += 1

        return i, holdings

    def _parse_activity_block(
        self, lines: List[str], i: int, wrapper: str, account_number: Optional[str]
    ) -> Tuple[int, List[Dict[str, Any]]]:
        """Parse the 'Activity from ... for your X' section for one wrapper."""
        activity = []
        current_txn_lines: List[str] = []

        def flush() -> Optional[Dict[str, Any]]:
            if not current_txn_lines:
                return None
            return self._parse_single_activity_txn(
                current_txn_lines, wrapper, account_number
            )

        while i < len(lines):
            line = lines[i]
            if line.startswith("Your ") or line.startswith("Activity from"):
                break
            if line in _ACTIVITY_HEADER_NOISE:
                i += 1
                continue

            is_new_txn = _ACTIVITY_DATE_RE.match(line)
            if is_new_txn and current_txn_lines:
                txn = flush()
                if txn:
                    activity.append(txn)
                current_txn_lines = [line]
            elif is_new_txn or current_txn_lines:
                current_txn_lines.append(line)

            i += 1

        txn = flush()
        if txn:
            activity.append(txn)

        return i, activity

    def _parse_single_activity_txn(
        self, lines: List[str], wrapper: str, account_number: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        if not lines or not _ACTIVITY_DATE_RE.match(lines[0]):
            return None
        date_str = lines[0]

        description_parts = []
        amounts = []
        for line in lines[1:]:
            if _ACTIVITY_AMOUNT_RE.match(line):
                sign = -1 if line.startswith("-") else 1
                amount_str = line.lstrip("+-").lstrip("£").replace(",", "")
                try:
                    amounts.append(sign * float(amount_str))
                except ValueError:
                    pass
            else:
                description_parts.append(line)

        if not description_parts or not amounts:
            return None

        account_identifier = f"{account_number}_{wrapper}" if account_number else None
        record = {
            "record_type": "transaction",
            "date": date_str,
            "description": " ".join(description_parts),
            "amount": amounts[0],  # cash amount
            "_account_identifier_raw": account_identifier,
        }
        if len(amounts) >= 2:
            # Cash balance within this wrapper - distinct from "Portfolio
            # Value" (the CSV ledger's balance metric), so kept as its own
            # field rather than fed into account_ledger.
            record["cash_balance"] = amounts[1]
        return record

    def generate_source_key(
        self,
        txn: Dict[str, Any],
        line_num: int,
        account_identifier: Optional[str] = None,
    ) -> str:
        """Generate deterministic key from account + (date|as_of_date) + description/fund + amount."""
        account_part = f"{account_identifier}_" if account_identifier else ""

        if txn.get("record_type") == "holding":
            as_of = txn.get("as_of_date", "").replace(" ", "_")
            fund = txn.get("fund_name", "")[:15].replace(" ", "_")
            return f"vanguard_holding_{account_part}{fund}_{as_of}"

        date_str = txn.get("date", "").replace("/", "")
        description = txn.get("description", "")[:15].replace(" ", "_")
        amount = str(abs(txn.get("amount", 0))).replace(".", "_")
        return f"vanguard_txn_{account_part}{date_str}_{description}_{amount}"

    def detect_source_type(self) -> str:
        """Return source type."""
        return "vanguard-pdf"
