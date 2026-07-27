"""Silver read-path safety (C3) and the DuckDB query surface (C4).

C3: read_silver used to fall through to the flat legacy `{entity}.parquet`
whenever the current/ build symlink didn't resolve, turning every way that
link can break into silently serving stale data. It now raises.

C4: DuckDB was connected but held nothing - no views, no schema - so the
"query engine" was a bare passthrough and the documented example pointed at
the stale flat path.
"""

import os

import pandas as pd
import pytest

import models.datalake as datalake_module
from models.build import publish_silver_build
from models.datalake import DataLake, StaleSilverError


@pytest.fixture
def lake(tmp_path, monkeypatch):
    """A DataLake rooted at tmp_path, with Bronze/Silver dirs redirected."""
    silver = tmp_path / "silver"
    bronze = tmp_path / "bronze"
    silver.mkdir()
    bronze.mkdir()
    monkeypatch.setattr(datalake_module, "SILVER_DIR", silver)
    monkeypatch.setattr(datalake_module, "BRONZE_DIR", bronze)
    return DataLake(db_path=str(tmp_path / "test.duckdb")), silver, bronze


def _publish(silver_dir, **tables):
    frames = {k: pd.DataFrame(v) for k, v in tables.items()}
    return publish_silver_build(
        tables=frames, input_ingestion_ids=["i1"], silver_dir=silver_dir
    )


class TestReadSilverRefusesStaleData:
    def test_dangling_current_symlink_raises(self, lake):
        dl, silver, _ = lake
        _publish(silver, transactions=[{"a": 1}])

        # A flat legacy file that the old code would have silently served.
        pd.DataFrame([{"a": 999}]).to_parquet(silver / "transactions.parquet")

        # Break the build the symlink points at (pruned build / moved lake).
        build = (silver / "current").resolve()
        for f in build.iterdir():
            f.unlink()
        build.rmdir()

        with pytest.raises(StaleSilverError, match="does not exist"):
            dl.read_silver("transactions")

    def test_builds_present_but_no_current_link_raises(self, lake):
        dl, silver, _ = lake
        _publish(silver, transactions=[{"a": 1}])
        pd.DataFrame([{"a": 999}]).to_parquet(silver / "transactions.parquet")

        (silver / "current").unlink()  # interrupted publish

        with pytest.raises(StaleSilverError, match="no published build"):
            dl.read_silver("transactions")

    def test_stale_flat_file_is_never_preferred_over_a_good_build(self, lake):
        dl, silver, _ = lake
        _publish(silver, transactions=[{"a": 1}])
        pd.DataFrame([{"a": 999}]).to_parquet(silver / "transactions.parquet")

        assert dl.read_silver("transactions")["a"].tolist() == [1]


class TestReadSilverLegitimateAbsences:
    def test_no_silver_at_all_returns_none(self, lake):
        dl, _, _ = lake
        assert dl.read_silver("transactions") is None

    def test_table_absent_from_build_returns_none(self, lake):
        """publish_silver_build skips empty frames, so a build legitimately
        omits tables - that is not a stale-data condition."""
        dl, silver, _ = lake
        _publish(silver, transactions=[{"a": 1}])
        assert dl.read_silver("holdings") is None

    def test_legacy_unversioned_lake_still_reads_but_warns(self, lake, caplog):
        """A data lake predating builds has no builds/ dir at all."""
        dl, silver, _ = lake
        pd.DataFrame([{"a": 999}]).to_parquet(silver / "transactions.parquet")

        with caplog.at_level("WARNING"):
            result = dl.read_silver("transactions")

        assert result["a"].tolist() == [999]
        assert "unversioned legacy Silver file" in caplog.text


class TestBuildSymlinkIsRelocatable:
    def test_symlink_target_is_relative_to_the_link(self, lake):
        dl, silver, _ = lake
        build_id = _publish(silver, transactions=[{"a": 1}])
        target = os.readlink(str(silver / "current"))

        assert not os.path.isabs(target), (
            f"absolute symlink target {target!r} hardcodes this machine's "
            f"path and breaks when the data lake is moved"
        )
        assert target == os.path.join("builds", build_id)

    def test_lake_still_reads_after_being_moved(self, lake, tmp_path, monkeypatch):
        dl, silver, _ = lake
        _publish(silver, transactions=[{"a": 1}])

        moved = tmp_path / "relocated_silver"
        os.rename(str(silver), str(moved))
        monkeypatch.setattr(datalake_module, "SILVER_DIR", moved)

        assert dl.read_silver("transactions")["a"].tolist() == [1]


class TestDuckDBViews:
    def test_silver_tables_are_queryable_by_name(self, lake):
        dl, silver, _ = lake
        _publish(
            silver,
            transactions=[{"account_id": "a1", "amount_minor": -100}],
            accounts=[{"account_id": "a1"}],
        )

        df = dl.query("SELECT sum(amount_minor) AS total FROM transactions")
        assert df["total"].iloc[0] == -100

    def test_bronze_is_queryable_per_source_type(self, lake):
        dl, _, bronze = lake
        src = bronze / "monzo-pdf"
        src.mkdir()
        pd.DataFrame([{"x": 1}, {"x": 2}]).to_parquet(src / "i1.parquet")

        # Hyphen is not a bare SQL identifier; the view normalizes it.
        assert dl.query("SELECT count(*) c FROM bronze_monzo_pdf")["c"].iloc[0] == 2

    def test_bronze_view_unions_files_with_differing_columns(self, lake):
        """write_bronze only adds reconciliation_*/statement_period_* columns
        when the adapter produced them, so one source's files differ in shape."""
        dl, _, bronze = lake
        src = bronze / "kroo"
        src.mkdir()
        pd.DataFrame([{"x": 1}]).to_parquet(src / "i1.parquet")
        pd.DataFrame([{"x": 2, "reconciliation_matches": True}]).to_parquet(
            src / "i2.parquet"
        )

        assert dl.query("SELECT count(*) c FROM bronze_kroo")["c"].iloc[0] == 2

    def test_views_follow_a_rebuild(self, lake):
        """A rebuild swaps the symlink; a cached view must not keep serving
        the superseded build."""
        dl, silver, _ = lake
        _publish(silver, transactions=[{"amount_minor": -100}])
        assert (
            dl.query("SELECT sum(amount_minor) t FROM transactions")["t"].iloc[0]
            == -100
        )

        _publish(silver, transactions=[{"amount_minor": -250}])
        assert (
            dl.query("SELECT sum(amount_minor) t FROM transactions")["t"].iloc[0]
            == -250
        )

    def test_refresh_views_reports_what_it_registered(self, lake):
        dl, silver, bronze = lake
        _publish(silver, transactions=[{"a": 1}], holdings=[{"b": 2}])
        (bronze / "chase").mkdir()
        pd.DataFrame([{"x": 1}]).to_parquet(bronze / "chase" / "i1.parquet")

        views = dl.refresh_views()
        assert {"transactions", "holdings", "bronze_chase"} <= set(views)

    def test_query_works_with_no_silver_build(self, lake):
        """Constructing/querying must not require Silver to exist."""
        dl, _, bronze = lake
        (bronze / "kroo").mkdir()
        pd.DataFrame([{"x": 7}]).to_parquet(bronze / "kroo" / "i1.parquet")

        assert dl.query("SELECT x FROM bronze_kroo")["x"].iloc[0] == 7


class TestSqlLiteralEscaping:
    def test_path_with_apostrophe_does_not_break_view_creation(
        self, tmp_path, monkeypatch
    ):
        """DuckDB rejects bound params in CREATE VIEW, so paths are inlined -
        an apostrophe in the path must be escaped, not concatenated raw."""
        root = tmp_path / "Lesther's lake"
        silver = root / "silver"
        bronze = root / "bronze"
        silver.mkdir(parents=True)
        bronze.mkdir(parents=True)
        monkeypatch.setattr(datalake_module, "SILVER_DIR", silver)
        monkeypatch.setattr(datalake_module, "BRONZE_DIR", bronze)

        dl = DataLake(db_path=str(tmp_path / "q.duckdb"))
        _publish(silver, transactions=[{"amount_minor": -42}])

        assert (
            dl.query("SELECT sum(amount_minor) t FROM transactions")["t"].iloc[0] == -42
        )


class TestBuildIdUniqueness:
    """Regression: build_id names the build *directory*, and
    publish_silver_build os.rename()s onto it assuming it is free. At second
    granularity two rebuilds in the same second collided."""

    def test_back_to_back_ids_are_unique(self):
        from models.build import generate_build_id

        ids = [generate_build_id() for _ in range(20)]
        assert len(set(ids)) == len(ids), f"collision among {ids}"

    def test_two_rebuilds_in_the_same_second_both_publish(self, lake):
        dl, silver, _ = lake
        first = _publish(silver, transactions=[{"amount_minor": -100}])
        second = _publish(silver, transactions=[{"amount_minor": -250}])

        assert first != second
        assert dl.read_silver("transactions")["amount_minor"].tolist() == [-250]

    def test_duplicate_build_id_is_refused_clearly(self, lake):
        dl, silver, _ = lake
        build_id = _publish(silver, transactions=[{"a": 1}])

        with pytest.raises(FileExistsError, match="already exists"):
            publish_silver_build(
                tables={"transactions": pd.DataFrame([{"a": 2}])},
                input_ingestion_ids=["i2"],
                build_id=build_id,
                silver_dir=silver,
            )
