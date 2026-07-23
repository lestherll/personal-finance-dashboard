"""Configuration for the personal finance system."""

import os
from pathlib import Path
from typing import Any

# Environment
ENV = os.getenv("ENV", "development")
DEBUG = ENV == "development"

# Data Lake Paths (file-based with Parquet + DuckDB)
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
BRONZE_DIR = DATA_DIR / "bronze"  # Raw CSV uploads (immutable)
SILVER_DIR = DATA_DIR / "silver"  # Normalized parquet files
GOLD_DIR = DATA_DIR / "gold"  # Enriched parquet files

# DuckDB (in-process query engine, no server needed)
DUCKDB_PATH = os.getenv("DUCKDB_PATH", str(DATA_DIR / "personal_finance.duckdb"))

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

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
for directory in [BRONZE_DIR, SILVER_DIR, GOLD_DIR]:
    directory.mkdir(parents=True, exist_ok=True)
