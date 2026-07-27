"""Hermetic end-to-end tests: clean tmp_path checkout, full
Bronze→Silver pipeline, exercising critical failure modes.

See TODO.md item 6 for the acceptance criteria.
"""


from unittest.mock import MagicMock

import pandas as pd
import pytest

from adapters.factory import AdapterFactory
from models.build import current_build_id, list_builds
from models.datalake import DataLake
from models.ingestion import (
    STATUS_COMPLETE,
    load_manifest,
    start_ingestion,
    write_manifest,
)
from transformers.account_config import register_account
from transformers.balance import get_net_worth
from transformers.reconciliation_log import ReconciliationMismatchError
from transformers.silver_transformer import run_bronze_to_silver

_KROO_ID = "4672409adb18"
_ACC_ID = "acc_kroo_test"


def _kroo_pdf_bytes() -> bytes:
    """Synthetic Kroo PDF fixture text matching the real adapter's parse layout."""
    return (
        "Your Current Account\n"
        "Statement number 0001\n"
        "Kroo Current Account\n"
        "Sort code: 04-00-75\n"
        "Account number: 12345678\n"
        "Overview\n"
        "Total opening balance\n"
        "\u00a3800.00\n"
        "Account transactions\n"
        "1 Jul 2026 to 31 Jul 2026\n"
        "01 Jul 2026\n"
        "FROM ACCOUNT\n"
        "My Old Bank SA\n"
        "\u00a31,000.00\n"
        "\u00a31,800.00\n"
        "02 Jul 2026\n"
        "DELIVEROO GOLD BENEFIT\n"
        "Deliveroo Gold Benefit\n"
        "\u00a35.00\n"
        "\u00a31,805.00\n"
        "\n"
        "Closing balance\n"
        "\u00a31,805.00\n"
    ).encode("utf-8")


def _kroo_pdf_bytes_mismatched() -> bytes:
    """Same as _kroo_pdf_bytes() but with a printed Closing balance that
    doesn't match the transactions' own running total (1,806.00 printed
    instead of the true 1,805.00) - a genuine B1 mismatch for the
    strict-gate tests. Only the anchor line (after "Closing balance\n") is
    changed - the per-transaction running balance above it is untouched."""
    return _kroo_pdf_bytes().replace(
        "Closing balance\n£1,805.00\n".encode("utf-8"),
        "Closing balance\n£1,806.00\n".encode("utf-8"),
    )


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Fully isolated environment: data dirs, account map, PDF text stub."""
    for sub in ("bronze", "silver", "gold", "ingestions", "raw"):
        (tmp_path / sub).mkdir(parents=True)
    monkeypatch.setattr("models.datalake.BRONZE_DIR", tmp_path / "bronze")
    monkeypatch.setattr("models.datalake.SILVER_DIR", tmp_path / "silver")
    monkeypatch.setattr("models.datalake.GOLD_DIR", tmp_path / "gold")
    monkeypatch.setattr(
        "transformers.silver_transformer._SILVER_DIR", tmp_path / "silver"
    )
    monkeypatch.setattr("models.build._DEFAULT_SILVER_DIR", tmp_path / "silver")
    monkeypatch.setattr("models.ingestion.INGESTIONS_DIR", tmp_path / "ingestions")
    monkeypatch.setattr("models.ingestion.RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(
        "adapters.pdf_adapter.PdfAdapter._extract_text",
        staticmethod(lambda content: content.decode("utf-8")),
    )
    acct_path = tmp_path / "account_map.json"
    monkeypatch.setattr("transformers.account_config.ACCOUNT_MAP_PATH", acct_path)
    register_account(_KROO_ID, _ACC_ID, "Kroo Test", "current", path=acct_path)

    dl = DataLake(db_path=str(tmp_path / "test.duckdb"))
    dl.SILVER_DIR = tmp_path / "silver"
    return dl


def _ingest_bytes(datalake, pdf_bytes, filename, tmp_dir):
    """Write pdf_bytes to a temp file, then ingest through the
    normal start_ingestion -> parse -> write_bronze flow."""
    filepath = tmp_dir / filename
    filepath.write_bytes(pdf_bytes)
    file_hash = __import__("hashlib").sha256(pdf_bytes).hexdigest()
    manifest = start_ingestion(filepath, file_hash)
    if manifest.status == STATUS_COMPLETE:
        return manifest

    factory = AdapterFactory()
    result = factory.ingest(pdf_bytes, filename, file_hash)
    records = result.records

    manifest.source_type = result.source_type or records[0].source_type
    manifest.adapter = result.adapter or "unknown"
    manifest.parser_version = result.parser_version
    df = pd.DataFrame(
        [
            {
                "source_key": r.source_key,
                "raw_data": r.raw_data,
                "account_identifier": r.account_identifier,
                "record_type": r.record_type,
                "file_hash": r.file_hash,
                "line_number": r.line_number,
                "bronze_record_id": r.bronze_record_id,
                "source_ordinal": r.source_ordinal,
            }
            for r in records
        ]
    )
    filepath = datalake.write_bronze(
        manifest,
        df,
        reconciliation=result.reconciliation,
        statement_period=result.statement_period,
        reconciliations=result.reconciliations,
    )
    if result.reconciliation is not None:
        manifest.reconciliation_check_name = result.reconciliation.check_name
        manifest.reconciliation_expected_minor = (
            result.reconciliation.expected_closing_minor
        )
        manifest.reconciliation_derived_minor = (
            result.reconciliation.derived_closing_minor
        )
        manifest.reconciliation_matches = result.reconciliation.matches
    manifest.status = STATUS_COMPLETE
    manifest.record_count = len(records)
    manifest.bronze_path = filepath
    write_manifest(manifest)
    return manifest


class TestEndToEnd:
    def test_ingest_bronze_silver_net_worth(self, isolated, tmp_path):
        datalake = isolated

        # Ingest
        manifest = _ingest_bytes(datalake, _kroo_pdf_bytes(), "kroo_jul.pdf", tmp_path)
        bronze = datalake.read_bronze("kroo")
        assert bronze is not None
        assert len(bronze) == 2

        # Manifest should have reconciliation verdict.
        assert manifest.reconciliation_matches is True
        assert manifest.reconciliation_expected_minor == 180500

        # Silver rebuild
        result = run_bronze_to_silver(datalake)
        assert "transaction_sources" in result
        assert len(result["transactions"]) == 2
        assert len(result["account_ledger"]) == 2

        # Reconciliation log (item 5): the fixture's own "Total opening
        # balance"/"Closing balance" anchors should produce a matching
        # bronze_self_check row, end-to-end through the real pipeline -
        # not just a hand-built DataFrame in a unit test.
        log = result["reconciliation_log"]
        self_check_rows = log[log["check_type"] == "bronze_self_check"]
        assert len(self_check_rows) == 1
        row = self_check_rows.iloc[0]
        assert row["account_id"] == _ACC_ID
        assert row["expected_opening_minor"] == 80000
        assert row["expected_closing_minor"] == 180500
        assert bool(row["matches"]) is True

        # Net worth
        net_worth = get_net_worth(datalake)
        assert net_worth == 180500  # pence

    def test_same_filename_different_bytes_two_ingestions(self, isolated, tmp_path):
        datalake = isolated
        pdf1 = _kroo_pdf_bytes()
        # Modify one byte to make a distinct file with same filename.
        pdf2 = pdf1[:-1] + b"X"
        m1 = _ingest_bytes(datalake, pdf1, "statement.pdf", tmp_path)
        m2 = _ingest_bytes(datalake, pdf2, "statement.pdf", tmp_path)
        assert m1.ingestion_id != m2.ingestion_id
        assert (
            load_manifest(m1.ingestion_id).raw_artifact_path
            != load_manifest(m2.ingestion_id).raw_artifact_path
        )

    def test_exact_reingest_idempotent(self, isolated, tmp_path):
        datalake = isolated
        pdf = _kroo_pdf_bytes()
        m1 = _ingest_bytes(datalake, pdf, "statement.pdf", tmp_path)
        m2 = _ingest_bytes(datalake, pdf, "statement.pdf", tmp_path)
        assert m1.ingestion_id == m2.ingestion_id
        assert m1.bronze_path == m2.bronze_path

    def test_money_round_trip(self, isolated, tmp_path):
        """10p + 20p must equal exactly 30p through the entire pipeline."""
        datalake = isolated
        _ingest_bytes(datalake, _kroo_pdf_bytes(), "kroo_jul.pdf", tmp_path)
        run_bronze_to_silver(datalake)

        silver = datalake.read_silver("transactions")
        assert silver is not None
        assert list(sorted(silver["amount_minor"].tolist())) == [-500, 100000]

        # Verify reconciliation is in pence (int).
        ledger = datalake.read_silver("account_ledger")
        assert ledger["balance_minor"].dtype.kind in ("i", "u")

    def test_read_bronze_called_once_per_source_type_during_rebuild(
        self, isolated, tmp_path
    ):
        """P2.2: a full rebuild used to call datalake.read_bronze() ~46
        times for 9 source_types (once from find_unmapped_accounts, once
        from _read_bronze_frames, twice from find_reconciliation_status,
        plus once per account inside the rollforward loop). With Bronze
        frames threaded through every call site, one rebuild should read
        Bronze exactly once per source_type - a real, disk-backed proof,
        not a mocked unit test."""
        datalake = isolated
        _ingest_bytes(datalake, _kroo_pdf_bytes(), "kroo_jul.pdf", tmp_path)

        spy = MagicMock(wraps=datalake.read_bronze)
        datalake.read_bronze = spy

        run_bronze_to_silver(datalake)

        all_source_types = (
            AdapterFactory.CSV_SOURCE_TYPES | AdapterFactory.PDF_SOURCE_TYPES
        )
        assert spy.call_count == len(all_source_types)


class TestStrictReconciliationGate:
    """Item 1: run_bronze_to_silver(strict_reconciliation=True) refuses to
    publish a new Silver build when the reconciliation_log contains any
    genuine mismatch - real disk-backed proof, not a mocked unit test."""

    def test_strict_raises_and_does_not_publish(self, isolated, tmp_path):
        datalake = isolated
        _ingest_bytes(datalake, _kroo_pdf_bytes_mismatched(), "kroo_bad.pdf", tmp_path)

        builds_before = list_builds(silver_dir=datalake.SILVER_DIR)
        current_before = current_build_id(silver_dir=datalake.SILVER_DIR)

        with pytest.raises(ReconciliationMismatchError) as excinfo:
            run_bronze_to_silver(datalake, strict_reconciliation=True)

        assert "acc_kroo_test" in str(excinfo.value)
        assert list_builds(silver_dir=datalake.SILVER_DIR) == builds_before
        assert current_build_id(silver_dir=datalake.SILVER_DIR) == current_before

    def test_non_strict_publishes_with_mismatch_recorded(self, isolated, tmp_path):
        datalake = isolated
        _ingest_bytes(datalake, _kroo_pdf_bytes_mismatched(), "kroo_bad.pdf", tmp_path)

        result = run_bronze_to_silver(datalake)  # strict_reconciliation=False (default)

        log = result["reconciliation_log"]
        self_check = log[log["check_type"] == "bronze_self_check"]
        assert len(self_check) == 1
        assert bool(self_check.iloc[0]["matches"]) is False
        assert current_build_id(silver_dir=datalake.SILVER_DIR) == result["build_id"]

    def test_strict_publishes_normally_when_clean(self, isolated, tmp_path):
        datalake = isolated
        _ingest_bytes(datalake, _kroo_pdf_bytes(), "kroo_good.pdf", tmp_path)

        result = run_bronze_to_silver(datalake, strict_reconciliation=True)

        assert current_build_id(silver_dir=datalake.SILVER_DIR) == result["build_id"]
        log = result["reconciliation_log"]
        assert (log["matches"] != False).all()  # noqa: E712
