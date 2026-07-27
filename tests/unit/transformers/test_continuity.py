"""Tests for transformers/continuity.py (cross-file balance continuity)."""

import json
from datetime import datetime

import pandas as pd

from transformers.continuity import find_balance_continuity


class _FakeDatalakeForContinuity:
    """Minimal stand-in exposing only what find_balance_continuity's two
    upstream calls (find_reconciliation_status/find_statement_periods) need."""

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
    ingestion_id,
    expected_opening_minor,
    expected_closing_minor,
    period_from,
    period_to,
    matches=True,
    check_name="test_check",
):
    return {
        "account_identifier": account_identifier,
        "filename": filename,
        "ingestion_id": ingestion_id,
        "reconciliation_check": check_name,
        "reconciliation_expected_opening_minor": expected_opening_minor,
        "reconciliation_expected_closing_minor": expected_closing_minor,
        "reconciliation_derived_closing_minor": expected_closing_minor,
        "reconciliation_matches": matches,
        "statement_period_from": period_from,
        "statement_period_to": period_to,
    }


_AMEX_MAP = {
    "hash_amex": {
        "account_id": "acc_amex",
        "display_name": "Amex",
        "account_type": "credit",
    }
}


class TestFindBalanceContinuity:
    def test_two_consecutive_files_match(self, tmp_path):
        path = _account_map_path(tmp_path, _AMEX_MAP)
        bronze_df = pd.DataFrame(
            [
                _bronze_row(
                    "hash_amex",
                    "jan.pdf",
                    "ing1",
                    10000,
                    12000,
                    datetime(2026, 1, 1),
                    datetime(2026, 1, 31),
                ),
                _bronze_row(
                    "hash_amex",
                    "feb.pdf",
                    "ing2",
                    12000,
                    15000,
                    datetime(2026, 2, 1),
                    datetime(2026, 2, 28),
                ),
            ]
        )
        datalake = _FakeDatalakeForContinuity({"amex": bronze_df})

        result = find_balance_continuity(datalake, path=path)

        assert len(result) == 1
        row = result.iloc[0]
        assert row["filename"] == "jan.pdf"
        assert row["next_filename"] == "feb.pdf"
        assert bool(row["matches"]) is True
        assert bool(row["gap_related"]) is False

    def test_mismatch_between_consecutive_files(self, tmp_path):
        path = _account_map_path(tmp_path, _AMEX_MAP)
        bronze_df = pd.DataFrame(
            [
                _bronze_row(
                    "hash_amex",
                    "jan.pdf",
                    "ing1",
                    10000,
                    12000,
                    datetime(2026, 1, 1),
                    datetime(2026, 1, 31),
                ),
                _bronze_row(
                    "hash_amex",
                    "feb.pdf",
                    "ing2",
                    99999,  # doesn't match jan's closing of 12000
                    15000,
                    datetime(2026, 2, 1),
                    datetime(2026, 2, 28),
                ),
            ]
        )
        datalake = _FakeDatalakeForContinuity({"amex": bronze_df})

        result = find_balance_continuity(datalake, path=path)

        assert len(result) == 1
        assert bool(result.iloc[0]["matches"]) is False
        assert bool(result.iloc[0]["gap_related"]) is False

    def test_gap_between_files_is_inconclusive_not_a_mismatch(self, tmp_path):
        """A real, known coverage gap (e.g. a missing statement) must not be
        reported as a balance break - the two figures genuinely aren't
        comparable, distinct from a real mismatch."""
        path = _account_map_path(tmp_path, _AMEX_MAP)
        bronze_df = pd.DataFrame(
            [
                _bronze_row(
                    "hash_amex",
                    "jan.pdf",
                    "ing1",
                    10000,
                    12000,
                    datetime(2026, 1, 1),
                    datetime(2026, 1, 31),
                ),
                _bronze_row(
                    "hash_amex",
                    "may.pdf",
                    "ing2",
                    99999,  # would look like a mismatch, but there's a real gap
                    15000,
                    datetime(2026, 5, 1),
                    datetime(2026, 5, 31),
                ),
            ]
        )
        datalake = _FakeDatalakeForContinuity({"amex": bronze_df})

        result = find_balance_continuity(datalake, path=path)

        assert len(result) == 1
        row = result.iloc[0]
        assert bool(row["gap_related"]) is True
        assert pd.isna(row["matches"])

    def test_missing_opening_anchor_is_inconclusive(self, tmp_path):
        """Monzo PDF never captures an opening anchor - pairing it with a
        prior file must report None (inconclusive), never a false mismatch."""
        map_path = _account_map_path(
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
                _bronze_row(
                    "hash_monzo",
                    "q1.pdf",
                    "ing1",
                    None,
                    12000,
                    datetime(2026, 1, 1),
                    datetime(2026, 3, 31),
                ),
                _bronze_row(
                    "hash_monzo",
                    "q2.pdf",
                    "ing2",
                    None,
                    15000,
                    datetime(2026, 4, 1),
                    datetime(2026, 6, 30),
                ),
            ]
        )
        datalake = _FakeDatalakeForContinuity({"monzo-pdf": bronze_df})

        result = find_balance_continuity(datalake, path=map_path)

        assert len(result) == 1
        assert pd.isna(result.iloc[0]["matches"])
        assert bool(result.iloc[0]["gap_related"]) is False

    def test_single_file_no_pair_produces_no_rows(self, tmp_path):
        path = _account_map_path(tmp_path, _AMEX_MAP)
        bronze_df = pd.DataFrame(
            [
                _bronze_row(
                    "hash_amex",
                    "jan.pdf",
                    "ing1",
                    10000,
                    12000,
                    datetime(2026, 1, 1),
                    datetime(2026, 1, 31),
                )
            ]
        )
        datalake = _FakeDatalakeForContinuity({"amex": bronze_df})

        result = find_balance_continuity(datalake, path=path)
        assert result.empty

    def test_empty_bronze_returns_empty(self, tmp_path):
        path = _account_map_path(tmp_path)
        datalake = _FakeDatalakeForContinuity({})

        result = find_balance_continuity(datalake, path=path)
        assert result.empty
