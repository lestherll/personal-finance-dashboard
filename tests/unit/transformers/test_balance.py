"""Tests for transformers/balance.py (current balance / net worth queries)."""

import json
from decimal import Decimal

import pandas as pd

from transformers.balance import get_current_balances, get_net_worth


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
    balance,
    as_of_date,
    upload_timestamp,
    statement_period_to=None,
    line_number=1,
):
    return {
        "account_id": account_id,
        "balance": balance,
        "as_of_date": as_of_date,
        "upload_timestamp": upload_timestamp,
        "statement_period_to": statement_period_to,
        "line_number": line_number,
    }


class TestGetCurrentBalances:
    def test_single_account_single_row(self):
        ledger = pd.DataFrame(
            [
                _ledger_row(
                    "acc_kroo",
                    576.23,
                    pd.Timestamp("2026-06-03"),
                    pd.Timestamp("2026-06-04"),
                )
            ]
        )
        datalake = _FakeDatalakeForBalance({"account_ledger": ledger})

        result = get_current_balances(datalake)

        assert len(result) == 1
        assert result.iloc[0]["account_id"] == "acc_kroo"
        assert result.iloc[0]["balance"] == 576.23

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
                    678.04,
                    pd.Timestamp("2026-01-31"),
                    upload_timestamp=pd.Timestamp("2026-02-01"),
                    statement_period_to=pd.Timestamp("2026-01-31"),
                    line_number=5,
                ),
                _ledger_row(
                    "acc_amex",
                    863.04,
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
        assert result.iloc[0]["balance"] == 863.04

    def test_falls_back_to_upload_timestamp_when_no_period(self):
        """Periodless sources (e.g. CSV) have no statement_period_to at all -
        upload_timestamp is the only available tiebreaker."""
        ledger = pd.DataFrame(
            [
                _ledger_row(
                    "acc_natwest",
                    100.00,
                    pd.Timestamp("2026-03-01"),
                    upload_timestamp=pd.Timestamp("2026-03-02"),
                    line_number=1,
                ),
                _ledger_row(
                    "acc_natwest",
                    150.00,
                    pd.Timestamp("2026-03-01"),
                    upload_timestamp=pd.Timestamp("2026-03-05"),
                    line_number=1,
                ),
            ]
        )
        datalake = _FakeDatalakeForBalance({"account_ledger": ledger})

        result = get_current_balances(datalake)

        assert len(result) == 1
        assert result.iloc[0]["balance"] == 150.00

    def test_multiple_accounts_each_get_their_own_latest_row(self):
        ledger = pd.DataFrame(
            [
                _ledger_row(
                    "acc_kroo",
                    100.0,
                    pd.Timestamp("2026-06-01"),
                    pd.Timestamp("2026-06-01"),
                ),
                _ledger_row(
                    "acc_kroo",
                    200.0,
                    pd.Timestamp("2026-06-02"),
                    pd.Timestamp("2026-06-02"),
                ),
                _ledger_row(
                    "acc_amex",
                    50.0,
                    pd.Timestamp("2026-06-01"),
                    pd.Timestamp("2026-06-01"),
                ),
            ]
        )
        datalake = _FakeDatalakeForBalance({"account_ledger": ledger})

        result = get_current_balances(datalake)

        assert len(result) == 2
        kroo_balance = result[result["account_id"] == "acc_kroo"].iloc[0]["balance"]
        amex_balance = result[result["account_id"] == "acc_amex"].iloc[0]["balance"]
        assert kroo_balance == 200.0
        assert amex_balance == 50.0

    def test_empty_ledger_returns_empty(self):
        datalake = _FakeDatalakeForBalance({"account_ledger": pd.DataFrame()})
        result = get_current_balances(datalake)
        assert result.empty

    def test_missing_ledger_returns_empty(self):
        datalake = _FakeDatalakeForBalance({})
        result = get_current_balances(datalake)
        assert result.empty


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
                    100.0,
                    pd.Timestamp("2026-06-01"),
                    pd.Timestamp("2026-06-01"),
                ),
                _ledger_row(
                    "acc_vanguard",
                    500.0,
                    pd.Timestamp("2026-06-01"),
                    pd.Timestamp("2026-06-01"),
                ),
            ]
        )
        datalake = _FakeDatalakeForBalance({"account_ledger": ledger})

        net_worth = get_net_worth(datalake, path=path)

        assert net_worth == Decimal("600.0")

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
                    1000.0,
                    pd.Timestamp("2026-06-01"),
                    pd.Timestamp("2026-06-01"),
                ),
                _ledger_row(
                    "acc_amex",
                    300.0,
                    pd.Timestamp("2026-06-01"),
                    pd.Timestamp("2026-06-01"),
                ),
            ]
        )
        datalake = _FakeDatalakeForBalance({"account_ledger": ledger})

        net_worth = get_net_worth(datalake, path=path)

        assert net_worth == Decimal("700.0")

    def test_empty_ledger_returns_zero(self, tmp_path):
        path = _account_map_path(tmp_path)
        datalake = _FakeDatalakeForBalance({})

        assert get_net_worth(datalake, path=path) == Decimal("0")
