"""Versioned Silver build management with atomic publication.

Each build lives in data/silver/builds/<build_id>/ with all .parquet
tables plus a build.json manifest. The data/silver/current symlink
points to the active build. Publication is atomic: build into a
staging directory, validate, then os.replace() the symlink.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import pyarrow.parquet as pq

import pyarrow as pa
from config import SILVER_DIR as _DEFAULT_SILVER_DIR

logger = logging.getLogger(__name__)


def _builds_dir(silver_dir: Path) -> Path:
    return silver_dir / "builds"


def _current_link(silver_dir: Path) -> Path:
    return silver_dir / "current"


BUILDS_DIR = _builds_dir(_DEFAULT_SILVER_DIR)
CURRENT_LINK = _current_link(_DEFAULT_SILVER_DIR)
KEEP_BUILDS = 2


def _git_sha() -> Optional[str]:
    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


@dataclass
class BuildManifest:
    build_id: str
    built_at: str
    git_sha: Optional[str] = None
    input_ingestion_ids: List[str] = field(default_factory=list)
    excluded_ingestions: List[Dict[str, str]] = field(default_factory=list)
    parser_versions: Dict[str, str] = field(default_factory=dict)
    row_counts: Dict[str, int] = field(default_factory=dict)


def publish_silver_build(
    tables: Dict[str, pd.DataFrame],
    input_ingestion_ids: List[str],
    excluded_ingestions: Optional[List[Dict[str, str]]] = None,
    parser_versions: Optional[Dict[str, str]] = None,
    build_id: Optional[str] = None,
    silver_dir: Optional[Path] = None,
) -> str:
    """Write all Silver tables to a versioned build directory, validate,
    and atomically swap the current/ symlink.

    Returns the build_id of the newly published build.
    """
    silver_dir = silver_dir or _DEFAULT_SILVER_DIR
    now = datetime.now(timezone.utc)
    if build_id is None:
        timestamp = now.strftime("%Y%m%d-%H%M%S")
        git = _git_sha()
        build_id = f"{timestamp}-{git}" if git else timestamp

    build_dir = _builds_dir(silver_dir) / build_id
    build_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(
        tempfile.mkdtemp(
            prefix=f".build-{build_id}-", dir=build_dir.parent
        )
    )
    current_link = _current_link(silver_dir)

    try:
        staging_dir.mkdir(parents=True, exist_ok=True)

        row_counts: Dict[str, int] = {}

        for entity_type, df in tables.items():
            if df is None or (isinstance(df, pd.DataFrame) and df.empty):
                row_counts[entity_type] = 0
                continue

            filepath = staging_dir / f"{entity_type}.parquet"
            table = pa.Table.from_pandas(df)
            pq.write_table(table, str(filepath))

            # Validate by reading back.
            _ = pq.read_table(str(filepath))
            row_counts[entity_type] = len(df)

        # Write build manifest.
        manifest = BuildManifest(
            build_id=build_id,
            built_at=now.isoformat(),
            git_sha=_git_sha(),
            input_ingestion_ids=input_ingestion_ids,
            excluded_ingestions=excluded_ingestions or [],
            parser_versions=parser_versions or {},
            row_counts=row_counts,
        )
        manifest_path = staging_dir / "build.json"
        manifest_path.write_text(
            json.dumps(asdict(manifest), indent=2, sort_keys=True)
        )

        # Atomic publish: rename staging to final, then swap symlink.
        # Final path must not exist (build_id is unique).
        os.rename(str(staging_dir), str(build_dir))
        staging_dir = Path("")  # prevent cleanup of already-renamed dir

        # Swap the current/ symlink atomically.
        absolute_build_dir = build_dir.resolve()
        if current_link.is_symlink() or current_link.exists():
            tmp_link = current_link.with_suffix(".tmp")
            if tmp_link.exists():
                tmp_link.unlink()
            tmp_link.symlink_to(str(absolute_build_dir), target_is_directory=False)
            os.replace(str(tmp_link), str(current_link))
        else:
            current_link.symlink_to(str(absolute_build_dir), target_is_directory=False)

        # Prune old builds.
        _prune_old_builds(silver_dir)

        logger.info(
            "Published Silver build %s: %d tables, %d total rows",
            build_id,
            len(tables),
            sum(row_counts.values()),
        )
        return build_id

    finally:
        if staging_dir and staging_dir != Path("") and staging_dir.exists():
            import shutil

            shutil.rmtree(str(staging_dir), ignore_errors=True)


def _prune_old_builds(silver_dir: Path) -> None:
    builds_dir = _builds_dir(silver_dir)
    if not builds_dir.exists():
        return
    builds = sorted(
        [d for d in builds_dir.iterdir() if d.is_dir()],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    for old in builds[KEEP_BUILDS:]:
        import shutil

        shutil.rmtree(str(old), ignore_errors=True)
        logger.debug("Pruned old Silver build: %s", old.name)


def list_builds(silver_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Return a list of known builds with summary info, newest first."""
    silver_dir = silver_dir or _DEFAULT_SILVER_DIR
    builds_dir = _builds_dir(silver_dir)
    if not builds_dir.exists():
        return []
    builds = []
    for d in sorted(
        builds_dir.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True
    ):
        if not d.is_dir():
            continue
        manifest_path = d / "build.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text())
            except json.JSONDecodeError:
                manifest = {"build_id": d.name, "error": "unparseable manifest"}
        else:
            manifest = {"build_id": d.name}
        builds.append(manifest)
    return builds


def current_build_id(silver_dir: Optional[Path] = None) -> Optional[str]:
    silver_dir = silver_dir or _DEFAULT_SILVER_DIR
    current_link = _current_link(silver_dir)
    if current_link.is_symlink():
        target = os.readlink(str(current_link))
        return Path(target).name
    return None
