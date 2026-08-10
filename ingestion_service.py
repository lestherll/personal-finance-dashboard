"""Bronze ingestion orchestration: raw file bytes -> archived ingestion
manifest -> parsed records -> published Bronze parquet.

Pure Python, no click/streamlit coupling - see cli.py's `ingest` command and
dashboard/app.py's Upload tab for the two renderers of IngestOutcome.
"""

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Union

import pandas as pd

from adapters.base import ReconciliationResult, StatementPeriod
from adapters.factory import AdapterDetectionError, AdapterFactory
from models.datalake import DataLake
from models.ingestion import (
    STATUS_BRONZE_FAILED,
    STATUS_COMPLETE,
    STATUS_PARSE_FAILED,
    IngestionManifest,
    start_ingestion,
    write_manifest,
)

STAGE_ALREADY_INGESTED = "already_ingested"
STAGE_DETECTION_FAILED = "detection_failed"
STAGE_PARSE_FAILED = "parse_failed"
STAGE_ZERO_RECORDS = "zero_records"
STAGE_BRONZE_FAILED = "bronze_failed"
STAGE_COMPLETE = "complete"

_FAILURE_STAGES = {
    STAGE_DETECTION_FAILED,
    STAGE_PARSE_FAILED,
    STAGE_ZERO_RECORDS,
    STAGE_BRONZE_FAILED,
}


@dataclass
class IngestOutcome:
    filename: str
    file_hash: str
    manifest: IngestionManifest
    stage: str
    error: Optional[Exception] = None
    reconciliation: Optional[ReconciliationResult] = None
    reconciliations: List[ReconciliationResult] = field(default_factory=list)
    statement_period: Optional[StatementPeriod] = None
    record_count: Optional[int] = None
    bronze_path: Optional[str] = None

    def is_failure(self) -> bool:
        """Same semantics as cli.py's former had_failure aggregation: true
        for every non-success stage, and also true for a STAGE_COMPLETE
        outcome whose reconciliation(s) mismatch. STAGE_ALREADY_INGESTED and
        an inconclusive (matches is None) reconciliation are never
        failures."""
        if self.stage in _FAILURE_STAGES:
            return True
        if self.reconciliation is not None and self.reconciliation.matches is False:
            return True
        return any(r.matches is False for r in self.reconciliations)


def ingest_file(
    path: Path,
    datalake: DataLake,
    factory: AdapterFactory,
    *,
    start_ingestion_fn: Callable[
        [Union[str, Path], str], IngestionManifest
    ] = start_ingestion,
    write_manifest_fn: Callable[[IngestionManifest], Path] = write_manifest,
) -> IngestOutcome:
    """Parse one statement file and publish it to Bronze.

    Content-addressed and idempotent: re-ingesting the same bytes returns a
    STAGE_ALREADY_INGESTED outcome without re-parsing or re-writing anything.
    """
    raw_bytes = path.read_bytes()
    file_hash = hashlib.sha256(raw_bytes).hexdigest()
    manifest = start_ingestion_fn(path, file_hash)
    if manifest.status == STATUS_COMPLETE:
        return IngestOutcome(
            filename=path.name,
            file_hash=file_hash,
            manifest=manifest,
            stage=STAGE_ALREADY_INGESTED,
        )

    content = (
        raw_bytes.decode("utf-8-sig") if path.suffix.lower() == ".csv" else raw_bytes
    )

    try:
        result = factory.ingest(content, path.name, file_hash)
    except AdapterDetectionError as e:
        manifest.status = STATUS_PARSE_FAILED
        manifest.error = str(e)
        write_manifest_fn(manifest)
        return IngestOutcome(
            filename=path.name,
            file_hash=file_hash,
            manifest=manifest,
            stage=STAGE_DETECTION_FAILED,
            error=e,
        )
    except ValueError as e:
        manifest.status = STATUS_PARSE_FAILED
        manifest.error = str(e)
        write_manifest_fn(manifest)
        return IngestOutcome(
            filename=path.name,
            file_hash=file_hash,
            manifest=manifest,
            stage=STAGE_PARSE_FAILED,
            error=e,
        )

    records = result.records
    manifest.source_type = result.source_type or records[0].source_type
    manifest.adapter = result.adapter or "test/legacy-adapter"
    manifest.parser_version = result.parser_version
    if not records:
        manifest.status = STATUS_PARSE_FAILED
        manifest.error = "Adapter parsed zero records"
        write_manifest_fn(manifest)
        return IngestOutcome(
            filename=path.name,
            file_hash=file_hash,
            manifest=manifest,
            stage=STAGE_ZERO_RECORDS,
        )

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
    try:
        filepath = datalake.write_bronze(
            manifest,
            df,
            reconciliation=result.reconciliation,
            statement_period=result.statement_period,
            reconciliations=result.reconciliations,
        )
    except Exception as e:
        manifest.status = STATUS_BRONZE_FAILED
        manifest.error = str(e)
        write_manifest_fn(manifest)
        return IngestOutcome(
            filename=path.name,
            file_hash=file_hash,
            manifest=manifest,
            stage=STAGE_BRONZE_FAILED,
            error=e,
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
    if result.reconciliations:
        manifest.reconciliations = [
            {
                "check_name": r.check_name,
                "expected_closing_minor": r.expected_closing_minor,
                "derived_closing_minor": r.derived_closing_minor,
                "matches": r.matches,
                "account_identifier": r.account_identifier,
            }
            for r in result.reconciliations
        ]

    manifest.status = STATUS_COMPLETE
    manifest.record_count = len(records)
    manifest.bronze_path = filepath
    manifest.error = None
    write_manifest_fn(manifest)

    return IngestOutcome(
        filename=path.name,
        file_hash=file_hash,
        manifest=manifest,
        stage=STAGE_COMPLETE,
        reconciliation=result.reconciliation,
        reconciliations=result.reconciliations,
        statement_period=result.statement_period,
        record_count=len(records),
        bronze_path=filepath,
    )
