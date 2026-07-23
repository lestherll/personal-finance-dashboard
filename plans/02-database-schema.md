# Plan: Database Schema & Migrations

## Goal
Define all PostgreSQL tables with proper constraints, indexes, and relationships. Create migration files that can be run independently.

## Scope
- All DDL (CREATE TABLE, INDEX, CONSTRAINT)
- Migration versioning
- Rollback procedures
- Schema documentation

---

## Migration Files

### Migration 001: Bronze Layer (Initial)
**File:** `migrations/001_create_bronze_layer.sql`

```sql
-- Bronze layer: raw, immutable data

CREATE TABLE IF NOT EXISTS raw_file_uploads (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    -- Deduplication
    file_hash VARCHAR(64) NOT NULL UNIQUE,
    filename VARCHAR NOT NULL,
    
    -- Record metadata
    source_key VARCHAR NOT NULL,
    source_type VARCHAR NOT NULL,
    raw_data JSONB NOT NULL,
    
    -- Timestamps
    upload_timestamp TIMESTAMP NOT NULL,
    line_number INT,
    
    -- Processing state
    processed BOOLEAN DEFAULT FALSE,
    processed_at TIMESTAMP,
    processing_error VARCHAR,
    
    -- Audit
    created_at TIMESTAMP DEFAULT NOW(),
    
    CONSTRAINT unique_source_per_file UNIQUE (file_hash, source_key)
);

CREATE INDEX idx_bronze_processed ON raw_file_uploads(processed, source_type);
CREATE INDEX idx_bronze_timestamp ON raw_file_uploads(upload_timestamp DESC);
CREATE INDEX idx_bronze_source_key ON raw_file_uploads(source_key);
```

**Tests:**
- [ ] Table created successfully
- [ ] file_hash UNIQUE enforced
- [ ] Indexes created
- [ ] sample INSERT works

---

### Migration 002: Silver Layer - Accounts & Core
**File:** `migrations/002_create_silver_accounts.sql`

```sql
-- Silver layer: accounts registry (single source of truth)

CREATE TABLE IF NOT EXISTS accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    -- External linkage
    source_type VARCHAR NOT NULL,
    external_account_id VARCHAR NOT NULL,
    
    -- Display
    account_name VARCHAR,
    account_type VARCHAR NOT NULL,
    
    -- Currency
    native_currency VARCHAR DEFAULT 'GBP',
    
    -- Status
    is_active BOOLEAN DEFAULT TRUE,
    
    -- Metadata
    created_at TIMESTAMP DEFAULT NOW(),
    last_transaction_date DATE,
    
    CONSTRAINT unique_account UNIQUE (source_type, external_account_id),
    CONSTRAINT account_type_valid CHECK (account_type IN ('checking', 'savings', 'investment_isa', 'general_investment'))
);

CREATE INDEX idx_account_active ON accounts(is_active);
CREATE INDEX idx_account_source ON accounts(source_type);
CREATE INDEX idx_account_name ON accounts(account_name);
```

**Tests:**
- [ ] Table created
- [ ] UNIQUE (source_type, external_account_id) enforced
- [ ] CHECK constraint on account_type works

---

### Migration 003: Silver Layer - Transactions
**File:** `migrations/003_create_silver_transactions.sql`

```sql
-- Silver layer: normalized transactions

CREATE TABLE IF NOT EXISTS silver_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    -- Lineage
    bronze_source_key VARCHAR NOT NULL,
    source_type VARCHAR NOT NULL,
    
    -- Account (required foreign key)
    account_id UUID NOT NULL,
    
    -- Dates
    transaction_date DATE NOT NULL,
    transaction_time TIME,
    
    -- Amounts (multi-currency support)
    amount_original DECIMAL(12, 2) NOT NULL,
    original_currency VARCHAR NOT NULL DEFAULT 'GBP',
    amount_gbp DECIMAL(12, 2) NOT NULL,
    exchange_rate DECIMAL(8, 6),
    exchange_rate_source VARCHAR,
    
    -- Transaction details
    direction VARCHAR NOT NULL,
    merchant_name VARCHAR,
    merchant_category_code VARCHAR,
    status VARCHAR DEFAULT 'posted',
    
    -- Audit
    ingested_at TIMESTAMP NOT NULL,
    ingested_from_file VARCHAR,
    
    created_at TIMESTAMP DEFAULT NOW(),
    
    CONSTRAINT fk_account FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT,
    CONSTRAINT unique_txn_per_source UNIQUE (source_type, bronze_source_key),
    CONSTRAINT direction_valid CHECK (direction IN ('credit', 'debit')),
    CONSTRAINT status_valid CHECK (status IN ('posted', 'pending', 'cancelled', 'reversed'))
);

CREATE INDEX idx_txn_date ON silver_transactions(transaction_date DESC);
CREATE INDEX idx_txn_account_date ON silver_transactions(account_id, transaction_date DESC);
CREATE INDEX idx_txn_status ON silver_transactions(status);
CREATE INDEX idx_txn_merchant ON silver_transactions(merchant_name);
CREATE INDEX idx_txn_source_key ON silver_transactions(bronze_source_key);
```

**Tests:**
- [ ] Table created
- [ ] UNIQUE (source_type, bronze_source_key) enforced
- [ ] CHECK constraints work
- [ ] Foreign key prevents orphan transactions
- [ ] Indexes created

---

### Migration 004: Silver Layer - Account Ledger & Holdings
**File:** `migrations/004_create_silver_ledger_holdings.sql`

```sql
-- Silver layer: account balances + holdings

CREATE TABLE IF NOT EXISTS account_ledger (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    account_id UUID NOT NULL,
    snapshot_date DATE NOT NULL,
    
    closing_balance DECIMAL(12, 2) NOT NULL,
    
    source_type VARCHAR,
    source_field VARCHAR,
    
    created_at TIMESTAMP DEFAULT NOW(),
    
    CONSTRAINT fk_account FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT,
    CONSTRAINT unique_balance UNIQUE (account_id, snapshot_date)
);

CREATE INDEX idx_ledger_account_date ON account_ledger(account_id, snapshot_date DESC);

-- Holdings (investments)

CREATE TABLE IF NOT EXISTS holdings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    account_id UUID NOT NULL,
    source_type VARCHAR NOT NULL,
    
    isin VARCHAR NOT NULL,
    fund_name VARCHAR,
    quantity DECIMAL(12, 6) NOT NULL,
    unit_price DECIMAL(12, 4) NOT NULL,
    total_value DECIMAL(12, 2) NOT NULL,
    
    as_of_date DATE NOT NULL,
    ingested_at TIMESTAMP NOT NULL,
    
    created_at TIMESTAMP DEFAULT NOW(),
    
    CONSTRAINT fk_account FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT,
    CONSTRAINT unique_holding UNIQUE (account_id, source_type, isin, as_of_date)
);

CREATE INDEX idx_holding_account_date ON holdings(account_id, as_of_date DESC);
CREATE INDEX idx_holding_isin ON holdings(isin, as_of_date DESC);
```

**Tests:**
- [ ] account_ledger.unique_balance enforced
- [ ] holdings.unique_holding enforced
- [ ] Foreign keys work

---

### Migration 005: Silver Layer - Quality Issues
**File:** `migrations/005_create_silver_quality_issues.sql`

```sql
-- Silver layer: data quality tracking

CREATE TABLE IF NOT EXISTS quality_issues (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    transaction_id UUID,
    account_id UUID,
    
    issue_type VARCHAR NOT NULL,
    severity VARCHAR NOT NULL,
    description VARCHAR,
    
    created_at TIMESTAMP DEFAULT NOW(),
    resolved_at TIMESTAMP,
    resolved_by VARCHAR,
    resolution_notes VARCHAR,
    
    CONSTRAINT fk_txn FOREIGN KEY (transaction_id) REFERENCES silver_transactions(id) ON DELETE CASCADE,
    CONSTRAINT fk_account FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE,
    CONSTRAINT severity_valid CHECK (severity IN ('info', 'warning', 'error')),
    CONSTRAINT has_subject CHECK (transaction_id IS NOT NULL OR account_id IS NOT NULL)
);

CREATE INDEX idx_quality_unresolved ON quality_issues(resolved_at) WHERE resolved_at IS NULL;
CREATE INDEX idx_quality_type ON quality_issues(issue_type, severity);
CREATE INDEX idx_quality_txn ON quality_issues(transaction_id);
```

**Tests:**
- [ ] issue_type validation works
- [ ] severity validation works
- [ ] At least one of (transaction_id, account_id) required

---

### Migration 006: Gold Layer - Transactions & Subscriptions
**File:** `migrations/006_create_gold_transactions.sql`

```sql
-- Gold layer: enriched transactions

CREATE TABLE IF NOT EXISTS gold_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    -- Core fields (copy from silver)
    account_id UUID NOT NULL,
    transaction_date DATE NOT NULL,
    amount_gbp DECIMAL(12, 2) NOT NULL,
    direction VARCHAR NOT NULL,
    merchant_name VARCHAR,
    status VARCHAR,
    
    -- Enrichment
    category VARCHAR,
    tags VARCHAR[],
    is_subscription BOOLEAN DEFAULT FALSE,
    subscription_id UUID,
    
    -- Lineage
    silver_transaction_id UUID NOT NULL UNIQUE,
    source_type VARCHAR,
    
    -- Audit
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    
    CONSTRAINT fk_silver FOREIGN KEY (silver_transaction_id) REFERENCES silver_transactions(id) ON DELETE RESTRICT,
    CONSTRAINT fk_account FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT
);

CREATE INDEX idx_gold_txn_date ON gold_transactions(transaction_date DESC);
CREATE INDEX idx_gold_txn_account_date ON gold_transactions(account_id, transaction_date DESC);
CREATE INDEX idx_gold_txn_category ON gold_transactions(category);

-- Subscriptions (detected recurring transactions)

CREATE TABLE IF NOT EXISTS subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    account_id UUID NOT NULL,
    merchant_name VARCHAR NOT NULL,
    
    amount_gbp DECIMAL(12, 2) NOT NULL,
    amount_tolerance_pct DECIMAL(3, 1) DEFAULT 5.0,
    expected_frequency VARCHAR NOT NULL,
    
    first_occurrence DATE,
    last_occurrence DATE,
    next_expected_date DATE,
    occurrence_count INT,
    
    confidence_score DECIMAL(3, 2),
    is_active BOOLEAN DEFAULT TRUE,
    
    created_at TIMESTAMP DEFAULT NOW(),
    last_detection_run TIMESTAMP,
    
    CONSTRAINT fk_account FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT,
    CONSTRAINT frequency_valid CHECK (expected_frequency IN ('weekly', 'bi-weekly', 'monthly', 'quarterly', 'annual', 'irregular')),
    CONSTRAINT confidence_valid CHECK (confidence_score >= 0 AND confidence_score <= 1)
);

CREATE INDEX idx_sub_account ON subscriptions(account_id, is_active);
CREATE INDEX idx_sub_merchant ON subscriptions(merchant_name);

-- Link gold_transactions to subscriptions
ALTER TABLE gold_transactions ADD CONSTRAINT fk_subscription FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE SET NULL;
CREATE INDEX idx_gold_txn_subscription ON gold_transactions(subscription_id) WHERE is_subscription = TRUE;
```

**Tests:**
- [ ] expected_frequency validation works
- [ ] confidence_score range validation works
- [ ] gold_transactions can link to subscriptions

---

### Migration 007: Gold Layer - Transfers & Snapshots
**File:** `migrations/007_create_gold_transfers.sql`

```sql
-- Gold layer: detected transfers between accounts

CREATE TABLE IF NOT EXISTS transfers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    from_account_id UUID NOT NULL,
    to_account_id UUID NOT NULL,
    amount_gbp DECIMAL(12, 2) NOT NULL,
    
    from_txn_id UUID NOT NULL,
    to_txn_id UUID,
    
    detected_date TIMESTAMP DEFAULT NOW(),
    confidence_score DECIMAL(3, 2),
    
    created_at TIMESTAMP DEFAULT NOW(),
    
    CONSTRAINT fk_from FOREIGN KEY (from_account_id) REFERENCES accounts(id) ON DELETE RESTRICT,
    CONSTRAINT fk_to FOREIGN KEY (to_account_id) REFERENCES accounts(id) ON DELETE RESTRICT,
    CONSTRAINT fk_from_txn FOREIGN KEY (from_txn_id) REFERENCES silver_transactions(id) ON DELETE RESTRICT,
    CONSTRAINT fk_to_txn FOREIGN KEY (to_txn_id) REFERENCES silver_transactions(id) ON DELETE SET NULL,
    CONSTRAINT different_accounts CHECK (from_account_id != to_account_id)
);

CREATE INDEX idx_transfer_from_date ON transfers(from_account_id, detected_date DESC);
CREATE INDEX idx_transfer_to_date ON transfers(to_account_id, detected_date DESC);

-- Account snapshots (daily aggregates)

CREATE TABLE IF NOT EXISTS account_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    account_id UUID NOT NULL,
    snapshot_date DATE NOT NULL,
    
    balance DECIMAL(12, 2),
    transaction_count INT,
    total_inflow DECIMAL(12, 2),
    total_outflow DECIMAL(12, 2),
    net_flow DECIMAL(12, 2),
    
    created_at TIMESTAMP DEFAULT NOW(),
    
    CONSTRAINT fk_account FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT,
    CONSTRAINT unique_snapshot UNIQUE (account_id, snapshot_date)
);

CREATE INDEX idx_snapshot_account_date ON account_snapshots(account_id, snapshot_date DESC);
```

**Tests:**
- [ ] Transfer from_account != to_account enforced
- [ ] All foreign keys work

---

## Migration Strategy

### Running Migrations
```bash
# Forward (development)
python -m alembic upgrade head

# Rollback
python -m alembic downgrade -1

# Check status
python -m alembic current
```

### Alembic Setup
**File:** `alembic.ini` + `alembic/env.py`

- [ ] Configure PostgreSQL connection
- [ ] Set up auto-migration
- [ ] Test forward + backward migrations

---

## Schema Validation Tests

### Unit Tests
**File:** `tests/test_schema.py`

```python
def test_schema_creation():
    """All migrations run without error"""

def test_unique_constraints():
    """UNIQUE constraints prevent duplicates"""

def test_foreign_key_integrity():
    """Foreign keys prevent orphan records"""

def test_check_constraints():
    """CHECK constraints validate data"""

def test_indexes_exist():
    """All expected indexes created"""

def test_indexes_functional():
    """Queries use indexes (query plans)"""
```

---

## Success Criteria

✅ All 7 migrations run cleanly
✅ Schema matches SYSTEM_REVISED.md
✅ No orphan records possible (foreign keys)
✅ No duplicates possible (unique constraints)
✅ All critical queries have indexes
✅ Migrations are reversible (alembic downgrade works)
✅ All validation tests pass

---

## Reference: Full ER Diagram

```
BRONZE:
┌─ raw_file_uploads (file_hash → immutable source of truth)

SILVER:
├─ accounts (source_type, external_account_id → canonical)
├─ silver_transactions (account_id FK, bronze_source_key)
├─ account_ledger (account_id, snapshot_date)
├─ holdings (account_id FK, ISIN)
└─ quality_issues (transaction_id/account_id FK, type + severity)

GOLD:
├─ gold_transactions (silver_transaction_id FK, subscription_id FK)
├─ subscriptions (account_id FK)
├─ transfers (from_account_id, to_account_id, from_txn_id FK)
└─ account_snapshots (account_id FK)
```

---

## Next Step
After schema is set up, implement SQLAlchemy models that match these tables exactly.
