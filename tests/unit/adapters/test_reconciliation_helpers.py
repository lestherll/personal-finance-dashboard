"""Tests for adapters/reconciliation.py - the shared per-adapter helper
extracted from Amex/Chase/First Direct/Natwest Statement/Kroo/Monzo PDF/
Monzo Flex's previously hand-duplicated ReconciliationResult construction."""

from adapters.reconciliation import build_reconciliation_result, roll_forward_balance


class TestRollForwardBalance:
    def test_positive_sign_asset_account(self):
        """Chase-style: balance moves the same direction as signed amount."""
        assert roll_forward_balance(10000, [2000, -500], sign=1) == 11500

    def test_negative_sign_liability_account(self):
        """Amex/First Direct/Chase-opposite style: balance moves opposite
        to signed, cash-received amount (spend increases what's owed)."""
        assert roll_forward_balance(10000, [2000, -500], sign=-1) == 8500

    def test_empty_amounts_returns_opening_unchanged(self):
        assert roll_forward_balance(5000, [], sign=1) == 5000

    def test_generator_input_consumed_once(self):
        amounts = (a for a in [100, 200, 300])
        assert roll_forward_balance(0, amounts, sign=1) == 600


class TestBuildReconciliationResult:
    def test_match(self):
        result = build_reconciliation_result(
            check_name="test_check",
            expected_closing_minor=1000,
            derived_closing_minor=1000,
        )
        assert result is not None
        assert result.matches is True
        assert result.expected_closing_minor == 1000
        assert result.derived_closing_minor == 1000
        assert result.expected_opening_minor is None

    def test_mismatch(self):
        result = build_reconciliation_result(
            check_name="test_check",
            expected_closing_minor=1000,
            derived_closing_minor=999,
        )
        assert result is not None
        assert result.matches is False

    def test_missing_expected_closing_returns_none(self):
        assert (
            build_reconciliation_result(
                check_name="test_check",
                expected_closing_minor=None,
                derived_closing_minor=1000,
            )
            is None
        )

    def test_missing_derived_closing_returns_none(self):
        assert (
            build_reconciliation_result(
                check_name="test_check",
                expected_closing_minor=1000,
                derived_closing_minor=None,
            )
            is None
        )

    def test_opening_anchor_carried_through_without_affecting_matches(self):
        result = build_reconciliation_result(
            check_name="test_check",
            expected_closing_minor=1000,
            derived_closing_minor=999,
            expected_opening_minor=500,
        )
        assert result is not None
        assert result.expected_opening_minor == 500
        assert result.matches is False  # opening anchor plays no part here

    def test_account_identifier_carried_through(self):
        result = build_reconciliation_result(
            check_name="test_check",
            expected_closing_minor=1000,
            derived_closing_minor=1000,
            account_identifier="hash_abc",
        )
        assert result is not None
        assert result.account_identifier == "hash_abc"

    def test_minor_unit_boundary_exactness(self):
        """A one-minor-unit (one penny) difference must not be swallowed by
        float rounding - these are plain ints, so this is really a
        regression guard against a future accidental float conversion."""
        result = build_reconciliation_result(
            check_name="test_check",
            expected_closing_minor=100_000_001,
            derived_closing_minor=100_000_000,
        )
        assert result is not None
        assert result.matches is False
