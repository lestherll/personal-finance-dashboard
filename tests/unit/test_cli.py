"""Tests for the account-mapping CLI commands."""

import json
from datetime import datetime

import pandas as pd
import pytest
from click.testing import CliRunner

import cli as cli_module
from adapters.base import RawRecord, ReconciliationResult, StatementPeriod
from adapters.factory import (
    AmbiguousFormatError,
    IngestResult,
    UnrecognizedFormatError,
)
from cli import cli
from models.ingestion import IngestionManifest


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def account_map_path(tmp_path, monkeypatch):
    """Point the CLI's underlying functions at an isolated data file."""
    path = tmp_path / "account_map.json"
    path.write_text(json.dumps({"identifiers": {}, "source_type_fallback": {}}))
    monkeypatch.setattr("transformers.account_config.ACCOUNT_MAP_PATH", path)
    return path


@pytest.fixture(autouse=True)
def fake_ingestion_lifecycle(monkeypatch):
    """Keep CLI command tests isolated from content-addressed disk storage."""
    manifests = {}

    def start(path, ingestion_id):
        manifest = manifests.get(ingestion_id)
        if manifest is None:
            manifest = IngestionManifest(
                ingestion_id=ingestion_id,
                original_filename=path.name,
                raw_artifact_path=f"/fake/raw/{ingestion_id}{path.suffix}",
                status="archived",
                created_at="2026-01-01T00:00:00+00:00",
            )
            manifests[ingestion_id] = manifest
        return manifest

    monkeypatch.setattr(cli_module, "start_ingestion", start)
    monkeypatch.setattr(cli_module, "write_manifest", lambda manifest: None)


class TestRegisterCommand:
    def test_register_writes_mapping(self, runner, account_map_path):
        result = runner.invoke(
            cli,
            [
                "accounts",
                "register",
                "hash_x",
                "acc_x",
                "Account X",
                "current",
            ],
        )
        assert result.exit_code == 0
        assert "Registered" in result.output

        data = json.loads(account_map_path.read_text())
        assert data["identifiers"]["hash_x"]["account_id"] == "acc_x"

    def test_register_rejects_invalid_account_type(self, runner, account_map_path):
        result = runner.invoke(
            cli,
            ["accounts", "register", "hash_x", "acc_x", "Account X", "not_a_type"],
        )
        assert result.exit_code != 0


class TestRegisterFallbackCommand:
    def test_register_fallback_writes_mapping(self, runner, account_map_path):
        result = runner.invoke(
            cli,
            ["accounts", "register-fallback", "monzo", "acc_monzo", "Monzo", "current"],
        )
        assert result.exit_code == 0

        data = json.loads(account_map_path.read_text())
        assert data["source_type_fallback"]["monzo"]["account_id"] == "acc_monzo"


class TestListUnmappedCommand:
    def test_reports_no_unmapped_accounts(self, runner, account_map_path, monkeypatch):
        monkeypatch.setattr(cli_module, "get_datalake", lambda: object())
        monkeypatch.setattr(
            cli_module,
            "find_unmapped_accounts",
            lambda datalake: pd.DataFrame(
                columns=[
                    "source_type",
                    "account_identifier",
                    "sample_description",
                    "record_count",
                ]
            ),
        )

        result = runner.invoke(cli, ["accounts", "list-unmapped"])
        assert result.exit_code == 0
        assert "No unmapped accounts found" in result.output

    def test_reports_unmapped_accounts_with_registration_hint(
        self, runner, account_map_path, monkeypatch
    ):
        monkeypatch.setattr(cli_module, "get_datalake", lambda: object())
        monkeypatch.setattr(
            cli_module,
            "find_unmapped_accounts",
            lambda datalake: pd.DataFrame(
                [
                    {
                        "source_type": "kroo",
                        "account_identifier": "brand_new_hash",
                        "sample_description": "Test Merchant",
                        "record_count": 5,
                    }
                ]
            ),
        )

        result = runner.invoke(cli, ["accounts", "list-unmapped"])
        assert result.exit_code == 0
        assert "kroo" in result.output
        assert "brand_new_hash" in result.output
        assert "accounts register" in result.output


def _make_record(source_type="amex", filename="statement.pdf"):
    return RawRecord(
        source_key=f"{source_type}_key1",
        source_type=source_type,
        raw_data={"date": "01 Jan 2026", "description": "Test", "amount": -10.0},
        filename=filename,
        file_hash="hash123",
        upload_timestamp=datetime(2026, 1, 1),
        line_number=1,
    )


class _FakeDatalake:
    """Stand-in for get_datalake(): records write_bronze calls for assertion."""

    def __init__(self):
        self.write_calls = []

    def write_bronze(
        self, ingestion, df, reconciliation=None, statement_period=None, reconciliations=None
    ):
        self.write_calls.append(
            {
                "source_type": ingestion.source_type,
                "filename": ingestion.original_filename,
                "reconciliation": reconciliation,
                "statement_period": statement_period,
                "reconciliations": reconciliations or [],
            }
        )
        return f"/fake/bronze/{ingestion.source_type}/{ingestion.ingestion_id}.parquet"


class _FakeFactory:
    """Stand-in for AdapterFactory: canned IngestResult/exception per filename."""

    def __init__(self, outcomes):
        self._outcomes = outcomes

    def ingest(self, content, filename, file_hash):
        outcome = self._outcomes[filename]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class TestIngestCommand:
    def test_reports_reconciliation_match_and_period(
        self, runner, tmp_path, monkeypatch
    ):
        pdf_path = tmp_path / "statement.pdf"
        pdf_path.write_bytes(b"%PDF-fake")

        outcome = IngestResult(
            records=[_make_record()],
            reconciliation=ReconciliationResult(
                check_name="amex_closing_balance",
                expected_closing_minor=86304,
                derived_closing_minor=86304,
                matches=True,
            ),
            statement_period=StatementPeriod(
                datetime(2026, 6, 20), datetime(2026, 7, 19)
            ),
        )
        fake_datalake = _FakeDatalake()
        monkeypatch.setattr(
            cli_module,
            "AdapterFactory",
            lambda: _FakeFactory({"statement.pdf": outcome}),
        )
        monkeypatch.setattr(cli_module, "get_datalake", lambda: fake_datalake)

        result = runner.invoke(cli, ["ingest", str(pdf_path)])
        assert result.exit_code == 0
        assert "reconciles against printed closing balance (£863.04)" in result.output
        assert "statement period: 2026-06-20 to 2026-07-19" in result.output
        assert fake_datalake.write_calls[0]["reconciliation"] is outcome.reconciliation

    def test_reports_reconciliation_mismatch(self, runner, tmp_path, monkeypatch):
        pdf_path = tmp_path / "statement.pdf"
        pdf_path.write_bytes(b"%PDF-fake")

        outcome = IngestResult(
            records=[_make_record()],
            reconciliation=ReconciliationResult(
                check_name="amex_closing_balance",
                expected_closing_minor=86304,
                derived_closing_minor=67804,
                matches=False,
            ),
            statement_period=None,
        )
        monkeypatch.setattr(
            cli_module,
            "AdapterFactory",
            lambda: _FakeFactory({"statement.pdf": outcome}),
        )
        monkeypatch.setattr(cli_module, "get_datalake", lambda: _FakeDatalake())

        result = runner.invoke(cli, ["ingest", str(pdf_path)])
        assert result.exit_code == 1
        assert "balance mismatch" in result.output
        assert "£678.04" in result.output
        assert "£863.04" in result.output

    def test_reports_multiple_reconciliations_all_match(
        self, runner, tmp_path, monkeypatch
    ):
        """A source like vanguard-pdf can produce more than one
        reconciliation result per file (one per wrapper) via the
        `reconciliations` list rather than the singular `reconciliation`."""
        pdf_path = tmp_path / "statement.pdf"
        pdf_path.write_bytes(b"%PDF-fake")

        outcome = IngestResult(
            records=[_make_record(source_type="vanguard-pdf")],
            reconciliation=None,
            statement_period=None,
            reconciliations=[
                ReconciliationResult(
                    check_name="vanguard_account_summary_isa",
                    expected_closing_minor=50015,
                    derived_closing_minor=50015,
                    matches=True,
                    account_identifier="hash_isa",
                ),
                ReconciliationResult(
                    check_name="vanguard_account_summary_pension",
                    expected_closing_minor=50100,
                    derived_closing_minor=50100,
                    matches=True,
                    account_identifier="hash_pension",
                ),
            ],
        )
        fake_datalake = _FakeDatalake()
        monkeypatch.setattr(
            cli_module,
            "AdapterFactory",
            lambda: _FakeFactory({"statement.pdf": outcome}),
        )
        monkeypatch.setattr(cli_module, "get_datalake", lambda: fake_datalake)

        result = runner.invoke(cli, ["ingest", str(pdf_path)])
        assert result.exit_code == 0
        assert "vanguard_account_summary_isa reconciles (£500.15)" in result.output
        assert "vanguard_account_summary_pension reconciles (£501.00)" in result.output
        assert (
            fake_datalake.write_calls[0]["reconciliations"] == outcome.reconciliations
        )

    def test_one_of_several_reconciliations_mismatching_fails_exit_code(
        self, runner, tmp_path, monkeypatch
    ):
        """A wrong wrapper shouldn't be masked by a correct one - ANY
        mismatch across the list fails the batch, same spirit as the
        singular `reconciliation` gate."""
        pdf_path = tmp_path / "statement.pdf"
        pdf_path.write_bytes(b"%PDF-fake")

        outcome = IngestResult(
            records=[_make_record(source_type="vanguard-pdf")],
            reconciliation=None,
            statement_period=None,
            reconciliations=[
                ReconciliationResult(
                    check_name="vanguard_account_summary_isa",
                    expected_closing_minor=99999,
                    derived_closing_minor=50015,
                    matches=False,
                    account_identifier="hash_isa",
                ),
                ReconciliationResult(
                    check_name="vanguard_account_summary_pension",
                    expected_closing_minor=50100,
                    derived_closing_minor=50100,
                    matches=True,
                    account_identifier="hash_pension",
                ),
            ],
        )
        monkeypatch.setattr(
            cli_module,
            "AdapterFactory",
            lambda: _FakeFactory({"statement.pdf": outcome}),
        )
        monkeypatch.setattr(cli_module, "get_datalake", lambda: _FakeDatalake())

        result = runner.invoke(cli, ["ingest", str(pdf_path)])
        assert result.exit_code == 1
        assert "vanguard_account_summary_isa mismatch" in result.output
        assert "vanguard_account_summary_pension reconciles" in result.output

    def test_matches_none_does_not_fail_exit_code(self, runner, tmp_path, monkeypatch):
        """matches=None means inconclusive (no anchor found) - not a known
        mismatch, so it must not set the exit code to non-zero (see
        Gotcha #17)."""
        pdf_path = tmp_path / "statement.pdf"
        pdf_path.write_bytes(b"%PDF-fake")

        outcome = IngestResult(
            records=[_make_record()],
            reconciliation=ReconciliationResult(
                check_name="amex_closing_balance",
                expected_closing_minor=None,
                derived_closing_minor=None,
                matches=None,
            ),
            statement_period=None,
        )
        monkeypatch.setattr(
            cli_module,
            "AdapterFactory",
            lambda: _FakeFactory({"statement.pdf": outcome}),
        )
        monkeypatch.setattr(cli_module, "get_datalake", lambda: _FakeDatalake())

        result = runner.invoke(cli, ["ingest", str(pdf_path)])
        assert result.exit_code == 0

    def test_continues_after_reconciliation_mismatch(
        self, runner, tmp_path, monkeypatch
    ):
        """A reconciliation mismatch must not stop the rest of the batch -
        only the final exit code reflects it (mirrors
        test_continues_after_one_bad_file for parse/detection failures)."""
        mismatched_path = tmp_path / "mismatched.pdf"
        mismatched_path.write_bytes(b"%PDF-fake")
        good_path = tmp_path / "good.pdf"
        good_path.write_bytes(b"%PDF-good")

        mismatched_outcome = IngestResult(
            records=[_make_record(filename="mismatched.pdf")],
            reconciliation=ReconciliationResult(
                check_name="amex_closing_balance",
                expected_closing_minor=86304,
                derived_closing_minor=67804,
                matches=False,
            ),
            statement_period=None,
        )
        good_outcome = IngestResult(
            records=[_make_record(filename="good.pdf")],
            reconciliation=None,
            statement_period=None,
        )
        outcomes = {
            "mismatched.pdf": mismatched_outcome,
            "good.pdf": good_outcome,
        }
        fake_datalake = _FakeDatalake()
        monkeypatch.setattr(
            cli_module, "AdapterFactory", lambda: _FakeFactory(outcomes)
        )
        monkeypatch.setattr(cli_module, "get_datalake", lambda: fake_datalake)

        result = runner.invoke(cli, ["ingest", str(mismatched_path), str(good_path)])
        assert result.exit_code == 1
        assert "mismatched.pdf: 1 record(s)" in result.output
        assert "good.pdf: 1 record(s)" in result.output
        assert len(fake_datalake.write_calls) == 2

    def test_unrecognized_format_message_and_exit_code(
        self, runner, tmp_path, monkeypatch
    ):
        bad_path = tmp_path / "unknown.pdf"
        bad_path.write_bytes(b"garbage")
        error = UnrecognizedFormatError("PDF", "amex, kroo")
        monkeypatch.setattr(
            cli_module, "AdapterFactory", lambda: _FakeFactory({"unknown.pdf": error})
        )
        monkeypatch.setattr(cli_module, "get_datalake", lambda: _FakeDatalake())

        result = runner.invoke(cli, ["ingest", str(bad_path)])
        assert result.exit_code == 1
        assert "File format not recognized" in result.output

    def test_ambiguous_format_message_and_exit_code(
        self, runner, tmp_path, monkeypatch
    ):
        bad_path = tmp_path / "ambiguous.pdf"
        bad_path.write_bytes(b"garbage")
        error = AmbiguousFormatError("amex", 0.95, "kroo", 0.95)
        monkeypatch.setattr(
            cli_module, "AdapterFactory", lambda: _FakeFactory({"ambiguous.pdf": error})
        )
        monkeypatch.setattr(cli_module, "get_datalake", lambda: _FakeDatalake())

        result = runner.invoke(cli, ["ingest", str(bad_path)])
        assert result.exit_code == 1
        assert "File format ambiguous" in result.output

    def test_generic_parse_failure_distinguished_from_detection_failure(
        self, runner, tmp_path, monkeypatch
    ):
        bad_path = tmp_path / "broken.pdf"
        bad_path.write_bytes(b"garbage")
        error = ValueError("Failed to parse PDF: something broke")
        monkeypatch.setattr(
            cli_module, "AdapterFactory", lambda: _FakeFactory({"broken.pdf": error})
        )
        monkeypatch.setattr(cli_module, "get_datalake", lambda: _FakeDatalake())

        result = runner.invoke(cli, ["ingest", str(bad_path)])
        assert result.exit_code == 1
        assert "recognized the format but failed to parse" in result.output

    def test_continues_after_one_bad_file(self, runner, tmp_path, monkeypatch):
        """One bad file must not stop the rest of the batch - only the
        final exit code reflects the failure."""
        bad_path = tmp_path / "bad.pdf"
        bad_path.write_bytes(b"garbage")
        good_path = tmp_path / "good.pdf"
        good_path.write_bytes(b"%PDF-fake")

        good_outcome = IngestResult(
            records=[_make_record(filename="good.pdf")],
            reconciliation=None,
            statement_period=None,
        )
        outcomes = {
            "bad.pdf": UnrecognizedFormatError("PDF", "amex"),
            "good.pdf": good_outcome,
        }
        fake_datalake = _FakeDatalake()
        monkeypatch.setattr(
            cli_module, "AdapterFactory", lambda: _FakeFactory(outcomes)
        )
        monkeypatch.setattr(cli_module, "get_datalake", lambda: fake_datalake)

        result = runner.invoke(cli, ["ingest", str(bad_path), str(good_path)])
        assert result.exit_code == 1
        assert "good.pdf: 1 record(s)" in result.output
        assert len(fake_datalake.write_calls) == 1

    def test_all_succeed_exit_code_zero(self, runner, tmp_path, monkeypatch):
        good_path = tmp_path / "good.pdf"
        good_path.write_bytes(b"%PDF-fake")
        good_outcome = IngestResult(
            records=[_make_record(filename="good.pdf")],
            reconciliation=None,
            statement_period=None,
        )
        monkeypatch.setattr(
            cli_module,
            "AdapterFactory",
            lambda: _FakeFactory({"good.pdf": good_outcome}),
        )
        monkeypatch.setattr(cli_module, "get_datalake", lambda: _FakeDatalake())

        result = runner.invoke(cli, ["ingest", str(good_path)])
        assert result.exit_code == 0


class TestCoverageCommand:
    def test_no_period_data_found(self, runner, monkeypatch):
        monkeypatch.setattr(cli_module, "get_datalake", lambda: object())
        monkeypatch.setattr(
            cli_module,
            "find_statement_periods",
            lambda datalake: pd.DataFrame(
                columns=[
                    "account_id",
                    "source_type",
                    "filename",
                    "period_from",
                    "period_to",
                ]
            ),
        )

        result = runner.invoke(cli, ["accounts", "coverage"])
        assert result.exit_code == 0
        assert "No statement-period data found" in result.output

    def test_lists_periods_and_flags_gaps(self, runner, monkeypatch):
        periods = pd.DataFrame(
            [
                {
                    "account_id": "acc_amex",
                    "source_type": "amex",
                    "filename": "jan.pdf",
                    "period_from": pd.Timestamp("2026-01-01"),
                    "period_to": pd.Timestamp("2026-01-31"),
                },
                {
                    "account_id": "acc_amex",
                    "source_type": "amex",
                    "filename": "mar.pdf",
                    "period_from": pd.Timestamp("2026-03-01"),
                    "period_to": pd.Timestamp("2026-03-31"),
                },
            ]
        )
        gaps = pd.DataFrame(
            [
                {
                    "account_id": "acc_amex",
                    "gap_start": pd.Timestamp("2026-01-31"),
                    "gap_end": pd.Timestamp("2026-03-01"),
                    "days": 29,
                }
            ]
        )
        monkeypatch.setattr(cli_module, "get_datalake", lambda: object())
        monkeypatch.setattr(
            cli_module, "find_statement_periods", lambda datalake: periods
        )
        monkeypatch.setattr(cli_module, "find_coverage_gaps", lambda periods: gaps)

        result = runner.invoke(cli, ["accounts", "coverage"])
        assert result.exit_code == 0
        assert "acc_amex" in result.output
        assert "2026-01-01 to 2026-01-31" in result.output
        assert "gap:" in result.output


class TestReconciliationCommand:
    def test_no_reconciliation_data_found(self, runner, monkeypatch):
        monkeypatch.setattr(cli_module, "get_datalake", lambda: object())
        monkeypatch.setattr(
            cli_module,
            "find_reconciliation_status",
            lambda datalake: pd.DataFrame(
                columns=[
                    "account_id",
                    "source_type",
                    "filename",
                    "ingestion_id",
                    "check_name",
                    "expected_opening_minor",
                    "expected_closing_minor",
                    "derived_closing_minor",
                    "matches",
                ]
            ),
        )

        result = runner.invoke(cli, ["accounts", "reconciliation"])
        assert result.exit_code == 0
        assert "No reconciliation data found" in result.output

    def test_lists_status_per_account(self, runner, monkeypatch):
        statuses = pd.DataFrame(
            [
                {
                    "account_id": "acc_amex",
                    "source_type": "amex",
                    "filename": "jan.pdf",
                    "check_name": "amex_closing_balance",
                    "expected_closing_minor": 86304,
                    "derived_closing_minor": 86304,
                    "matches": True,
                },
                {
                    "account_id": "acc_amex",
                    "source_type": "amex",
                    "filename": "feb.pdf",
                    "check_name": "amex_closing_balance",
                    "expected_closing_minor": 90000,
                    "derived_closing_minor": 67804,
                    "matches": False,
                },
            ]
        )
        monkeypatch.setattr(cli_module, "get_datalake", lambda: object())
        monkeypatch.setattr(
            cli_module, "find_reconciliation_status", lambda datalake: statuses
        )

        result = runner.invoke(cli, ["accounts", "reconciliation"])
        assert result.exit_code == 0
        assert "acc_amex" in result.output
        assert "✓ jan.pdf" in result.output
        assert "⚠ feb.pdf" in result.output


class TestContinuityCommand:
    def test_no_continuity_data_found(self, runner, monkeypatch):
        monkeypatch.setattr(cli_module, "get_datalake", lambda: object())
        monkeypatch.setattr(
            cli_module,
            "find_balance_continuity",
            lambda datalake: pd.DataFrame(
                columns=[
                    "account_id",
                    "source_type",
                    "next_source_type",
                    "filename",
                    "next_filename",
                    "expected_closing_minor",
                    "expected_opening_minor",
                    "matches",
                    "gap_related",
                ]
            ),
        )

        result = runner.invoke(cli, ["accounts", "continuity"])
        assert result.exit_code == 0
        assert "No continuity data found" in result.output

    def test_lists_continuity_per_account(self, runner, monkeypatch):
        breaks = pd.DataFrame(
            [
                {
                    "account_id": "acc_amex",
                    "source_type": "amex",
                    "next_source_type": "amex",
                    "filename": "jan.pdf",
                    "next_filename": "feb.pdf",
                    "expected_closing_minor": 86304,
                    "expected_opening_minor": 86304,
                    "matches": True,
                    "gap_related": False,
                },
                {
                    "account_id": "acc_amex",
                    "source_type": "amex",
                    "next_source_type": "amex",
                    "filename": "feb.pdf",
                    "next_filename": "mar.pdf",
                    "expected_closing_minor": 90000,
                    "expected_opening_minor": 12000,
                    "matches": False,
                    "gap_related": False,
                },
                {
                    "account_id": "acc_amex",
                    "source_type": "amex",
                    "next_source_type": "amex",
                    "filename": "mar.pdf",
                    "next_filename": "jul.pdf",
                    "expected_closing_minor": 12000,
                    "expected_opening_minor": None,
                    "matches": None,
                    "gap_related": True,
                },
            ]
        )
        monkeypatch.setattr(cli_module, "get_datalake", lambda: object())
        monkeypatch.setattr(
            cli_module, "find_balance_continuity", lambda datalake: breaks
        )

        result = runner.invoke(cli, ["accounts", "continuity"])
        assert result.exit_code == 0
        assert "acc_amex" in result.output
        assert "✓ jan.pdf -> feb.pdf: continuous" in result.output
        assert "⚠ feb.pdf -> mar.pdf: mismatch" in result.output
        assert "? mar.pdf -> jul.pdf: known coverage gap" in result.output


class TestSilverRebuildCommand:
    def _fake_result(self, log_rows=None):
        return {
            "build_id": "build_1",
            "transactions": pd.DataFrame([{"amount_minor": 100}]),
            "account_ledger": pd.DataFrame([{"balance_minor": 100}]),
            "holdings": pd.DataFrame(),
            "reconciliation_log": pd.DataFrame(
                log_rows or [],
                columns=["check_type", "matches"],
            ),
        }

    def test_strict_flag_threaded_to_run_bronze_to_silver(self, runner, monkeypatch):
        captured = {}

        def fake_run(strict_reconciliation=False):
            captured["strict_reconciliation"] = strict_reconciliation
            return self._fake_result()

        monkeypatch.setattr(cli_module, "run_bronze_to_silver", fake_run)

        result = runner.invoke(cli, ["silver", "rebuild", "--strict"])
        assert result.exit_code == 0
        assert captured["strict_reconciliation"] is True
        assert "Published Silver build build_1" in result.output

    def test_default_is_non_strict(self, runner, monkeypatch):
        captured = {}

        def fake_run(strict_reconciliation=False):
            captured["strict_reconciliation"] = strict_reconciliation
            return self._fake_result()

        monkeypatch.setattr(cli_module, "run_bronze_to_silver", fake_run)

        result = runner.invoke(cli, ["silver", "rebuild"])
        assert result.exit_code == 0
        assert captured["strict_reconciliation"] is False

    def test_mismatch_error_exits_nonzero(self, runner, monkeypatch):
        from transformers.reconciliation_log import ReconciliationMismatchError

        mismatches = pd.DataFrame(
            [
                {
                    "check_type": "bronze_self_check",
                    "account_id": "acc_amex",
                    "source_type": "amex",
                    "filename": "jan.pdf",
                    "next_filename": None,
                    "expected_closing_minor": 12000,
                    "derived_closing_minor": 99999,
                }
            ]
        )

        def fake_run(strict_reconciliation=False):
            raise ReconciliationMismatchError(mismatches)

        monkeypatch.setattr(cli_module, "run_bronze_to_silver", fake_run)

        result = runner.invoke(cli, ["silver", "rebuild", "--strict"])
        assert result.exit_code == 1
        assert "reconciliation mismatch" in result.output

    def test_reconciliation_summary_printed(self, runner, monkeypatch):
        def fake_run(strict_reconciliation=False):
            return self._fake_result(
                log_rows=[
                    {"check_type": "bronze_self_check", "matches": True},
                    {"check_type": "continuity", "matches": False},
                    {"check_type": "silver_rollforward", "matches": None},
                ]
            )

        monkeypatch.setattr(cli_module, "run_bronze_to_silver", fake_run)

        result = runner.invoke(cli, ["silver", "rebuild"])
        assert result.exit_code == 0
        assert "bronze_self_check: 1 match" in result.output
        assert "continuity: 1 mismatch" in result.output
        assert "silver_rollforward: 1 inconclusive" in result.output


class TestReconciliationFailOnMismatch:
    def test_no_flag_exits_zero_on_mismatch(self, runner, monkeypatch):
        statuses = pd.DataFrame(
            [
                {
                    "account_id": "acc_amex",
                    "source_type": "amex",
                    "filename": "jan.pdf",
                    "check_name": "amex_closing_balance",
                    "expected_closing_minor": 90000,
                    "derived_closing_minor": 67804,
                    "matches": False,
                }
            ]
        )
        monkeypatch.setattr(cli_module, "get_datalake", lambda: object())
        monkeypatch.setattr(
            cli_module, "find_reconciliation_status", lambda datalake: statuses
        )

        result = runner.invoke(cli, ["accounts", "reconciliation"])
        assert result.exit_code == 0

    def test_flag_exits_nonzero_on_mismatch(self, runner, monkeypatch):
        statuses = pd.DataFrame(
            [
                {
                    "account_id": "acc_amex",
                    "source_type": "amex",
                    "filename": "jan.pdf",
                    "check_name": "amex_closing_balance",
                    "expected_closing_minor": 90000,
                    "derived_closing_minor": 67804,
                    "matches": False,
                }
            ]
        )
        monkeypatch.setattr(cli_module, "get_datalake", lambda: object())
        monkeypatch.setattr(
            cli_module, "find_reconciliation_status", lambda datalake: statuses
        )

        result = runner.invoke(
            cli, ["accounts", "reconciliation", "--fail-on-mismatch"]
        )
        assert result.exit_code == 1

    def test_flag_exits_zero_when_all_match(self, runner, monkeypatch):
        statuses = pd.DataFrame(
            [
                {
                    "account_id": "acc_amex",
                    "source_type": "amex",
                    "filename": "jan.pdf",
                    "check_name": "amex_closing_balance",
                    "expected_closing_minor": 86304,
                    "derived_closing_minor": 86304,
                    "matches": True,
                }
            ]
        )
        monkeypatch.setattr(cli_module, "get_datalake", lambda: object())
        monkeypatch.setattr(
            cli_module, "find_reconciliation_status", lambda datalake: statuses
        )

        result = runner.invoke(
            cli, ["accounts", "reconciliation", "--fail-on-mismatch"]
        )
        assert result.exit_code == 0


class TestContinuityFailOnMismatch:
    def test_flag_exits_nonzero_on_mismatch(self, runner, monkeypatch):
        breaks = pd.DataFrame(
            [
                {
                    "account_id": "acc_amex",
                    "source_type": "amex",
                    "next_source_type": "amex",
                    "filename": "feb.pdf",
                    "next_filename": "mar.pdf",
                    "expected_closing_minor": 90000,
                    "expected_opening_minor": 12000,
                    "matches": False,
                    "gap_related": False,
                }
            ]
        )
        monkeypatch.setattr(cli_module, "get_datalake", lambda: object())
        monkeypatch.setattr(
            cli_module, "find_balance_continuity", lambda datalake: breaks
        )

        result = runner.invoke(cli, ["accounts", "continuity", "--fail-on-mismatch"])
        assert result.exit_code == 1

    def test_flag_exits_zero_on_gap_related_inconclusive(self, runner, monkeypatch):
        breaks = pd.DataFrame(
            [
                {
                    "account_id": "acc_amex",
                    "source_type": "amex",
                    "next_source_type": "amex",
                    "filename": "mar.pdf",
                    "next_filename": "jul.pdf",
                    "expected_closing_minor": 12000,
                    "expected_opening_minor": None,
                    "matches": None,
                    "gap_related": True,
                }
            ]
        )
        monkeypatch.setattr(cli_module, "get_datalake", lambda: object())
        monkeypatch.setattr(
            cli_module, "find_balance_continuity", lambda datalake: breaks
        )

        result = runner.invoke(cli, ["accounts", "continuity", "--fail-on-mismatch"])
        assert result.exit_code == 0


class TestReconciliationLogCommand:
    def test_no_log_found(self, runner, monkeypatch):
        fake_datalake = type("D", (), {"read_silver": lambda self, entity: None})()
        monkeypatch.setattr(cli_module, "get_datalake", lambda: fake_datalake)

        result = runner.invoke(cli, ["accounts", "reconciliation-log"])
        assert result.exit_code == 0
        assert "No reconciliation log found" in result.output

    def test_lists_summary_by_check_type(self, runner, monkeypatch):
        log = pd.DataFrame(
            [
                {"check_type": "bronze_self_check", "matches": True},
                {"check_type": "bronze_self_check", "matches": False},
                {"check_type": "continuity", "matches": None},
            ]
        )
        fake_datalake = type(
            "D", (), {"read_silver": lambda self, entity: log}
        )()
        monkeypatch.setattr(cli_module, "get_datalake", lambda: fake_datalake)

        result = runner.invoke(cli, ["accounts", "reconciliation-log"])
        assert result.exit_code == 0
        assert "bronze_self_check:" in result.output
        assert "match: 1" in result.output
        assert "mismatch: 1" in result.output
        assert "continuity:" in result.output
        assert "inconclusive: 1" in result.output
