"""Tests for the account-mapping CLI commands."""

import json

import pandas as pd
import pytest
from click.testing import CliRunner

import cli as cli_module
from cli import cli


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
