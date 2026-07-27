"""Exact-money representation: integer minor units + ISO currency.

All financial values in Bronze and Silver must be stored as signed integers
(smallest unit count) rather than binary floating-point. Formatting for
display happens at the CLI/SQL boundary only.

Example (GBP): £12.34 → 1234, -£0.01 → -1.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Optional

# Minor unit precision per ISO 4217 currency code.
MINOR_UNITS: dict[str, int] = {
    "GBP": 2,
    "USD": 2,
    "EUR": 2,
    "JPY": 0,
    # Add more as needed.
}

_DECIMAL_RE = re.compile(r"[-+]?[\d,]+\.\d{2}")
_AMOUNT_RE = re.compile(r"^[-+]?[\d,]+\.\d+$")


class MoneyParseError(ValueError):
    """A monetary field could not be parsed as an exact minor-unit value."""


def parse_money_minor(text: str, currency: str = "GBP") -> int:
    """Parse a formatted currency string into signed minor units.

    Raises MoneyParseError if the text cannot be parsed exactly.
    Trailing markers such as 'CR' are stripped and ignored (direction is
    encoded by the sign, not the marker).

    Examples (GBP):
        "£1,234.56"  → 123456
        "-£47.22"     → -4722
        "0.00"        → 0
        "not money"   → MoneyParseError
    """
    if text is None:
        raise MoneyParseError("cannot parse None as money")
    stripped = text.strip()
    if stripped == "-":
        raise MoneyParseError(f"ambiguous dash in money field: {text!r}")
    stripped = (
        stripped.upper().replace("CR", "").replace("£", "").replace(",", "").strip()
    )
    match = _AMOUNT_RE.search(stripped)
    if not match:
        # Try with the more permissive _DECIMAL_RE as a fallback (handles
        # strings with non-decimal content but still containing a number).
        match = _DECIMAL_RE.search(stripped)
        if not match:
            raise MoneyParseError(f"no numeric value found: {text!r}")
    try:
        decimal = Decimal(match.group(0))
    except InvalidOperation as e:
        raise MoneyParseError(f"invalid decimal: {text!r}") from e
    if currency not in MINOR_UNITS:
        raise MoneyParseError(f"unknown currency for minor-unit conversion: {currency}")
    scale = MINOR_UNITS[currency]
    return int(decimal * (10**scale))


def try_parse_money_minor(text: str, currency: str = "GBP") -> Optional[int]:
    """Convenience: parse or return None (never silently returns zero)."""
    try:
        return parse_money_minor(text, currency)
    except MoneyParseError:
        return None


def minor_to_decimal(minor: int, currency: str = "GBP") -> Decimal:
    """Convert integer minor units back to a Decimal for display or computation."""
    scale = MINOR_UNITS[currency]
    return Decimal(minor).scaleb(-scale)


def format_minor(minor: int, currency: str = "GBP") -> str:
    """Format minor units for display, e.g. 123456 → '£1,234.56'."""
    d = minor_to_decimal(minor, currency)
    prefix = "£" if currency == "GBP" else f"{currency} "
    return f"{prefix}{d:,.{MINOR_UNITS[currency]}f}"
