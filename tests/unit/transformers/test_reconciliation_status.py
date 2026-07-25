"""Tests for transformers/reconciliation_status.py (queryable B1 reconciliation status)."""

import json

import pandas as pd

from transformers.reconciliation_status import find_reconciliation_status


class _FakeDatalakeForReconciliation:
    """Minimal stand-in exposing only what find_reconciliation_status needs."""

    def __init__(self, bronze_data):
        self._bronze_data = bronze_data  # dict: source_type -> DataFrame

    def read_bronze(self, source_type):
        return self._bronze_data.get(source_type)


def _account_map_path(tmp_path, identifiers=None):
    path = tmp_path / "account_map.json"
    path.write_text(
        json.dumps({"identifiers": identifiers or {}, "source_type_fallback": {}})
    )
    return path


def _bronze_row(account_identifier, filename, check_name, expected, derived, matches):
    return {
        "account_identifier": account_identifier,
        "filename": filename,
        "reconciliation_check": check_name,
        "reconciliation_expected_closing": expected,
        "reconciliation_derived_closing": derived,
        "reconciliation_matches": matches,
    }


class TestFindReconciliationStatus:
    def test_finds_status_per_account(self, tmp_path):
        path = _account_map_path(
            tmp_path,
            {
                "hash_amex": {
                    "account_id": "acc_amex",
                    "display_name": "Amex",
                    "account_type": "credit",
                }
            },
        )
        bronze_df = pd.DataFrame(
            [
                _bronze_row(
                    "hash_amex",
                    "jan.pdf",
                    "amex_closing_balance",
                    863.04,
                    863.04,
                    True,
                )
            ]
        )
        datalake = _FakeDatalakeForReconciliation({"amex": bronze_df})

        result = find_reconciliation_status(datalake, path=path)

        assert len(result) == 1
        row = result.iloc[0]
        assert row["account_id"] == "acc_amex"
        assert row["source_type"] == "amex"
        assert row["filename"] == "jan.pdf"
        assert bool(row["matches"]) is True

    def test_mismatch_is_reported(self, tmp_path):
        path = _account_map_path(
            tmp_path,
            {
                "hash_amex": {
                    "account_id": "acc_amex",
                    "display_name": "Amex",
                    "account_type": "credit",
                }
            },
        )
        bronze_df = pd.DataFrame(
            [
                _bronze_row(
                    "hash_amex",
                    "jan.pdf",
                    "amex_closing_balance",
                    863.04,
                    678.04,
                    False,
                )
            ]
        )
        datalake = _FakeDatalakeForReconciliation({"amex": bronze_df})

        result = find_reconciliation_status(datalake, path=path)

        assert len(result) == 1
        assert bool(result.iloc[0]["matches"]) is False

    def test_deduplicates_multiple_rows_from_same_file(self, tmp_path):
        """One statement produces many Bronze rows (one per transaction) -
        each carrying the same reconciliation columns - only one row per
        filename should be reported."""
        path = _account_map_path(
            tmp_path,
            {
                "hash_amex": {
                    "account_id": "acc_amex",
                    "display_name": "Amex",
                    "account_type": "credit",
                }
            },
        )
        bronze_df = pd.DataFrame(
            [
                _bronze_row(
                    "hash_amex", "jan.pdf", "amex_closing_balance", 863.04, 863.04, True
                ),
                _bronze_row(
                    "hash_amex", "jan.pdf", "amex_closing_balance", 863.04, 863.04, True
                ),
            ]
        )
        datalake = _FakeDatalakeForReconciliation({"amex": bronze_df})

        result = find_reconciliation_status(datalake, path=path)
        assert len(result) == 1

    def test_missing_reconciliation_columns_returns_empty(self, tmp_path):
        """A reconciliation-tracked source_type present in Bronze, but from
        a file with no reconciliation columns written (e.g. no anchor found
        in that particular statement)."""
        path = _account_map_path(tmp_path)
        bronze_df = pd.DataFrame(
            [{"account_identifier": "hash_kroo", "filename": "f.pdf"}]
        )
        datalake = _FakeDatalakeForReconciliation({"kroo": bronze_df})

        result = find_reconciliation_status(datalake, path=path)
        assert result.empty

    def test_empty_bronze_returns_empty(self, tmp_path):
        path = _account_map_path(tmp_path)
        datalake = _FakeDatalakeForReconciliation({})

        result = find_reconciliation_status(datalake, path=path)
        assert result.empty

    def test_unmapped_account_is_skipped(self, tmp_path):
        """A reporting command, not a pipeline pre-flight check - an
        unregistered account is skipped rather than raising."""
        path = _account_map_path(tmp_path)
        bronze_df = pd.DataFrame(
            [
                _bronze_row(
                    "hash_unmapped",
                    "jan.pdf",
                    "amex_closing_balance",
                    863.04,
                    863.04,
                    True,
                )
            ]
        )
        datalake = _FakeDatalakeForReconciliation({"amex": bronze_df})

        result = find_reconciliation_status(datalake, path=path)
        assert result.empty
