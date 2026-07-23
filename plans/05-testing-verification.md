# Plan: Testing & Verification

## Goal
Comprehensive test suite for core engine: unit tests, integration tests, end-to-end tests.

## Scope
- Unit tests for each component
- Integration tests between layers
- End-to-end tests with real CSV files
- Data integrity tests
- Performance tests

---

## Test Structure

```
tests/
├── conftest.py              # Fixtures, setup/teardown
├── unit/
│   ├── adapters/
│   │   ├── test_monzo_adapter.py
│   │   ├── test_natwest_adapter.py
│   │   ├── test_vanguard_adapter.py
│   │   └── test_adapter_factory.py
│   ├── transformers/
│   │   ├── test_account_linker.py
│   │   ├── test_silver_transformer.py
│   │   ├── test_account_ledger.py
│   │   ├── test_subscription_detector.py
│   │   └── test_transfer_detector.py
│   └── models/
│       └── test_schema.py
├── integration/
│   ├── test_bronze_layer.py
│   ├── test_silver_layer.py
│   ├── test_gold_layer.py
│   ├── test_job_orchestration.py
│   └── test_pipeline_flow.py
├── e2e/
│   ├── test_monzo_to_gold.py
│   ├── test_natwest_to_gold.py
│   ├── test_vanguard_to_gold.py
│   └── test_full_pipeline.py
└── fixtures/
    ├── sample_monzo.csv
    ├── sample_natwest.csv
    ├── sample_vanguard.csv
    └── expected_outputs.json
```

---

## Phase 1: Fixtures & Setup

### File: `tests/conftest.py`

```python
import pytest
import tempfile
from datetime import datetime, date
from uuid import uuid4

from db import get_db_session, init_db
from models import Account, SilverTransaction, RawFileUpload
from config import DATABASE_URL


@pytest.fixture(scope='session')
def test_db():
    """Create test database, run migrations"""
    # Use test database URL
    test_url = DATABASE_URL.replace('personal_finance', 'personal_finance_test')
    
    # Create database
    init_db(test_url)
    
    yield test_url
    
    # Cleanup (optional: keep for debugging)
    # drop_db(test_url)


@pytest.fixture
def session(test_db):
    """Database session for each test"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    
    engine = create_engine(test_db)
    Session = sessionmaker(bind=engine)
    session = Session()
    
    yield session
    
    # Cleanup
    session.rollback()
    session.close()


@pytest.fixture
def test_account(session):
    """Create a test account"""
    account = Account(
        source_type='monzo',
        external_account_id='test_acc_123',
        account_name='Test Monzo Account',
        account_type='checking',
        is_active=True,
    )
    session.add(account)
    session.commit()
    
    return account


@pytest.fixture
def sample_monzo_csv():
    """Read sample Monzo CSV file"""
    with open('tests/fixtures/sample_monzo.csv', 'r') as f:
        return f.read()


@pytest.fixture
def sample_natwest_csv():
    """Read sample Natwest CSV file"""
    with open('tests/fixtures/sample_natwest.csv', 'r') as f:
        return f.read()


@pytest.fixture
def sample_vanguard_csv():
    """Read sample Vanguard CSV file"""
    with open('tests/fixtures/sample_vanguard.csv', 'r') as f:
        return f.read()
```

### File: `tests/fixtures/sample_monzo.csv`

```csv
Transaction ID,Date,Time,Type,Name,Emoji,Category,Amount,Currency,Local Amount,Local Currency,Notes,Receipt,Description
tx_abc123,15/01/2024,14:30:00,card_payment,Tesco Groceries,🛒,Groceries,-25.50,GBP,-25.50,GBP,,0,Tesco Stores Ltd
tx_abc124,15/01/2024,15:45:00,card_payment,Sainsbury Coffee,☕,Restaurants & Cafes,-5.20,GBP,-5.20,GBP,,0,Sainsbury's Plc
tx_abc125,16/01/2024,09:00:00,transfer_in,Salary Deposit,💷,Transfers,2500.00,GBP,2500.00,GBP,,0,Employer Ltd
```

### File: `tests/fixtures/sample_natwest.csv`

```csv
Transaction Type,Transaction Date,Transaction Amount,Transaction Narrative,Balance,Balance Date
DEBIT,15/01/2024,-50.00,FUEL SHELL PETROL STATION,450.00,15/01/2024
DEBIT,15/01/2024,-75.00,ONLINE PAYMENT TO SAVINGS ACCOUNT,375.00,15/01/2024
CREDIT,16/01/2024,2000.00,SALARY RECEIVED,2375.00,16/01/2024
```

### File: `tests/fixtures/sample_vanguard.csv`

```csv
ISIN,Fund Name,Quantity,Price,Value,Account Reference,Portfolio Value,Time
GB0009374884,Vanguard FTSE All-World UCITS ETF,50.00,150.25,7512.50,VA123456,50000.00,15/01/2024
GB0001702304,Vanguard UK Gilt UCITS ETF,30.00,200.10,6003.00,VA123456,50000.00,15/01/2024
```

---

## Phase 2: Unit Tests

### File: `tests/unit/adapters/test_monzo_adapter.py`

```python
import pytest
from adapters.monzo_adapter import MonzoAdapter
from adapters.base import RawRecord


@pytest.fixture
def adapter():
    return MonzoAdapter()


def test_monzo_validate_correct_format(adapter, sample_monzo_csv):
    """Monzo CSV with correct headers validates"""
    is_valid, confidence = adapter.validate(sample_monzo_csv)
    assert is_valid
    assert confidence >= 0.8


def test_monzo_validate_wrong_format(adapter):
    """Non-Monzo CSV fails validation"""
    csv_content = "Date,Amount,Description\n15/01/2024,100.00,Test"
    is_valid, confidence = adapter.validate(csv_content)
    assert not is_valid


def test_monzo_parse_records(adapter, sample_monzo_csv):
    """Monzo CSV parsed correctly"""
    records = adapter.parse(sample_monzo_csv, 'test.csv', 'abc123')
    
    assert len(records) == 3
    assert all(isinstance(r, RawRecord) for r in records)
    assert all(r.source_type == 'monzo' for r in records)


def test_monzo_source_key_generation(adapter, sample_monzo_csv):
    """Source keys are generated from Transaction IDs"""
    records = adapter.parse(sample_monzo_csv, 'test.csv', 'abc123')
    
    assert records[0].source_key == 'monzo_txn_tx_abc123'
    assert records[1].source_key == 'monzo_txn_tx_abc124'


def test_monzo_deterministic_source_key(adapter):
    """Source key is deterministic (same input → same key)"""
    raw_data = {'Transaction ID': 'tx_abc123'}
    
    key1 = adapter.generate_source_key(raw_data, 1)
    key2 = adapter.generate_source_key(raw_data, 1)
    
    assert key1 == key2


def test_monzo_parse_date_time(adapter, sample_monzo_csv):
    """Monzo dates and times parsed correctly"""
    records = adapter.parse(sample_monzo_csv, 'test.csv', 'abc123')
    
    assert records[0].raw_data['Date'] == '15/01/2024'
    assert records[0].raw_data['Time'] == '14:30:00'


def test_monzo_handle_empty_file(adapter):
    """Empty Monzo file handled gracefully"""
    records = adapter.parse('', 'test.csv', 'abc123')
    assert len(records) == 0
```

### File: `tests/unit/transformers/test_silver_transformer.py`

```python
import pytest
from datetime import date, time
from uuid import uuid4

from transformers.silver_transformer import SilverTransformer
from adapters.base import RawRecord


@pytest.fixture
def account_id():
    return uuid4()


@pytest.fixture
def monzo_raw_record(account_id):
    return RawRecord(
        source_key='monzo_txn_abc123',
        source_type='monzo',
        raw_data={
            'Date': '15/01/2024',
            'Time': '14:30:00',
            'Amount': '-25.50',
            'Name': 'Tesco Groceries',
            'Currency': 'GBP',
            'Category': 'Groceries',
        },
        filename='test.csv',
        file_hash='abc123',
        upload_timestamp=datetime.now(),
        line_number=2,
    )


def test_monzo_normalization(monzo_raw_record, account_id):
    """Monzo transaction normalized correctly"""
    normalized = SilverTransformer.normalize_monzo(monzo_raw_record, account_id)
    
    assert normalized['transaction_date'] == date(2024, 1, 15)
    assert normalized['transaction_time'] == time(14, 30, 0)
    assert normalized['amount_gbp'] == 25.50
    assert normalized['direction'] == 'debit'
    assert normalized['merchant_name'] == 'Tesco Groceries'
    assert normalized['account_id'] == account_id


def test_monzo_foreign_transaction(account_id):
    """Monzo foreign transaction handled"""
    raw_record = RawRecord(
        source_key='monzo_txn_eur123',
        source_type='monzo',
        raw_data={
            'Date': '15/01/2024',
            'Time': '14:30:00',
            'Amount': '-50.00',
            'Currency': 'EUR',
            'Local Currency': 'EUR',
            'Local Amount': '-50.00',
            'Name': 'Paris Cafe',
        },
        filename='test.csv',
        file_hash='abc123',
        upload_timestamp=datetime.now(),
        line_number=2,
    )
    
    normalized = SilverTransformer.normalize_monzo(raw_record, account_id)
    
    assert normalized['original_currency'] == 'EUR'
    assert normalized['amount_original'] == 50.00
    assert normalized['exchange_rate'] is not None


def test_monzo_invalid_date():
    """Invalid Monzo date raises error"""
    raw_record = RawRecord(
        source_key='monzo_txn_bad',
        source_type='monzo',
        raw_data={'Date': 'invalid_date', 'Amount': '25.50'},
        filename='test.csv',
        file_hash='abc123',
        upload_timestamp=datetime.now(),
        line_number=2,
    )
    
    with pytest.raises(ValidationError):
        SilverTransformer.normalize_monzo(raw_record, uuid4())


def test_natwest_normalization(account_id):
    """Natwest transaction normalized correctly"""
    raw_record = RawRecord(
        source_key='natwest_txn_...',
        source_type='natwest',
        raw_data={
            'Transaction Date': '15/01/2024',
            'Transaction Amount': '-50.00',
            'Transaction Narrative': 'FUEL SHELL PETROL',
            'Balance': '450.00',
        },
        filename='test.csv',
        file_hash='abc123',
        upload_timestamp=datetime.now(),
        line_number=2,
    )
    
    normalized = SilverTransformer.normalize_natwest(raw_record, account_id)
    
    assert normalized['transaction_date'] == date(2024, 1, 15)
    assert normalized['transaction_time'] is None  # Natwest has no time
    assert normalized['amount_gbp'] == 50.00
    assert normalized['direction'] == 'debit'
```

### File: `tests/unit/transformers/test_subscription_detector.py`

```python
import pytest
from datetime import date, timedelta
from uuid import uuid4

from transformers.subscription_detector import detect_subscriptions
from db import get_db_session
from models import Account, SilverTransaction


@pytest.fixture
def account_with_transactions(session, test_account):
    """Create account with subscription transactions"""
    # Create 4 monthly Netflix transactions
    for month in range(1, 5):
        txn = SilverTransaction(
            bronze_source_key=f'sub_test_{month}',
            source_type='monzo',
            account_id=test_account.id,
            transaction_date=date(2024, month, 15),
            amount_original=9.99,
            original_currency='GBP',
            amount_gbp=9.99,
            direction='debit',
            merchant_name='Netflix',
            status='posted',
            ingested_at=datetime.now(),
        )
        session.add(txn)
    
    session.commit()
    return test_account


def test_monthly_subscription_detected(account_with_transactions):
    """Regular monthly subscription detected"""
    detected = detect_subscriptions(account_with_transactions.id)
    
    assert len(detected) == 1
    sub = detected[0]
    assert sub['merchant_name'] == 'Netflix'
    assert sub['expected_frequency'] == 'monthly'
    assert sub['occurrence_count'] == 4
    assert sub['confidence_score'] >= 0.8


def test_irregular_not_detected(session, test_account):
    """Irregular transactions not flagged as subscriptions"""
    # Create irregular transactions (10, 20, 40 days apart)
    dates = [date(2024, 1, 1), date(2024, 1, 11), date(2024, 1, 31), date(2024, 3, 12)]
    
    for d in dates:
        txn = SilverTransaction(
            bronze_source_key=f'irregular_{d}',
            source_type='monzo',
            account_id=test_account.id,
            transaction_date=d,
            amount_original=50.00,
            original_currency='GBP',
            amount_gbp=50.00,
            direction='debit',
            merchant_name='Irregular Store',
            status='posted',
            ingested_at=datetime.now(),
        )
        session.add(txn)
    
    session.commit()
    
    detected = detect_subscriptions(test_account.id)
    
    # Should not detect as subscription (std_dev too high)
    subscriptions = [s for s in detected if s['merchant_name'] == 'Irregular Store']
    assert len(subscriptions) == 0


def test_insufficient_history():
    """Subscription not detected with < 2 occurrences"""
    account = create_account('monzo', 'test_acc')
    
    # Single transaction
    create_silver_transaction(account, date(2024, 1, 15), 'Netflix', 9.99)
    
    detected = detect_subscriptions(account.id)
    
    assert len(detected) == 0
```

---

## Phase 3: Integration Tests

### File: `tests/integration/test_pipeline_flow.py`

```python
import pytest
from datetime import datetime, date
from hashlib import sha256

from db import get_db_session
from models import RawFileUpload, SilverTransaction, GoldTransaction, Account
from adapters.factory import AdapterFactory
from transformers.account_linker import get_or_create_account
from transformers.silver_transformer import SilverTransformer
from tasks.transform_bronze_to_silver import transform_bronze_to_silver
from tasks.enrich_silver_to_gold import enrich_silver_to_gold


def test_full_pipeline_monzo_to_gold(session, sample_monzo_csv):
    """
    Full end-to-end: Monzo CSV → Bronze → Silver → Gold
    """
    # 1. Parse CSV
    factory = AdapterFactory()
    file_hash = sha256(sample_monzo_csv.encode()).hexdigest()
    raw_records = factory.ingest(sample_monzo_csv, 'test.csv', file_hash)
    
    assert len(raw_records) == 3
    
    # 2. Insert to bronze
    for record in raw_records:
        bronze = RawFileUpload(
            file_hash=file_hash,
            filename='test.csv',
            source_key=record.source_key,
            source_type=record.source_type,
            raw_data=record.raw_data,
            upload_timestamp=record.upload_timestamp,
            line_number=record.line_number,
        )
        session.add(bronze)
    
    session.commit()
    
    # 3. Run transformation job (manually, without Celery)
    result = transform_bronze_to_silver.apply_async().get()
    
    assert result['status'] == 'success'
    assert result['records_processed'] == 3
    
    # Verify silver layer
    silver_count = session.query(SilverTransaction).count()
    assert silver_count == 3
    
    # 4. Run enrichment job
    result = enrich_silver_to_gold.apply_async().get()
    
    # Verify gold layer
    gold_count = session.query(GoldTransaction).count()
    assert gold_count == 3
    
    # Verify enrichment
    gold_txns = session.query(GoldTransaction).all()
    assert gold_txns[0].category is not None  # Should be categorized


def test_idempotent_rerun(session, sample_monzo_csv):
    """Running pipeline twice doesn't create duplicates"""
    # First run
    factory = AdapterFactory()
    file_hash = sha256(sample_monzo_csv.encode()).hexdigest()
    raw_records = factory.ingest(sample_monzo_csv, 'test.csv', file_hash)
    
    for record in raw_records:
        bronze = RawFileUpload(
            file_hash=file_hash,
            filename='test.csv',
            source_key=record.source_key,
            source_type=record.source_type,
            raw_data=record.raw_data,
            upload_timestamp=record.upload_timestamp,
            line_number=record.line_number,
        )
        session.add(bronze)
    
    session.commit()
    
    transform_bronze_to_silver.apply_async().get()
    enrich_silver_to_gold.apply_async().get()
    
    # Second run (same file hash)
    result = transform_bronze_to_silver.apply_async().get()
    
    assert result['records_processed'] == 0  # No new records
    
    # Verify no duplicates
    silver_count = session.query(SilverTransaction).count()
    assert silver_count == 3  # Still 3, not 6
```

### File: `tests/integration/test_data_integrity.py`

```python
def test_no_orphan_transactions(session, sample_natwest_csv):
    """All transactions linked to valid accounts"""
    # Run pipeline
    ...
    
    # Query for orphan transactions
    orphans = session.query(SilverTransaction).filter(
        SilverTransaction.account_id == None
    ).all()
    
    assert len(orphans) == 0


def test_transfer_not_double_counted(session):
    """Transfer between own accounts doesn't inflate balance"""
    # Create two accounts
    acc1 = create_account('monzo', 'monzo_123')
    acc2 = create_account('natwest', 'natwest_456')
    
    # Create transfer: acc1 → acc2
    create_silver_transaction(acc1, date(2024, 1, 15), 'Transfer to Natwest', -100.00)
    create_silver_transaction(acc2, date(2024, 1, 15), 'Received from Monzo', 100.00)
    
    # Run enrichment
    enrich_silver_to_gold.apply_async().get()
    
    # Check that transfer is detected
    transfers = session.query(Transfer).all()
    assert len(transfers) == 1
    
    # Check that net flow doesn't double-count
    # (Both accounts have it, but one is incoming, one outgoing → net = 0)


def test_duplicate_file_rejection(session):
    """Re-uploading same file doesn't create duplicates"""
    csv_content = "..."
    file_hash = sha256(csv_content.encode()).hexdigest()
    
    # Insert first upload
    bronze1 = RawFileUpload(
        file_hash=file_hash,
        filename='export.csv',
        source_key='record_1',
        source_type='monzo',
        raw_data={...},
        upload_timestamp=datetime.now(),
        line_number=2,
    )
    session.add(bronze1)
    session.commit()
    
    # Try to insert same file again
    bronze2 = RawFileUpload(
        file_hash=file_hash,
        filename='export.csv',
        source_key='record_1',
        source_type='monzo',
        raw_data={...},
        upload_timestamp=datetime.now(),
        line_number=2,
    )
    
    with pytest.raises(IntegrityError):
        session.add(bronze2)
        session.commit()
```

---

## Phase 4: End-to-End Tests

### File: `tests/e2e/test_full_pipeline.py`

```python
@pytest.mark.e2e
def test_monzo_csv_to_gold_with_real_file(session):
    """
    Real-world test: Load actual Monzo export, run full pipeline, verify results
    """
    with open('tests/fixtures/sample_monzo.csv', 'r') as f:
        csv_content = f.read()
    
    file_hash = sha256(csv_content.encode()).hexdigest()
    
    # 1. Ingest
    factory = AdapterFactory()
    raw_records = factory.ingest(csv_content, 'monzo_export_2024.csv', file_hash)
    
    for record in raw_records:
        session.add(RawFileUpload(
            file_hash=file_hash,
            filename='monzo_export_2024.csv',
            source_key=record.source_key,
            source_type=record.source_type,
            raw_data=record.raw_data,
            upload_timestamp=record.upload_timestamp,
            line_number=record.line_number,
        ))
    session.commit()
    
    # 2. Transform
    result1 = transform_bronze_to_silver.apply_async().get()
    assert result1['status'] == 'success'
    
    result2 = enrich_silver_to_gold.apply_async().get()
    assert result2['status'] == 'success'
    
    # 3. Verify end state
    account = session.query(Account).first()
    assert account is not None
    
    transactions = session.query(GoldTransaction).all()
    assert len(transactions) > 0
    
    # Each transaction should have:
    # - Valid date
    # - Valid amount
    # - Valid direction
    # - Valid account link
    # - Categorization (if applicable)
    
    for txn in transactions:
        assert txn.transaction_date is not None
        assert txn.amount_gbp > 0
        assert txn.direction in ['credit', 'debit']
        assert txn.account_id is not None
```

---

## Phase 5: Performance Tests

### File: `tests/performance/test_pipeline_speed.py`

```python
import pytest
import time

@pytest.mark.performance
def test_ingest_1000_records_under_5s():
    """Ingest + transform 1000 records in < 5 seconds"""
    
    # Generate 1000 records
    csv_lines = ["Transaction ID,Date,Time,Type,Name,Emoji,Category,Amount,Currency,Local Amount,Local Currency,Notes,Receipt,Description"]
    for i in range(1000):
        csv_lines.append(f"tx_{i:06d},15/01/2024,14:30:00,card_payment,Merchant {i},🛒,Groceries,{-10.00 - i*0.01},GBP,-10.00,GBP,,0,Merchant")
    
    csv_content = "\n".join(csv_lines)
    
    # Time the pipeline
    start = time.time()
    
    factory = AdapterFactory()
    file_hash = sha256(csv_content.encode()).hexdigest()
    raw_records = factory.ingest(csv_content, 'test.csv', file_hash)
    
    # Insert to bronze + transform
    for record in raw_records:
        session.add(RawFileUpload(...))
    session.commit()
    
    transform_bronze_to_silver.apply_async().get()
    enrich_silver_to_gold.apply_async().get()
    
    elapsed = time.time() - start
    
    assert elapsed < 5.0, f"Pipeline took {elapsed}s, expected < 5s"
```

---

## Success Criteria

✅ All unit tests pass (adapters, transformers, models)
✅ All integration tests pass (layer interactions)
✅ All end-to-end tests pass (real CSV files)
✅ No data integrity violations
✅ Pipeline is idempotent (safe to rerun)
✅ Error handling works (errors logged, not stopping)
✅ Performance is acceptable (1000 records < 5 seconds)
✅ Coverage > 80% (critical paths)

---

## Running Tests

```bash
# All tests
pytest tests/

# Unit only
pytest tests/unit/

# Integration only
pytest tests/integration/

# E2E only
pytest tests/e2e/ -m e2e

# Performance only
pytest tests/performance/ -m performance

# With coverage
pytest tests/ --cov=adapters --cov=transformers --cov=db --cov-report=html

# Specific test
pytest tests/unit/adapters/test_monzo_adapter.py::test_monzo_parse_records
```

---

## Next Step
After tests pass, create CLI tool for manual testing and demo.
