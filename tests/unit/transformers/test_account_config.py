"""Tests for the account identifier -> canonical account resolution.

The actual mapping is user data (a JSON file in the data store), not code -
these tests exercise the resolution logic against isolated temp files, not
the real data/account_map.json.
"""

import json

import pandas as pd
import pytest

from transformers.account_config import (
    UnmappedAccountsError,
    build_accounts_table,
    find_unmapped_accounts,
    get_account_id,
    register_account,
    register_source_type_fallback,
)


@pytest.fixture
def account_map_file(tmp_path):
    """A small, isolated account map file - independent of the real data store."""
    path = tmp_path / "account_map.json"
    path.write_text(
        json.dumps(
            {
                "identifiers": {
                    "hash_kroo": {
                        "account_id": "acc_kroo_current",
                        "display_name": "Kroo Current Account",
                        "account_type": "current",
                    },
                    "hash_amex_1": {
                        "account_id": "acc_amex_1",
                        "display_name": "Amex Card 1",
                        "account_type": "credit",
                    },
                    "hash_amex_2": {
                        "account_id": "acc_amex_2",
                        "display_name": "Amex Card 2",
                        "account_type": "credit",
                    },
                },
                "source_type_fallback": {
                    "monzo": {
                        "account_id": "acc_monzo_current",
                        "display_name": "Monzo Current Account",
                        "account_type": "current",
                    },
                },
            }
        )
    )
    return path


class TestGetAccountId:
    def test_known_identifier_resolves(self, account_map_file):
        assert (
            get_account_id("hash_kroo", "kroo", path=account_map_file)
            == "acc_kroo_current"
        )

    def test_two_identifiers_of_same_source_type_resolve_differently(
        self, account_map_file
    ):
        """The whole point: two Amex cards must not collapse into one account."""
        assert get_account_id("hash_amex_1", "amex", path=account_map_file) != (
            get_account_id("hash_amex_2", "amex", path=account_map_file)
        )

    def test_none_identifier_falls_back_to_source_type(self, account_map_file):
        assert (
            get_account_id(None, "monzo", path=account_map_file) == "acc_monzo_current"
        )

    def test_unrecognized_identifier_falls_back_to_source_type(self, account_map_file):
        """An identifier not in the map (extraction succeeded but unmapped) falls back too."""
        assert (
            get_account_id("some_unmapped_hash", "monzo", path=account_map_file)
            == "acc_monzo_current"
        )

    def test_no_identifier_and_no_fallback_raises(self, account_map_file):
        with pytest.raises(KeyError):
            get_account_id(None, "some_new_bank", path=account_map_file)

    def test_missing_file_treated_as_unconfigured(self, tmp_path):
        """A data store with no account_map.json yet - not a crash, just unmapped."""
        missing_path = tmp_path / "does_not_exist.json"
        with pytest.raises(KeyError):
            get_account_id(None, "monzo", path=missing_path)


class TestBuildAccountsTable:
    def test_one_row_per_canonical_account(self, account_map_file):
        df = build_accounts_table(path=account_map_file)
        assert len(df) == df["account_id"].nunique()
        assert len(df) == 4  # kroo, amex_1, amex_2, monzo

    def test_identifiers_grouped_under_their_account(self, account_map_file):
        df = build_accounts_table(path=account_map_file)
        kroo_row = df[df["account_id"] == "acc_kroo_current"].iloc[0]
        assert kroo_row["account_identifiers"] == ["hash_kroo"]

    def test_fallback_entries_list_source_types(self, account_map_file):
        df = build_accounts_table(path=account_map_file)
        monzo_row = df[df["account_id"] == "acc_monzo_current"].iloc[0]
        assert monzo_row["source_types"] == ["monzo"]

    def test_columns_present(self, account_map_file):
        df = build_accounts_table(path=account_map_file)
        assert {"account_id", "display_name", "account_type"} <= set(df.columns)

    def test_missing_file_yields_empty_table(self, tmp_path):
        missing_path = tmp_path / "does_not_exist.json"
        df = build_accounts_table(path=missing_path)
        assert df.empty


class TestRealDataStoreFile:
    """Light sanity check that the actual data/account_map.json is well-formed."""

    def test_default_file_loads_without_error(self):
        df = build_accounts_table()
        assert isinstance(df.to_dict(), dict)  # loads and builds without raising


class TestRegisterAccount:
    def test_adds_new_identifier(self, tmp_path):
        path = tmp_path / "account_map.json"
        register_account("hash_x", "acc_x", "Account X", "current", path=path)

        assert get_account_id("hash_x", "irrelevant", path=path) == "acc_x"

    def test_overwrites_existing_identifier(self, tmp_path):
        path = tmp_path / "account_map.json"
        register_account("hash_x", "acc_x", "Account X", "current", path=path)
        register_account("hash_x", "acc_x", "Renamed Account X", "current", path=path)

        df = build_accounts_table(path=path)
        assert len(df) == 1
        assert df.iloc[0]["display_name"] == "Renamed Account X"

    def test_creates_missing_parent_directories(self, tmp_path):
        path = tmp_path / "nested" / "dir" / "account_map.json"
        register_account("hash_x", "acc_x", "Account X", "current", path=path)
        assert path.exists()


class TestRegisterSourceTypeFallback:
    def test_adds_new_fallback(self, tmp_path):
        path = tmp_path / "account_map.json"
        register_source_type_fallback(
            "monzo", "acc_monzo", "Monzo", "current", path=path
        )

        assert get_account_id(None, "monzo", path=path) == "acc_monzo"


class _FakeDatalakeForUnmapped:
    """Minimal stand-in exposing only what find_unmapped_accounts needs."""

    def __init__(self, bronze_data):
        self._bronze_data = bronze_data  # dict: source_type -> DataFrame

    def read_bronze(self, source_type):
        return self._bronze_data.get(source_type)


def _bronze_row(account_identifier, raw_data):
    return {
        "bronze_source_key": "key_0",
        "account_identifier": account_identifier,
        "raw_data": raw_data,
    }


class TestFindUnmappedAccounts:
    def test_detects_unmapped_identifier(self, tmp_path):
        path = tmp_path / "account_map.json"
        path.write_text(json.dumps({"identifiers": {}, "source_type_fallback": {}}))

        bronze_df = pd.DataFrame(
            [_bronze_row("brand_new_hash", {"description": "Test Merchant"})]
        )
        datalake = _FakeDatalakeForUnmapped({"kroo": bronze_df})

        result = find_unmapped_accounts(datalake, path=path)

        assert len(result) == 1
        row = result.iloc[0]
        assert row["source_type"] == "kroo"
        assert row["account_identifier"] == "brand_new_hash"
        assert row["sample_description"] == "Test Merchant"
        assert row["record_count"] == 1

    def test_mapped_identifier_excluded(self, tmp_path):
        path = tmp_path / "account_map.json"
        path.write_text(
            json.dumps(
                {
                    "identifiers": {
                        "known_hash": {
                            "account_id": "acc_x",
                            "display_name": "X",
                            "account_type": "current",
                        }
                    },
                    "source_type_fallback": {},
                }
            )
        )
        bronze_df = pd.DataFrame([_bronze_row("known_hash", {"description": "Test"})])
        datalake = _FakeDatalakeForUnmapped({"kroo": bronze_df})

        result = find_unmapped_accounts(datalake, path=path)
        assert result.empty

    def test_none_identifier_with_no_fallback_is_unmapped(self, tmp_path):
        path = tmp_path / "account_map.json"
        path.write_text(json.dumps({"identifiers": {}, "source_type_fallback": {}}))

        bronze_df = pd.DataFrame([_bronze_row(None, {"description": "Test"})])
        datalake = _FakeDatalakeForUnmapped({"monzo": bronze_df})

        result = find_unmapped_accounts(datalake, path=path)
        assert len(result) == 1
        assert result.iloc[0]["account_identifier"] is None

    def test_none_identifier_with_fallback_excluded(self, tmp_path):
        path = tmp_path / "account_map.json"
        path.write_text(
            json.dumps(
                {
                    "identifiers": {},
                    "source_type_fallback": {
                        "monzo": {
                            "account_id": "acc_monzo",
                            "display_name": "Monzo",
                            "account_type": "current",
                        }
                    },
                }
            )
        )
        bronze_df = pd.DataFrame([_bronze_row(None, {"description": "Test"})])
        datalake = _FakeDatalakeForUnmapped({"monzo": bronze_df})

        result = find_unmapped_accounts(datalake, path=path)
        assert result.empty

    def test_repeated_unmapped_identifier_aggregates_count(self, tmp_path):
        path = tmp_path / "account_map.json"
        path.write_text(json.dumps({"identifiers": {}, "source_type_fallback": {}}))

        bronze_df = pd.DataFrame(
            [
                _bronze_row("brand_new_hash", {"description": "Txn 1"}),
                _bronze_row("brand_new_hash", {"description": "Txn 2"}),
            ]
        )
        datalake = _FakeDatalakeForUnmapped({"kroo": bronze_df})

        result = find_unmapped_accounts(datalake, path=path)
        assert len(result) == 1
        assert result.iloc[0]["record_count"] == 2

    def test_no_bronze_data_returns_empty(self, tmp_path):
        path = tmp_path / "account_map.json"
        path.write_text(json.dumps({"identifiers": {}, "source_type_fallback": {}}))

        datalake = _FakeDatalakeForUnmapped({})
        result = find_unmapped_accounts(datalake, path=path)
        assert result.empty


class TestUnmappedAccountsError:
    def test_message_includes_registration_hint_and_details(self):
        df = pd.DataFrame(
            [
                {
                    "source_type": "kroo",
                    "account_identifier": "brand_new_hash",
                    "sample_description": "Test Merchant",
                    "record_count": 3,
                }
            ]
        )
        error = UnmappedAccountsError(df)
        message = str(error)

        assert "accounts register" in message
        assert "kroo" in message
        assert "brand_new_hash" in message
        assert "3 records" in message
