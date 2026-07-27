"""Vanguard investment account PDF statement adapter."""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from models.money import parse_money_minor, MoneyParseError

from adapters.base import (
    ReconciliationResult,
    StatementPeriod,
    hash_account_identifier,
    make_snapshot_source_key,
    make_transaction_source_key,
)
from adapters.pdf_adapter import PdfAdapter

_ACCOUNT_NUMBER_RE = re.compile(r"Account number:\s*([A-Z0-9]+)")
_WRAPPER_INVESTMENTS_RE = re.compile(r"^Your (.+) investments at (.+)$")
_HOLDING_VALUE_RE = re.compile(r"^-$|^£[\d,]+\.\d{2}$|^\d+\.\d{2}$")
_ACTIVITY_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_ACTIVITY_AMOUNT_RE = re.compile(r"^[-+]?£[\d,]+\.\d{2}$")
_HOLDINGS_HEADER_TOKENS = {"Description", "Quantity", "Price", "Value"}
# "Your Vanguard account summary" table (page 1, before either wrapper's
# sections) - a per-wrapper reconciliation anchor: "Value on <end date>"
# equals that wrapper's holdings total (fund total_value + cash total_value).
# See _parse_account_summary_block/_check_reconciliation.
_ACCOUNT_SUMMARY_HEADER_RE = re.compile(r"^Value on (.+)$")
_ACCOUNT_SUMMARY_VALUE_RE = re.compile(r"^£[\d,]+\.\d{2}$")
_SLUGIFY_RE = re.compile(r"[^a-z0-9]+")
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

    PARSER_VERSION = "2"

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
        self.last_reconciliations = []
        period = self._extract_statement_period(text)
        if period:
            self.last_statement_period = StatementPeriod(period[0], period[1])

        account_number = self._extract_account_number(text)
        lines = [line.strip() for line in text.split("\n")]
        lines = [line for line in lines if line]
        lines = self._strip_page_boilerplate(lines)

        records: List[Dict[str, Any]] = []
        account_summary: Dict[str, int] = {}
        current_wrapper: Optional[str] = None
        i = 0
        while i < len(lines):
            line = lines[i]

            if line == "Your Vanguard account summary":
                i, account_summary = self._parse_account_summary_block(lines, i + 1)
                continue

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

        self._check_reconciliation(records, account_summary, account_number)
        return records

    def _parse_account_summary_block(
        self, lines: List[str], i: int
    ) -> Tuple[int, Dict[str, int]]:
        """Parse 'Your Vanguard account summary' (page 1): a per-wrapper
        table of 'Value on <date>' columns, one row per wrapper plus a
        trailing 'Account total' row. Returns the closing (rightmost)
        value per wrapper name - no new records, this is purely a
        reconciliation anchor consumed by _check_reconciliation, same
        pattern as Kroo's printed closing-balance search."""
        summary: Dict[str, int] = {}

        if i < len(lines) and lines[i] == "Product":
            i += 1
        else:
            return i, summary

        num_value_columns = 0
        while i < len(lines) and _ACCOUNT_SUMMARY_HEADER_RE.match(lines[i]):
            num_value_columns += 1
            i += 1

        if num_value_columns == 0:
            return i, summary

        label_parts: List[str] = []
        values: List[str] = []
        while i < len(lines):
            line = lines[i]

            if line == "Account total":
                i += 1
                consumed = 0
                while (
                    i < len(lines)
                    and consumed < num_value_columns
                    and _ACCOUNT_SUMMARY_VALUE_RE.match(lines[i])
                ):
                    i += 1
                    consumed += 1
                break

            if line.startswith("Activity from") or _WRAPPER_INVESTMENTS_RE.match(line):
                break

            if _ACCOUNT_SUMMARY_VALUE_RE.match(line):
                values.append(line)
            else:
                label_parts.append(line)

            if len(values) == num_value_columns:
                wrapper_name = " ".join(label_parts).strip()
                try:
                    summary[wrapper_name] = parse_money_minor(values[-1])
                except MoneyParseError:
                    pass
                label_parts = []
                values = []

            i += 1

        return i, summary

    def _check_reconciliation(
        self,
        records: List[Dict[str, Any]],
        account_summary: Dict[str, int],
        account_number: Optional[str],
    ) -> None:
        """Compare each wrapper's 'Your Vanguard account summary' closing
        figure against that wrapper's own holdings total (fund + cash) -
        confirmed an exact anchor against real statement data. A wrapper
        with no (or malformed) holdings section is skipped - "no signal",
        not a false mismatch."""
        for wrapper_name, expected_closing in account_summary.items():
            derived = self._sum_wrapper_holdings_total(records, wrapper_name)
            if derived is None:
                continue

            raw_identifier = (
                f"{account_number}_{wrapper_name}" if account_number else None
            )
            self.last_reconciliations.append(
                ReconciliationResult(
                    check_name=f"vanguard_account_summary_{_slugify(wrapper_name)}",
                    expected_closing_minor=expected_closing,
                    derived_closing_minor=derived,
                    matches=derived == expected_closing,
                    account_identifier=(
                        hash_account_identifier(raw_identifier)
                        if raw_identifier
                        else None
                    ),
                )
            )

    @staticmethod
    def _sum_wrapper_holdings_total(
        records: List[Dict[str, Any]], wrapper_name: str
    ) -> Optional[int]:
        """Sum total_value_minor across every holding row for this wrapper -
        already includes the 'Cash account' row _parse_holdings_block
        emits alongside fund rows, so this sum *is* "fund total + cash
        total" with no separate split needed."""
        total = 0
        found = False
        for record in records:
            if (
                record.get("record_type") != "holding"
                or record.get("wrapper") != wrapper_name
            ):
                continue
            found = True
            try:
                total += int(record["total_value_minor"])
            except (KeyError, TypeError, ValueError):
                return None
        return total if found else None

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
                quantity_text = values[0]
                unit_price_text = values[1]
                total_value_text = values[2]
                unit_price_minor: Optional[int] = None
                total_value_minor: Optional[int] = None
                if total_value_text != "-":
                    try:
                        total_value_minor = parse_money_minor(total_value_text)
                    except MoneyParseError:
                        pass
                if unit_price_text != "-":
                    try:
                        unit_price_minor = parse_money_minor(unit_price_text)
                    except MoneyParseError:
                        pass
                holdings.append(
                    {
                        "record_type": "holding",
                        "wrapper": wrapper,
                        "fund_name": " ".join(description_parts).strip(),
                        "quantity_text": quantity_text,
                        "unit_price_minor": unit_price_minor,
                        "total_value_minor": total_value_minor,
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
        amounts_minor: List[int] = []
        for line in lines[1:]:
            if _ACTIVITY_AMOUNT_RE.match(line):
                try:
                    amounts_minor.append(parse_money_minor(line))
                except MoneyParseError:
                    pass
            else:
                description_parts.append(line)

        if not description_parts or not amounts_minor:
            return None

        account_identifier = f"{account_number}_{wrapper}" if account_number else None
        record: Dict[str, Any] = {
            "record_type": "transaction",
            "date": date_str,
            "description": " ".join(description_parts),
            "amount_minor": amounts_minor[0],  # cash amount
            "_account_identifier_raw": account_identifier,
        }
        if len(amounts_minor) >= 2:
            # Cash balance within this wrapper - distinct from "Portfolio
            # Value" (the CSV ledger's balance metric), so kept as its own
            # field rather than fed into account_ledger.
            record["cash_balance_minor"] = amounts_minor[1]
        return record

    def generate_source_key(
        self,
        txn: Dict[str, Any],
        line_num: int,
        account_identifier: Optional[str] = None,
    ) -> str:
        """Generate deterministic key from account + (date|as_of_date) + description/fund + amount."""
        if txn.get("record_type") == "holding":
            return make_snapshot_source_key(
                "vanguard_holding",
                txn.get("fund_name", ""),
                txn.get("as_of_date", ""),
                account_identifier,
            )

        return make_transaction_source_key(
            "vanguard_txn",
            txn.get("date", ""),
            txn.get("description", ""),
            int(txn.get("amount_minor", 0)),
            account_identifier,
        )

    def detect_source_type(self) -> str:
        """Return source type."""
        return "vanguard-pdf"


def _slugify(name: str) -> str:
    """'Vanguard Personal Pension' -> 'vanguard_personal_pension', for
    building a distinguishable check_name per wrapper."""
    return _SLUGIFY_RE.sub("_", name.lower()).strip("_")
