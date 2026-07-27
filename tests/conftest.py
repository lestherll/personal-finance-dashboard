"""Pytest configuration and fixtures."""

import json

import pytest

_SYNTHETIC_ACCOUNT_MAP = {
    "identifiers": {
        "fd7a2651d39e": {
            "account_id": "acc_kroo_current",
            "display_name": "Kroo Test",
            "account_type": "current",
        },
        "43ae9e53d8a2": {
            "account_id": "acc_natwest_current",
            "display_name": "Natwest Test",
            "account_type": "current",
        },
        "8765efc92b23": {
            "account_id": "acc_firstdirect_credit",
            "display_name": "First Direct Test",
            "account_type": "current",
        },
        "63e97de2060d": {
            "account_id": "acc_amex_credit_1",
            "display_name": "Amex Test",
            "account_type": "credit",
        },
        "992add198186": {
            "account_id": "acc_vanguard_isa_test",
            "display_name": "Vanguard ISA Test",
            "account_type": "investment",
        },
        "4fa9c17f5f09": {
            "account_id": "acc_monzo_current",
            "display_name": "Monzo PDF Test",
            "account_type": "current",
        },
        "263b465ff6a8": {
            "account_id": "acc_chase_current",
            "display_name": "Chase Test",
            "account_type": "credit",
        },
    },
    "source_type_fallback": {
        "monzo": {
            "account_id": "acc_monzo_current",
            "display_name": "Monzo Test",
            "account_type": "current",
        },
        "kroo": {
            "account_id": "acc_kroo_current",
            "display_name": "Kroo Test",
            "account_type": "current",
        },
        "natwest-transactions": {
            "account_id": "acc_natwest_current",
            "display_name": "Natwest Test",
            "account_type": "current",
        },
        "natwest-statement": {
            "account_id": "acc_natwest_current",
            "display_name": "Natwest Test",
            "account_type": "current",
        },
        "firstdirect": {
            "account_id": "acc_firstdirect_credit",
            "display_name": "First Direct Test",
            "account_type": "current",
        },
        "amex": {
            "account_id": "acc_amex_credit_1",
            "display_name": "Amex Test",
            "account_type": "credit",
        },
        "vanguard-pdf": {
            "account_id": "acc_vanguard_isa_test",
            "display_name": "Vanguard ISA Test",
            "account_type": "investment",
        },
        "monzo-pdf": {
            "account_id": "acc_monzo_pdf_test",
            "display_name": "Monzo PDF Test",
            "account_type": "current",
        },
        "chase": {
            "account_id": "acc_chase_current",
            "display_name": "Chase Test",
            "account_type": "credit",
        },
        "monzo-flex": {
            "account_id": "acc_monzo_pdf_test",
            "display_name": "Monzo Flex Test",
            "account_type": "current",
        },
    },
}


@pytest.fixture
def isolated_account_map(tmp_path, monkeypatch):
    """Point transformers.account_config at a synthetic, repo-local account
    map so tests never depend on the gitignored data/account_map.json.

    Must patch the module-bound name directly - account_config.py imports
    ACCOUNT_MAP_PATH into its own namespace at load time
    (`from config import ACCOUNT_MAP_PATH`), so patching
    config.ACCOUNT_MAP_PATH has no effect on _load()'s lookup.
    """
    path = tmp_path / "account_map.json"
    path.write_text(json.dumps(_SYNTHETIC_ACCOUNT_MAP))
    monkeypatch.setattr("transformers.account_config.ACCOUNT_MAP_PATH", path)
    return path


@pytest.fixture
def sample_monzo_csv():
    """Sample Monzo CSV content."""
    return """Transaction ID,Date,Time,Type,Name,Emoji,Category,Amount,Currency,Local Amount,Local Currency,Notes,Receipt,Description
tx_abc123,15/01/2024,14:30:00,card_payment,Tesco Groceries,🛒,Groceries,-25.50,GBP,-25.50,GBP,,0,Tesco Stores Ltd
tx_abc124,15/01/2024,15:45:00,card_payment,Sainsbury Coffee,☕,Restaurants & Cafes,-5.20,GBP,-5.20,GBP,,0,Sainsbury's Plc
tx_abc125,16/01/2024,09:00:00,transfer_in,Salary Deposit,💷,Transfers,2500.00,GBP,2500.00,GBP,,0,Employer Ltd
"""


@pytest.fixture
def invalid_csv():
    """Invalid CSV content."""
    return """Random,Data,Here
value1,value2,value3
"""
