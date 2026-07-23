"""Data lake utilities for Parquet files and DuckDB queries."""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb
import pandas as pd
import pyarrow.parquet as pq
from config import BRONZE_DIR, DUCKDB_PATH, GOLD_DIR, SILVER_DIR

logger = logging.getLogger(__name__)


class DataLake:
    """File-based data lake with DuckDB query engine."""

    def __init__(self, db_path: str = DUCKDB_PATH):
        """Initialize DuckDB connection."""
        self.db_path = db_path
        self.conn = duckdb.connect(db_path)
        logger.info(f"DuckDB initialized at {db_path}")

    def write_bronze(self, source_type: str, filename: str, df: pd.DataFrame) -> str:
        """
        Write raw records to Bronze layer (immutable).

        Args:
            source_type: 'monzo', 'natwest', 'vanguard'
            filename: Original filename
            df: DataFrame with raw data

        Returns:
            Path to written Parquet file
        """
        filepath = BRONZE_DIR / source_type / f"{filename}.parquet"
        filepath.parent.mkdir(parents=True, exist_ok=True)

        # Add metadata columns
        df["bronze_source_key"] = df.get("source_key", "")
        df["source_type"] = source_type
        df["upload_timestamp"] = pd.Timestamp.now()
        df["filename"] = filename

        # Write Parquet (immutable, append-only)
        table = pa.Table.from_pandas(df)
        pq.write_table(table, filepath, filesystem=None)

        logger.info(f"✓ Wrote {len(df)} records to Bronze: {filepath}")
        return str(filepath)

    def write_silver(self, entity_type: str, df: pd.DataFrame) -> str:
        """
        Write normalized records to Silver layer.

        Args:
            entity_type: 'transactions', 'accounts', 'holdings', 'account_ledger'
            df: DataFrame with normalized data

        Returns:
            Path to written Parquet file
        """
        filepath = SILVER_DIR / f"{entity_type}.parquet"
        filepath.parent.mkdir(parents=True, exist_ok=True)

        table = pa.Table.from_pandas(df)
        pq.write_table(table, filepath)

        logger.info(f"✓ Wrote {len(df)} records to Silver ({entity_type}): {filepath}")
        return str(filepath)

    def write_gold(self, entity_type: str, df: pd.DataFrame) -> str:
        """
        Write enriched records to Gold layer.

        Args:
            entity_type: 'transactions', 'subscriptions', 'transfers', 'snapshots'
            df: DataFrame with enriched data

        Returns:
            Path to written Parquet file
        """
        filepath = GOLD_DIR / f"{entity_type}.parquet"
        filepath.parent.mkdir(parents=True, exist_ok=True)

        table = pa.Table.from_pandas(df)
        pq.write_table(table, filepath)

        logger.info(f"✓ Wrote {len(df)} records to Gold ({entity_type}): {filepath}")
        return str(filepath)

    def read_bronze(self, source_type: str) -> Optional[pd.DataFrame]:
        """
        Read all Bronze records for a source type.

        Args:
            source_type: 'monzo', 'natwest', 'vanguard'

        Returns:
            DataFrame or None if not found
        """
        bronze_path = BRONZE_DIR / source_type
        if not bronze_path.exists():
            return None

        # Union all Parquet files in the directory
        files = list(bronze_path.glob("*.parquet"))
        if not files:
            return None

        dfs = [pd.read_parquet(f) for f in files]
        return pd.concat(dfs, ignore_index=True)

    def read_silver(self, entity_type: str) -> Optional[pd.DataFrame]:
        """
        Read Silver records.

        Args:
            entity_type: 'transactions', 'accounts', 'holdings', 'account_ledger'

        Returns:
            DataFrame or None if not found
        """
        filepath = SILVER_DIR / f"{entity_type}.parquet"
        if not filepath.exists():
            return None
        return pd.read_parquet(filepath)

    def read_gold(self, entity_type: str) -> Optional[pd.DataFrame]:
        """
        Read Gold records.

        Args:
            entity_type: 'transactions', 'subscriptions', 'transfers', 'snapshots'

        Returns:
            DataFrame or None if not found
        """
        filepath = GOLD_DIR / f"{entity_type}.parquet"
        if not filepath.exists():
            return None
        return pd.read_parquet(filepath)

    def query(self, sql: str) -> pd.DataFrame:
        """
        Execute SQL query using DuckDB.

        Can query across Bronze/Silver/Gold Parquet files directly.

        Example:
            df = datalake.query('''
                SELECT * FROM read_parquet('data/silver/transactions.parquet')
                WHERE transaction_date >= '2024-01-01'
            ''')
        """
        return self.conn.execute(sql).df()

    def close(self):
        """Close DuckDB connection."""
        self.conn.close()
        logger.info("DuckDB connection closed")


# Singleton instance
_datalake: Optional[DataLake] = None


def get_datalake() -> DataLake:
    """Get or create the DataLake singleton."""
    global _datalake
    if _datalake is None:
        _datalake = DataLake()
    return _datalake


# Re-export for convenience
import pyarrow as pa
