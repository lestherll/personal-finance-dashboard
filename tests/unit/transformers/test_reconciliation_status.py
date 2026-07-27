"""Tests for transformers/reconciliation_status.py (queryable B1 reconciliation status)."""

import json
from unittest.mock import MagicMock

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


def _bronze_row(
    account_identifier,
    filename,
    check_name,
    expected_minor,
    derived_minor,
    matches,
    ingestion_id=None,
    expected_opening_minor=None,
):
    return {
        "account_identifier": account_identifier,
        "filename": filename,
        "ingestion_id": ingestion_id or f"ingestion_{filename}",
        "reconciliation_check": check_name,
        "reconciliation_expected_opening_minor": expected_opening_minor,
        "reconciliation_expected_closing_minor": expected_minor,
        "reconciliation_derived_closing_minor": derived_minor,
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
                    86304,
                    86304,
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

    def test_surfaces_ingestion_id_and_opening_anchor(self, tmp_path):
        """ingestion_id and expected_opening_minor feed the continuity check
        (transformers/continuity.py) - both must round-trip through here."""
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
                    86304,
                    86304,
                    True,
                    ingestion_id="abc123",
                    expected_opening_minor=76958,
                )
            ]
        )
        datalake = _FakeDatalakeForReconciliation({"amex": bronze_df})

        result = find_reconciliation_status(datalake, path=path)

        assert len(result) == 1
        row = result.iloc[0]
        assert row["ingestion_id"] == "abc123"
        assert row["expected_opening_minor"] == 76958

    def test_missing_opening_anchor_column_defaults_to_none(self, tmp_path):
        """Monzo PDF never captures an opening anchor at all - a Bronze
        parquet written before this column existed, or by an adapter that
        never sets it, should surface as None, not raise."""
        path = _account_map_path(
            tmp_path,
            {
                "hash_monzo": {
                    "account_id": "acc_monzo",
                    "display_name": "Monzo",
                    "account_type": "current",
                }
            },
        )
        bronze_df = pd.DataFrame(
            [
                {
                    "account_identifier": "hash_monzo",
                    "filename": "q2.pdf",
                    "ingestion_id": "xyz",
                    "reconciliation_check": "monzo_pdf_personal_account_balance",
                    "reconciliation_expected_closing_minor": 225537,
                    "reconciliation_derived_closing_minor": 225537,
                    "reconciliation_matches": True,
                }
            ]
        )
        datalake = _FakeDatalakeForReconciliation({"monzo-pdf": bronze_df})

        result = find_reconciliation_status(datalake, path=path)

        assert len(result) == 1
        assert result.iloc[0]["expected_opening_minor"] is None

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
                    86304,
                    67804,
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
                    "hash_amex", "jan.pdf", "amex_closing_balance", 86304, 86304, True
                ),
                _bronze_row(
                    "hash_amex", "jan.pdf", "amex_closing_balance", 86304, 86304, True
                ),
            ]
        )
        datalake = _FakeDatalakeForReconciliation({"amex": bronze_df})

        result = find_reconciliation_status(datalake, path=path)
        assert len(result) == 1

    def test_two_accounts_sharing_one_filename_both_reported(self, tmp_path):
        """A source covering multiple accounts in one file (e.g. Vanguard's
        ISA + Personal Pension wrappers) can carry two genuinely different
        reconciliation verdicts under the same filename - the dedup key
        must be (filename, account_identifier), not filename alone, or one
        wrapper's status would be silently dropped."""
        path = _account_map_path(
            tmp_path,
            {
                "hash_isa": {
                    "account_id": "acc_isa",
                    "display_name": "ISA",
                    "account_type": "investment",
                },
                "hash_pension": {
                    "account_id": "acc_pension",
                    "display_name": "Pension",
                    "account_type": "investment",
                },
            },
        )
        bronze_df = pd.DataFrame(
            [
                _bronze_row(
                    "hash_isa",
                    "statement.pdf",
                    "vanguard_account_summary_isa",
                    50015,
                    50015,
                    True,
                ),
                _bronze_row(
                    "hash_pension",
                    "statement.pdf",
                    "vanguard_account_summary_pension",
                    50100,
                    99999,
                    False,
                ),
            ]
        )
        datalake = _FakeDatalakeForReconciliation({"vanguard-pdf": bronze_df})

        result = find_reconciliation_status(datalake, path=path)

        assert len(result) == 2
        by_account = {row.account_id: row for row in result.itertuples()}
        assert bool(by_account["acc_isa"].matches) is True
        assert bool(by_account["acc_pension"].matches) is False

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
                    86304,
                    86304,
                    True,
                )
            ]
        )
        datalake = _FakeDatalakeForReconciliation({"amex": bronze_df})

        result = find_reconciliation_status(datalake, path=path)
        assert result.empty

    def test_bronze_frames_bypasses_datalake_read(self, tmp_path):
        """Passing bronze_frames must not touch datalake.read_bronze at all
        (P2.2b) - same result as the datalake-driven path above."""
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
                    86304,
                    86304,
                    True,
                )
            ]
        )
        mock_dl = MagicMock()
        mock_dl.read_bronze.side_effect = AssertionError(
            "read_bronze should not be called when bronze_frames is provided"
        )

        result = find_reconciliation_status(
            mock_dl, path=path, bronze_frames={"amex": bronze_df}
        )

        mock_dl.read_bronze.assert_not_called()
        assert len(result) == 1
        assert result.iloc[0]["account_id"] == "acc_amex"
