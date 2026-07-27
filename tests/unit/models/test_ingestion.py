"""Tests for content-addressed artifacts and immutable Bronze publication."""

import hashlib

import pandas as pd
import pytest

from models.datalake import DataLake
from models.ingestion import STATUS_ARCHIVED, load_manifest, start_ingestion


@pytest.fixture
def isolated_storage(tmp_path, monkeypatch):
    raw_dir = tmp_path / "raw"
    manifests_dir = tmp_path / "ingestions"
    bronze_dir = tmp_path / "bronze"
    monkeypatch.setattr("models.ingestion.RAW_DIR", raw_dir)
    monkeypatch.setattr("models.ingestion.INGESTIONS_DIR", manifests_dir)
    monkeypatch.setattr("models.datalake.BRONZE_DIR", bronze_dir)
    datalake = DataLake(db_path=str(tmp_path / "test.duckdb"))
    yield datalake, raw_dir, manifests_dir, bronze_dir
    datalake.close()


def _manifest_for(path, content):
    ingestion_id = hashlib.sha256(content).hexdigest()
    manifest = start_ingestion(path, ingestion_id)
    manifest.source_type = "monzo"
    manifest.adapter = "MonzoAdapter"
    manifest.parser_version = "1"
    return manifest


def _bronze_frame():
    return pd.DataFrame(
        [
            {
                "source_key": "monzo_txn_1",
                "raw_data": {"Transaction ID": "1", "Amount": "1.23"},
                "account_identifier": None,
                "record_type": "transaction",
                "file_hash": "ignored-by-new-identity",
                "line_number": 2,
            }
        ]
    )


def test_same_filename_with_different_bytes_creates_two_immutable_ingestions(
    tmp_path, isolated_storage
):
    datalake, raw_dir, manifests_dir, bronze_dir = isolated_storage
    first = tmp_path / "first" / "statement.pdf"
    second = tmp_path / "second" / "statement.pdf"
    first.parent.mkdir()
    second.parent.mkdir()
    first_bytes = b"first statement"
    second_bytes = b"second statement"
    first.write_bytes(first_bytes)
    second.write_bytes(second_bytes)

    first_manifest = _manifest_for(first, first_bytes)
    second_manifest = _manifest_for(second, second_bytes)
    first_path = datalake.write_bronze(first_manifest, _bronze_frame())
    second_path = datalake.write_bronze(second_manifest, _bronze_frame())

    assert first_path != second_path
    assert len(list((bronze_dir / "monzo").glob("*.parquet"))) == 2
    assert len(list((raw_dir / "sha256").rglob("*.pdf"))) == 2
    assert len(list(manifests_dir.glob("*.json"))) == 2


def test_exact_repeat_is_idempotent(tmp_path, isolated_storage):
    datalake, _, _, bronze_dir = isolated_storage
    statement = tmp_path / "statement.pdf"
    content = b"same statement"
    statement.write_bytes(content)

    first = _manifest_for(statement, content)
    first_path = datalake.write_bronze(first, _bronze_frame())
    second = _manifest_for(statement, content)
    second_path = datalake.write_bronze(second, _bronze_frame())

    assert first_path == second_path
    assert len(list((bronze_dir / "monzo").glob("*.parquet"))) == 1
    assert load_manifest(first.ingestion_id).status == STATUS_ARCHIVED


def test_failed_bronze_write_never_publishes_a_partial_file(
    tmp_path, isolated_storage, monkeypatch
):
    datalake, _, _, bronze_dir = isolated_storage
    statement = tmp_path / "statement.pdf"
    content = b"bad parquet write"
    statement.write_bytes(content)
    manifest = _manifest_for(statement, content)

    monkeypatch.setattr(
        "models.datalake.pq.write_table",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        datalake.write_bronze(manifest, _bronze_frame())

    assert not (bronze_dir / "monzo" / f"{manifest.ingestion_id}.parquet").exists()
