"""Tests for models/datalake.py::DataLake.write_bronze's reconciliation
column handling - both the existing single-result scalar broadcast and the
new per-row `reconciliations` list path (see adapters.base.ReconciliationResult
/ DataSourceAdapter.last_reconciliations)."""

import pandas as pd
import pytest

from adapters.base import ReconciliationResult
from models.datalake import DataLake
from models.ingestion import IngestionManifest


def _manifest(source_type="amex", filename="statement.pdf", ingestion_id="sha256ab"):
    return IngestionManifest(
        ingestion_id=ingestion_id,
        original_filename=filename,
        raw_artifact_path=f"/raw/{ingestion_id}.pdf",
        status="complete",
        created_at="2026-01-01T00:00:00+00:00",
        source_type=source_type,
        adapter="TestAdapter",
        parser_version="1",
    )


def _df(rows):
    return pd.DataFrame(rows)


@pytest.fixture
def datalake(tmp_path, monkeypatch):
    monkeypatch.setattr("models.datalake.BRONZE_DIR", tmp_path / "bronze")
    return DataLake(db_path=":memory:")


class TestWriteBronzeSingleReconciliation:
    """Existing scalar-broadcast behavior - must be unchanged."""

    def test_broadcasts_one_result_across_all_rows(self, datalake, tmp_path):
        source_type = "amex"
        df = _df(
            [
                {"source_key": "k1", "account_identifier": "hash_a"},
                {"source_key": "k2", "account_identifier": "hash_a"},
            ]
        )
        reconciliation = ReconciliationResult(
            check_name="amex_closing_balance",
            expected_closing_minor=86304,
            derived_closing_minor=86304,
            matches=True,
        )
        datalake.write_bronze(
            _manifest(source_type=source_type), df, reconciliation=reconciliation
        )

        result = datalake.read_bronze(source_type)
        assert (result["reconciliation_check"] == "amex_closing_balance").all()
        assert (result["reconciliation_matches"] == True).all()  # noqa: E712


class TestWriteBronzeMultipleReconciliations:
    """New per-row assignment - each result applies only to the rows whose
    account_identifier it names, so two accounts covered by one file (e.g.
    Vanguard's two wrappers) can carry genuinely different verdicts."""

    def test_partitions_columns_by_account_identifier(self, datalake):
        source_type = "vanguard-pdf"
        df = _df(
            [
                {"source_key": "k1", "account_identifier": "hash_isa"},
                {"source_key": "k2", "account_identifier": "hash_isa"},
                {"source_key": "k3", "account_identifier": "hash_pension"},
            ]
        )
        reconciliations = [
            ReconciliationResult(
                check_name="vanguard_account_summary_isa",
                expected_closing_minor=50015,
                derived_closing_minor=50015,
                matches=True,
                account_identifier="hash_isa",
            ),
            ReconciliationResult(
                check_name="vanguard_account_summary_pension",
                expected_closing_minor=50100,
                derived_closing_minor=99999,
                matches=False,
                account_identifier="hash_pension",
            ),
        ]

        datalake.write_bronze(
            _manifest(source_type=source_type),
            df,
            reconciliations=reconciliations,
        )

        result = datalake.read_bronze(source_type)
        isa_rows = result[result["account_identifier"] == "hash_isa"]
        pension_rows = result[result["account_identifier"] == "hash_pension"]

        assert (
            isa_rows["reconciliation_check"] == "vanguard_account_summary_isa"
        ).all()
        assert (isa_rows["reconciliation_matches"] == True).all()  # noqa: E712
        assert (
            pension_rows["reconciliation_check"] == "vanguard_account_summary_pension"
        ).all()
        assert (pension_rows["reconciliation_matches"] == False).all()  # noqa: E712

    def test_row_with_unmatched_identifier_stays_null(self, datalake):
        """A row whose account_identifier doesn't match any result's
        (e.g. account number extraction failed for that row) keeps the
        initialized null - same semantics as 'no reconciliation at all'."""
        source_type = "vanguard-pdf"
        df = _df(
            [
                {"source_key": "k1", "account_identifier": "hash_isa"},
                {"source_key": "k2", "account_identifier": "hash_unrelated"},
            ]
        )
        reconciliations = [
            ReconciliationResult(
                check_name="vanguard_account_summary_isa",
                expected_closing_minor=50015,
                derived_closing_minor=50015,
                matches=True,
                account_identifier="hash_isa",
            )
        ]

        datalake.write_bronze(
            _manifest(source_type=source_type),
            df,
            reconciliations=reconciliations,
        )

        result = datalake.read_bronze(source_type)
        unrelated_row = result[result["account_identifier"] == "hash_unrelated"].iloc[0]
        assert pd.isna(unrelated_row["reconciliation_check"])
        assert pd.isna(unrelated_row["reconciliation_matches"])

    def test_both_reconciliation_and_reconciliations_raises(self, datalake):
        df = _df([{"source_key": "k1", "account_identifier": "hash_a"}])
        reconciliation = ReconciliationResult(
            check_name="x",
            expected_closing_minor=None,
            derived_closing_minor=None,
            matches=None,
        )
        reconciliations = [reconciliation]

        with pytest.raises(ValueError):
            datalake.write_bronze(
                _manifest(source_type="vanguard-pdf"),
                df,
                reconciliation=reconciliation,
                reconciliations=reconciliations,
            )
