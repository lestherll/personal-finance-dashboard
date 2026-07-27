"""Tests for transformers/reconciliation_log.py (item 5: persisted,
auditable reconciliation history unifying all three check types)."""

import pandas as pd

from transformers.reconciliation_log import (
    RECONCILIATION_LOG_COLUMNS,
    ReconciliationMismatchError,
    build_reconciliation_log,
)


def _bronze_self_check_row(matches=True):
    return {
        "account_id": "acc_amex",
        "source_type": "amex",
        "filename": "jan.pdf",
        "ingestion_id": "ing1",
        "check_name": "amex_closing_balance",
        "expected_opening_minor": 10000,
        "expected_closing_minor": 12000,
        "derived_closing_minor": 12000 if matches else 99999,
        "matches": matches,
    }


def _continuity_row(matches=True, gap_related=False):
    return {
        "account_id": "acc_amex",
        "source_type": "amex",
        "next_source_type": "amex",
        "ingestion_id": "ing1",
        "next_ingestion_id": "ing2",
        "filename": "jan.pdf",
        "next_filename": "feb.pdf",
        "expected_closing_minor": 12000,
        "expected_opening_minor": 12000 if matches else 99999,
        "matches": None if gap_related else matches,
        "gap_related": gap_related,
    }


def _silver_break_row(matches=True):
    return {
        "account_id": "acc_amex",
        "source_type": "amex",
        "ingestion_id": "ing1",
        "filename": "jan.pdf",
        "expected_opening_minor": 10000,
        "expected_closing_minor": 12000,
        "silver_derived_closing_minor": 12000 if matches else 88888,
        "matches": matches,
    }


_EMPTY_BRONZE = pd.DataFrame(
    columns=[
        "account_id",
        "source_type",
        "filename",
        "ingestion_id",
        "check_name",
        "expected_opening_minor",
        "expected_closing_minor",
        "derived_closing_minor",
        "matches",
    ]
)
_EMPTY_CONTINUITY = pd.DataFrame(
    columns=[
        "account_id",
        "source_type",
        "next_source_type",
        "ingestion_id",
        "next_ingestion_id",
        "filename",
        "next_filename",
        "expected_closing_minor",
        "expected_opening_minor",
        "matches",
        "gap_related",
    ]
)
_EMPTY_SILVER_BREAKS = pd.DataFrame(
    columns=[
        "account_id",
        "source_type",
        "ingestion_id",
        "filename",
        "expected_opening_minor",
        "expected_closing_minor",
        "silver_derived_closing_minor",
        "matches",
    ]
)


class TestBuildReconciliationLog:
    def test_schema_has_all_declared_columns(self):
        result = build_reconciliation_log(
            _EMPTY_BRONZE, _EMPTY_CONTINUITY, _EMPTY_SILVER_BREAKS, "build_1"
        )
        assert list(result.columns) == RECONCILIATION_LOG_COLUMNS
        assert result.empty

    def test_bronze_self_check_row_shape(self):
        bronze = pd.DataFrame([_bronze_self_check_row()])
        result = build_reconciliation_log(
            bronze, _EMPTY_CONTINUITY, _EMPTY_SILVER_BREAKS, "build_1"
        )
        assert len(result) == 1
        row = result.iloc[0]
        assert row["check_type"] == "bronze_self_check"
        assert row["build_id"] == "build_1"
        assert bool(row["matches"]) is True
        assert row["next_filename"] is None
        assert bool(row["gap_related"]) is False

    def test_continuity_row_shape(self):
        continuity = pd.DataFrame([_continuity_row()])
        result = build_reconciliation_log(
            _EMPTY_BRONZE, continuity, _EMPTY_SILVER_BREAKS, "build_1"
        )
        assert len(result) == 1
        row = result.iloc[0]
        assert row["check_type"] == "continuity"
        assert row["next_filename"] == "feb.pdf"
        assert row["derived_closing_minor"] is None

    def test_continuity_gap_related_row_has_null_matches(self):
        continuity = pd.DataFrame([_continuity_row(gap_related=True)])
        result = build_reconciliation_log(
            _EMPTY_BRONZE, continuity, _EMPTY_SILVER_BREAKS, "build_1"
        )
        row = result.iloc[0]
        assert pd.isna(row["matches"])
        assert bool(row["gap_related"]) is True

    def test_silver_rollforward_row_shape(self):
        silver_breaks = pd.DataFrame([_silver_break_row(matches=False)])
        result = build_reconciliation_log(
            _EMPTY_BRONZE, _EMPTY_CONTINUITY, silver_breaks, "build_1"
        )
        assert len(result) == 1
        row = result.iloc[0]
        assert row["check_type"] == "silver_rollforward"
        assert row["derived_closing_minor"] == 88888
        assert bool(row["matches"]) is False

    def test_all_three_check_types_combine_in_one_table(self):
        bronze = pd.DataFrame([_bronze_self_check_row()])
        continuity = pd.DataFrame([_continuity_row()])
        silver_breaks = pd.DataFrame([_silver_break_row()])
        result = build_reconciliation_log(bronze, continuity, silver_breaks, "build_1")
        assert len(result) == 3
        assert set(result["check_type"]) == {
            "bronze_self_check",
            "continuity",
            "silver_rollforward",
        }
        assert (result["build_id"] == "build_1").all()


class TestReconciliationMismatchError:
    def test_message_lists_every_mismatch_not_just_first(self):
        mismatches = pd.DataFrame(
            [
                {
                    "check_type": "bronze_self_check",
                    "account_id": "acc_amex",
                    "source_type": "amex",
                    "filename": "jan.pdf",
                    "next_filename": None,
                    "expected_closing_minor": 12000,
                    "derived_closing_minor": 99999,
                },
                {
                    "check_type": "continuity",
                    "account_id": "acc_kroo",
                    "source_type": "kroo",
                    "filename": "mar.pdf",
                    "next_filename": "apr.pdf",
                    "expected_closing_minor": 5000,
                    "derived_closing_minor": None,
                },
            ]
        )
        error = ReconciliationMismatchError(mismatches)
        message = str(error)
        assert "2 reconciliation mismatch" in message
        assert "acc_amex" in message
        assert "jan.pdf" in message
        assert "acc_kroo" in message
        assert "mar.pdf" in message
        assert "apr.pdf" in message

    def test_stores_mismatches_dataframe(self):
        mismatches = pd.DataFrame(
            [
                {
                    "check_type": "bronze_self_check",
                    "account_id": "acc_amex",
                    "source_type": "amex",
                    "filename": "jan.pdf",
                    "next_filename": None,
                    "expected_closing_minor": 12000,
                    "derived_closing_minor": 99999,
                }
            ]
        )
        error = ReconciliationMismatchError(mismatches)
        assert len(error.mismatches) == 1
