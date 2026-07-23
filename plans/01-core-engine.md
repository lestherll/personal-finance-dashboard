# Plan: Core Engine Implementation

## Goal
Build the heart of the system: adapters, data lake (Bronze/Silver/Gold), and transformation logic. No APIs, no UI—just pure data pipeline.

## Scope
- Adapter pattern + concrete adapters (Monzo, Natwest, Vanguard)
- Database schema (all 3 layers)
- Data transformation pipeline (Bronze→Silver→Gold)
- Async job orchestration (Celery)
- Quality tracking

**Out of scope:** REST APIs, React dashboard, Claude integration, deployment

---

## Phase 1: Adapter Pattern & Core Infra (Days 1-2)

### 1.1 Project Setup
```bash
# Create project structure
poetry init personal-finance-dashboard
poetry add fastapi sqlalchemy psycopg2-binary celery redis pydantic python-dateutil
```

**Tasks:**
- [ ] Create `/adapters` folder with `__init__.py`, `base.py`
- [ ] Create `/transformers` folder
- [ ] Create `/models` folder (Pydantic + SQLAlchemy)
- [ ] Create `/tests` folder
- [ ] Add `config.py` for DB + Celery config
- [ ] Add `logging.py` for structured logging

### 1.2 Base Adapter Interface
**File:** `adapters/base.py`

```python
# What to implement:
- RawRecord dataclass
- DataSourceAdapter abstract class
  - validate(file_content) -> (bool, float)
  - parse(file_content, filename, file_hash) -> List[RawRecord]
  - detect_source_type() -> str
  - generate_source_key(row_data, line_num) -> str
```

**Tests:**
- [ ] Test RawRecord creation
- [ ] Test abstract methods are enforced

### 1.3 Adapter Factory
**File:** `adapters/factory.py`

```python
# What to implement:
- AdapterFactory class
  - __init__ with adapter registry
  - detect_adapter(file_content) -> DataSourceAdapter
    - Confidence scoring
    - Tie-breaking
    - Error on ambiguity
  - ingest(file_content, filename, file_hash) -> List[RawRecord]
```

**Tests:**
- [ ] Single adapter matches
- [ ] Multiple adapters compete; highest wins
- [ ] Tie handling (ambiguous)
- [ ] No adapter matches

### 1.4 Concrete Adapters (Monzo, Natwest, Vanguard)
**Files:** `adapters/{monzo,natwest,vanguard}_adapter.py`

**Monzo:**
- [ ] EXPECTED_COLUMNS validation
- [ ] Confidence scoring logic
- [ ] CSV parsing
- [ ] Deterministic source_key from Transaction ID
- [ ] Handle edge cases (empty files, missing columns)

**Natwest:**
- [ ] EXPECTED_COLUMNS validation
- [ ] Confidence scoring (should be lower than Monzo if both match)
- [ ] CSV parsing
- [ ] Deterministic source_key from (date + amount + narrative)
- [ ] Handle missing optional columns

**Vanguard:**
- [ ] EXPECTED_COLUMNS validation
- [ ] Holdings (not transactions) parsing
- [ ] source_key from (ISIN + quantity)

**Tests per adapter:**
- [ ] validate() returns correct confidence
- [ ] parse() handles real export files
- [ ] parse() handles malformed CSVs gracefully
- [ ] generate_source_key() is deterministic (same input → same key)

---

## Phase 2: Database Schema (Days 2-3)

### 2.1 Schema Design
**File:** `migrations/001_initial_schema.sql`

**Bronze Layer:**
- [ ] raw_file_uploads table
- [ ] Indexes on (file_hash, processed, source_type, upload_timestamp)

**Silver Layer:**
- [ ] accounts table
- [ ] transactions table
- [ ] account_ledger table
- [ ] holdings table
- [ ] quality_issues table
- [ ] All indexes (date, account_id combinations)
- [ ] All foreign keys

**Gold Layer:**
- [ ] transactions table (enriched)
- [ ] subscriptions table
- [ ] transfers table
- [ ] account_snapshots table
- [ ] All indexes

**Tests:**
- [ ] Schema creation is idempotent
- [ ] Foreign keys enforce referential integrity
- [ ] UNIQUE constraints work
- [ ] Indexes are created

### 2.2 SQLAlchemy Models
**File:** `models/schema.py`

```python
# What to create:
- Bronze models
  - RawFileUpload
- Silver models
  - Account
  - Transaction
  - AccountLedger
  - Holding
  - QualityIssue
- Gold models
  - Transaction (enriched)
  - Subscription
  - Transfer
  - AccountSnapshot
```

**Tests:**
- [ ] Model creation works
- [ ] Relationships resolve
- [ ] to_dict() serialization works

### 2.3 Database Connection
**File:** `db.py` or `database.py`

```python
# What to implement:
- PostgreSQL connection pool
- Session factory
- Transaction context manager
- Query helpers (execute, query, query_one)
```

**Tests:**
- [ ] Connection succeeds
- [ ] Can insert/update/query
- [ ] Transactions rollback on error

---

## Phase 3: Data Transformations (Days 3-4)

### 3.1 Account Linker
**File:** `transformers/account_linker.py`

```python
# What to implement:
def get_or_create_account(source_type: str, raw_data: Dict) -> UUID:
    """
    Map source-specific data to silver.accounts.
    
    Monzo: raw['account_id']
    Natwest: raw['sort_code'] + raw['account_number']
    Vanguard: raw['account_reference']
    """
```

**Tests:**
- [ ] Monzo account linking
- [ ] Natwest account linking
- [ ] Vanguard account linking
- [ ] Idempotency (same raw_data → same UUID)
- [ ] Account creation on first encounter

### 3.2 Silver Transformer
**File:** `transformers/silver_transformer.py`

```python
# What to implement:
class SilverTransformer:
    - normalize_monzo(raw_record) -> dict
    - normalize_natwest(raw_record) -> dict
    - normalize_vanguard(raw_record) -> dict
    - to_silver(raw_record, account_id) -> dict
```

**Logic per source:**
- Parse dates (various formats)
- Convert amounts (handle negative/positive, local currency)
- Determine direction (credit/debit)
- Handle missing fields (set NULL, add quality flags)
- Validate data (e.g., dates not in future)

**Tests:**
- [ ] Monzo transformation preserves all fields
- [ ] Natwest handles missing time
- [ ] Vanguard creates holdings records
- [ ] Date parsing handles different formats
- [ ] Amount conversion works
- [ ] Direction detection is correct
- [ ] Quality flags set for missing data

### 3.3 Account Ledger Builder
**File:** `transformers/account_ledger.py`

```python
# What to implement:
def build_account_ledger(account_id: UUID, raw_records: List[RawRecord]) -> List[Dict]:
    """
    Extract balance snapshots from transactions.
    
    Natwest provides balance per transaction → insert all
    Monzo doesn't → skip (balance calculated elsewhere)
    """
```

**Tests:**
- [ ] Natwest transactions → account_ledger entries
- [ ] Monzo transactions → no ledger entries
- [ ] Deduplication (same date → latest balance)

---

## Phase 4: Job Orchestration (Days 4-5)

### 4.1 Celery Setup
**File:** `tasks/celery_config.py` + `tasks/__init__.py`

```python
# What to implement:
- Celery app creation
- Redis backend config
- Task registration
```

**Tests:**
- [ ] Celery connects to Redis
- [ ] Tasks can be enqueued + dequeued

### 4.2 Bronze→Silver Job
**File:** `tasks/bronze_to_silver.py`

```python
# What to implement:
@celery_app.task(bind=True, max_retries=3)
def transform_bronze_to_silver():
    """
    Idempotent transformation of unprocessed bronze records.
    
    Flow:
    1. Fetch unprocessed records (processed=FALSE)
    2. For each:
       a. Check if already in silver (skip if yes)
       b. Link account
       c. Transform to silver
       d. Insert into silver.transactions
       e. If Natwest: insert into account_ledger
       f. Mark bronze.processed=TRUE
    3. On error: log to quality_issues, mark processed=TRUE (don't retry)
    4. On success: trigger next job
    """
```

**Tests:**
- [ ] Idempotency (running twice doesn't duplicate)
- [ ] Error handling (one failure doesn't stop others)
- [ ] Quality issues logged on error
- [ ] Processed flag set correctly

### 4.3 Silver→Gold Job (Enrichment)
**File:** `tasks/silver_to_gold.py`

```python
# What to implement:
@celery_app.task
def enrich_silver_to_gold():
    """
    Enrich silver transactions + detect patterns.
    
    Flow:
    1. Fetch unprocessed silver transactions
    2. For each: categorize + insert into gold.transactions
    3. For each account: detect subscriptions + upsert
    4. Detect transfers between accounts
    5. Build account snapshots
    """
```

**Tests:**
- [ ] Transactions enriched
- [ ] Subscriptions detected
- [ ] Transfers detected
- [ ] Snapshots created

### 4.4 Subscription Detection
**File:** `transformers/subscription_detector.py`

```python
# What to implement:
def detect_subscriptions(account_id: UUID, lookback_months=6) -> List[Dict]:
    """
    Algorithm:
    1. Group by (merchant, amount ±5%)
    2. For each group: calculate intervals
    3. If std_dev(intervals) <= 3 days: flag as subscription
    4. Calculate confidence
    """
```

**Tests:**
- [ ] Monthly subscription detected (30±3 days)
- [ ] Bi-weekly detected (14±3 days)
- [ ] Irregular not flagged
- [ ] Confidence scoring works
- [ ] Handles amount variance (±5%)

### 4.5 Transfer Detection
**File:** `transformers/transfer_detector.py`

```python
# What to implement:
def detect_transfers() -> List[Dict]:
    """
    Find debit + credit pairs:
    - Same amount
    - Within 2 days
    - Merchant names suggest transfer
    """
```

**Tests:**
- [ ] Debit/credit within 0-2 days detected
- [ ] Same amount required
- [ ] Merchant keywords checked
- [ ] Confidence scoring

---

## Phase 5: CLI Tool for Testing (Day 5)

### 5.1 Upload Command
**File:** `cli.py`

```python
@click.command()
@click.argument('filepath', type=click.File('rb'))
def ingest(filepath):
    """Upload and ingest a CSV file"""
    # 1. Read file
    # 2. Calculate hash
    # 3. Detect adapter
    # 4. Parse
    # 5. Insert to bronze
    # 6. Print summary
```

**Tests:**
- [ ] Real Monzo CSV ingested
- [ ] Real Natwest CSV ingested
- [ ] Duplicate detection works
- [ ] Output shows record count

### 5.2 Transform Command
**File:** `cli.py`

```python
@click.command()
def transform():
    """Run transformation pipeline (Bronze→Silver→Gold)"""
    # 1. Call transform_bronze_to_silver
    # 2. Call enrich_silver_to_gold
    # 3. Print progress
```

**Tests:**
- [ ] Pipeline runs end-to-end
- [ ] Data flows correctly through layers

### 5.3 Inspect Command
**File:** `cli.py`

```python
@click.command()
@click.argument('table', type=click.Choice(['bronze', 'silver', 'gold']))
@click.option('--limit', default=10)
def inspect(table, limit):
    """Peek at data in a layer"""
    # Show sample records from specified layer
```

---

## Phase 6: Integration Tests (End of Day 5)

### 6.1 End-to-End Test
**File:** `tests/test_e2e.py`

```python
def test_monzo_file_upload_to_gold():
    """
    Given: Real Monzo CSV file
    When: Ingest + transform pipeline runs
    Then: 
    - Bronze has raw records
    - Silver has normalized transactions
    - Gold has enriched transactions
    - Accounts linked correctly
    - Quality issues logged if any
    """
```

### 6.2 Data Integrity Tests
**File:** `tests/test_data_integrity.py`

```python
def test_no_duplicate_transactions():
    """Re-uploading same file doesn't create duplicates"""

def test_duplicate_source_key_rejected():
    """Two different files with same source_key → warning"""

def test_account_balance_consistency():
    """If Natwest provides balance, it matches ledger"""

def test_transfer_not_double_counted():
    """Transfer between own accounts doesn't inflate net worth"""

def test_subscription_with_3_mo_history():
    """Subscription detected only with sufficient history"""
```

---

## Success Criteria

✅ Adapters detect and parse Monzo, Natwest, Vanguard CSVs correctly
✅ Bronze layer stores raw data immutably with deduplication
✅ Silver layer normalizes + links accounts (no NULL account_ids)
✅ Gold layer enriches + detects subscriptions/transfers
✅ Transformation jobs are idempotent (safe to rerun)
✅ Quality issues tracked for manual review
✅ CLI tool allows manual ingestion + inspection
✅ All unit + integration tests pass
✅ No SQL injection vectors (parameterized queries only)

---

## Tech Stack (Locked)
- Python 3.11+
- SQLAlchemy 2.0+ (ORM)
- PostgreSQL 15+ (database)
- Celery 5.3+ (job queue)
- Redis 7+ (broker)
- pytest (testing)
- Click (CLI)

---

## Estimated Timeline
- **Phase 1:** 1.5 days (adapter pattern)
- **Phase 2:** 1 day (schema + models)
- **Phase 3:** 1.5 days (transformations)
- **Phase 4:** 1 day (Celery jobs)
- **Phase 5:** 0.5 days (CLI)
- **Phase 6:** 0.5 days (integration tests)

**Total: 5-6 days for one developer**

---

## Key Decisions
1. **Celery + Redis** for job queue (async + reliable)
2. **Parameterized queries** everywhere (security)
3. **Idempotent transforms** (safe rerun)
4. **Deterministic source_keys** (no duplicates on re-upload)
5. **Separate quality_issues table** (structured logging)
6. **No API layer yet** (focus on engine only)

---

## Next: Once Engine is Done
Then build: API layer → CLI/Dashboard → Claude integration
