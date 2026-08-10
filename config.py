"""Configuration for the personal finance system."""

import os
from pathlib import Path
from typing import Any

# Environment
ENV = os.getenv("ENV", "development")
DEBUG = ENV == "development"

# Data Lake Paths (file-based with Parquet + DuckDB)
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
RAW_DIR = (
    DATA_DIR / "raw"
)  # Original uploaded statement files (PDF/CSV), by source_type
INGESTIONS_DIR = DATA_DIR / "ingestions"  # One manifest per raw artifact
BRONZE_DIR = DATA_DIR / "bronze"  # Raw CSV uploads (immutable)
SILVER_DIR = DATA_DIR / "silver"  # Normalized parquet files
GOLD_DIR = DATA_DIR / "gold"  # Enriched parquet files

# DuckDB (in-process query engine, no server needed)
DUCKDB_PATH = os.getenv("DUCKDB_PATH", str(DATA_DIR / "personal_finance.duckdb"))

# User-editable data: hashed account_identifier -> canonical account mapping.
# Lives in the data store, not source code - see transformers/account_config.py.
ACCOUNT_MAP_PATH = Path(
    os.getenv("ACCOUNT_MAP_PATH", str(DATA_DIR / "account_map.json"))
)

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Object storage (SeaweedFS via the platform's ObjectStorage/Application
# attachment, alias "data" - see homelab's docs/platform-api-usage.md).
# AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY are deliberately NOT read here:
# those are boto3's own default credential-chain env var names, and the
# platform suffixes every attachment's vars with its alias so a second
# bucket attachment never collides with the first - so they must be passed
# to the client explicitly rather than picked up automatically. None of
# these are required at import time; a dev machine or an app with no
# ObjectStorage attachment simply gets None for all four and
# check_s3_connectivity() reports "not configured".
S3_ENDPOINT = os.getenv("S3_ENDPOINT_DATA")
S3_BUCKET = os.getenv("S3_BUCKET_DATA")
S3_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID_DATA")
S3_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY_DATA")
# The exact key prefix our credentials are scoped to (the platform's
# isolation policy denies everything outside it, including a bare
# HeadBucket/ListBucket with no prefix - confirmed live, that's the
# isolation working as designed). check_s3_connectivity() needs this to
# probe a call our own credentials actually cover.
S3_PREFIX = os.getenv("S3_PREFIX_DATA")

# Redis (Celery broker for task orchestration)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Celery Configuration
CELERY_CONFIG: dict[str, Any] = {
    "broker_url": REDIS_URL,
    "result_backend": REDIS_URL,
    "broker_connection_retry_on_startup": True,
    "task_serializer": "json",
    "accept_content": ["json"],
    "result_serializer": "json",
    "timezone": "UTC",
    "enable_utc": True,
    "task_track_started": True,
    "task_time_limit": 30 * 60,  # 30 minutes hard limit
    "task_soft_time_limit": 25 * 60,  # 25 minutes soft limit
}

# Create directories if they don't exist
for directory in [RAW_DIR, INGESTIONS_DIR, BRONZE_DIR, SILVER_DIR, GOLD_DIR]:
    directory.mkdir(parents=True, exist_ok=True)
