"""Tests for derived-balance rollforward logic."""

from unittest.mock import MagicMock

import pandas as pd

from models.datalake import DataLake
from transformers.silver_transformer import (
    _LEDGER_COLUMNS,
    _contiguous_coverage_end,
    _derive_rollforward_ledger_rows,
)


def _transactions_df(rows):
    """Build a Silver transactions DataFrame (post-matching shape)."""
    defaults = {
        "bronze_record_id": "rid",
        "bronze_source_key": "rsk",
        "silver_transaction_id": "stid",
        "source_type": "natwest-transactions",
        "account_id": "acc_test",
        "transaction_date": pd.Timestamp("2026-06-01"),
        "description": "desc",
        "amount_minor": -1000,
        "currency": "GBP",
        "category": None,
        "bank_transaction_id": None,
        "ingested_at": pd.Timestamp("2026-06-01"),
        "upload_timestamp": pd.Timestamp("2026-06-01"),
        "statement_period_to": pd.Timestamp("2026-06-30"),
        "line_number": 1,
    }
    for i, row in enumerate(rows):
        r = dict(defaults, **row)
        # Make unique keys per row.
        r["bronze_record_id"] = row.get("bronze_record_id", f"rid_{i}")
        r["bronze_source_key"] = row.get("bronze_source_key", f"rsk_{i}")
        r["silver_transaction_id"] = row.get("silver_transaction_id", f"stid_{i}")
        rows[i] = r
    return pd.DataFrame(rows)


def _ledger_df(rows):
    """Build a Silver account_ledger DataFrame."""
    defaults = {
        "bronze_record_id": "rid",
        "bronze_source_key": "rsk",
        "account_id": "acc_test",
        "source_type": "natwest-statement",
        "balance_minor": 100000,
        "as_of_date": pd.Timestamp("2026-05-01"),
        "upload_timestamp": pd.Timestamp("2026-05-15"),
        "statement_period_to": pd.Timestamp("2026-05-13"),
        "line_number": 1,
        "reconciled": True,
        "balance_source": "printed",
    }
    for i, row in enumerate(rows):
        r = dict(defaults, **row)
        rows[i] = r
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# _derive_rollforward_ledger_rows
# ---------------------------------------------------------------------------


class TestDeriveRollforwardBasic:
    def test_produces_derived_rows_when_anchor_and_transactions_after_exist(self):
        txns = _transactions_df(
            [{"transaction_date": pd.Timestamp("2026-06-15"), "amount_minor": -3000}]
        )
        ledger = _ledger_df(
            [{"balance_minor": 95000, "as_of_date": pd.Timestamp("2026-05-13")}]
        )
        mock_dl = MagicMock(spec=DataLake)
        mock_dl.read_bronze.return_value = None

        result = _derive_rollforward_ledger_rows(txns, ledger, mock_dl)

        assert not result.empty
        assert len(result) == 1
        row = result.iloc[0]
        assert row["balance_minor"] == 92000
        assert row["balance_source"] == "derived"
        assert row["source_type"] == "natwest-transactions"
        assert row["reconciled"] is None
        assert row["account_id"] == "acc_test"
        assert row["bronze_record_id"].startswith("derived_")
        assert row["bronze_source_key"].startswith("derived_")

    def test_multiple_transactions_accumulate_correctly(self):
        txns = _transactions_df(
            [
                {
                    "transaction_date": pd.Timestamp("2026-06-01"),
                    "amount_minor": 20000,
                    "bronze_record_id": "r1",
                    "bronze_source_key": "k1",
                },
                {
                    "transaction_date": pd.Timestamp("2026-06-15"),
                    "amount_minor": -10000,
                    "bronze_record_id": "r2",
                    "bronze_source_key": "k2",
                },
                {
                    "transaction_date": pd.Timestamp("2026-06-30"),
                    "amount_minor": -5000,
                    "bronze_record_id": "r3",
                    "bronze_source_key": "k3",
                },
            ]
        )
        ledger = _ledger_df(
            [{"balance_minor": 100000, "as_of_date": pd.Timestamp("2026-05-01")}]
        )
        mock_dl = MagicMock(spec=DataLake)
        mock_dl.read_bronze.return_value = None

        result = _derive_rollforward_ledger_rows(txns, ledger, mock_dl)

        assert len(result) == 3
        assert result.iloc[0]["balance_minor"] == 120000  # 100000 + 20000
        assert result.iloc[1]["balance_minor"] == 110000  # 120000 + (-10000)
        assert result.iloc[2]["balance_minor"] == 105000  # 110000 + (-5000)

    def test_transactions_sorted_ascending_by_date(self):
        txns = _transactions_df(
            [
                {
                    "transaction_date": pd.Timestamp("2026-06-30"),
                    "amount_minor": -1000,
                },
                {
                    "transaction_date": pd.Timestamp("2026-06-01"),
                    "amount_minor": 2000,
                },
            ]
        )
        ledger = _ledger_df([{"balance_minor": 1000}])
        mock_dl = MagicMock(spec=DataLake)
        mock_dl.read_bronze.return_value = None

        result = _derive_rollforward_ledger_rows(txns, ledger, mock_dl)

        # Should be sorted by date, so Jun 1 before Jun 30.
        assert result.iloc[0]["balance_minor"] == 3000  # 1000 + 2000
        assert result.iloc[1]["balance_minor"] == 2000  # 3000 + (-1000)


class TestDeriveRollforwardEdgeCases:
    def test_returns_empty_when_no_confirmed_anchor(self):
        txns = _transactions_df([{"amount_minor": -1000}])
        # Anchor with reconciled=None is NOT a confirmed anchor.
        ledger = _ledger_df([{"reconciled": None}])
        mock_dl = MagicMock(spec=DataLake)
        mock_dl.read_bronze.return_value = None

        result = _derive_rollforward_ledger_rows(txns, ledger, mock_dl)
        assert result.empty

    def test_returns_empty_when_anchor_reconciled_false(self):
        txns = _transactions_df([{"amount_minor": -1000}])
        ledger = _ledger_df([{"reconciled": False}])
        mock_dl = MagicMock(spec=DataLake)
        mock_dl.read_bronze.return_value = None

        result = _derive_rollforward_ledger_rows(txns, ledger, mock_dl)
        assert result.empty

    def test_returns_empty_when_no_anchor_source_in_ledger(self):
        txns = _transactions_df([{"amount_minor": -1000}])
        # Ledger has no natwest-statement rows at all.
        ledger = pd.DataFrame(columns=_LEDGER_COLUMNS)
        mock_dl = MagicMock(spec=DataLake)
        mock_dl.read_bronze.return_value = None

        result = _derive_rollforward_ledger_rows(txns, ledger, mock_dl)
        assert result.empty

    def test_returns_empty_when_no_transactions_after_anchor(self):
        txns = _transactions_df(
            [{"transaction_date": pd.Timestamp("2026-04-01"), "amount_minor": -1000}]
        )
        ledger = _ledger_df(
            [{"balance_minor": 100000, "as_of_date": pd.Timestamp("2026-05-01")}]
        )
        mock_dl = MagicMock(spec=DataLake)
        mock_dl.read_bronze.return_value = None

        result = _derive_rollforward_ledger_rows(txns, ledger, mock_dl)
        assert result.empty

    def test_returns_empty_when_empty_dataframes(self):
        empty_txns = pd.DataFrame()
        empty_ledger = _ledger_df([])
        mock_dl = MagicMock(spec=DataLake)
        mock_dl.read_bronze.return_value = None

        assert _derive_rollforward_ledger_rows(empty_txns, empty_ledger, mock_dl).empty
        assert _derive_rollforward_ledger_rows(
            _transactions_df([]), empty_ledger, mock_dl
        ).empty

    def test_excludes_same_date_as_anchor(self):
        """Transactions on the same date as the anchor are excluded (strict >)."""
        txns = _transactions_df(
            [
                {
                    "transaction_date": pd.Timestamp("2026-05-01"),
                    "amount_minor": -1000,
                },
            ]
        )
        ledger = _ledger_df(
            [{"balance_minor": 100000, "as_of_date": pd.Timestamp("2026-05-01")}]
        )
        mock_dl = MagicMock(spec=DataLake)
        mock_dl.read_bronze.return_value = None

        result = _derive_rollforward_ledger_rows(txns, ledger, mock_dl)
        assert result.empty

    def test_multiple_accounts_derived_independently(self):
        txns = pd.concat(
            [
                _transactions_df(
                    [
                        {
                            "account_id": "acc_a",
                            "transaction_date": pd.Timestamp("2026-06-15"),
                            "amount_minor": -1000,
                        },
                    ]
                ),
                _transactions_df(
                    [
                        {
                            "account_id": "acc_b",
                            "transaction_date": pd.Timestamp("2026-06-20"),
                            "amount_minor": 5000,
                        },
                    ]
                ),
            ]
        )
        ledger = pd.concat(
            [
                _ledger_df(
                    [
                        {
                            "account_id": "acc_a",
                            "balance_minor": 90000,
                            "as_of_date": pd.Timestamp("2026-05-01"),
                        },
                    ]
                ),
                _ledger_df(
                    [
                        {
                            "account_id": "acc_b",
                            "balance_minor": 200000,
                            "as_of_date": pd.Timestamp("2026-05-01"),
                        },
                    ]
                ),
            ]
        )
        mock_dl = MagicMock(spec=DataLake)
        mock_dl.read_bronze.return_value = None

        result = _derive_rollforward_ledger_rows(txns, ledger, mock_dl)

        assert len(result) == 2
        row_a = result[result["account_id"] == "acc_a"].iloc[0]
        row_b = result[result["account_id"] == "acc_b"].iloc[0]
        assert row_a["balance_minor"] == 89000
        assert row_b["balance_minor"] == 205000

    def test_uses_latest_anchor_when_multiple_exist(self):
        """When there are multiple anchor ledger rows, pick the latest one."""
        txns = _transactions_df(
            [
                {
                    "transaction_date": pd.Timestamp("2026-07-01"),
                    "amount_minor": -500,
                },
            ]
        )
        ledger = pd.concat(
            [
                _ledger_df(
                    [
                        {
                            "balance_minor": 100000,
                            "as_of_date": pd.Timestamp("2026-05-01"),
                        },
                    ]
                ),
                _ledger_df(
                    [
                        {
                            "balance_minor": 80000,
                            "as_of_date": pd.Timestamp("2026-06-01"),
                        },
                    ]
                ),
            ]
        )
        mock_dl = MagicMock(spec=DataLake)
        mock_dl.read_bronze.return_value = None

        result = _derive_rollforward_ledger_rows(txns, ledger, mock_dl)

        assert len(result) == 1
        # Latest anchor is June with 80000, so derived = 80000 + (-500) = 79500.
        assert result.iloc[0]["balance_minor"] == 79500

    def test_missing_sort_metadata_falls_back_to_defaults(self):
        txns = _transactions_df(
            [
                {
                    "transaction_date": pd.Timestamp("2026-06-15"),
                    "amount_minor": -1000,
                    "upload_timestamp": pd.NaT,
                    "statement_period_to": pd.NaT,
                    "line_number": None,
                },
            ]
        )
        ledger = _ledger_df([{"balance_minor": 95000}])
        mock_dl = MagicMock(spec=DataLake)
        mock_dl.read_bronze.return_value = None

        result = _derive_rollforward_ledger_rows(txns, ledger, mock_dl)

        assert not result.empty
        row = result.iloc[0]
        assert pd.isna(row["upload_timestamp"])
        assert pd.isna(row["statement_period_to"])
        assert row["line_number"] == 0

    def test_no_matching_account_in_anchor_ledger(self):
        txns = _transactions_df([{"account_id": "acc_test", "amount_minor": -1000}])
        # Ledger only has a different account.
        ledger = _ledger_df([{"account_id": "acc_other"}])
        mock_dl = MagicMock(spec=DataLake)
        mock_dl.read_bronze.return_value = None

        result = _derive_rollforward_ledger_rows(txns, ledger, mock_dl)
        assert result.empty

    def test_no_dependent_transactions_in_df(self):
        """Only non-dependent source_types in transactions_df → empty."""
        txns = _transactions_df(
            [
                {
                    "source_type": "monzo",
                    "transaction_date": pd.Timestamp("2026-06-15"),
                    "amount_minor": -1000,
                },
            ]
        )
        ledger = _ledger_df([{"balance_minor": 95000}])
        mock_dl = MagicMock(spec=DataLake)
        mock_dl.read_bronze.return_value = None

        result = _derive_rollforward_ledger_rows(txns, ledger, mock_dl)
        assert result.empty


# ---------------------------------------------------------------------------
# _contiguous_coverage_end
# ---------------------------------------------------------------------------


class TestContiguousCoverageEnd:
    _SOURCE = "natwest-transactions"
    _ACCOUNT = "acc_test"

    def _mk_datalake(self, tmp_path, monkeypatch, df):
        """Create a DataLake with a real Bronze dir that contains test
        parquet data for the coverage function to read."""
        monkeypatch.setattr(
            "transformers.silver_transformer.get_account_id",
            lambda ai, st, path=None: self._ACCOUNT,
        )
        monkeypatch.setattr("models.datalake.BRONZE_DIR", tmp_path / "bronze")
        monkeypatch.setattr("models.datalake.SILVER_DIR", tmp_path / "silver")
        monkeypatch.setattr("models.datalake.GOLD_DIR", tmp_path / "gold")

        dl = DataLake(db_path=str(tmp_path / "test.duckdb"))
        from models.ingestion import IngestionManifest

        manifest = IngestionManifest(
            ingestion_id="test_hash",
            original_filename="test.pdf",
            raw_artifact_path="/test/raw/test.pdf",
            status="complete",
            created_at="2026-01-01T00:00:00+00:00",
            source_type=self._SOURCE,
        )
        dl.write_bronze(manifest, df)
        return dl

    def test_returns_max_when_no_bronze_data(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "transformers.silver_transformer.get_account_id",
            lambda ai, st, path=None: self._ACCOUNT,
        )
        monkeypatch.setattr("models.datalake.BRONZE_DIR", tmp_path / "bronze")
        monkeypatch.setattr("models.datalake.SILVER_DIR", tmp_path / "silver")
        monkeypatch.setattr("models.datalake.GOLD_DIR", tmp_path / "gold")
        dl = DataLake(db_path=str(tmp_path / "test.duckdb"))
        result = _contiguous_coverage_end(
            self._ACCOUNT, pd.Timestamp("2026-05-01"), self._SOURCE, dl
        )
        assert result == pd.Timestamp.max
        dl.close()

    def test_returns_max_when_no_period_columns(self, tmp_path, monkeypatch):
        from models.ingestion import IngestionManifest

        monkeypatch.setattr("models.datalake.BRONZE_DIR", tmp_path / "bronze")
        monkeypatch.setattr("models.datalake.SILVER_DIR", tmp_path / "silver")
        monkeypatch.setattr("models.datalake.GOLD_DIR", tmp_path / "gold")

        dl = DataLake(db_path=str(tmp_path / "test.duckdb"))
        manifest = IngestionManifest(
            ingestion_id="test_hash",
            original_filename="test.pdf",
            raw_artifact_path="/test/raw/test.pdf",
            status="complete",
            created_at="2026-01-01T00:00:00+00:00",
            source_type=self._SOURCE,
        )
        # DataFrame without statement_period_from/to columns.
        df = pd.DataFrame([{"source_type": self._SOURCE, "account_identifier": "id1"}])
        dl.write_bronze(manifest, df)
        result = _contiguous_coverage_end(
            self._ACCOUNT, pd.Timestamp("2026-05-01"), self._SOURCE, dl
        )
        assert result == pd.Timestamp.max
        dl.close()

    def test_continuous_periods_return_last_end(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "transformers.silver_transformer.get_account_id",
            lambda ai, st, path=None: self._ACCOUNT,
        )
        from models.ingestion import IngestionManifest

        monkeypatch.setattr("models.datalake.BRONZE_DIR", tmp_path / "bronze")
        monkeypatch.setattr("models.datalake.SILVER_DIR", tmp_path / "silver")
        monkeypatch.setattr("models.datalake.GOLD_DIR", tmp_path / "gold")

        dl = DataLake(db_path=str(tmp_path / "test.duckdb"))

        df = pd.DataFrame(
            [
                {
                    "source_type": self._SOURCE,
                    "account_identifier": "id1",
                    "statement_period_from": pd.Timestamp("2026-05-01"),
                    "statement_period_to": pd.Timestamp("2026-06-30"),
                    "filename": "f1",
                    "ingestion_id": "h1",
                },
                {
                    "source_type": self._SOURCE,
                    "account_identifier": "id1",
                    "statement_period_from": pd.Timestamp("2026-07-01"),
                    "statement_period_to": pd.Timestamp("2026-07-26"),
                    "filename": "f2",
                    "ingestion_id": "h2",
                },
            ]
        )
        manifest = IngestionManifest(
            ingestion_id="h",
            original_filename="test.pdf",
            raw_artifact_path="/test/raw/test.pdf",
            status="complete",
            created_at="2026-01-01T00:00:00+00:00",
            source_type=self._SOURCE,
        )
        dl.write_bronze(manifest, df)

        result = _contiguous_coverage_end(
            self._ACCOUNT, pd.Timestamp("2026-05-01"), self._SOURCE, dl
        )
        # May→Jun→Jul periods are contiguous (1-day gap ≤ tolerance).
        assert result == pd.Timestamp("2026-07-26")
        dl.close()

    def test_gap_truncates_coverage(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "transformers.silver_transformer.get_account_id",
            lambda ai, st, path=None: self._ACCOUNT,
        )
        from models.ingestion import IngestionManifest

        monkeypatch.setattr("models.datalake.BRONZE_DIR", tmp_path / "bronze")
        monkeypatch.setattr("models.datalake.SILVER_DIR", tmp_path / "silver")
        monkeypatch.setattr("models.datalake.GOLD_DIR", tmp_path / "gold")

        dl = DataLake(db_path=str(tmp_path / "test.duckdb"))

        df = pd.DataFrame(
            [
                {
                    "source_type": self._SOURCE,
                    "account_identifier": "id1",
                    "statement_period_from": pd.Timestamp("2026-05-01"),
                    "statement_period_to": pd.Timestamp("2026-05-13"),
                    "filename": "f1",
                    "ingestion_id": "h1",
                },
                # Gap: 2026-05-13 → 2026-07-01 is > 3 days.
                {
                    "source_type": self._SOURCE,
                    "account_identifier": "id1",
                    "statement_period_from": pd.Timestamp("2026-07-01"),
                    "statement_period_to": pd.Timestamp("2026-07-26"),
                    "filename": "f2",
                    "ingestion_id": "h2",
                },
            ]
        )
        manifest = IngestionManifest(
            ingestion_id="h",
            original_filename="test.pdf",
            raw_artifact_path="/test/raw/test.pdf",
            status="complete",
            created_at="2026-01-01T00:00:00+00:00",
            source_type=self._SOURCE,
        )
        dl.write_bronze(manifest, df)

        result = _contiguous_coverage_end(
            self._ACCOUNT, pd.Timestamp("2026-05-01"), self._SOURCE, dl
        )
        # First period (May 1-13) = contiguous from anchor.
        # Second period (Jul 1-26) has gap: May 13 → Jul 1 = 49 days > 3.
        # Contiguous window stops at May 13.
        assert result == pd.Timestamp("2026-05-13")
        dl.close()

    def test_anchor_date_after_first_period_correctly_shifts_window(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "transformers.silver_transformer.get_account_id",
            lambda ai, st, path=None: self._ACCOUNT,
        )
        from models.ingestion import IngestionManifest

        monkeypatch.setattr("models.datalake.BRONZE_DIR", tmp_path / "bronze")
        monkeypatch.setattr("models.datalake.SILVER_DIR", tmp_path / "silver")
        monkeypatch.setattr("models.datalake.GOLD_DIR", tmp_path / "gold")

        dl = DataLake(db_path=str(tmp_path / "test.duckdb"))

        df = pd.DataFrame(
            [
                {
                    "source_type": self._SOURCE,
                    "account_identifier": "id1",
                    "statement_period_from": pd.Timestamp("2026-06-01"),
                    "statement_period_to": pd.Timestamp("2026-07-26"),
                    "filename": "f2",
                    "ingestion_id": "h2",
                },
                {
                    "source_type": self._SOURCE,
                    "account_identifier": "id1",
                    "statement_period_from": pd.Timestamp("2026-03-29"),
                    "statement_period_to": pd.Timestamp("2026-04-15"),
                    "filename": "f1",
                    "ingestion_id": "h1",
                },
            ]
        )
        manifest = IngestionManifest(
            ingestion_id="h",
            original_filename="test.pdf",
            raw_artifact_path="/test/raw/test.pdf",
            status="complete",
            created_at="2026-01-01T00:00:00+00:00",
            source_type=self._SOURCE,
        )
        dl.write_bronze(manifest, df)

        # Anchor (2026-04-01) lies between the two periods.
        # Sorted: Mar 29–Apr 15, then Jun 1–Jul 26.
        # The Mar period extends max_to through Apr 15; June starts just
        # 47 days later (> 3 day tolerance) → gap detected at Apr 15.
        result = _contiguous_coverage_end(
            self._ACCOUNT, pd.Timestamp("2026-04-01"), self._SOURCE, dl
        )
        assert result == pd.Timestamp("2026-04-15")
        dl.close()
