"""Tests for ingestion_service.ingest_file / IngestOutcome."""

import hashlib
from datetime import datetime
from pathlib import Path

import pytest

from adapters.base import RawRecord, ReconciliationResult, StatementPeriod
from adapters.factory import (
    AmbiguousFormatError,
    IngestResult,
    UnrecognizedFormatError,
)
from ingestion_service import (
    STAGE_ALREADY_INGESTED,
    STAGE_BRONZE_FAILED,
    STAGE_COMPLETE,
    STAGE_DETECTION_FAILED,
    STAGE_PARSE_FAILED,
    STAGE_ZERO_RECORDS,
    ingest_file,
)
from models.ingestion import STATUS_ARCHIVED, STATUS_COMPLETE, IngestionManifest


def _make_record(source_type="amex", filename="statement.pdf"):
    return RawRecord(
        source_key=f"{source_type}_key1",
        source_type=source_type,
        raw_data={"date": "01 Jan 2026", "description": "Test", "amount": -10.0},
        filename=filename,
        file_hash="hash123",
        upload_timestamp=datetime(2026, 1, 1),
        line_number=1,
    )


def _make_manifest(path: Path, file_hash: str, status: str = STATUS_ARCHIVED):
    return IngestionManifest(
        ingestion_id=file_hash,
        original_filename=path.name,
        raw_artifact_path=f"/fake/raw/{file_hash}{path.suffix}",
        status=status,
        created_at="2026-01-01T00:00:00+00:00",
    )


class _FakeDatalake:
    """Stand-in for get_datalake(): records write_bronze calls for assertion."""

    def __init__(self, raise_on_write=None):
        self.write_calls = []
        self._raise_on_write = raise_on_write

    def write_bronze(
        self,
        ingestion,
        df,
        reconciliation=None,
        statement_period=None,
        reconciliations=None,
    ):
        if self._raise_on_write is not None:
            raise self._raise_on_write
        self.write_calls.append(
            {
                "source_type": ingestion.source_type,
                "filename": ingestion.original_filename,
                "reconciliation": reconciliation,
                "statement_period": statement_period,
                "reconciliations": reconciliations or [],
            }
        )
        return f"/fake/bronze/{ingestion.source_type}/{ingestion.ingestion_id}.parquet"


class _FakeFactory:
    """Stand-in for AdapterFactory: canned IngestResult/exception, and
    records whether .ingest() was called at all (to assert the
    already-ingested short-circuit never reaches the factory)."""

    def __init__(self, outcome):
        self._outcome = outcome
        self.called = False

    def ingest(self, content, filename, file_hash):
        self.called = True
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


@pytest.fixture
def manifests():
    """Backing store shared by start_ingestion_fn/write_manifest_fn fakes,
    keyed by ingestion_id - mirrors the real start_ingestion/write_manifest
    contract closely enough for these unit tests."""
    return {}


def _start_ingestion_fn(manifests):
    def start(path, ingestion_id):
        existing = manifests.get(ingestion_id)
        if existing is not None:
            return existing
        manifest = _make_manifest(path, ingestion_id)
        manifests[ingestion_id] = manifest
        return manifest

    return start


def _write_manifest_fn(calls):
    def write(manifest):
        calls.append(manifest)
        return Path(f"/fake/ingestions/{manifest.ingestion_id}.json")

    return write


def test_already_ingested_short_circuits(tmp_path, manifests):
    pdf_path = tmp_path / "statement.pdf"
    pdf_path.write_bytes(b"%PDF-fake")
    file_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    manifests[file_hash] = _make_manifest(pdf_path, file_hash, status=STATUS_COMPLETE)

    factory = _FakeFactory(
        IngestResult(
            records=[_make_record()], reconciliation=None, statement_period=None
        )
    )
    write_calls = []

    outcome = ingest_file(
        pdf_path,
        _FakeDatalake(),
        factory,
        start_ingestion_fn=_start_ingestion_fn(manifests),
        write_manifest_fn=_write_manifest_fn(write_calls),
    )

    assert outcome.stage == STAGE_ALREADY_INGESTED
    assert outcome.is_failure() is False
    assert factory.called is False
    assert write_calls == []


def test_reconciliation_match_and_period(tmp_path, manifests):
    pdf_path = tmp_path / "statement.pdf"
    pdf_path.write_bytes(b"%PDF-fake")

    result = IngestResult(
        records=[_make_record()],
        reconciliation=ReconciliationResult(
            check_name="amex_closing_balance",
            expected_closing_minor=86304,
            derived_closing_minor=86304,
            matches=True,
        ),
        statement_period=StatementPeriod(datetime(2026, 6, 20), datetime(2026, 7, 19)),
    )
    datalake = _FakeDatalake()

    outcome = ingest_file(
        pdf_path,
        datalake,
        _FakeFactory(result),
        start_ingestion_fn=_start_ingestion_fn(manifests),
        write_manifest_fn=_write_manifest_fn([]),
    )

    assert outcome.stage == STAGE_COMPLETE
    assert outcome.is_failure() is False
    assert outcome.reconciliation.matches is True
    assert outcome.statement_period.from_date == datetime(2026, 6, 20)
    assert outcome.record_count == 1
    assert datalake.write_calls[0]["reconciliation"] is result.reconciliation


def test_reconciliation_mismatch_is_failure(tmp_path, manifests):
    pdf_path = tmp_path / "statement.pdf"
    pdf_path.write_bytes(b"%PDF-fake")

    result = IngestResult(
        records=[_make_record()],
        reconciliation=ReconciliationResult(
            check_name="amex_closing_balance",
            expected_closing_minor=86304,
            derived_closing_minor=67804,
            matches=False,
        ),
        statement_period=None,
    )

    outcome = ingest_file(
        pdf_path,
        _FakeDatalake(),
        _FakeFactory(result),
        start_ingestion_fn=_start_ingestion_fn(manifests),
        write_manifest_fn=_write_manifest_fn([]),
    )

    assert outcome.stage == STAGE_COMPLETE
    assert outcome.is_failure() is True


def test_matches_none_is_not_a_failure(tmp_path, manifests):
    pdf_path = tmp_path / "statement.pdf"
    pdf_path.write_bytes(b"%PDF-fake")

    result = IngestResult(
        records=[_make_record()],
        reconciliation=ReconciliationResult(
            check_name="amex_closing_balance",
            expected_closing_minor=None,
            derived_closing_minor=None,
            matches=None,
        ),
        statement_period=None,
    )

    outcome = ingest_file(
        pdf_path,
        _FakeDatalake(),
        _FakeFactory(result),
        start_ingestion_fn=_start_ingestion_fn(manifests),
        write_manifest_fn=_write_manifest_fn([]),
    )

    assert outcome.is_failure() is False


def test_one_of_several_reconciliations_mismatching_is_failure(tmp_path, manifests):
    pdf_path = tmp_path / "statement.pdf"
    pdf_path.write_bytes(b"%PDF-fake")

    result = IngestResult(
        records=[_make_record(source_type="vanguard-pdf")],
        reconciliation=None,
        statement_period=None,
        reconciliations=[
            ReconciliationResult(
                check_name="vanguard_account_summary_isa",
                expected_closing_minor=99999,
                derived_closing_minor=50015,
                matches=False,
                account_identifier="hash_isa",
            ),
            ReconciliationResult(
                check_name="vanguard_account_summary_pension",
                expected_closing_minor=50100,
                derived_closing_minor=50100,
                matches=True,
                account_identifier="hash_pension",
            ),
        ],
    )

    outcome = ingest_file(
        pdf_path,
        _FakeDatalake(),
        _FakeFactory(result),
        start_ingestion_fn=_start_ingestion_fn(manifests),
        write_manifest_fn=_write_manifest_fn([]),
    )

    assert outcome.is_failure() is True
    assert outcome.reconciliations == result.reconciliations


def test_unrecognized_format(tmp_path, manifests):
    bad_path = tmp_path / "unknown.pdf"
    bad_path.write_bytes(b"garbage")
    error = UnrecognizedFormatError("PDF", "amex, kroo")
    write_calls = []

    outcome = ingest_file(
        bad_path,
        _FakeDatalake(),
        _FakeFactory(error),
        start_ingestion_fn=_start_ingestion_fn(manifests),
        write_manifest_fn=_write_manifest_fn(write_calls),
    )

    assert outcome.stage == STAGE_DETECTION_FAILED
    assert outcome.error is error
    assert outcome.is_failure() is True
    assert outcome.manifest.status == "parse_failed"
    assert len(write_calls) == 1


def test_ambiguous_format(tmp_path, manifests):
    bad_path = tmp_path / "ambiguous.pdf"
    bad_path.write_bytes(b"garbage")
    error = AmbiguousFormatError("amex", 0.95, "kroo", 0.95)

    outcome = ingest_file(
        bad_path,
        _FakeDatalake(),
        _FakeFactory(error),
        start_ingestion_fn=_start_ingestion_fn(manifests),
        write_manifest_fn=_write_manifest_fn([]),
    )

    assert outcome.stage == STAGE_DETECTION_FAILED
    assert outcome.is_failure() is True


def test_generic_parse_valueerror(tmp_path, manifests):
    bad_path = tmp_path / "broken.pdf"
    bad_path.write_bytes(b"garbage")
    error = ValueError("Failed to parse PDF: something broke")

    outcome = ingest_file(
        bad_path,
        _FakeDatalake(),
        _FakeFactory(error),
        start_ingestion_fn=_start_ingestion_fn(manifests),
        write_manifest_fn=_write_manifest_fn([]),
    )

    assert outcome.stage == STAGE_PARSE_FAILED
    assert outcome.is_failure() is True


def test_zero_records(tmp_path, manifests):
    pdf_path = tmp_path / "empty.pdf"
    pdf_path.write_bytes(b"%PDF-fake")
    # Real AdapterFactory.ingest() always sets source_type from the detected
    # adapter, even when the adapter parses zero records - set it explicitly
    # here too, since an empty records list AND an empty source_type is a
    # combination the real factory never produces.
    result = IngestResult(
        records=[], reconciliation=None, statement_period=None, source_type="amex"
    )

    outcome = ingest_file(
        pdf_path,
        _FakeDatalake(),
        _FakeFactory(result),
        start_ingestion_fn=_start_ingestion_fn(manifests),
        write_manifest_fn=_write_manifest_fn([]),
    )

    assert outcome.stage == STAGE_ZERO_RECORDS
    assert outcome.is_failure() is True
    assert outcome.manifest.error == "Adapter parsed zero records"


def test_bronze_write_failure(tmp_path, manifests):
    pdf_path = tmp_path / "statement.pdf"
    pdf_path.write_bytes(b"%PDF-fake")
    result = IngestResult(
        records=[_make_record()], reconciliation=None, statement_period=None
    )
    datalake = _FakeDatalake(raise_on_write=RuntimeError("disk full"))

    outcome = ingest_file(
        pdf_path,
        datalake,
        _FakeFactory(result),
        start_ingestion_fn=_start_ingestion_fn(manifests),
        write_manifest_fn=_write_manifest_fn([]),
    )

    assert outcome.stage == STAGE_BRONZE_FAILED
    assert outcome.is_failure() is True
    assert outcome.manifest.status == "bronze_failed"


def test_adapter_fallback_string_used_when_result_adapter_empty(tmp_path, manifests):
    """Regression guard: the `manifest.adapter = result.adapter or
    'test/legacy-adapter'` fallback from cli.py's original loop must survive
    the extraction into ingest_file."""
    pdf_path = tmp_path / "statement.pdf"
    pdf_path.write_bytes(b"%PDF-fake")
    result = IngestResult(
        records=[_make_record()],
        reconciliation=None,
        statement_period=None,
        adapter=None,
    )

    outcome = ingest_file(
        pdf_path,
        _FakeDatalake(),
        _FakeFactory(result),
        start_ingestion_fn=_start_ingestion_fn(manifests),
        write_manifest_fn=_write_manifest_fn([]),
    )

    assert outcome.manifest.adapter == "test/legacy-adapter"


def test_manifest_error_reset_to_none_on_success(tmp_path, manifests):
    """Regression guard: a manifest re-used across a previously-failed
    attempt must have its .error field cleared on eventual success."""
    pdf_path = tmp_path / "statement.pdf"
    pdf_path.write_bytes(b"%PDF-fake")

    def start(path, ingestion_id):
        manifest = manifests.get(ingestion_id)
        if manifest is None:
            manifest = _make_manifest(path, ingestion_id)
            manifest.error = "previous attempt failed"
            manifests[ingestion_id] = manifest
        return manifest

    result = IngestResult(
        records=[_make_record()], reconciliation=None, statement_period=None
    )

    outcome = ingest_file(
        pdf_path,
        _FakeDatalake(),
        _FakeFactory(result),
        start_ingestion_fn=start,
        write_manifest_fn=_write_manifest_fn([]),
    )

    assert outcome.stage == STAGE_COMPLETE
    assert outcome.manifest.error is None
