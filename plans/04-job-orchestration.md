# Plan: Job Orchestration (Celery)

## Goal
Implement async transformation pipeline: Bronze→Silver→Gold with idempotency, error handling, and retry logic.

## Scope
- Celery setup + Redis broker
- Bronze→Silver transformation job
- Silver→Gold enrichment job
- Job chaining + triggering
- Error recovery + logging

---

## Phase 1: Celery Setup

### File: `config.py`

```python
import os

# Database
DATABASE_URL = os.getenv(
    'DATABASE_URL',
    'postgresql://localhost/personal_finance'
)

# Redis (Celery broker)
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')

# Celery
CELERY_CONFIG = {
    'broker_url': REDIS_URL,
    'result_backend': REDIS_URL,
    'broker_connection_retry_on_startup': True,
    'task_serializer': 'json',
    'accept_content': ['json'],
    'result_serializer': 'json',
    'timezone': 'UTC',
    'enable_utc': True,
    'task_track_started': True,
    'task_time_limit': 30 * 60,  # 30 minutes hard limit
    'task_soft_time_limit': 25 * 60,  # 25 minutes soft limit
}
```

### File: `tasks/__init__.py`

```python
from celery import Celery
from config import CELERY_CONFIG, REDIS_URL
import logging

# Create Celery app
celery_app = Celery('finance_pipeline')
celery_app.config_from_object('config:CELERY_CONFIG')

# Autodiscover tasks from all task modules
celery_app.autodiscover_tasks(['tasks'])

logger = logging.getLogger(__name__)

@celery_app.task(bind=True)
def debug_task(self):
    """Test task to verify Celery is working"""
    logger.info(f'Request: {self.request!r}')
```

**Tests:**

```python
def test_celery_connection():
    """Celery connects to Redis"""
    result = debug_task.apply_async()
    assert result.state in ['PENDING', 'SUCCESS']

def test_celery_task_execution():
    """Celery can execute tasks"""
    result = debug_task.apply_async()
    assert result.get(timeout=5) is not None
```

---

## Phase 2: Bronze→Silver Transformation Job

### File: `tasks/transform_bronze_to_silver.py`

```python
from celery import Celery, Task
from celery.exceptions import SoftTimeLimitExceeded, Retry
from datetime import datetime
from uuid import UUID
import logging

from db import get_db_session
from models import (
    RawFileUpload, SilverTransaction, Account, 
    QualityIssue, AccountLedger
)
from transformers.account_linker import get_or_create_account
from transformers.silver_transformer import SilverTransformer
from transformers.account_ledger import build_account_ledger
from tasks import celery_app

logger = logging.getLogger(__name__)


class TransformTask(Task):
    """Base task with error handling"""
    autoretry_for = (Exception,)
    retry_kwargs = {'max_retries': 3}
    retry_backoff = True
    retry_backoff_max = 600  # 10 minutes
    retry_jitter = True


@celery_app.task(base=TransformTask, bind=True, name='transform_bronze_to_silver')
def transform_bronze_to_silver(self, batch_size: int = 1000):
    """
    Async job: Transform unprocessed bronze records to silver.
    
    Idempotent: if record already in silver, skip.
    Error handling: log failures to quality_issues, mark processed anyway.
    
    Flow:
    1. Fetch unprocessed bronze records (processed=FALSE)
    2. For each record:
       a. Check if already in silver (idempotency)
       b. Link to account
       c. Transform to silver schema
       d. Insert into silver.transactions
       e. If Natwest: extract balance, insert to account_ledger
       f. Mark bronze.processed=TRUE
    3. On error: log to quality_issues, mark processed=TRUE
    4. Trigger next job (enrich_silver_to_gold) if processed > 0
    
    Args:
        batch_size: Process this many records per job invocation
    """
    logger.info("🔄 Starting Bronze→Silver transformation")
    
    session = get_db_session()
    
    try:
        # Fetch unprocessed records
        unprocessed = session.query(RawFileUpload).filter(
            RawFileUpload.processed == False
        ).order_by(
            RawFileUpload.created_at.asc()
        ).limit(batch_size).all()
        
        if not unprocessed:
            logger.info("✓ No unprocessed records")
            return {'status': 'success', 'records_processed': 0}
        
        logger.info(f"Processing {len(unprocessed)} bronze records")
        
        processed_count = 0
        error_count = 0
        
        for bronze_record in unprocessed:
            record_id = bronze_record.id
            source_key = bronze_record.source_key
            
            try:
                # Idempotency check: already in silver?
                existing = session.query(SilverTransaction).filter(
                    SilverTransaction.bronze_source_key == source_key
                ).first()
                
                if existing:
                    logger.debug(f"⊘ {source_key} already in silver, skipping")
                    bronze_record.processed = True
                    bronze_record.processed_at = datetime.now()
                    session.commit()
                    processed_count += 1
                    continue
                
                # Account linking
                try:
                    account_id = get_or_create_account(
                        bronze_record.source_type,
                        bronze_record.raw_data
                    )
                except Exception as e:
                    logger.error(f"✗ Account linking failed for {source_key}: {e}")
                    log_quality_issue(
                        session,
                        transaction_id=None,
                        issue_type='account_linking_error',
                        severity='error',
                        description=f"Failed to link account: {str(e)}"
                    )
                    bronze_record.processed = True
                    bronze_record.processing_error = f"Account linking: {str(e)}"
                    bronze_record.processed_at = datetime.now()
                    session.commit()
                    error_count += 1
                    continue
                
                # Transform to silver schema
                try:
                    if bronze_record.source_type == 'vanguard':
                        # Vanguard records are holdings, not transactions
                        # Skip; handled separately
                        logger.debug(f"⊘ {source_key} is Vanguard holding, skipping transactions")
                        bronze_record.processed = True
                        bronze_record.processed_at = datetime.now()
                        session.commit()
                        processed_count += 1
                        continue
                    
                    silver_data = SilverTransformer.to_silver(
                        bronze_record.raw_data,
                        bronze_record.source_type,
                        account_id
                    )
                
                except Exception as e:
                    logger.error(f"✗ Silver transformation failed for {source_key}: {e}")
                    log_quality_issue(
                        session,
                        issue_type='silver_transformation_error',
                        severity='error',
                        description=str(e)
                    )
                    bronze_record.processed = True
                    bronze_record.processing_error = f"Transformation: {str(e)}"
                    bronze_record.processed_at = datetime.now()
                    session.commit()
                    error_count += 1
                    continue
                
                # Insert into silver.transactions
                try:
                    silver_txn = SilverTransaction(
                        bronze_source_key=bronze_record.source_key,
                        source_type=bronze_record.source_type,
                        account_id=account_id,
                        transaction_date=silver_data['transaction_date'],
                        transaction_time=silver_data['transaction_time'],
                        amount_original=silver_data['amount_original'],
                        original_currency=silver_data['original_currency'],
                        amount_gbp=silver_data['amount_gbp'],
                        exchange_rate=silver_data.get('exchange_rate'),
                        exchange_rate_source=silver_data.get('exchange_rate_source'),
                        direction=silver_data['direction'],
                        merchant_name=silver_data['merchant_name'],
                        status=silver_data['status'],
                        ingested_at=datetime.now(),
                        ingested_from_file=bronze_record.filename,
                    )
                    session.add(silver_txn)
                    session.flush()  # Get ID
                    silver_txn_id = silver_txn.id
                
                except Exception as e:
                    logger.error(f"✗ Failed to insert silver transaction for {source_key}: {e}")
                    log_quality_issue(
                        session,
                        issue_type='silver_insert_error',
                        severity='error',
                        description=str(e)
                    )
                    session.rollback()
                    bronze_record.processed = True
                    bronze_record.processing_error = f"Silver insert: {str(e)}"
                    bronze_record.processed_at = datetime.now()
                    session.commit()
                    error_count += 1
                    continue
                
                # If Natwest: extract balance and insert to account_ledger
                if bronze_record.source_type == 'natwest':
                    try:
                        balance_str = bronze_record.raw_data.get('Balance')
                        snapshot_date_str = bronze_record.raw_data.get('Balance Date') or \
                                           bronze_record.raw_data.get('Transaction Date')
                        
                        if balance_str and snapshot_date_str:
                            from datetime import datetime as dt
                            balance = float(balance_str)
                            snapshot_date = dt.strptime(snapshot_date_str, '%d/%m/%Y').date()
                            
                            # Upsert (dedup by date)
                            existing_ledger = session.query(AccountLedger).filter(
                                AccountLedger.account_id == account_id,
                                AccountLedger.snapshot_date == snapshot_date
                            ).first()
                            
                            if not existing_ledger:
                                ledger = AccountLedger(
                                    account_id=account_id,
                                    snapshot_date=snapshot_date,
                                    closing_balance=balance,
                                    source_type='natwest',
                                    source_field='Balance',
                                )
                                session.add(ledger)
                    
                    except Exception as e:
                        logger.warning(f"⚠ Failed to extract Natwest balance: {e}")
                        # Don't fail; balance is informational
                
                # Mark bronze as processed
                bronze_record.processed = True
                bronze_record.processed_at = datetime.now()
                session.commit()
                
                logger.debug(f"✓ Transformed {source_key}")
                processed_count += 1
            
            except SoftTimeLimitExceeded:
                logger.warning(f"⏱ Soft time limit reached, will retry")
                session.rollback()
                raise
            
            except Exception as e:
                logger.error(f"✗ Unexpected error for {source_key}: {e}")
                session.rollback()
                error_count += 1
        
        logger.info(f"✓ Bronze→Silver complete: {processed_count} processed, {error_count} errors")
        
        # Trigger enrichment job if we processed anything
        if processed_count > 0:
            logger.info("🔄 Triggering Silver→Gold enrichment")
            enrich_silver_to_gold.delay()
        
        return {
            'status': 'success',
            'records_processed': processed_count,
            'errors': error_count
        }
    
    except Exception as e:
        logger.error(f"✗ Fatal error in Bronze→Silver: {e}")
        session.rollback()
        raise self.retry(exc=e)
    
    finally:
        session.close()


def log_quality_issue(session, transaction_id=None, issue_type=None, severity=None, description=None):
    """Log to quality_issues table"""
    issue = QualityIssue(
        transaction_id=transaction_id,
        issue_type=issue_type,
        severity=severity,
        description=description,
    )
    session.add(issue)
    session.commit()
```

**Tests:**

```python
def test_bronze_to_silver_idempotent():
    """Running job twice doesn't duplicate records"""
    # Create bronze record
    bronze = create_bronze_record('monzo', {'account_id': 'acc_123', ...})
    
    # Run job
    result1 = transform_bronze_to_silver.apply_async().get()
    assert result1['records_processed'] == 1
    
    # Run again
    result2 = transform_bronze_to_silver.apply_async().get()
    assert result2['records_processed'] == 0  # Should skip (already processed)
    
    # Verify single silver record
    session = get_db_session()
    count = session.query(SilverTransaction).count()
    assert count == 1

def test_error_logged_to_quality_issues():
    """Transformation error logged, doesn't stop job"""
    # Create bronze record with invalid data
    bronze = create_bronze_record('monzo', {'account_id': 'acc_123', 'Date': 'invalid'})
    
    result = transform_bronze_to_silver.apply_async().get()
    
    # Job should complete, but error logged
    session = get_db_session()
    issues = session.query(QualityIssue).filter(
        QualityIssue.issue_type == 'silver_transformation_error'
    ).all()
    assert len(issues) > 0

def test_natwest_balance_extracted():
    """Natwest balance extracted to account_ledger"""
    bronze = create_bronze_record('natwest', {
        'Transaction Date': '15/01/2024',
        'Balance': '1000.00',
        ...
    })
    
    transform_bronze_to_silver.apply_async().get()
    
    session = get_db_session()
    ledger = session.query(AccountLedger).first()
    assert ledger is not None
    assert ledger.closing_balance == 1000.00
```

---

## Phase 3: Silver→Gold Enrichment Job

### File: `tasks/enrich_silver_to_gold.py`

```python
from celery import Celery
from datetime import datetime
import logging

from db import get_db_session
from models import SilverTransaction, GoldTransaction, Account, Subscription, Transfer, QualityIssue
from transformers.subscription_detector import detect_subscriptions
from transformers.transfer_detector import detect_transfers
from tasks import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, name='enrich_silver_to_gold')
def enrich_silver_to_gold(self):
    """
    Async job: Enrich silver transactions + detect patterns.
    
    Flow:
    1. For each unprocessed silver transaction:
       - Categorize
       - Insert into gold.transactions
    2. For each account with new transactions:
       - Detect subscriptions
       - Upsert into gold.subscriptions
    3. Detect transfers between owned accounts
    4. Calculate account snapshots (daily aggregates)
    """
    logger.info("🔄 Starting Silver→Gold enrichment")
    
    session = get_db_session()
    
    try:
        # 1. Enrich transactions
        logger.info("Enriching transactions...")
        
        unprocessed = session.query(SilverTransaction).filter(
            ~SilverTransaction.silver_transactions.any(
                GoldTransaction.silver_transaction_id == SilverTransaction.id
            )
        ).all()
        
        enriched_count = 0
        
        for silver_txn in unprocessed:
            try:
                # Categorize (simple heuristic; can be enhanced)
                category = categorize_merchant(silver_txn.merchant_name)
                
                # Insert into gold
                gold_txn = GoldTransaction(
                    silver_transaction_id=silver_txn.id,
                    account_id=silver_txn.account_id,
                    transaction_date=silver_txn.transaction_date,
                    amount_gbp=silver_txn.amount_gbp,
                    direction=silver_txn.direction,
                    merchant_name=silver_txn.merchant_name,
                    status=silver_txn.status,
                    category=category,
                    is_subscription=False,  # Will be updated if detected
                )
                session.add(gold_txn)
                enriched_count += 1
            
            except Exception as e:
                logger.error(f"✗ Failed to enrich transaction {silver_txn.id}: {e}")
                log_quality_issue(session, silver_txn.id, 'enrichment_error', 'error', str(e))
        
        session.commit()
        logger.info(f"✓ Enriched {enriched_count} transactions")
        
        # 2. Detect subscriptions
        logger.info("Detecting subscriptions...")
        
        accounts = session.query(Account).filter(Account.is_active == True).all()
        subscriptions_found = 0
        
        for account in accounts:
            try:
                detected_subs = detect_subscriptions(account.id)
                
                for sub_data in detected_subs:
                    # Upsert (update if exists)
                    existing_sub = session.query(Subscription).filter(
                        Subscription.account_id == account.id,
                        Subscription.merchant_name == sub_data['merchant_name']
                    ).first()
                    
                    if existing_sub:
                        # Update
                        existing_sub.amount_gbp = sub_data['amount_gbp']
                        existing_sub.expected_frequency = sub_data['expected_frequency']
                        existing_sub.last_occurrence = sub_data['last_occurrence']
                        existing_sub.occurrence_count = sub_data['occurrence_count']
                        existing_sub.confidence_score = sub_data['confidence_score']
                        existing_sub.next_expected_date = sub_data['next_expected_date']
                        existing_sub.last_detection_run = datetime.now()
                    else:
                        # Create
                        new_sub = Subscription(
                            **sub_data,
                            last_detection_run=datetime.now()
                        )
                        session.add(new_sub)
                    
                    subscriptions_found += 1
            
            except Exception as e:
                logger.error(f"✗ Subscription detection failed for account {account.id}: {e}")
                log_quality_issue(session, None, 'subscription_detection_error', 'warning', str(e))
        
        session.commit()
        logger.info(f"✓ Found {subscriptions_found} subscriptions")
        
        # 3. Detect transfers
        logger.info("Detecting transfers...")
        
        try:
            detected_transfers = detect_transfers()
            
            for transfer_data in detected_transfers:
                existing = session.query(Transfer).filter(
                    Transfer.from_txn_id == transfer_data['from_txn_id'],
                    Transfer.to_txn_id == transfer_data['to_txn_id']
                ).first()
                
                if not existing:
                    transfer = Transfer(**transfer_data)
                    session.add(transfer)
            
            session.commit()
            logger.info(f"✓ Found {len(detected_transfers)} transfers")
        
        except Exception as e:
            logger.error(f"✗ Transfer detection failed: {e}")
            log_quality_issue(session, None, 'transfer_detection_error', 'warning', str(e))
        
        # 4. Build account snapshots
        logger.info("Building account snapshots...")
        
        try:
            build_account_snapshots(session)
            logger.info("✓ Account snapshots created")
        except Exception as e:
            logger.error(f"✗ Snapshot creation failed: {e}")
            log_quality_issue(session, None, 'snapshot_error', 'warning', str(e))
        
        logger.info("✓ Silver→Gold enrichment complete")
        
        return {
            'status': 'success',
            'transactions_enriched': enriched_count,
            'subscriptions_found': subscriptions_found
        }
    
    except Exception as e:
        logger.error(f"✗ Fatal error in Silver→Gold: {e}")
        session.rollback()
        raise
    
    finally:
        session.close()


def categorize_merchant(merchant_name: str) -> str:
    """Simple merchant categorization"""
    if not merchant_name:
        return 'Uncategorized'
    
    merchant_lower = merchant_name.lower()
    
    # Simple heuristic rules
    categories = {
        'Groceries': ['tesco', 'sainsbury', 'asda', 'morrisons', 'waitrose', 'sainsburys'],
        'Transport': ['fuel', 'shell', 'bp', 'tfl', 'uber', 'taxi', 'train', 'railway'],
        'Subscription': ['netflix', 'spotify', 'amazon prime', 'subscription'],
        'Restaurants': ['restaurant', 'cafe', 'coffee', 'pizza', 'burger'],
        'Entertainment': ['cinema', 'theatre', 'concert', 'ticket'],
    }
    
    for category, keywords in categories.items():
        if any(kw in merchant_lower for kw in keywords):
            return category
    
    return 'Other'


def build_account_snapshots(session):
    """Create daily snapshots for all accounts"""
    from datetime import date, timedelta
    from models import AccountSnapshot, AccountLedger
    
    accounts = session.query(Account).all()
    
    for account in accounts:
        # Get latest date with data
        latest_txn = session.query(
            func.max(GoldTransaction.transaction_date)
        ).filter(
            GoldTransaction.account_id == account.id
        ).scalar()
        
        if not latest_txn:
            continue
        
        # Create snapshot for latest date
        transactions = session.query(GoldTransaction).filter(
            GoldTransaction.account_id == account.id,
            GoldTransaction.transaction_date == latest_txn
        ).all()
        
        total_inflow = sum(t.amount_gbp for t in transactions if t.direction == 'credit')
        total_outflow = sum(t.amount_gbp for t in transactions if t.direction == 'debit')
        
        # Get balance from ledger
        balance = session.query(
            AccountLedger.closing_balance
        ).filter(
            AccountLedger.account_id == account.id,
            AccountLedger.snapshot_date == latest_txn
        ).scalar()
        
        snapshot = AccountSnapshot(
            account_id=account.id,
            snapshot_date=latest_txn,
            balance=balance,
            transaction_count=len(transactions),
            total_inflow=total_inflow,
            total_outflow=total_outflow,
            net_flow=total_inflow - total_outflow,
        )
        
        session.merge(snapshot)  # Upsert
    
    session.commit()


def log_quality_issue(session, transaction_id, issue_type, severity, description):
    """Log to quality_issues table"""
    issue = QualityIssue(
        transaction_id=transaction_id,
        issue_type=issue_type,
        severity=severity,
        description=description,
    )
    session.add(issue)
    session.commit()
```

**Tests:**

```python
def test_transactions_enriched():
    """Silver transactions enriched and moved to gold"""
    create_silver_transaction('Tesco Groceries', 25.00)
    
    enrich_silver_to_gold.apply_async().get()
    
    session = get_db_session()
    gold_txn = session.query(GoldTransaction).first()
    assert gold_txn is not None
    assert gold_txn.category == 'Groceries'

def test_subscriptions_detected_in_enrichment():
    """Subscriptions detected during enrichment"""
    account = create_account('monzo', 'acc_123')
    
    # Create 4 monthly subscriptions
    for month in range(1, 5):
        create_silver_transaction(
            'Netflix',
            9.99,
            date(2024, month, 15),
            account.id
        )
    
    enrich_silver_to_gold.apply_async().get()
    
    session = get_db_session()
    sub = session.query(Subscription).filter(
        Subscription.merchant_name == 'Netflix'
    ).first()
    assert sub is not None
    assert sub.expected_frequency == 'monthly'
```

---

## Phase 4: Job Triggering & Chaining

### File: `tasks/pipeline.py`

```python
from celery import chain, group
from datetime import datetime
from db import get_db_session
from models import RawFileUpload
from tasks.transform_bronze_to_silver import transform_bronze_to_silver
from tasks.enrich_silver_to_gold import enrich_silver_to_gold
import logging

logger = logging.getLogger(__name__)


def trigger_pipeline():
    """
    Manually trigger the full pipeline.
    Used after file upload or on schedule.
    """
    logger.info("🚀 Triggering full transformation pipeline")
    
    # Create job chain: Bronze→Silver→Gold
    # Gold job will only run if Bronze→Silver returns > 0 records processed
    pipeline = chain(
        transform_bronze_to_silver.s(),
        enrich_silver_to_gold.s(),
    )
    
    result = pipeline.apply_async()
    logger.info(f"Pipeline submitted with task ID: {result.id}")
    return result.id


def schedule_daily_pipeline():
    """
    Run pipeline once per day (via Celery Beat).
    Ensures all data is up-to-date.
    """
    logger.info("📅 Running scheduled daily pipeline")
    trigger_pipeline()
```

### File: `tasks/celery_beat.py` (Scheduled Tasks)

```python
from celery.schedules import crontab
from tasks.pipeline import schedule_daily_pipeline

# Celery Beat schedule
app.conf.beat_schedule = {
    'daily-pipeline': {
        'task': 'tasks.pipeline.schedule_daily_pipeline',
        'schedule': crontab(hour=2, minute=0),  # 2 AM daily
    },
}
```

---

## Phase 5: Monitoring & Debugging

### File: `tasks/monitoring.py`

```python
from celery import Celery
from db import get_db_session
from models import RawFileUpload, QualityIssue
import logging

logger = logging.getLogger(__name__)


def get_pipeline_status():
    """Get current status of transformation pipeline"""
    session = get_db_session()
    
    unprocessed = session.query(RawFileUpload).filter(
        RawFileUpload.processed == False
    ).count()
    
    failed = session.query(RawFileUpload).filter(
        RawFileUpload.processing_error != None
    ).count()
    
    issues = session.query(QualityIssue).filter(
        QualityIssue.resolved_at == None
    ).count()
    
    return {
        'unprocessed_bronze_records': unprocessed,
        'failed_records': failed,
        'unresolved_quality_issues': issues,
    }


def get_task_status(task_id: str):
    """Get status of a specific task"""
    from tasks import celery_app
    
    task = celery_app.AsyncResult(task_id)
    return {
        'task_id': task_id,
        'state': task.state,
        'result': task.result,
        'status': task.status,
    }


def retry_failed_records():
    """Retry records that failed transformation"""
    session = get_db_session()
    
    failed = session.query(RawFileUpload).filter(
        RawFileUpload.processing_error != None
    ).all()
    
    for record in failed:
        record.processed = False
        record.processing_error = None
    
    session.commit()
    logger.info(f"Reset {len(failed)} failed records for retry")
```

---

## Success Criteria

✅ Bronze→Silver job idempotent (runs safely multiple times)
✅ Errors logged to quality_issues table (doesn't stop pipeline)
✅ Soft time limit handled gracefully (job retried)
✅ Silver→Gold job triggered after Bronze→Silver succeeds
✅ Subscriptions detected + upseried (updates on re-run)
✅ Transfers detected between owned accounts
✅ Account snapshots created daily
✅ All job errors logged with context
✅ Pipeline can be manually triggered or scheduled

---

## Next Step
Implement CLI commands and integration tests for full end-to-end testing.
