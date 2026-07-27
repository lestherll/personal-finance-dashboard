"""Data lake utilities for Parquet files and DuckDB queries."""

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import List, Optional

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from adapters.base import ReconciliationResult, StatementPeriod, make_bronze_record_id
from config import BRONZE_DIR, DUCKDB_PATH, GOLD_DIR, SILVER_DIR
from models.ingestion import IngestionManifest

logger = logging.getLogger(__name__)


def _sql_literal(value: str) -> str:
    """Quote a path as a SQL string literal.

    DuckDB rejects bound parameters inside CREATE VIEW ("Unexpected prepared
    parameter"), so view definitions have to inline their paths - and a data
    lake living under a directory with an apostrophe in it would otherwise
    produce a broken (or injectable) statement.
    """
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


class StaleSilverError(RuntimeError):
    """The Silver build layout exists but no published build can be read.

    Raised instead of silently falling back to unversioned Silver files,
    which would serve stale data indefinitely with no visible symptom.
    """


class DataLake:
    """File-based data lake with DuckDB query engine."""

    def __init__(self, db_path: str = DUCKDB_PATH):
        """Initialize DuckDB connection."""
        self.db_path = db_path
        self.conn = duckdb.connect(db_path)
        # Views are registered lazily on first query() rather than here, so
        # constructing a DataLake never depends on Silver having been built.
        self._views_registered = False
        self._views_build_path: Optional[Path] = None
        logger.info(f"DuckDB initialized at {db_path}")

    def write_bronze(
        self,
        ingestion: IngestionManifest,
        df: pd.DataFrame,
        reconciliation: Optional[ReconciliationResult] = None,
        statement_period: Optional[StatementPeriod] = None,
        reconciliations: Optional[List[ReconciliationResult]] = None,
    ) -> str:
        """
        Write raw records to Bronze layer (immutable).

        Args:
            ingestion: Immutable artifact manifest for this upload
            df: DataFrame with raw data
            reconciliation: whole-file balance self-check result, if the
                adapter performed one (see adapters.base.ReconciliationResult)
            statement_period: whole-file statement coverage period, if the
                adapter extracted one (see adapters.base.StatementPeriod)
            reconciliations: per-account balance self-check results, for a
                source where one file covers multiple accounts (e.g.
                Vanguard's ISA + Personal Pension wrappers) - see
                adapters.base.DataSourceAdapter.last_reconciliations.
                Mutually exclusive with `reconciliation`: an adapter sets
                exactly one of the two channels.

        Returns:
            Path to written Parquet file
        """
        if not ingestion.source_type:
            raise ValueError("Bronze publication requires a detected source_type")

        if reconciliation is not None and reconciliations:
            raise ValueError(
                "write_bronze got both `reconciliation` and `reconciliations` - "
                "an adapter should set exactly one of last_reconciliation/"
                "last_reconciliations, never both"
            )

        filepath = (
            BRONZE_DIR / ingestion.source_type / f"{ingestion.ingestion_id}.parquet"
        )
        filepath.parent.mkdir(parents=True, exist_ok=True)

        # A complete content-addressed ingestion is idempotent. Never rewrite
        # an already published Bronze artifact.
        if filepath.exists():
            return str(filepath)

        df = df.copy()

        # Add metadata columns
        # `source_key` is a legacy semantic fingerprint. It is useful for
        # diagnosis/matching but not stable enough to identify Bronze rows.
        df["legacy_transaction_fingerprint"] = df.get("source_key", "")
        df["bronze_source_key"] = df["legacy_transaction_fingerprint"]
        if "bronze_record_id" not in df.columns:
            df["bronze_record_id"] = [
                make_bronze_record_id(
                    ingestion.ingestion_id,
                    row.get("record_type", "transaction"),
                    int(row.get("line_number", index + 1)),
                )
                for index, (_, row) in enumerate(df.iterrows())
            ]
        if "source_ordinal" not in df.columns:
            df["source_ordinal"] = df.get("line_number")
        df["source_type"] = ingestion.source_type
        df["upload_timestamp"] = pd.Timestamp.now()
        df["filename"] = ingestion.original_filename
        df["ingestion_id"] = ingestion.ingestion_id
        df["raw_artifact_path"] = ingestion.raw_artifact_path
        df["parser_version"] = ingestion.parser_version

        # Only added when the adapter actually produced the fact, so
        # sources with no reconciliation/period concept simply don't get
        # these columns rather than getting them NaN-filled.
        if reconciliation is not None:
            df["reconciliation_check"] = reconciliation.check_name
            df[
                "reconciliation_expected_opening_minor"
            ] = reconciliation.expected_opening_minor
            df[
                "reconciliation_expected_closing_minor"
            ] = reconciliation.expected_closing_minor
            df[
                "reconciliation_derived_closing_minor"
            ] = reconciliation.derived_closing_minor
            df["reconciliation_matches"] = reconciliation.matches

        if reconciliations:
            # Per-row (not file-wide scalar) assignment: each result applies
            # only to the rows whose account_identifier it names, so two
            # accounts covered by one file (e.g. Vanguard's two wrappers)
            # can carry genuinely different reconciliation verdicts.
            df["reconciliation_check"] = None
            df["reconciliation_expected_opening_minor"] = None
            df["reconciliation_expected_closing_minor"] = None
            df["reconciliation_derived_closing_minor"] = None
            df["reconciliation_matches"] = None
            for rec in reconciliations:
                mask = (
                    df["account_identifier"].isna()
                    if rec.account_identifier is None
                    else df["account_identifier"] == rec.account_identifier
                )
                df.loc[mask, "reconciliation_check"] = rec.check_name
                df.loc[
                    mask, "reconciliation_expected_opening_minor"
                ] = rec.expected_opening_minor
                df.loc[
                    mask, "reconciliation_expected_closing_minor"
                ] = rec.expected_closing_minor
                df.loc[
                    mask, "reconciliation_derived_closing_minor"
                ] = rec.derived_closing_minor
                df.loc[mask, "reconciliation_matches"] = rec.matches

        if statement_period is not None:
            df["statement_period_from"] = statement_period.from_date
            df["statement_period_to"] = statement_period.to_date

        # Write and validate a sibling temporary file before publishing it.
        table = pa.Table.from_pandas(df)
        with tempfile.NamedTemporaryFile(
            dir=filepath.parent, suffix=".parquet", delete=False
        ) as temp:
            temp_path = temp.name
        try:
            pq.write_table(table, temp_path, filesystem=None)
            pq.read_table(temp_path)
            os.replace(temp_path, filepath)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

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
            source_type: 'monzo', 'kroo', 'amex'

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
        Read Silver records through the current/ build symlink.

        Silver's whole correctness story is that `current` points at one
        atomically-published, fully-validated build. An earlier version of
        this method fell through to the flat legacy `{entity}.parquet`
        whenever that link didn't resolve - which turned every way the link
        can break (repo moved while the target was absolute, build pruned,
        publish interrupted) into *silently serving months-old data* instead
        of an error. Net worth would still compute, and still be wrong.

        So a broken build is now loud. The flat-file path survives only for
        a genuinely pre-build data lake - no builds/ directory at all - and
        even then it warns, because that layout is legacy and unversioned.

        Returns None when the entity legitimately doesn't exist (no Silver
        has ever been built, or this build doesn't contain that table).

        Raises:
            StaleSilverError: the build layout exists but is unusable.
        """
        from models.build import _builds_dir, _current_link

        current_link = _current_link(SILVER_DIR)
        builds_dir = _builds_dir(SILVER_DIR)

        if current_link.is_symlink():
            if not current_link.exists():  # dangling target
                raise StaleSilverError(
                    f"data/silver/current points at "
                    f"{os.readlink(str(current_link))!r}, which does not exist. "
                    f"The build was pruned, or the data lake was moved while "
                    f"the symlink held an absolute path. Refusing to fall back "
                    f"to unversioned Silver files, which would silently serve "
                    f"stale data. Run 'cli.py silver rebuild' to republish."
                )
            filepath = current_link / f"{entity_type}.parquet"
            if filepath.exists():
                return pd.read_parquet(filepath)
            # A published build legitimately omits tables that were empty at
            # publish time (publish_silver_build skips empty frames).
            return None

        if builds_dir.exists() and any(builds_dir.iterdir()):
            raise StaleSilverError(
                f"{builds_dir} contains builds but data/silver/current does not "
                f"exist, so there is no published build to read. A publish was "
                f"likely interrupted. Run 'cli.py silver rebuild' to republish."
            )

        filepath = SILVER_DIR / f"{entity_type}.parquet"
        if not filepath.exists():
            return None
        logger.warning(
            "Reading unversioned legacy Silver file %s - this data lake predates "
            "versioned builds and has no provenance or atomicity guarantees. "
            "Run 'cli.py silver rebuild' to publish a real build.",
            filepath,
        )
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

    def _view_name(self, raw: str) -> str:
        """DuckDB identifier for a source_type/entity ('monzo-pdf' is not a
        bare identifier; 'monzo_pdf' is)."""
        return re.sub(r"[^0-9a-zA-Z_]", "_", raw)

    def refresh_views(self) -> List[str]:
        """(Re)create DuckDB views over the current Silver build and Bronze.

        Silver tables are exposed under their own names (`transactions`,
        `account_ledger`, ...) and Bronze under `bronze_<source_type>`, so
        callers write `SELECT * FROM transactions` rather than hand-rolling a
        `read_parquet('<path>')` against a build directory whose name changes
        on every rebuild.

        Views are defined over the *resolved* build path, not the `current`
        symlink, so a rebuild that swaps the symlink mid-session can never
        leave a view silently straddling two builds - `query()` detects the
        swap and calls this again.

        Returns the view names created.
        """
        from models.build import _current_link

        created: List[str] = []
        current_link = _current_link(SILVER_DIR)

        if current_link.is_symlink() and current_link.exists():
            build_dir = current_link.resolve()
            for parquet in sorted(build_dir.glob("*.parquet")):
                view = self._view_name(parquet.stem)
                self.conn.execute(
                    f"CREATE OR REPLACE VIEW {view} AS "
                    f"SELECT * FROM read_parquet({_sql_literal(str(parquet))})"
                )
                created.append(view)
            self._views_build_path = build_dir
        else:
            self._views_build_path = None

        if BRONZE_DIR.exists():
            for source_dir in sorted(BRONZE_DIR.iterdir()):
                if not source_dir.is_dir() or not any(source_dir.glob("*.parquet")):
                    continue
                view = f"bronze_{self._view_name(source_dir.name)}"
                glob_path = _sql_literal(str(source_dir / "*.parquet"))
                # union_by_name: a source's Bronze files can differ in columns
                # (reconciliation_*/statement_period_* only appear when the
                # adapter produced them - see write_bronze).
                self.conn.execute(
                    f"CREATE OR REPLACE VIEW {view} AS "
                    f"SELECT * FROM read_parquet({glob_path}, union_by_name = true)"
                )
                created.append(view)

        logger.debug("Registered %d DuckDB views", len(created))
        return created

    def _current_build_path(self) -> Optional[Path]:
        from models.build import _current_link

        link = _current_link(SILVER_DIR)
        if link.is_symlink() and link.exists():
            return link.resolve()
        return None

    def query(self, sql: str) -> pd.DataFrame:
        """
        Execute SQL against the data lake.

        Silver tables from the current build, and Bronze per source_type, are
        registered as named views before the query runs, and re-registered
        whenever a rebuild swaps the current/ symlink:

            df = datalake.query('''
                SELECT account_id, sum(amount_minor) AS spend_minor
                FROM transactions
                WHERE transaction_date >= '2026-01-01'
                GROUP BY account_id
            ''')

        Bronze is available as `bronze_<source_type>` with hyphens replaced by
        underscores (`bronze_monzo_pdf`, `bronze_natwest_statement`).

        Call `refresh_views()` directly to list what's available. Raw
        `read_parquet('<path>')` still works, but note that
        `data/silver/*.parquet` is the *legacy unversioned* location - querying
        it reads whatever predates the build model, not the current build.
        """
        build_path = self._current_build_path()
        if not self._views_registered or build_path != self._views_build_path:
            self.refresh_views()
            self._views_registered = True
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
