"""Shared helpers for building a per-file ReconciliationResult.

Every PDF adapter with a balance anchor (Amex, Chase, First Direct, Natwest
Statement, Kroo, Monzo PDF, Monzo Flex) used to hand-construct its own
ReconciliationResult inline - identical boilerplate (null-check both anchors,
compute `matches`, build the dataclass) duplicated seven times. This module
extracts that tail. It deliberately does NOT try to unify the rollforward
loop itself: Amex/First Direct need to write a per-record `balance_minor`
side effect as they go, and Amex additionally folds in a Plan-It adjustment
term - forcing those through one generic loop would obscure more than it
shares. `roll_forward_balance` below is only for adapters (Chase) whose
derived total has no such per-record side effect.
"""

from typing import Iterable, Optional

from adapters.base import ReconciliationResult


def roll_forward_balance(
    opening_minor: int, amounts_minor: Iterable[int], sign: int = 1
) -> int:
    """Return opening_minor + sign * sum(amounts_minor).

    `sign` encodes whether the anchor is a liability that moves opposite to
    the signed, cash-received `amount` convention (-1, e.g. a credit card)
    or an asset that moves the same way (+1, e.g. a current account).
    """
    return opening_minor + sign * sum(amounts_minor)


def build_reconciliation_result(
    check_name: str,
    expected_closing_minor: Optional[int],
    derived_closing_minor: Optional[int],
    expected_opening_minor: Optional[int] = None,
    account_identifier: Optional[str] = None,
) -> Optional[ReconciliationResult]:
    """Build a ReconciliationResult from an already-derived closing balance
    and the statement's own printed closing anchor.

    Returns None if either value is missing - mirrors every adapter's
    existing "leave last_reconciliation alone" behavior when no anchor was
    found in this file. `expected_opening_minor` is carried through
    untouched (and may be None) purely so a cross-file continuity check has
    both ends of the statement's own printed anchor pair later - it plays no
    part in computing `matches` here.
    """
    if expected_closing_minor is None or derived_closing_minor is None:
        return None
    return ReconciliationResult(
        check_name=check_name,
        expected_closing_minor=expected_closing_minor,
        derived_closing_minor=derived_closing_minor,
        matches=derived_closing_minor == expected_closing_minor,
        account_identifier=account_identifier,
        expected_opening_minor=expected_opening_minor,
    )
