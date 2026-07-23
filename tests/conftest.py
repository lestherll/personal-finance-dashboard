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
def sample_natwest_csv():
    """Sample Natwest CSV content."""
    return """Transaction Type,Transaction Date,Transaction Amount,Transaction Narrative,Balance,Balance Date
DEBIT,15/01/2024,-50.00,FUEL SHELL PETROL STATION,450.00,15/01/2024
DEBIT,15/01/2024,-75.00,ONLINE PAYMENT TO SAVINGS ACCOUNT,375.00,15/01/2024
CREDIT,16/01/2024,2000.00,SALARY RECEIVED,2375.00,16/01/2024
"""


@pytest.fixture
def sample_vanguard_csv():
    """Sample Vanguard CSV content."""
    return """ISIN,Fund Name,Quantity,Price,Value,Account Reference,Portfolio Value,Time
GB0009374884,Vanguard FTSE All-World UCITS ETF,50.00,150.25,7512.50,VA123456,50000.00,15/01/2024
GB0001702304,Vanguard UK Gilt UCITS ETF,30.00,200.10,6003.00,VA123456,50000.00,15/01/2024
"""


@pytest.fixture
def invalid_csv():
    """Invalid CSV content."""
    return """Random,Data,Here
value1,value2,value3
"""
