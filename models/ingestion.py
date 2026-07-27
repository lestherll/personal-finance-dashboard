"""Immutable raw-artifact storage and ingestion manifests.

An ingestion is identified by the SHA-256 hash of the original uploaded
bytes.  Its raw artifact is content-addressed and never overwritten; the
manifest is mutable operational metadata describing how far that ingestion
progressed through parsing and Bronze publication.
"""

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

from config import INGESTIONS_DIR, RAW_DIR

PathLike = Union[str, Path]

STATUS_ARCHIVED = "archived"
STATUS_PARSE_FAILED = "parse_failed"
STATUS_BRONZE_FAILED = "bronze_failed"
STATUS_COMPLETE = "complete"


@dataclass
class IngestionManifest:
    ingestion_id: str
    original_filename: str
    raw_artifact_path: str
    status: str
    created_at: str
    source_type: Optional[str] = None
    adapter: Optional[str] = None
    parser_version: Optional[str] = None
    record_count: Optional[int] = None
    bronze_path: Optional[str] = None
    error: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> "IngestionManifest":
        return cls(**data)


def _atomic_write_bytes(destination: Path, content: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temp:
        temp.write(content)
        temp_path = Path(temp.name)
    try:
        os.replace(temp_path, destination)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def raw_artifact_path(ingestion_id: str, original_filename: str) -> Path:
    suffix = Path(original_filename).suffix.lower()
    return RAW_DIR / "sha256" / ingestion_id[:2] / f"{ingestion_id}{suffix}"


def manifest_path(ingestion_id: str) -> Path:
    return INGESTIONS_DIR / f"{ingestion_id}.json"


def load_manifest(ingestion_id: str) -> Optional[IngestionManifest]:
    path = manifest_path(ingestion_id)
    if not path.exists():
        return None
    return IngestionManifest.from_dict(json.loads(path.read_text()))


def write_manifest(manifest: IngestionManifest) -> Path:
    path = manifest_path(manifest.ingestion_id)
    payload = (json.dumps(asdict(manifest), indent=2, sort_keys=True) + "\n").encode()
    _atomic_write_bytes(path, payload)
    return path


def archive_raw_artifact(path: PathLike, ingestion_id: str) -> Path:
    """Content-address a raw artifact, verifying any existing destination."""
    source = Path(path)
    destination = raw_artifact_path(ingestion_id, source.name)
    if destination.exists():
        existing_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
        if existing_hash != ingestion_id:
            raise ValueError(f"Raw artifact hash mismatch at {destination}")
        return destination

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temp:
        temp_path = Path(temp.name)
    try:
        shutil.copyfile(source, temp_path)
        copied_hash = hashlib.sha256(temp_path.read_bytes()).hexdigest()
        if copied_hash != ingestion_id:
            raise ValueError(f"Raw artifact changed while being archived: {source}")
        os.replace(temp_path, destination)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return destination


def start_ingestion(path: PathLike, ingestion_id: str) -> IngestionManifest:
    """Archive raw bytes and create/reuse the ingestion manifest."""
    existing = load_manifest(ingestion_id)
    if existing is not None:
        return existing

    source = Path(path)
    artifact = archive_raw_artifact(source, ingestion_id)
    manifest = IngestionManifest(
        ingestion_id=ingestion_id,
        original_filename=source.name,
        raw_artifact_path=str(artifact),
        status=STATUS_ARCHIVED,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    write_manifest(manifest)
    return manifest
