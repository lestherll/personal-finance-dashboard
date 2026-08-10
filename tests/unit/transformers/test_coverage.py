"""Tests for transformers/coverage.py (statement-period coverage tracking)."""

import json
from unittest.mock import MagicMock

import pandas as pd

from transformers.coverage import (
    build_coverage_calendar,
    find_coverage_gaps,
    find_statement_periods,
)


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
        "ingestion_id": filename,
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

    def test_multi_wrapper_statement_keeps_both_accounts(self, tmp_path):
        """A single Vanguard statement file legitimately bundles two accounts
        (ISA + Pension), each with its own account_identifier - both must
        survive dedup, not just whichever sorts first by filename alone."""
        path = _account_map_path(
            tmp_path,
            {
                "hash_vanguard_isa": {
                    "account_id": "acc_vanguard_isa",
                    "display_name": "Vanguard ISA",
                    "account_type": "investment",
                },
                "hash_vanguard_pension": {
                    "account_id": "acc_vanguard_pension",
                    "display_name": "Vanguard Pension",
                    "account_type": "investment",
                },
            },
        )
        bronze_df = pd.DataFrame(
            [
                _bronze_row(
                    "hash_vanguard_isa",
                    "Vanguard_Statement.pdf",
                    pd.Timestamp("2026-04-09"),
                    pd.Timestamp("2026-07-08"),
                ),
                _bronze_row(
                    "hash_vanguard_pension",
                    "Vanguard_Statement.pdf",
                    pd.Timestamp("2026-04-09"),
                    pd.Timestamp("2026-07-08"),
                ),
            ]
        )
        datalake = _FakeDatalakeForCoverage({"vanguard-pdf": bronze_df})

        result = find_statement_periods(datalake, path=path)

        assert len(result) == 2
        assert set(result["account_id"]) == {
            "acc_vanguard_isa",
            "acc_vanguard_pension",
        }

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
                    pd.Timestamp("2026-01-01"),
                    pd.Timestamp("2026-01-31"),
                )
            ]
        )
        mock_dl = MagicMock()
        mock_dl.read_bronze.side_effect = AssertionError(
            "read_bronze should not be called when bronze_frames is provided"
        )

        result = find_statement_periods(
            mock_dl, path=path, bronze_frames={"amex": bronze_df}
        )

        mock_dl.read_bronze.assert_not_called()
        assert len(result) == 1
        assert result.iloc[0]["account_id"] == "acc_amex"


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


def _period_row(account_id, filename, period_from, period_to, source_type="amex"):
    return {
        "account_id": account_id,
        "source_type": source_type,
        "filename": filename,
        "period_from": period_from,
        "period_to": period_to,
    }


class TestBuildCoverageCalendar:
    def test_empty_periods_returns_empty_frame(self):
        periods = pd.DataFrame(columns=["account_id", "period_from", "period_to"])
        accounts = pd.DataFrame(columns=["account_id", "display_name"])
        calendar = build_coverage_calendar(periods, accounts)
        assert calendar.empty
        assert list(calendar.columns) == [
            "account_id",
            "display_name",
            "month",
            "status",
        ]

    def test_single_account_all_covered_no_gaps(self):
        periods = pd.DataFrame(
            [
                _period_row(
                    "acc_amex",
                    "jan.pdf",
                    pd.Timestamp("2026-01-01"),
                    pd.Timestamp("2026-01-31"),
                ),
                _period_row(
                    "acc_amex",
                    "feb.pdf",
                    pd.Timestamp("2026-02-01"),
                    pd.Timestamp("2026-02-28"),
                ),
            ]
        )
        accounts = pd.DataFrame([{"account_id": "acc_amex", "display_name": "Amex"}])

        calendar = build_coverage_calendar(periods, accounts)

        assert len(calendar) == 2
        assert set(calendar["status"]) == {"covered"}
        assert set(calendar["display_name"]) == {"Amex"}

    def test_missing_month_between_periods_is_a_gap(self):
        periods = pd.DataFrame(
            [
                _period_row(
                    "acc_amex",
                    "jan.pdf",
                    pd.Timestamp("2026-01-01"),
                    pd.Timestamp("2026-01-31"),
                ),
                _period_row(
                    "acc_amex",
                    "mar.pdf",
                    pd.Timestamp("2026-03-01"),
                    pd.Timestamp("2026-03-31"),
                ),
            ]
        )
        accounts = pd.DataFrame([{"account_id": "acc_amex", "display_name": "Amex"}])

        calendar = build_coverage_calendar(periods, accounts)
        by_month = calendar.set_index(calendar["month"].dt.strftime("%Y-%m"))["status"]

        assert by_month["2026-01"] == "covered"
        assert by_month["2026-02"] == "gap"
        assert by_month["2026-03"] == "covered"

    def test_shorter_account_gets_no_data_outside_its_own_range(self):
        """Two accounts sharing the same month columns: the account with a
        narrower observed range shows 'no_data' (not 'gap') for months
        before its first period or after its last - nothing was expected
        there yet, so it isn't a coverage problem."""
        periods = pd.DataFrame(
            [
                _period_row(
                    "acc_amex",
                    "jan.pdf",
                    pd.Timestamp("2026-01-01"),
                    pd.Timestamp("2026-01-31"),
                ),
                _period_row(
                    "acc_amex",
                    "feb.pdf",
                    pd.Timestamp("2026-02-01"),
                    pd.Timestamp("2026-02-28"),
                ),
                _period_row(
                    "acc_amex",
                    "mar.pdf",
                    pd.Timestamp("2026-03-01"),
                    pd.Timestamp("2026-03-31"),
                ),
                _period_row(
                    "acc_kroo",
                    "feb.pdf",
                    pd.Timestamp("2026-02-01"),
                    pd.Timestamp("2026-02-28"),
                    source_type="kroo",
                ),
            ]
        )
        accounts = pd.DataFrame(
            [
                {"account_id": "acc_amex", "display_name": "Amex"},
                {"account_id": "acc_kroo", "display_name": "Kroo"},
            ]
        )

        calendar = build_coverage_calendar(periods, accounts)
        kroo_rows = calendar[calendar["account_id"] == "acc_kroo"]
        kroo = kroo_rows.set_index(kroo_rows["month"].dt.strftime("%Y-%m"))["status"]

        assert kroo["2026-01"] == "no_data"
        assert kroo["2026-02"] == "covered"
        assert kroo["2026-03"] == "no_data"

    def test_missing_display_name_falls_back_to_account_id(self):
        periods = pd.DataFrame(
            [
                _period_row(
                    "acc_unmapped",
                    "jan.pdf",
                    pd.Timestamp("2026-01-01"),
                    pd.Timestamp("2026-01-31"),
                )
            ]
        )
        accounts = pd.DataFrame(columns=["account_id", "display_name"])

        calendar = build_coverage_calendar(periods, accounts)

        assert calendar.iloc[0]["display_name"] == "acc_unmapped"
