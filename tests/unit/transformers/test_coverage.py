"""Tests for transformers/coverage.py (statement-period coverage tracking)."""

import json

import pandas as pd

from transformers.coverage import find_coverage_gaps, find_statement_periods


class _FakeDatalakeForCoverage:
    """Minimal stand-in exposing only what find_statement_periods needs."""

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


def _bronze_row(account_identifier, filename, period_from, period_to):
    return {
        "account_identifier": account_identifier,
        "filename": filename,
        "statement_period_from": period_from,
        "statement_period_to": period_to,
    }


class TestFindStatementPeriods:
    def test_finds_periods_per_account(self, tmp_path):
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
                    pd.Timestamp("2026-01-01"),
                    pd.Timestamp("2026-01-31"),
                )
            ]
        )
        datalake = _FakeDatalakeForCoverage({"amex": bronze_df})

        result = find_statement_periods(datalake, path=path)

        assert len(result) == 1
        row = result.iloc[0]
        assert row["account_id"] == "acc_amex"
        assert row["source_type"] == "amex"
        assert row["filename"] == "jan.pdf"

    def test_deduplicates_multiple_rows_from_same_file(self, tmp_path):
        """One statement produces many Bronze rows (one per transaction) -
        each carrying the same statement_period_from/to - only one row per
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
                    "hash_amex",
                    "jan.pdf",
                    pd.Timestamp("2026-01-01"),
                    pd.Timestamp("2026-01-31"),
                ),
                _bronze_row(
                    "hash_amex",
                    "jan.pdf",
                    pd.Timestamp("2026-01-01"),
                    pd.Timestamp("2026-01-31"),
                ),
            ]
        )
        datalake = _FakeDatalakeForCoverage({"amex": bronze_df})

        result = find_statement_periods(datalake, path=path)
        assert len(result) == 1

    def test_missing_period_columns_returns_empty(self, tmp_path):
        """A period-tracked source_type present in Bronze, but from a file
        ingested before B4's period capture existed for it - no
        statement_period_from/to columns written yet."""
        path = _account_map_path(tmp_path)
        bronze_df = pd.DataFrame(
            [{"account_identifier": "hash_kroo", "filename": "f.pdf"}]
        )
        datalake = _FakeDatalakeForCoverage({"kroo": bronze_df})

        result = find_statement_periods(datalake, path=path)
        assert result.empty

    def test_empty_bronze_returns_empty(self, tmp_path):
        path = _account_map_path(tmp_path)
        datalake = _FakeDatalakeForCoverage({})

        result = find_statement_periods(datalake, path=path)
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
                    pd.Timestamp("2026-01-01"),
                    pd.Timestamp("2026-01-31"),
                )
            ]
        )
        datalake = _FakeDatalakeForCoverage({"amex": bronze_df})

        result = find_statement_periods(datalake, path=path)
        assert result.empty


class TestFindCoverageGaps:
    def test_flags_gap_beyond_tolerance(self):
        periods = pd.DataFrame(
            [
                {
                    "account_id": "acc_amex",
                    "source_type": "amex",
                    "filename": "jan.pdf",
                    "period_from": pd.Timestamp("2026-01-01"),
                    "period_to": pd.Timestamp("2026-01-31"),
                },
                {
                    "account_id": "acc_amex",
                    "source_type": "amex",
                    "filename": "mar.pdf",
                    "period_from": pd.Timestamp("2026-03-01"),
                    "period_to": pd.Timestamp("2026-03-31"),
                },
            ]
        )
        gaps = find_coverage_gaps(periods)
        assert len(gaps) == 1
        assert gaps.iloc[0]["account_id"] == "acc_amex"
        assert gaps.iloc[0]["days"] == 29

    def test_no_gap_within_tolerance(self):
        """Mirrors adapters/pdf_adapter.py's _PERIOD_BOUNDARY_TOLERANCE
        rationale: a couple of days between statements isn't a real gap."""
        periods = pd.DataFrame(
            [
                {
                    "account_id": "acc_amex",
                    "source_type": "amex",
                    "filename": "jan.pdf",
                    "period_from": pd.Timestamp("2026-01-01"),
                    "period_to": pd.Timestamp("2026-01-31"),
                },
                {
                    "account_id": "acc_amex",
                    "source_type": "amex",
                    "filename": "feb.pdf",
                    "period_from": pd.Timestamp("2026-02-02"),
                    "period_to": pd.Timestamp("2026-02-28"),
                },
            ]
        )
        gaps = find_coverage_gaps(periods)
        assert gaps.empty

    def test_no_periods_returns_empty(self):
        periods = pd.DataFrame(
            columns=[
                "account_id",
                "source_type",
                "filename",
                "period_from",
                "period_to",
            ]
        )
        gaps = find_coverage_gaps(periods)
        assert gaps.empty
