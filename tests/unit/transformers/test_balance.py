"""Tests for transformers/balance.py (current balance / net worth queries)."""

import json

import pandas as pd
import pytest

from transformers.balance import (
    MixedCurrencyError,
    get_current_balances,
    get_net_worth,
    get_net_worth_breakdown,
)


class _FakeDatalakeForBalance:
    """Minimal stand-in exposing only what get_current_balances needs."""

    def __init__(self, silver_data=None):
        self._silver_data = silver_data or {}  # dict: entity_type -> DataFrame

    def read_silver(self, entity_type):
        return self._silver_data.get(entity_type)


def _account_map_path(tmp_path, identifiers=None):
    path = tmp_path / "account_map.json"
    path.write_text(
        json.dumps({"identifiers": identifiers or {}, "source_type_fallback": {}})
    )
    return path


def _ledger_row(
    account_id,
    balance_minor,
    as_of_date,
    upload_timestamp,
    statement_period_to=None,
    line_number=1,
    source_type="kroo",
    reconciled=None,
    currency="GBP",
):
    return {
        "account_id": account_id,
        "balance_minor": balance_minor,
        "currency": currency,
        "as_of_date": as_of_date,
        "upload_timestamp": upload_timestamp,
        "statement_period_to": statement_period_to,
        "line_number": line_number,
        "source_type": source_type,
        "reconciled": reconciled,
    }


def _holdings_row(
    account_id, total_value_minor, fund_name="Test Fund", as_of_date=None, currency="GBP"
):
    return {
        "account_id": account_id,
        "fund_name": fund_name,
        "total_value_minor": total_value_minor,
        "currency": currency,
        "as_of_date": as_of_date,
    }


class TestGetCurrentBalances:
    def test_single_account_single_row(self):
        ledger = pd.DataFrame(
            [
                _ledger_row(
                    "acc_kroo",
                    57623,
                    pd.Timestamp("2026-06-03"),
                    pd.Timestamp("2026-06-04"),
                )
            ]
        )
        datalake = _FakeDatalakeForBalance({"account_ledger": ledger})

        result = get_current_balances(datalake)

        assert len(result) == 1
        assert result.iloc[0]["account_id"] == "acc_kroo"
        assert result.iloc[0]["balance_minor"] == 57623

    def test_picks_later_row_across_files_by_statement_period_to(self):
        """The exact same-day-tie scenario that originally produced a wrong
        current balance (see BRONZE_SILVER_HARDENING_PLAN.md): two rows
        share an as_of_date, but come from statements with different
        cycle-end dates - the later cycle's balance must win, regardless
        of which file happened to be uploaded first."""
        ledger = pd.DataFrame(
            [
                _ledger_row(
                    "acc_amex",
                    67804,
                    pd.Timestamp("2026-01-31"),
                    upload_timestamp=pd.Timestamp("2026-02-01"),
                    statement_period_to=pd.Timestamp("2026-01-31"),
                    line_number=5,
                ),
                _ledger_row(
                    "acc_amex",
                    86304,
                    pd.Timestamp("2026-01-31"),
                    upload_timestamp=pd.Timestamp("2026-01-20"),  # uploaded earlier
                    statement_period_to=pd.Timestamp("2026-02-19"),  # later cycle
                    line_number=1,
                ),
            ]
        )
        datalake = _FakeDatalakeForBalance({"account_ledger": ledger})

        result = get_current_balances(datalake)

        assert len(result) == 1
        assert result.iloc[0]["balance_minor"] == 86304

    def test_monzo_pdf_same_day_tie_picks_newest_not_last_parsed(self):
        """Monzo PDF prints transactions newest-first within a file (verified
        against a real statement - see transformers/balance.py's module
        docstring) - the opposite of every other ledger source. An earlier
        version of this function assumed ascending line_number was always
        ascending time and picked line_number=5 (the *oldest* of these 5,
        balance 95.59) instead of line_number=1 (the newest, balance
        2255.37). Values here are the real ones that surfaced the bug."""
        same_day = pd.Timestamp("2026-06-30")
        period_to = pd.Timestamp("2026-06-30")
        ledger = pd.DataFrame(
            [
                _ledger_row(
                    "acc_monzo",
                    225537,
                    same_day,
                    same_day,
                    statement_period_to=period_to,
                    line_number=1,
                    source_type="monzo-pdf",
                ),
                _ledger_row(
                    "acc_monzo",
                    235537,
                    same_day,
                    same_day,
                    statement_period_to=period_to,
                    line_number=2,
                    source_type="monzo-pdf",
                ),
                _ledger_row(
                    "acc_monzo",
                    537,
                    same_day,
                    same_day,
                    statement_period_to=period_to,
                    line_number=3,
                    source_type="monzo-pdf",
                ),
                _ledger_row(
                    "acc_monzo",
                    4980,
                    same_day,
                    same_day,
                    statement_period_to=period_to,
                    line_number=4,
                    source_type="monzo-pdf",
                ),
                _ledger_row(
                    "acc_monzo",
                    9559,
                    same_day,
                    same_day,
                    statement_period_to=period_to,
                    line_number=5,
                    source_type="monzo-pdf",
                ),
            ]
        )
        datalake = _FakeDatalakeForBalance({"account_ledger": ledger})

        result = get_current_balances(datalake)

        assert len(result) == 1
        assert result.iloc[0]["balance_minor"] == 225537

    def test_monzo_flex_same_day_tie_picks_newest_not_last_parsed(self):
        """Monzo Flex prints transactions newest-first within a file too
        (same layout convention as Monzo PDF, see
        transformers/balance.py's module docstring) - verified against a
        real statement, where two same-day rows (Selecta U.K, line_number=1,
        balance 1101.37; Greggs, line_number=2, balance 1099.07) resolved to
        the wrong (older) balance before "monzo-flex" was added to
        _REVERSE_CHRONOLOGICAL_SOURCE_TYPES."""
        same_day = pd.Timestamp("2026-06-30")
        period_to = pd.Timestamp("2026-06-30")
        ledger = pd.DataFrame(
            [
                _ledger_row(
                    "acc_monzo_flex",
                    110137,
                    same_day,
                    same_day,
                    statement_period_to=period_to,
                    line_number=1,
                    source_type="monzo-flex",
                ),
                _ledger_row(
                    "acc_monzo_flex",
                    109907,
                    same_day,
                    same_day,
                    statement_period_to=period_to,
                    line_number=2,
                    source_type="monzo-flex",
                ),
            ]
        )
        datalake = _FakeDatalakeForBalance({"account_ledger": ledger})

        result = get_current_balances(datalake)

        assert len(result) == 1
        assert result.iloc[0]["balance_minor"] == 110137

    def test_falls_back_to_upload_timestamp_when_no_period(self):
        """Periodless sources (e.g. CSV) have no statement_period_to at all -
        upload_timestamp is the only available tiebreaker."""
        ledger = pd.DataFrame(
            [
                _ledger_row(
                    "acc_natwest",
                    10000,
                    pd.Timestamp("2026-03-01"),
                    upload_timestamp=pd.Timestamp("2026-03-02"),
                    line_number=1,
                ),
                _ledger_row(
                    "acc_natwest",
                    15000,
                    pd.Timestamp("2026-03-01"),
                    upload_timestamp=pd.Timestamp("2026-03-05"),
                    line_number=1,
                ),
            ]
        )
        datalake = _FakeDatalakeForBalance({"account_ledger": ledger})

        result = get_current_balances(datalake)

        assert len(result) == 1
        assert result.iloc[0]["balance_minor"] == 15000

    def test_as_of_date_bumped_up_to_statement_period_end(self):
        """An account whose only transaction fell early in its statement
        (e.g. Chase: a single transaction on day 1 of a 28-day statement)
        still has its balance confirmed accurate through the statement's
        own printed closing date - as_of_date should reflect that, not
        just the last transaction's own (earlier) date."""
        ledger = pd.DataFrame(
            [
                _ledger_row(
                    "acc_chase",
                    275000,
                    pd.Timestamp("2026-06-02"),
                    upload_timestamp=pd.Timestamp("2026-07-25"),
                    statement_period_to=pd.Timestamp("2026-06-30"),
                    source_type="chase",
                )
            ]
        )
        datalake = _FakeDatalakeForBalance({"account_ledger": ledger})

        result = get_current_balances(datalake)

        assert len(result) == 1
        assert result.iloc[0]["balance_minor"] == 275000
        assert result.iloc[0]["as_of_date"] == pd.Timestamp("2026-06-30")

    def test_multiple_accounts_each_get_their_own_latest_row(self):
        ledger = pd.DataFrame(
            [
                _ledger_row(
                    "acc_kroo",
                    10000,
                    pd.Timestamp("2026-06-01"),
                    pd.Timestamp("2026-06-01"),
                ),
                _ledger_row(
                    "acc_kroo",
                    20000,
                    pd.Timestamp("2026-06-02"),
                    pd.Timestamp("2026-06-02"),
                ),
                _ledger_row(
                    "acc_amex",
                    5000,
                    pd.Timestamp("2026-06-01"),
                    pd.Timestamp("2026-06-01"),
                ),
            ]
        )
        datalake = _FakeDatalakeForBalance({"account_ledger": ledger})

        result = get_current_balances(datalake)

        assert len(result) == 2
        kroo_balance = result[result["account_id"] == "acc_kroo"].iloc[0]["balance_minor"]
        amex_balance = result[result["account_id"] == "acc_amex"].iloc[0]["balance_minor"]
        assert kroo_balance == 20000
        assert amex_balance == 5000

    def test_empty_ledger_returns_empty(self):
        datalake = _FakeDatalakeForBalance({"account_ledger": pd.DataFrame()})
        result = get_current_balances(datalake)
        assert result.empty

    def test_missing_ledger_returns_empty(self):
        datalake = _FakeDatalakeForBalance({})
        result = get_current_balances(datalake)
        assert result.empty

    def test_mismatched_row_excluded_falls_back_to_last_known_good(self):
        """See Gotcha #17: a file that fails reconciliation must not win
        "current balance" selection - the last reconciling row should be
        used instead ("stale but correct, rather than current but wrong")."""
        ledger = pd.DataFrame(
            [
                _ledger_row(
                    "acc_firstdirect",
                    152677,
                    pd.Timestamp("2026-05-05"),
                    pd.Timestamp("2026-05-05"),
                    statement_period_to=pd.Timestamp("2026-05-05"),
                    reconciled=True,
                ),
                _ledger_row(
                    "acc_firstdirect",
                    193654,
                    pd.Timestamp("2026-07-05"),
                    pd.Timestamp("2026-07-05"),
                    statement_period_to=pd.Timestamp("2026-07-05"),
                    reconciled=False,
                ),
            ]
        )
        datalake = _FakeDatalakeForBalance({"account_ledger": ledger})

        result = get_current_balances(datalake)

        assert len(result) == 1
        assert result.iloc[0]["balance_minor"] == 152677
        assert result.iloc[0]["as_of_date"] == pd.Timestamp("2026-05-05")
        assert result.iloc[0]["balance_may_be_stale"] == True  # noqa: E712

    def test_all_rows_mismatched_account_excluded_entirely(self, caplog):
        """If literally every statement for an account fails reconciliation,
        there is no known-good balance to fall back to - the account is
        absent from the result (not zero/NaN), logged rather than raised."""
        ledger = pd.DataFrame(
            [
                _ledger_row(
                    "acc_amex",
                    67804,
                    pd.Timestamp("2026-01-31"),
                    pd.Timestamp("2026-02-01"),
                    reconciled=False,
                ),
                _ledger_row(
                    "acc_kroo",
                    49550,
                    pd.Timestamp("2026-01-12"),
                    pd.Timestamp("2026-01-13"),
                    reconciled=True,
                ),
            ]
        )
        datalake = _FakeDatalakeForBalance({"account_ledger": ledger})

        import logging

        with caplog.at_level(logging.WARNING):
            result = get_current_balances(datalake)

        assert list(result["account_id"]) == ["acc_kroo"]
        assert "acc_amex" in caplog.text

    def test_reconciled_none_treated_as_included(self):
        """None (no anchor found/inconclusive, or predates the reconciled
        column) must not be excluded - only an explicit False is a known
        mismatch."""
        ledger = pd.DataFrame(
            [
                _ledger_row(
                    "acc_kroo",
                    49550,
                    pd.Timestamp("2026-01-12"),
                    pd.Timestamp("2026-01-13"),
                    reconciled=None,
                ),
            ]
        )
        datalake = _FakeDatalakeForBalance({"account_ledger": ledger})

        result = get_current_balances(datalake)

        assert len(result) == 1
        assert result.iloc[0]["balance_minor"] == 49550
        assert result.iloc[0]["balance_may_be_stale"] == False  # noqa: E712

    def test_balance_may_be_stale_true_when_newer_mismatch_exists(self):
        ledger = pd.DataFrame(
            [
                _ledger_row(
                    "acc_firstdirect",
                    152677,
                    pd.Timestamp("2026-05-05"),
                    pd.Timestamp("2026-05-05"),
                    reconciled=True,
                ),
                _ledger_row(
                    "acc_firstdirect",
                    193654,
                    pd.Timestamp("2026-07-05"),
                    pd.Timestamp("2026-07-05"),
                    reconciled=False,
                ),
            ]
        )
        datalake = _FakeDatalakeForBalance({"account_ledger": ledger})

        result = get_current_balances(datalake)

        assert result.iloc[0]["balance_may_be_stale"] == True  # noqa: E712

    def test_balance_may_be_stale_false_when_no_newer_mismatch(self):
        ledger = pd.DataFrame(
            [
                _ledger_row(
                    "acc_kroo",
                    49550,
                    pd.Timestamp("2026-01-12"),
                    pd.Timestamp("2026-01-13"),
                    reconciled=True,
                ),
            ]
        )
        datalake = _FakeDatalakeForBalance({"account_ledger": ledger})

        result = get_current_balances(datalake)

        assert result.iloc[0]["balance_may_be_stale"] == False  # noqa: E712


class TestGetNetWorth:
    def test_asset_accounts_are_added(self, tmp_path):
        path = _account_map_path(
            tmp_path,
            {
                "hash_kroo": {
                    "account_id": "acc_kroo",
                    "display_name": "Kroo",
                    "account_type": "current",
                },
                "hash_vanguard": {
                    "account_id": "acc_vanguard",
                    "display_name": "Vanguard",
                    "account_type": "investment",
                },
            },
        )
        ledger = pd.DataFrame(
            [
                _ledger_row(
                    "acc_kroo",
                    10000,
                    pd.Timestamp("2026-06-01"),
                    pd.Timestamp("2026-06-01"),
                ),
                _ledger_row(
                    "acc_vanguard",
                    50000,
                    pd.Timestamp("2026-06-01"),
                    pd.Timestamp("2026-06-01"),
                ),
            ]
        )
        datalake = _FakeDatalakeForBalance({"account_ledger": ledger})

        net_worth = get_net_worth(datalake, path=path)

        assert net_worth == 60000

    def test_credit_accounts_are_subtracted(self, tmp_path):
        """A credit account's balance is stored as a positive "amount owed"
        figure (see CLAUDE.md Gotcha #6) - it must reduce net worth, not
        add to it."""
        path = _account_map_path(
            tmp_path,
            {
                "hash_kroo": {
                    "account_id": "acc_kroo",
                    "display_name": "Kroo",
                    "account_type": "current",
                },
                "hash_amex": {
                    "account_id": "acc_amex",
                    "display_name": "Amex",
                    "account_type": "credit",
                },
            },
        )
        ledger = pd.DataFrame(
            [
                _ledger_row(
                    "acc_kroo",
                    100000,
                    pd.Timestamp("2026-06-01"),
                    pd.Timestamp("2026-06-01"),
                ),
                _ledger_row(
                    "acc_amex",
                    30000,
                    pd.Timestamp("2026-06-01"),
                    pd.Timestamp("2026-06-01"),
                ),
            ]
        )
        datalake = _FakeDatalakeForBalance({"account_ledger": ledger})

        net_worth = get_net_worth(datalake, path=path)

        assert net_worth == 70000

    def test_empty_ledger_returns_zero(self, tmp_path):
        path = _account_map_path(tmp_path)
        datalake = _FakeDatalakeForBalance({})

        assert get_net_worth(datalake, path=path) == 0

    def test_holdings_are_included_in_net_worth(self, tmp_path):
        """Holdings from the holdings table (investment accounts) are always
        assets and must be added to net worth alongside account ledger
        balances."""
        path = _account_map_path(
            tmp_path,
            {
                "hash_kroo": {
                    "account_id": "acc_kroo",
                    "display_name": "Kroo",
                    "account_type": "current",
                },
                "hash_vanguard_isa": {
                    "account_id": "acc_vanguard_isa",
                    "display_name": "Vanguard ISA",
                    "account_type": "investment",
                },
            },
        )
        ledger = pd.DataFrame(
            [
                _ledger_row(
                    "acc_kroo",
                    100000,
                    pd.Timestamp("2026-06-01"),
                    pd.Timestamp("2026-06-01"),
                ),
            ]
        )
        holdings = pd.DataFrame(
            [
                _holdings_row("acc_vanguard_isa", 150000, "UK Index Fund"),
                _holdings_row("acc_vanguard_isa", 50000, "Global Fund"),
            ]
        )
        datalake = _FakeDatalakeForBalance(
            {"account_ledger": ledger, "holdings": holdings}
        )

        net_worth = get_net_worth(datalake, path=path)

        assert net_worth == 300000

    def test_holdings_alone_included_in_net_worth(self, tmp_path):
        """If there are only holdings and no ledger balances, net worth is
        just the sum of holdings."""
        path = _account_map_path(
            tmp_path,
            {
                "hash_vanguard_pension": {
                    "account_id": "acc_vanguard_pension",
                    "display_name": "Vanguard Pension",
                    "account_type": "investment",
                },
            },
        )
        holdings = pd.DataFrame(
            [
                _holdings_row("acc_vanguard_pension", 300000, "Pension Fund"),
            ]
        )
        datalake = _FakeDatalakeForBalance(
            {"account_ledger": pd.DataFrame(), "holdings": holdings}
        )

        net_worth = get_net_worth(datalake, path=path)

        assert net_worth == 300000

    def test_holdings_grouped_by_account_id(self, tmp_path):
        """Multiple holdings for the same account_id are grouped and summed
        together before adding to net worth."""
        path = _account_map_path(
            tmp_path,
            {
                "hash_vanguard_isa": {
                    "account_id": "acc_vanguard_isa",
                    "display_name": "Vanguard ISA",
                    "account_type": "investment",
                },
            },
        )
        holdings = pd.DataFrame(
            [
                _holdings_row("acc_vanguard_isa", 100000, "Fund A"),
                _holdings_row("acc_vanguard_isa", 200000, "Fund B"),
                _holdings_row("acc_vanguard_isa", 50000, "Fund C"),
            ]
        )
        datalake = _FakeDatalakeForBalance(
            {"account_ledger": pd.DataFrame(), "holdings": holdings}
        )

        net_worth = get_net_worth(datalake, path=path)

        assert net_worth == 350000


class TestMixedCurrencyGuard:
    def test_mixed_currency_ledger_raises(self, tmp_path):
        path = _account_map_path(
            tmp_path,
            {
                "hash_kroo": {
                    "account_id": "acc_kroo",
                    "display_name": "Kroo",
                    "account_type": "current",
                },
                "hash_usd": {
                    "account_id": "acc_usd",
                    "display_name": "USD Account",
                    "account_type": "current",
                },
            },
        )
        ledger = pd.DataFrame(
            [
                _ledger_row(
                    "acc_kroo",
                    10000,
                    pd.Timestamp("2026-06-01"),
                    pd.Timestamp("2026-06-01"),
                    currency="GBP",
                ),
                _ledger_row(
                    "acc_usd",
                    5000,
                    pd.Timestamp("2026-06-01"),
                    pd.Timestamp("2026-06-01"),
                    currency="USD",
                ),
            ]
        )
        datalake = _FakeDatalakeForBalance({"account_ledger": ledger})

        with pytest.raises(MixedCurrencyError):
            get_net_worth(datalake, path=path)

    def test_mixed_ledger_and_holdings_currency_raises(self, tmp_path):
        path = _account_map_path(
            tmp_path,
            {
                "hash_kroo": {
                    "account_id": "acc_kroo",
                    "display_name": "Kroo",
                    "account_type": "current",
                },
                "hash_vanguard": {
                    "account_id": "acc_vanguard",
                    "display_name": "Vanguard",
                    "account_type": "investment",
                },
            },
        )
        ledger = pd.DataFrame(
            [
                _ledger_row(
                    "acc_kroo",
                    10000,
                    pd.Timestamp("2026-06-01"),
                    pd.Timestamp("2026-06-01"),
                    currency="GBP",
                ),
            ]
        )
        holdings = pd.DataFrame(
            [_holdings_row("acc_vanguard", 100000, "Fund A", currency="USD")]
        )
        datalake = _FakeDatalakeForBalance({"account_ledger": ledger, "holdings": holdings})

        with pytest.raises(MixedCurrencyError):
            get_net_worth(datalake, path=path)

        with pytest.raises(MixedCurrencyError):
            get_net_worth_breakdown(datalake, path=path)

    def test_single_currency_all_gbp_unchanged(self, tmp_path):
        path = _account_map_path(
            tmp_path,
            {
                "hash_kroo": {
                    "account_id": "acc_kroo",
                    "display_name": "Kroo",
                    "account_type": "current",
                },
                "hash_vanguard": {
                    "account_id": "acc_vanguard",
                    "display_name": "Vanguard",
                    "account_type": "investment",
                },
            },
        )
        ledger = pd.DataFrame(
            [
                _ledger_row(
                    "acc_kroo",
                    10000,
                    pd.Timestamp("2026-06-01"),
                    pd.Timestamp("2026-06-01"),
                ),
                _ledger_row(
                    "acc_vanguard",
                    50000,
                    pd.Timestamp("2026-06-01"),
                    pd.Timestamp("2026-06-01"),
                ),
            ]
        )
        datalake = _FakeDatalakeForBalance({"account_ledger": ledger})

        assert get_net_worth(datalake, path=path) == 60000


class TestGetNetWorthBreakdown:
    def test_breakdown_includes_ledger_and_holdings(self, tmp_path):
        """Breakdown shows both ledger balances and holdings with their
        contributions to net worth."""
        path = _account_map_path(
            tmp_path,
            {
                "hash_kroo": {
                    "account_id": "acc_kroo",
                    "display_name": "Kroo",
                    "account_type": "current",
                },
                "hash_amex": {
                    "account_id": "acc_amex",
                    "display_name": "Amex",
                    "account_type": "credit",
                },
                "hash_vanguard": {
                    "account_id": "acc_vanguard",
                    "display_name": "Vanguard",
                    "account_type": "investment",
                },
            },
        )
        ledger = pd.DataFrame(
            [
                _ledger_row(
                    "acc_kroo",
                    100000,
                    pd.Timestamp("2026-06-01"),
                    pd.Timestamp("2026-06-01"),
                ),
                _ledger_row(
                    "acc_amex",
                    30000,
                    pd.Timestamp("2026-06-01"),
                    pd.Timestamp("2026-06-01"),
                ),
                _ledger_row(
                    "acc_vanguard",
                    20000,
                    pd.Timestamp("2026-06-01"),
                    pd.Timestamp("2026-06-01"),
                ),
            ]
        )
        holdings = pd.DataFrame(
            [
                _holdings_row("acc_vanguard", 150000, "Fund A"),
                _holdings_row("acc_vanguard", 50000, "Fund B"),
            ]
        )
        datalake = _FakeDatalakeForBalance(
            {"account_ledger": ledger, "holdings": holdings}
        )

        breakdown = get_net_worth_breakdown(datalake, path=path)

        assert len(breakdown) == 5
        assert "contribution_to_net_worth" in breakdown.columns
        # Kroo 100000 + Vanguard 20000 - Amex 30000 + Fund A 150000 + Fund B 50000 = 290000
        assert breakdown["contribution_to_net_worth"].sum() == 290000

        kroo_row = breakdown[breakdown["account_id"] == "acc_kroo"].iloc[0]
        assert kroo_row["contribution_to_net_worth"] == 100000

        amex_row = breakdown[breakdown["account_id"] == "acc_amex"].iloc[0]
        assert amex_row["contribution_to_net_worth"] == -30000

        fund_rows = breakdown[
            (breakdown["account_id"] == "acc_vanguard")
            & (breakdown["source"].isin(["Fund A", "Fund B"]))
        ]
        assert len(fund_rows) == 2
        assert fund_rows["contribution_to_net_worth"].sum() == 200000

    def test_breakdown_credit_accounts_negative_contribution(self, tmp_path):
        """Credit account balances show negative contribution to net worth."""
        path = _account_map_path(
            tmp_path,
            {
                "hash_amex": {
                    "account_id": "acc_amex",
                    "display_name": "Amex",
                    "account_type": "credit",
                },
            },
        )
        ledger = pd.DataFrame(
            [
                _ledger_row(
                    "acc_amex",
                    50000,
                    pd.Timestamp("2026-06-01"),
                    pd.Timestamp("2026-06-01"),
                ),
            ]
        )
        datalake = _FakeDatalakeForBalance({"account_ledger": ledger})

        breakdown = get_net_worth_breakdown(datalake, path=path)

        assert len(breakdown) == 1
        assert breakdown.iloc[0]["balance_or_value"] == 50000
        assert breakdown.iloc[0]["contribution_to_net_worth"] == -50000

    def test_breakdown_sorted_by_as_of_date_then_contribution(self, tmp_path):
        """Breakdown is sorted by as_of_date (descending) then contribution
        (descending)."""
        path = _account_map_path(
            tmp_path,
            {
                "hash_kroo": {
                    "account_id": "acc_kroo",
                    "display_name": "Kroo",
                    "account_type": "current",
                },
            },
        )
        ledger = pd.DataFrame(
            [
                _ledger_row(
                    "acc_kroo",
                    10000,
                    pd.Timestamp("2026-06-01"),
                    pd.Timestamp("2026-06-01"),
                ),
                _ledger_row(
                    "acc_kroo",
                    20000,
                    pd.Timestamp("2026-06-02"),
                    pd.Timestamp("2026-06-02"),
                ),
            ]
        )
        datalake = _FakeDatalakeForBalance({"account_ledger": ledger})

        breakdown = get_net_worth_breakdown(datalake, path=path)

        assert breakdown.iloc[0]["as_of_date"] == pd.Timestamp("2026-06-02")
        assert breakdown.iloc[0]["balance_or_value"] == 20000

    def test_holdings_as_of_date_is_populated_not_blank(self, tmp_path):
        """A holding's as_of_date (populated by normalize_holdings() in
        Silver) must be surfaced in the breakdown, not hardcoded to None -
        otherwise every holdings row sinks to the bottom of the
        as_of_date-descending sort regardless of how current it is."""
        path = _account_map_path(
            tmp_path,
            {
                "hash_kroo": {
                    "account_id": "acc_kroo",
                    "display_name": "Kroo",
                    "account_type": "current",
                },
                "hash_vanguard_isa": {
                    "account_id": "acc_vanguard_isa",
                    "display_name": "Vanguard ISA",
                    "account_type": "investment",
                },
            },
        )
        ledger = pd.DataFrame(
            [
                _ledger_row(
                    "acc_kroo",
                    10000,
                    pd.Timestamp("2026-01-01"),
                    pd.Timestamp("2026-01-01"),
                ),
            ]
        )
        holdings = pd.DataFrame(
            [
                _holdings_row(
                    "acc_vanguard_isa",
                    150000,
                    "Fund A",
                    as_of_date=pd.Timestamp("2026-07-08"),
                ),
            ]
        )
        datalake = _FakeDatalakeForBalance(
            {"account_ledger": ledger, "holdings": holdings}
        )

        breakdown = get_net_worth_breakdown(datalake, path=path)

        holding_row = breakdown[breakdown["source"] == "Fund A"].iloc[0]
        assert holding_row["as_of_date"] == pd.Timestamp("2026-07-08")
        # The more recent holding sorts ahead of the older ledger balance,
        # instead of always sinking to the bottom via a hardcoded None.
        assert breakdown.iloc[0]["source"] == "Fund A"

    def test_breakdown_empty_when_no_data(self, tmp_path):
        """Breakdown returns empty DataFrame with correct columns when
        there's no ledger or holdings data."""
        path = _account_map_path(tmp_path)
        datalake = _FakeDatalakeForBalance({})

        breakdown = get_net_worth_breakdown(datalake, path=path)

        assert breakdown.empty
        assert list(breakdown.columns) == [
            "account_id",
            "source",
            "balance_or_value",
            "as_of_date",
            "contribution_to_net_worth",
            "balance_may_be_stale",
        ]
