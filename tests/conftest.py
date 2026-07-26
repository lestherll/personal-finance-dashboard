"""Pytest configuration and fixtures."""

import pytest


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
