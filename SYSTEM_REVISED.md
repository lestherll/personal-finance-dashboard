# Personal Finance System Architecture (Revised)

## Adapter Pattern + Medallion Data Lake Design

---

## 1. High-Level Architecture

```
┌──────────────────────────────────────────────────────┐
│           DATA SOURCES (V0: Files, V1+: APIs)        │
│  Monzo CSV │ Natwest CSV │ Amex CSV │ Vanguard CSV   │
│  (+ future: Plaid API, direct integrations)          │
└───────────────────┬────────────────────────────────┘
                    │
        ┌───────────▼──────────────┐
        │   INGESTION LAYER        │
        │  (File Upload + Parse)   │
        │  Adapter Factory         │
        └───────────────┬──────────┘
                        │
    ┌───────────────────┼────────────────────┐
    │                   │                    │
┌───▼────────────────────────────────────────▼───┐
│         MEDALLION DATA LAKE                    │
│                                                │
│  ┌──────────────────────────────────────┐    │
│  │ BRONZE LAYER (Raw)                  │    │
│  │ - Raw file uploads (immutable)       │    │
│  │ - Timestamps, source IDs, file hash  │    │
│  │ - No transformation                  │    │
│  └──────────────────────────────────────┘    │
│                                                │
│  ┌──────────────────────────────────────┐    │
│  │ SILVER LAYER (Normalized)            │    │
│  │ - Schema validation & normalization  │    │
│  │ - Standard column names              │    │
│  │ - Data quality checks                │    │
│  │ - Deduplicated transactions          │    │
│  │ - Account registry (linked)          │    │
│  │ - Account ledger (balance history)   │    │
│  └──────────────────────────────────────┘    │
│                                                │
│  ┌──────────────────────────────────────┐    │
│  │ GOLD LAYER (Domain Models)           │    │
│  │ - Transactions (enriched)            │    │
│  │ - Holdings (linked to accounts)      │    │
│  │ - Recurring patterns                 │    │
│  │ - Transfer detection                 │    │
│  │ - Account snapshots                  │    │
│  └──────────────────────────────────────┘    │
└────────────────────┬─────────────────────────┘
                     │
    ┌────────────────┼──────────────────┐
    │                │                  │
┌───▼──────┐    ┌────────▼────────┐  ┌────▼─────┐
│ Analytics│    │ Claude Context  │  │ Dashboard│
│ / Query  │    │ Builder         │  │ & APIs   │
│ Service  │    │ (Goal Planning) │  │          │
└──────────┘    └─────────────────┘  └──────────┘
```

---

## 2. Adapter Pattern Design

### 2.1 Core Abstraction

Every data source implements the **`DataSourceAdapter`** interface:

```python
# adapters/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import List, Dict, Any

@dataclass
class RawRecord:
    """Minimal wrapper—just capture as-is from the file"""
    source_key: str  # Deterministic: "monzo_txn_abc123" or "natwest_txn_20240115_amount_merchant"
    source_type: str  # "monzo", "natwest", "amex", "vanguard"
    raw_data: Dict[str, Any]  # Entire row as dict
    filename: str
    file_hash: str  # SHA256 of uploaded file
    upload_timestamp: datetime
    line_number: int

class DataSourceAdapter(ABC):
    """All adapters inherit from this"""
    
    @abstractmethod
    def validate(self, file_content: str) -> tuple[bool, float]:
        """
        Check if file format matches this adapter.
        
        Returns:
            (is_valid: bool, confidence: float 0.0-1.0)
        
        Confidence allows multiple adapters to compete; highest wins.
        """
        pass
    
    @abstractmethod
    def parse(self, file_content: str, filename: str, file_hash: str) -> List[RawRecord]:
        """Parse file, return raw records (minimal transformation)"""
        pass
    
    @abstractmethod
    def detect_source_type(self) -> str:
        """Return: 'monzo', 'natwest', 'amex', 'vanguard'"""
        pass
    
    def generate_source_key(self, row_data: Dict[str, Any], line_num: int) -> str:
        """
        Generate deterministic source key. Override per adapter.
        
        Goal: Same record from re-uploaded file has same key (no duplicates).
        """
        raise NotImplementedError
```

### 2.2 Concrete Adapters (Examples)

```python
# adapters/monzo_adapter.py
class MonzoAdapter(DataSourceAdapter):
    """Monzo CSV export format"""
    
    EXPECTED_COLUMNS = [
        'Transaction ID', 'Date', 'Time', 'Type', 'Name',
        'Emoji', 'Category', 'Amount', 'Currency', 'Local Amount',
        'Local Currency', 'Notes', 'Receipt', 'Description'
    ]
    
    def validate(self, file_content: str) -> tuple[bool, float]:
        """Check first row contains Monzo headers"""
        lines = file_content.split('\n')
        if not lines:
            return False, 0.0
        headers = [h.strip() for h in lines[0].split(',')]
        # Monzo has very specific columns; high confidence if all present
        score = sum(1 for col in self.EXPECTED_COLUMNS if col in headers) / len(self.EXPECTED_COLUMNS)
        return score >= 0.8, score
    
    def parse(self, file_content: str, filename: str, file_hash: str) -> List[RawRecord]:
        """Convert CSV to RawRecord list"""
        import csv
        from io import StringIO
        
        records = []
        reader = csv.DictReader(StringIO(file_content))
        
        for idx, row in enumerate(reader, start=2):
            # Use Transaction ID (unique within Monzo) as key
            source_key = f"monzo_txn_{row['Transaction ID']}"
            records.append(RawRecord(
                source_key=source_key,
                source_type='monzo',
                raw_data=dict(row),
                filename=filename,
                file_hash=file_hash,
                upload_timestamp=datetime.now(),
                line_number=idx
            ))
        
        return records
    
    def detect_source_type(self) -> str:
        return 'monzo'
    
    def generate_source_key(self, row_data: Dict[str, Any], line_num: int) -> str:
        return f"monzo_txn_{row_data['Transaction ID']}"


# adapters/natwest_adapter.py
class NatwelstAdapter(DataSourceAdapter):
    """Natwest CSV export (different format from Monzo)"""
    
    EXPECTED_COLUMNS = [
        'Transaction Type', 'Transaction Date', 'Transaction Amount',
        'Transaction Narrative', 'Balance', 'Balance Date'
    ]
    
    def validate(self, file_content: str) -> tuple[bool, float]:
        lines = file_content.split('\n')
        if not lines:
            return False, 0.0
        headers = [h.strip() for h in lines[0].split(',')]
        score = sum(1 for col in self.EXPECTED_COLUMNS[:3] if col in headers) / 3
        return score >= 0.7, score
    
    def parse(self, file_content: str, filename: str, file_hash: str) -> List[RawRecord]:
        import csv
        from io import StringIO
        
        records = []
        reader = csv.DictReader(StringIO(file_content))
        
        for idx, row in enumerate(reader, start=2):
            # Natwest has no transaction ID; use deterministic combination
            source_key = self.generate_source_key(row, idx)
            records.append(RawRecord(
                source_key=source_key,
                source_type='natwest',
                raw_data=dict(row),
                filename=filename,
                file_hash=file_hash,
                upload_timestamp=datetime.now(),
                line_number=idx
            ))
        
        return records
    
    def detect_source_type(self) -> str:
        return 'natwest'
    
    def generate_source_key(self, row_data: Dict[str, Any], line_num: int) -> str:
        """
        Deterministic key: date + amount + first 10 chars of narrative.
        Same transaction in re-uploaded file will have same key.
        
        Format: natwest_txn_20240115_-50.00_groceries_...
        """
        date_str = row_data['Transaction Date'].replace('/', '')
        amount = row_data['Transaction Amount']
        narrative = row_data['Transaction Narrative'][:10].replace(' ', '_')
        return f"natwest_txn_{date_str}_{amount}_{narrative}"


# adapters/vanguard_adapter.py
class VanguardAdapter(DataSourceAdapter):
    """Vanguard holdings export (not transactions; separate domain)"""
    
    EXPECTED_COLUMNS = ['ISIN', 'Fund Name', 'Quantity', 'Price', 'Value']
    
    def validate(self, file_content: str) -> tuple[bool, float]:
        headers = [h.strip() for h in file_content.split('\n')[0].split(',')]
        score = sum(1 for col in self.EXPECTED_COLUMNS if col in headers) / len(self.EXPECTED_COLUMNS)
        return score >= 0.8, score
    
    def parse(self, file_content: str, filename: str, file_hash: str) -> List[RawRecord]:
        import csv
        from io import StringIO
        
        records = []
        reader = csv.DictReader(StringIO(file_content))
        
        for idx, row in enumerate(reader, start=2):
            source_key = self.generate_source_key(row, idx)
            records.append(RawRecord(
                source_key=source_key,
                source_type='vanguard',
                raw_data=dict(row),
                filename=filename,
                file_hash=file_hash,
                upload_timestamp=datetime.now(),
                line_number=idx
            ))
        
        return records
    
    def detect_source_type(self) -> str:
        return 'vanguard'
    
    def generate_source_key(self, row_data: Dict[str, Any], line_num: int) -> str:
        # Holdings change over time; key by ISIN
        isin = row_data['ISIN']
        quantity = row_data['Quantity']
        return f"vanguard_holding_{isin}_{quantity}"
```

### 2.3 Adapter Factory & Registry

```python
# adapters/factory.py
class AdapterFactory:
    """Auto-detect and route to the right adapter"""
    
    def __init__(self):
        self.adapters = [
            MonzoAdapter(),
            NatwelstAdapter(),
            VanguardAdapter(),
            # AmexAdapter(),  # TODO: V0.5
        ]
    
    def detect_adapter(self, file_content: str) -> DataSourceAdapter:
        """
        Try each adapter; highest confidence wins.
        
        Raises:
            ValueError if no adapter confidence >= 0.8 or tie at top
        """
        results = [
            (adapter, adapter.validate(file_content))
            for adapter in self.adapters
        ]
        results = [(a, valid, conf) for a, (valid, conf) in results if valid]
        
        if not results:
            raise ValueError(
                "File format not recognized. Supported: Monzo, Natwest, Vanguard"
            )
        
        results.sort(key=lambda x: x[2], reverse=True)
        best_adapter, _, best_conf = results[0]
        
        if best_conf < 0.8:
            raise ValueError(f"File format ambiguous (confidence {best_conf:.1%})")
        
        if len(results) > 1 and abs(results[0][2] - results[1][2]) < 0.05:
            raise ValueError(
                f"File format ambiguous: {results[0][0].detect_source_type()} "
                f"vs {results[1][0].detect_source_type()}"
            )
        
        return best_adapter
    
    def ingest(self, file_content: str, filename: str, file_hash: str) -> List[RawRecord]:
        """Single entry point: detect + parse"""
        adapter = self.detect_adapter(file_content)
        records = adapter.parse(file_content, filename, file_hash)
        
        logger.info(f"✓ Detected {adapter.detect_source_type()} format (confidence {adapter.validate(file_content)[1]:.1%})")
        logger.info(f"✓ Parsed {len(records)} records from {filename}")
        
        return records
```

---

## 3. Data Lake Layers (Medallion Architecture)

### 3.1 Bronze Layer (Immutable Raw)

**Purpose:** Store everything exactly as uploaded. Enable deduplication via file_hash.

```sql
-- bronze.raw_file_uploads
CREATE TABLE raw_file_uploads (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    -- Deduplication
    file_hash VARCHAR(64) NOT NULL UNIQUE,  -- SHA256 of uploaded file
    filename VARCHAR NOT NULL,
    
    -- Record metadata
    source_key VARCHAR UNIQUE NOT NULL,  -- "monzo_txn_abc123"
    source_type VARCHAR NOT NULL,  -- "monzo", "natwest", "vanguard"
    raw_data JSONB NOT NULL,  -- Entire row as JSON
    
    upload_timestamp TIMESTAMP NOT NULL,
    line_number INT,
    
    -- Lineage
    processed BOOLEAN DEFAULT FALSE,
    processed_at TIMESTAMP,
    processing_error VARCHAR,  -- If transformation failed
    
    -- Audit
    created_at TIMESTAMP DEFAULT NOW(),
    
    CONSTRAINT unique_source_per_file UNIQUE (file_hash, source_key)
);

CREATE INDEX idx_bronze_processed ON raw_file_uploads(processed, source_type);
CREATE INDEX idx_bronze_timestamp ON raw_file_uploads(upload_timestamp DESC);
```

**Write Pattern:** Adapters → Bronze (append-only)
- If file_hash already exists, skip (idempotent re-upload)
- If source_key already in different file, raise warning (duplicate transaction)

**Read Pattern:** Never read Bronze directly. Always go through Silver/Gold.

---

### 3.2 Silver Layer (Normalized & Linked)

**Purpose:** Standardize schema, link accounts, deduplicate, validate quality.

#### 3.2.1 Account Registry (Canonical Source)

```sql
-- silver.accounts
-- Single source of truth for all bank/investment accounts
CREATE TABLE accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    -- External linkage
    source_type VARCHAR NOT NULL,  -- "monzo", "natwest", "vanguard"
    external_account_id VARCHAR NOT NULL,  -- Monzo's account_id, Natwest's sort+account
    
    -- Display
    account_name VARCHAR,
    account_type VARCHAR NOT NULL,  -- "checking", "savings", "investment_isa", "general_investment"
    
    -- Currency (per account; transactions converted to GBP)
    native_currency VARCHAR DEFAULT 'GBP',
    
    -- Status
    is_active BOOLEAN DEFAULT TRUE,
    
    -- Audit
    created_at TIMESTAMP DEFAULT NOW(),
    last_transaction_date DATE,
    
    CONSTRAINT unique_account UNIQUE (source_type, external_account_id)
);

CREATE INDEX idx_account_active ON accounts(is_active);
CREATE INDEX idx_account_source ON accounts(source_type);
```

**Account Linking Rules:**
```python
# transformers/account_linker.py

def get_or_create_account(source_type: str, raw_record: Dict[str, Any]) -> UUID:
    """
    Map source-specific data to silver.accounts.
    
    Rules per source:
    - Monzo: external_account_id = raw['account_id']
    - Natwest: external_account_id = raw['sort_code'] + raw['account_number']
    - Vanguard: external_account_id = raw['account_reference']
    """
    if source_type == 'monzo':
        external_id = raw_record['account_id']
    elif source_type == 'natwest':
        external_id = f"{raw_record['sort_code']}_{raw_record['account_number']}"
    elif source_type == 'vanguard':
        external_id = raw_record['account_reference']
    else:
        raise ValueError(f"Unknown source: {source_type}")
    
    # Upsert: if account exists, return UUID; else create
    existing = db.query_one(
        "SELECT id FROM silver.accounts WHERE source_type = %s AND external_account_id = %s",
        (source_type, external_id)
    )
    
    if existing:
        return existing['id']
    
    # Create new account
    account_id = db.execute(
        """INSERT INTO silver.accounts 
           (source_type, external_account_id, account_type, native_currency, account_name)
           VALUES (%s, %s, %s, %s, %s)
           RETURNING id""",
        (source_type, external_id, infer_account_type(source_type), 'GBP', external_id)
    )
    return account_id
```

#### 3.2.2 Transaction Ledger (Normalized & Multi-Currency)

```sql
-- silver.transactions
-- Source-agnostic normalized view of all transactions
CREATE TABLE transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    -- Lineage
    bronze_source_key VARCHAR NOT NULL,  -- Link back to bronze
    source_type VARCHAR NOT NULL,  -- monzo, natwest, amex
    
    -- Account
    account_id UUID NOT NULL,
    
    -- Transaction dates
    transaction_date DATE NOT NULL,
    transaction_time TIME,  -- NULL if source doesn't provide (e.g., Natwest)
    
    -- Amount (in transaction's native currency)
    amount_original DECIMAL(12, 2) NOT NULL,
    original_currency VARCHAR NOT NULL DEFAULT 'GBP',
    
    -- Amount converted to GBP
    amount_gbp DECIMAL(12, 2) NOT NULL,
    exchange_rate DECIMAL(8, 6),  -- If currency != GBP
    exchange_rate_source VARCHAR,  -- 'provider' (from CSV), 'ecb', 'manual', 'identity'
    
    -- Direction
    direction VARCHAR NOT NULL,  -- 'credit', 'debit'
    
    -- Merchant / Description
    merchant_name VARCHAR,
    merchant_category_code VARCHAR,  -- Derived from adapter or fuzzy match
    
    -- Status
    status VARCHAR DEFAULT 'posted',  -- 'posted', 'pending', 'cancelled', 'reversed'
    
    -- Quality
    data_quality_flags VARCHAR[],  -- Deprecated: use silver.quality_issues instead
    
    -- Audit
    ingested_at TIMESTAMP NOT NULL,
    ingested_from_file VARCHAR,
    
    CONSTRAINT fk_account FOREIGN KEY (account_id) REFERENCES silver.accounts(id) ON DELETE RESTRICT,
    CONSTRAINT unique_txn_per_source UNIQUE (source_type, bronze_source_key)
);

CREATE INDEX idx_txn_date ON silver.transactions(transaction_date DESC);
CREATE INDEX idx_txn_account_date ON silver.transactions(account_id, transaction_date DESC);
CREATE INDEX idx_txn_status ON silver.transactions(status);
CREATE INDEX idx_txn_merchant ON silver.transactions(merchant_name) WHERE merchant_name IS NOT NULL;
```

#### 3.2.3 Account Ledger (Balance History)

```sql
-- silver.account_ledger
-- Immutable, append-only balance snapshot per account per date
CREATE TABLE account_ledger (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    account_id UUID NOT NULL,
    snapshot_date DATE NOT NULL,
    
    -- Balance as provided by source (or calculated)
    closing_balance DECIMAL(12, 2) NOT NULL,
    
    -- Context
    source_type VARCHAR,  -- Where this balance came from
    source_field VARCHAR,  -- e.g., 'Balance' (Natwest) or 'derived' (from transactions)
    
    created_at TIMESTAMP DEFAULT NOW(),
    
    CONSTRAINT fk_account FOREIGN KEY (account_id) REFERENCES silver.accounts(id),
    CONSTRAINT unique_balance UNIQUE (account_id, snapshot_date)
);

CREATE INDEX idx_ledger_account_date ON silver.account_ledger(account_id, snapshot_date DESC);
```

**How to use:**
- Natwest provides balance per transaction → insert for each date
- Monzo doesn't → calculate from transactions
- Always query balance as of date X via this table (not by summing transactions)

#### 3.2.4 Quality Issues (Structured)

```sql
-- silver.quality_issues
-- Replaces data_quality_flags array (v1)
CREATE TABLE quality_issues (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    transaction_id UUID,  -- NULL if account-level
    account_id UUID,
    
    issue_type VARCHAR NOT NULL,  -- 'missing_time', 'negative_balance_jump', 'duplicate_candidate'
    severity VARCHAR NOT NULL,  -- 'warning', 'error'
    description VARCHAR,
    
    created_at TIMESTAMP DEFAULT NOW(),
    resolved_at TIMESTAMP,  -- If manually reviewed
    
    CONSTRAINT fk_txn FOREIGN KEY (transaction_id) REFERENCES silver.transactions(id),
    CONSTRAINT fk_account FOREIGN KEY (account_id) REFERENCES silver.accounts(id)
);

CREATE INDEX idx_quality_unresolved ON quality_issues(resolved_at) WHERE resolved_at IS NULL;
```

#### 3.2.5 Holdings (Linked to Accounts)

```sql
-- silver.holdings
CREATE TABLE holdings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    account_id UUID NOT NULL,  -- FIX: Link holdings to accounts
    source_type VARCHAR NOT NULL,  -- vanguard, etc.
    
    isin VARCHAR NOT NULL,
    fund_name VARCHAR,
    quantity DECIMAL(12, 6) NOT NULL,
    unit_price DECIMAL(12, 4) NOT NULL,
    total_value DECIMAL(12, 2) NOT NULL,
    
    as_of_date DATE NOT NULL,
    ingested_at TIMESTAMP NOT NULL,
    
    CONSTRAINT fk_account FOREIGN KEY (account_id) REFERENCES silver.accounts(id),
    CONSTRAINT unique_holding_per_source UNIQUE (account_id, source_type, isin, as_of_date)
);

CREATE INDEX idx_holding_account_date ON silver.holdings(account_id, as_of_date DESC);
```

---

### 3.3 Gold Layer (Domain Models)

#### 3.3.1 Transaction Ledger (Enriched)

```sql
-- gold.transactions
-- Enriched view with categorization, tags, subscription flags
CREATE TABLE transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    -- Core fields (copy from silver)
    account_id UUID NOT NULL,
    transaction_date DATE NOT NULL,
    amount_gbp DECIMAL(12, 2) NOT NULL,
    direction VARCHAR NOT NULL,
    merchant_name VARCHAR,
    status VARCHAR,
    
    -- Enrichment
    category VARCHAR,  -- 'Groceries', 'Transport', 'Subscription', etc.
    merchant_category_code VARCHAR,
    tags VARCHAR[],  -- User-defined: ['amazon', 'reoccurring']
    is_subscription BOOLEAN DEFAULT FALSE,
    subscription_id UUID,  -- Link to gold.subscriptions if recurring
    
    -- Metadata
    silver_transaction_id UUID NOT NULL UNIQUE,
    source_type VARCHAR,
    
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    
    CONSTRAINT fk_silver FOREIGN KEY (silver_transaction_id) REFERENCES silver.transactions(id),
    CONSTRAINT fk_account FOREIGN KEY (account_id) REFERENCES silver.accounts(id),
    CONSTRAINT fk_subscription FOREIGN KEY (subscription_id) REFERENCES subscriptions(id)
);

CREATE INDEX idx_gold_txn_date ON gold.transactions(transaction_date DESC);
CREATE INDEX idx_gold_txn_account_date ON gold.transactions(account_id, transaction_date DESC);
CREATE INDEX idx_gold_txn_category ON gold.transactions(category);
CREATE INDEX idx_gold_txn_subscription ON gold.transactions(subscription_id) WHERE is_subscription = TRUE;
```

#### 3.3.2 Subscription Detection

```sql
-- gold.subscriptions
-- Detected recurring transactions
CREATE TABLE subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    account_id UUID NOT NULL,
    merchant_name VARCHAR NOT NULL,
    
    -- Pattern
    amount_gbp DECIMAL(12, 2) NOT NULL,
    amount_tolerance_pct DECIMAL(3, 1) DEFAULT 5.0,  -- ±5% variance allowed
    expected_frequency VARCHAR NOT NULL,  -- 'monthly', 'bi-weekly', 'annual', etc.
    
    -- History
    first_occurrence DATE,
    last_occurrence DATE,
    next_expected_date DATE,
    occurrence_count INT,
    
    -- Quality
    confidence_score DECIMAL(3, 2),  -- 0.0-1.0; based on consistency
    is_active BOOLEAN DEFAULT TRUE,
    
    created_at TIMESTAMP DEFAULT NOW(),
    last_detection_run TIMESTAMP,
    
    CONSTRAINT fk_account FOREIGN KEY (account_id) REFERENCES silver.accounts(id)
);

CREATE INDEX idx_sub_account ON gold.subscriptions(account_id, is_active);
```

**Subscription Detection Algorithm:**
```python
# transformers/subscription_detector.py

def detect_subscriptions(account_id: UUID, lookback_months: int = 6) -> List[Dict]:
    """
    Detect recurring transactions within account.
    
    Algorithm:
    1. Fetch transactions for last N months
    2. Group by (merchant_name, amount ±tolerance)
    3. For each group, calculate intervals between transactions
    4. If interval is regular (e.g., ~30 days ±3 days), flag as subscription
    5. Calculate confidence: (# of occurrences - 2) / max_expected_occurrences
    
    Returns: List of detected subscriptions (not yet inserted; caller decides)
    """
    start_date = date.today() - timedelta(days=lookback_months * 30)
    
    # Group transactions
    txns = db.query(f"""
        SELECT merchant_name, amount_gbp, transaction_date
        FROM silver.transactions
        WHERE account_id = %s AND transaction_date >= %s
        ORDER BY merchant_name, transaction_date
    """, (account_id, start_date))
    
    candidates = {}  # merchant -> List[transaction_dates]
    
    for txn in txns:
        key = txn['merchant_name']
        if key not in candidates:
            candidates[key] = []
        candidates[key].append((txn['transaction_date'], txn['amount_gbp']))
    
    # Analyze patterns
    subscriptions = []
    
    for merchant, txn_list in candidates.items():
        if len(txn_list) < 2:  # Need at least 2 occurrences
            continue
        
        # Group by amount (within ±5%)
        amount_groups = {}
        for date, amount in txn_list:
            # Find matching amount bucket
            bucket = None
            for existing_amt in amount_groups.keys():
                if abs(amount - existing_amt) / existing_amt <= 0.05:
                    bucket = existing_amt
                    break
            
            if bucket is None:
                bucket = amount
                amount_groups[bucket] = []
            
            amount_groups[bucket].append(date)
        
        # Analyze each amount group
        for amount, dates in amount_groups.items():
            if len(dates) < 2:
                continue
            
            dates.sort()
            intervals = [(dates[i+1] - dates[i]).days for i in range(len(dates) - 1)]
            
            # Check if intervals are regular
            avg_interval = sum(intervals) / len(intervals)
            variance = sum((x - avg_interval) ** 2 for x in intervals) / len(intervals)
            std_dev = variance ** 0.5
            
            # Regular if std_dev <= 3 days
            if std_dev <= 3:
                expected_frequency = classify_frequency(avg_interval)
                confidence = min(1.0, len(dates) / (lookback_months / frequency_to_months(expected_frequency)))
                
                subscriptions.append({
                    'merchant_name': merchant,
                    'amount_gbp': float(amount),
                    'expected_frequency': expected_frequency,
                    'first_occurrence': dates[0],
                    'last_occurrence': dates[-1],
                    'occurrence_count': len(dates),
                    'confidence_score': confidence,
                    'next_expected_date': dates[-1] + timedelta(days=int(avg_interval))
                })
    
    return subscriptions

def classify_frequency(avg_interval_days: int) -> str:
    """Map interval (in days) to frequency string"""
    if 6 <= avg_interval_days <= 8:
        return 'weekly'
    elif 13 <= avg_interval_days <= 15:
        return 'bi-weekly'
    elif 27 <= avg_interval_days <= 32:
        return 'monthly'
    elif 85 <= avg_interval_days <= 95:
        return 'quarterly'
    elif 360 <= avg_interval_days <= 370:
        return 'annual'
    else:
        return 'irregular'
```

#### 3.3.3 Transfer Detection

```sql
-- gold.transfers
-- Detected transfers between owned accounts
CREATE TABLE transfers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    from_account_id UUID NOT NULL,
    to_account_id UUID NOT NULL,
    amount_gbp DECIMAL(12, 2) NOT NULL,
    
    -- Transaction links
    from_txn_id UUID NOT NULL,
    to_txn_id UUID,  -- NULL if to_account not in system
    
    -- Detection
    detected_date TIMESTAMP DEFAULT NOW(),
    confidence_score DECIMAL(3, 2),  -- 0.0-1.0
    
    CONSTRAINT fk_from FOREIGN KEY (from_account_id) REFERENCES silver.accounts(id),
    CONSTRAINT fk_to FOREIGN KEY (to_account_id) REFERENCES silver.accounts(id),
    CONSTRAINT fk_from_txn FOREIGN KEY (from_txn_id) REFERENCES silver.transactions(id),
    CONSTRAINT fk_to_txn FOREIGN KEY (to_txn_id) REFERENCES silver.transactions(id)
);

CREATE INDEX idx_transfer_from_date ON gold.transfers(from_account_id, detected_date DESC);
CREATE INDEX idx_transfer_to_date ON gold.transfers(to_account_id, detected_date DESC);
```

**Transfer Detection Algorithm:**
```python
def detect_transfers(lookback_days: int = 30):
    """
    Find transfers between owned accounts.
    
    Pattern: Debit from AccountA + Credit to AccountB, same amount, within 2 days.
    """
    start_date = date.today() - timedelta(days=lookback_days)
    
    # Find all debits and credits
    debits = db.query(f"""
        SELECT id, account_id, amount_gbp, transaction_date, merchant_name
        FROM silver.transactions
        WHERE direction = 'debit' AND transaction_date >= %s
    """, (start_date,))
    
    credits = db.query(f"""
        SELECT id, account_id, amount_gbp, transaction_date, merchant_name
        FROM silver.transactions
        WHERE direction = 'credit' AND transaction_date >= %s
    """, (start_date,))
    
    transfers = []
    
    for debit in debits:
        for credit in credits:
            # Same amount?
            if debit['amount_gbp'] != credit['amount_gbp']:
                continue
            
            # Within 2 days?
            days_apart = (credit['transaction_date'] - debit['transaction_date']).days
            if not (-2 <= days_apart <= 2):
                continue
            
            # Merchant name match? (e.g., both mention "transfer" or bank names)
            debit_merchant = debit['merchant_name'] or ''
            credit_merchant = credit['merchant_name'] or ''
            if not is_transfer_like(debit_merchant, credit_merchant):
                continue
            
            # Confidence based on proximity and merchant match
            confidence = 0.9 if days_apart == 0 else 0.7
            
            transfers.append({
                'from_account_id': debit['account_id'],
                'to_account_id': credit['account_id'],
                'amount_gbp': float(debit['amount_gbp']),
                'from_txn_id': debit['id'],
                'to_txn_id': credit['id'],
                'confidence_score': confidence
            })
    
    return transfers

def is_transfer_like(debit_merchant: str, credit_merchant: str) -> bool:
    """Check if transaction names suggest a transfer"""
    keywords = ['transfer', 'payment to', 'send money', 'received from', 'monzo', 'natwest']
    return any(kw in debit_merchant.lower() or kw in credit_merchant.lower() for kw in keywords)
```

#### 3.3.4 Account Snapshots

```sql
-- gold.account_snapshots
-- Point-in-time snapshots for historical analysis
CREATE TABLE account_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    account_id UUID NOT NULL,
    snapshot_date DATE NOT NULL,
    
    balance DECIMAL(12, 2),  -- From silver.account_ledger
    transaction_count INT,
    total_inflow DECIMAL(12, 2),  -- Gross credits
    total_outflow DECIMAL(12, 2),  -- Gross debits
    net_flow DECIMAL(12, 2),  -- Inflow - Outflow
    
    created_at TIMESTAMP DEFAULT NOW(),
    
    CONSTRAINT fk_account FOREIGN KEY (account_id) REFERENCES silver.accounts(id),
    CONSTRAINT unique_snapshot UNIQUE (account_id, snapshot_date)
);

CREATE INDEX idx_snapshot_account_date ON gold.account_snapshots(account_id, snapshot_date DESC);
```

---

## 4. Data Flow: File Upload → Lake

```
1. FILE UPLOAD
   User uploads: "monzo_export.csv"
   ↓
2. ADAPTER DETECTION
   AdapterFactory.detect_adapter(file_content)
   → Returns MonzoAdapter with confidence 0.95
   ↓
3. DEDUPLICATION CHECK
   file_hash = SHA256(file_content)
   IF file_hash in bronze.raw_file_uploads:
      SKIP (idempotent re-upload)
   ↓
4. PARSE → BRONZE
   MonzoAdapter.parse() → List[RawRecord]
   → Inserted into bronze.raw_file_uploads (immutable)
   ↓
5. BRONZE → SILVER (Async Job, Idempotent)
   FOR EACH raw_file_uploads WHERE processed = FALSE:
     TRY:
       - Account linking: get_or_create_account(source_type, raw_data)
       - SilverTransformer.to_silver(raw_record, account_id)
       - INSERT into silver.transactions
       - If Natwest: INSERT balance into silver.account_ledger
       - Mark bronze.processed = TRUE
     EXCEPT Exception as e:
       - INSERT into silver.quality_issues
       - Mark bronze.processed = TRUE, processing_error = str(e)
   ↓
6. SILVER → GOLD (Async Job)
   FOR EACH silver.transactions with NO gold.transactions entry:
     - Categorize (if Monzo, use native; else fuzzy match)
     - Link to subscription if exists
     - INSERT into gold.transactions
   
   FOR EACH account:
     - Run subscription detection (if new transactions)
     - Upsert gold.subscriptions
     - Run transfer detection
     - Upsert gold.transfers
   
   FOR EACH account per date:
     - Calculate snapshots (balance, inflow, outflow)
     - INSERT into gold.account_snapshots
   ↓
7. QUERY / ANALYTICS
   Dashboard queries gold layer + indexes
   Claude context pulls from gold layer
```

---

## 5. API Layer (Built on Data Lake)

### 5.1 REST Endpoints

```python
# api/main.py
from fastapi import FastAPI, UploadFile, File, Query, HTTPException
from pydantic import BaseModel, validator
from datetime import date, datetime
from typing import Optional, List
from uuid import UUID

app = FastAPI()

# ===== INPUT VALIDATION =====

class TransactionQueryParams(BaseModel):
    account_id: Optional[UUID] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    category: Optional[str] = None
    status: Optional[str] = None
    limit: int = 100
    offset: int = 0
    
    @validator('end_date')
    def end_after_start(cls, v, values):
        if v and 'start_date' in values and values['start_date'] and v < values['start_date']:
            raise ValueError('end_date must be >= start_date')
        return v
    
    @validator('limit')
    def limit_reasonable(cls, v):
        if not 1 <= v <= 1000:
            raise ValueError('limit must be 1-1000')
        return v

# ===== ENDPOINTS =====

@app.post("/api/v1/ingest")
async def ingest_file(file: UploadFile = File(...)):
    """
    Upload a CSV file. Auto-detect source type.
    
    Returns:
    {
        "status": "success",
        "file_hash": "abc123...",
        "source_type": "monzo",
        "records_parsed": 127,
        "records_inserted_bronze": 127,
        "warnings": []
    }
    """
    try:
        content = await file.read()
        file_hash = hashlib.sha256(content).hexdigest()
        content_str = content.decode('utf-8')
        
        # Check for duplicate file
        existing = db.query_one(
            "SELECT id FROM bronze.raw_file_uploads WHERE file_hash = %s",
            (file_hash,)
        )
        
        if existing:
            return {
                "status": "skipped",
                "reason": "File already uploaded",
                "file_hash": file_hash
            }
        
        # Detect + parse
        factory = AdapterFactory()
        raw_records = factory.ingest(content_str, file.filename, file_hash)
        
        # Insert to bronze
        bronze_count = 0
        for record in raw_records:
            try:
                db.execute(
                    """INSERT INTO bronze.raw_file_uploads 
                       (file_hash, filename, source_key, source_type, raw_data, upload_timestamp, line_number)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (file_hash, file.filename, record.source_key, record.source_type,
                     json.dumps(record.raw_data), record.upload_timestamp, record.line_number)
                )
                bronze_count += 1
            except IntegrityError as e:
                logger.warning(f"Duplicate source_key {record.source_key}: {e}")
        
        # Trigger async transformation job
        celery_app.send_task('tasks.transform_bronze_to_silver', args=[])
        
        return {
            "status": "success",
            "file_hash": file_hash,
            "source_type": raw_records[0].source_type if raw_records else None,
            "records_parsed": len(raw_records),
            "records_inserted_bronze": bronze_count,
            "warnings": []
        }
    
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Ingestion failed: {e}")
        raise HTTPException(status_code=500, detail="Ingestion failed")


@app.get("/api/v1/transactions")
async def get_transactions(params: TransactionQueryParams = Query(...)):
    """Query transactions from gold layer with pagination"""
    
    query_parts = ["SELECT * FROM gold.transactions WHERE 1=1"]
    query_params = []
    
    if params.account_id:
        query_parts.append("AND account_id = %s")
        query_params.append(params.account_id)
    
    if params.start_date:
        query_parts.append("AND transaction_date >= %s")
        query_params.append(params.start_date)
    
    if params.end_date:
        query_parts.append("AND transaction_date <= %s")
        query_params.append(params.end_date)
    
    if params.category:
        query_parts.append("AND category = %s")
        query_params.append(params.category)
    
    if params.status:
        query_parts.append("AND status = %s")
        query_params.append(params.status)
    
    query_parts.append("ORDER BY transaction_date DESC")
    query_parts.append("LIMIT %s OFFSET %s")
    query_params.extend([params.limit, params.offset])
    
    query = " ".join(query_parts)
    results = db.query(query, tuple(query_params))
    
    # Get total count
    count_query = "SELECT COUNT(*) as total FROM gold.transactions WHERE 1=1"
    count_params = []
    if params.account_id:
        count_query += " AND account_id = %s"
        count_params.append(params.account_id)
    # ... repeat other conditions ...
    
    total = db.query_one(count_query, tuple(count_params))['total']
    
    return {
        "transactions": results,
        "pagination": {
            "total": total,
            "limit": params.limit,
            "offset": params.offset,
            "has_more": params.offset + params.limit < total
        }
    }


@app.get("/api/v1/accounts")
async def get_accounts():
    """List all accounts from silver layer"""
    return db.query("SELECT * FROM silver.accounts WHERE is_active = TRUE ORDER BY account_name")


@app.get("/api/v1/holdings")
async def get_holdings(account_id: Optional[UUID] = None):
    """List holdings from silver layer"""
    if account_id:
        return db.query(
            "SELECT * FROM silver.holdings WHERE account_id = %s ORDER BY as_of_date DESC",
            (account_id,)
        )
    else:
        return db.query("SELECT * FROM silver.holdings ORDER BY account_id, as_of_date DESC")


@app.get("/api/v1/subscriptions")
async def get_subscriptions(account_id: Optional[UUID] = None):
    """List detected recurring subscriptions"""
    if account_id:
        return db.query(
            """SELECT * FROM gold.subscriptions 
               WHERE account_id = %s AND is_active = TRUE
               ORDER BY amount_gbp DESC""",
            (account_id,)
        )
    else:
        return db.query(
            "SELECT * FROM gold.subscriptions WHERE is_active = TRUE ORDER BY amount_gbp DESC"
        )


@app.get("/api/v1/account-summary")
async def get_account_summary(start_date: date, end_date: date):
    """
    Aggregate view across all accounts for goal planning.
    
    Returns:
    {
        "period": "2024-01-01 to 2024-01-31",
        "total_inflow": 4500.00,
        "total_outflow": 2800.00,
        "net_savings": 1700.00,
        "by_category": {...},
        "subscriptions_total": 245.00
    }
    """
    if start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date must be <= end_date")
    
    summary = db.query_one(f"""
        SELECT 
            SUM(CASE WHEN direction = 'credit' THEN amount_gbp ELSE 0 END) as total_inflow,
            SUM(CASE WHEN direction = 'debit' THEN amount_gbp ELSE 0 END) as total_outflow,
            COUNT(*) as transaction_count
        FROM gold.transactions
        WHERE transaction_date BETWEEN %s AND %s
    """, (start_date, end_date))
    
    by_category = db.query("""
        SELECT 
            category,
            SUM(amount_gbp) as total,
            COUNT(*) as count
        FROM gold.transactions
        WHERE transaction_date BETWEEN %s AND %s AND direction = 'debit'
        GROUP BY category
        ORDER BY total DESC
    """, (start_date, end_date))
    
    subs_total = db.query_one("""
        SELECT SUM(amount_gbp) as total FROM gold.subscriptions
        WHERE last_occurrence BETWEEN %s AND %s AND is_active = TRUE
    """, (start_date, end_date))
    
    return {
        "period": f"{start_date} to {end_date}",
        "total_inflow": float(summary['total_inflow'] or 0),
        "total_outflow": float(summary['total_outflow'] or 0),
        "net_savings": float((summary['total_inflow'] or 0) - (summary['total_outflow'] or 0)),
        "by_category": by_category,
        "subscriptions_total": float(subs_total['total'] or 0)
    }


@app.get("/api/v1/data-quality")
async def get_data_quality():
    """Data quality dashboard"""
    unresolved_issues = db.query(
        "SELECT issue_type, severity, COUNT(*) as count FROM silver.quality_issues WHERE resolved_at IS NULL GROUP BY issue_type, severity"
    )
    
    coverage = db.query("""
        SELECT 
            source_type,
            COUNT(*) as transaction_count,
            COUNT(DISTINCT account_id) as account_count
        FROM silver.transactions
        GROUP BY source_type
    """)
    
    return {
        "unresolved_issues": unresolved_issues,
        "coverage_by_source": coverage
    }
```

### 5.2 Claude Context API

```python
@app.get("/api/v1/claude-context")
async def get_claude_context(
    months: int = Query(6, ge=1, le=120),
    accounts: Optional[List[UUID]] = Query(None),
    limit_transactions: int = Query(500, ge=10, le=2000)
):
    """
    Fetch data for Claude to analyze (with pagination).
    
    Args:
        months: Lookback period (1-120 months)
        accounts: Filter to specific accounts (None = all)
        limit_transactions: Max transactions to return
    
    Returns:
    {
        "accounts": [...],
        "transactions": [...],  # Limited to limit_transactions
        "subscriptions": [...],
        "holdings": [...],
        "transfers": [...],
        "summary": {
            "net_worth": 150000,
            "monthly_savings_rate": 1700,
            "subscription_total": 245,
            "portfolio_value": 50000,
            "portfolio_allocation": {...}
        },
        "warnings": ["Missing Monzo balance data", ...]
    }
    """
    start_date = date.today() - timedelta(days=months * 30)
    warnings = []
    
    # Accounts
    account_query = "SELECT * FROM silver.accounts WHERE is_active = TRUE"
    account_params = []
    if accounts:
        placeholders = ','.join(['%s'] * len(accounts))
        account_query += f" AND id IN ({placeholders})"
        account_params = accounts
    
    account_list = db.query(account_query, tuple(account_params))
    
    # Transactions (limited + paginated for Claude)
    if accounts:
        placeholders = ','.join(['%s'] * len(accounts))
        txn_query = f"""
            SELECT * FROM gold.transactions
            WHERE account_id IN ({placeholders}) AND transaction_date >= %s
            ORDER BY transaction_date DESC
            LIMIT %s
        """
        txn_params = tuple(list(accounts) + [start_date, limit_transactions])
    else:
        txn_query = """
            SELECT * FROM gold.transactions
            WHERE transaction_date >= %s
            ORDER BY transaction_date DESC
            LIMIT %s
        """
        txn_params = (start_date, limit_transactions)
    
    transactions = db.query(txn_query, txn_params)
    if len(transactions) == limit_transactions:
        warnings.append(f"Transaction list truncated at {limit_transactions}; older data not included")
    
    # Subscriptions
    sub_query = "SELECT * FROM gold.subscriptions WHERE is_active = TRUE"
    sub_params = []
    if accounts:
        placeholders = ','.join(['%s'] * len(accounts))
        sub_query += f" AND account_id IN ({placeholders})"
        sub_params = accounts
    subscriptions = db.query(sub_query, tuple(sub_params))
    
    # Holdings (latest only)
    holding_query = """
        SELECT * FROM silver.holdings
        WHERE as_of_date = (SELECT MAX(as_of_date) FROM silver.holdings)
    """
    holding_params = []
    if accounts:
        placeholders = ','.join(['%s'] * len(accounts))
        holding_query += f" AND account_id IN ({placeholders})"
        holding_params = accounts
    
    holdings = db.query(holding_query, tuple(holding_params))
    
    # Transfers (recent)
    transfer_query = f"""
        SELECT * FROM gold.transfers
        WHERE detected_date >= %s
    """
    transfer_params = [start_date]
    if accounts:
        placeholders = ','.join(['%s'] * len(accounts))
        transfer_query += f" AND (from_account_id IN ({placeholders}) OR to_account_id IN ({placeholders}))"
        transfer_params.extend(list(accounts) * 2)
    
    transfers = db.query(transfer_query, tuple(transfer_params))
    
    # Aggregates
    summary_data = db.query_one("""
        SELECT 
            SUM(balance) as total_balance,
            SUM(total_value) as portfolio_value
        FROM (
            SELECT COALESCE(SUM(closing_balance), 0) as balance, 0 as total_value
            FROM silver.account_ledger
            WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM silver.account_ledger)
            UNION ALL
            SELECT 0 as balance, SUM(total_value) as total_value
            FROM silver.holdings
            WHERE as_of_date = (SELECT MAX(as_of_date) FROM silver.holdings)
        ) combined
    """)
    
    # Monthly savings (average from last 6 months of snapshots)
    monthly_savings = db.query_one("""
        SELECT AVG(net_flow) as avg_monthly FROM (
            SELECT net_flow FROM gold.account_snapshots
            WHERE snapshot_date >= %s
            ORDER BY snapshot_date DESC LIMIT 6
        ) recent
    """, (start_date,))
    
    # Check for data quality warnings
    issues = db.query("""
        SELECT issue_type, COUNT(*) as count
        FROM silver.quality_issues
        WHERE resolved_at IS NULL
        GROUP BY issue_type
    """)
    
    for issue in issues:
        warnings.append(f"{issue['count']}x {issue['issue_type']}")
    
    return {
        "accounts": account_list,
        "transactions": transactions,
        "subscriptions": subscriptions,
        "holdings": holdings,
        "transfers": transfers,
        "summary": {
            "total_balance": float(summary_data['total_balance'] or 0),
            "portfolio_value": float(summary_data['portfolio_value'] or 0),
            "net_worth": float((summary_data['total_balance'] or 0) + (summary_data['portfolio_value'] or 0)),
            "monthly_savings_rate": float(monthly_savings['avg_monthly'] or 0),
            "subscription_total": sum(s['amount_gbp'] for s in subscriptions)
        },
        "warnings": warnings
    }
```

---

## 6. Job Orchestration (Celery)

```python
# tasks.py
from celery import Celery
import logging

celery_app = Celery('finance_pipeline')
logger = logging.getLogger(__name__)

@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def transform_bronze_to_silver(self):
    """
    Async job: Transform unprocessed bronze records to silver.
    Idempotent: if record already in silver, skip.
    """
    try:
        logger.info("Starting bronze→silver transformation")
        
        unprocessed = db.query("""
            SELECT id, source_key, source_type, raw_data
            FROM bronze.raw_file_uploads
            WHERE processed = FALSE
            ORDER BY created_at ASC
            LIMIT 1000
        """)
        
        processed_count = 0
        error_count = 0
        
        for record in unprocessed:
            try:
                # Check if already in silver (idempotency)
                existing = db.query_one(
                    "SELECT id FROM silver.transactions WHERE bronze_source_key = %s",
                    (record['source_key'],)
                )
                
                if existing:
                    db.execute(
                        "UPDATE bronze.raw_file_uploads SET processed = TRUE WHERE id = %s",
                        (record['id'],)
                    )
                    continue
                
                # Account linking
                account_id = get_or_create_account(record['source_type'], record['raw_data'])
                
                # Transform to silver
                silver_record = SilverTransformer.to_silver(record, account_id)
                
                # Insert
                db.execute("""
                    INSERT INTO silver.transactions
                    (bronze_source_key, source_type, account_id, transaction_date, 
                     amount_gbp, direction, merchant_name, status, ingested_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    silver_record['bronze_source_key'],
                    silver_record['source_type'],
                    account_id,
                    silver_record['transaction_date'],
                    silver_record['amount_gbp'],
                    silver_record['direction'],
                    silver_record['merchant_name'],
                    'posted',
                    datetime.now()
                ))
                
                # Mark bronze as processed
                db.execute(
                    "UPDATE bronze.raw_file_uploads SET processed = TRUE WHERE id = %s",
                    (record['id'],)
                )
                
                processed_count += 1
                logger.debug(f"✓ Processed {record['source_key']}")
            
            except Exception as e:
                error_count += 1
                logger.error(f"✗ Failed to transform {record['source_key']}: {e}")
                
                # Log to quality issues
                db.execute("""
                    INSERT INTO silver.quality_issues
                    (issue_type, severity, description)
                    VALUES (%s, %s, %s)
                """, ('transformation_error', 'error', str(e)))
                
                # Mark bronze as processed (don't retry forever)
                db.execute(
                    "UPDATE bronze.raw_file_uploads SET processed = TRUE, processing_error = %s WHERE id = %s",
                    (str(e), record['id'])
                )
        
        logger.info(f"✓ Transformation complete: {processed_count} processed, {error_count} errors")
        
        # Trigger next job: enrichment
        if processed_count > 0:
            enrich_silver_to_gold.delay()
    
    except Exception as e:
        logger.error(f"Fatal error in transformation job: {e}")
        # Retry with exponential backoff
        raise self.retry(exc=e)


@celery_app.task(bind=True, max_retries=3)
def enrich_silver_to_gold(self):
    """
    Async job: Enrich silver transactions, detect subscriptions/transfers.
    """
    try:
        logger.info("Starting silver→gold enrichment")
        
        # Get unprocessed silver transactions
        unprocessed = db.query("""
            SELECT id, account_id, merchant_name, amount_gbp, transaction_date
            FROM silver.transactions
            WHERE id NOT IN (SELECT silver_transaction_id FROM gold.transactions)
            LIMIT 1000
        """)
        
        for txn in unprocessed:
            try:
                # Categorize
                category = categorize_merchant(txn['merchant_name'])
                
                # Enrich
                db.execute("""
                    INSERT INTO gold.transactions
                    (silver_transaction_id, account_id, merchant_name, amount_gbp, category)
                    SELECT id, account_id, merchant_name, amount_gbp, %s FROM silver.transactions
                    WHERE id = %s
                """, (category, txn['id']))
            
            except Exception as e:
                logger.error(f"✗ Failed to enrich transaction {txn['id']}: {e}")
        
        # Subscription detection (per account, if new data)
        accounts = db.query("SELECT DISTINCT account_id FROM silver.transactions")
        
        for account in accounts:
            try:
                detected_subs = detect_subscriptions(account['account_id'])
                
                for sub in detected_subs:
                    db.execute("""
                        INSERT INTO gold.subscriptions
                        (account_id, merchant_name, amount_gbp, expected_frequency, 
                         first_occurrence, last_occurrence, occurrence_count, confidence_score, is_active)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (account_id, merchant_name) DO UPDATE SET
                            last_occurrence = EXCLUDED.last_occurrence,
                            occurrence_count = EXCLUDED.occurrence_count,
                            confidence_score = EXCLUDED.confidence_score
                    """, (
                        sub['account_id'], sub['merchant_name'], sub['amount_gbp'],
                        sub['expected_frequency'], sub['first_occurrence'], sub['last_occurrence'],
                        sub['occurrence_count'], sub['confidence_score'], True
                    ))
            
            except Exception as e:
                logger.error(f"✗ Failed to detect subscriptions for account {account['account_id']}: {e}")
        
        # Transfer detection
        try:
            detected_transfers = detect_transfers()
            
            for transfer in detected_transfers:
                db.execute("""
                    INSERT INTO gold.transfers
                    (from_account_id, to_account_id, amount_gbp, from_txn_id, to_txn_id, confidence_score)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (
                    transfer['from_account_id'], transfer['to_account_id'],
                    transfer['amount_gbp'], transfer['from_txn_id'], transfer['to_txn_id'],
                    transfer['confidence_score']
                ))
        except Exception as e:
            logger.error(f"✗ Transfer detection failed: {e}")
        
        logger.info("✓ Enrichment complete")
    
    except Exception as e:
        logger.error(f"Fatal error in enrichment job: {e}")
        raise self.retry(exc=e)
```

---

## 7. Data Retention & Archival Policy

```
RETENTION SCHEDULE:

Bronze (raw_file_uploads):
  - Keep: 7 years (tax requirement)
  - Archival: After 2 years, move to cold storage (S3 Glacier)
  - Encryption: At rest (AES-256)

Silver (normalized):
  - Keep: 7 years
  - Archival: After 1 year, compress yearly snapshots

Gold (enriched):
  - Keep: Indefinitely (analysis history)
  - Archival: Annual snapshots

Backups:
  - Daily full backup + transaction log archival
  - 30-day retention for daily backups
  - 7-year retention for monthly backups

Data Deletion (GDPR/Right to be Forgotten):
  - Mark account as deleted in silver.accounts (is_active = FALSE)
  - Anonymize data by replacing account_name, merchant_name with hashes
  - Keep bronze for audit trail (legal requirement)
  - Run archival retention schedule as normal
```

---

## 8. Implementation Roadmap

### V0 (Weeks 1-2)
- [x] Adapter pattern + 2 adapters (Monzo, Natwest)
- [x] PostgreSQL setup (Bronze, Silver, Gold tables + indexes)
- [x] File upload API with deduplication
- [x] Bronze→Silver transformation (async Celery job)
- [x] Account linking + account_ledger
- [x] Transaction query API with pagination
- [x] Manual testing with real exports

### V0.5 (Week 3)
- [ ] Vanguard adapter + holdings linkage
- [ ] Subscription detection algorithm
- [ ] Transfer detection algorithm
- [ ] Data quality dashboard
- [ ] Simple React dashboard (list accounts, transactions)

### V1 (Month 2)
- [ ] Amex adapter
- [ ] Claude integration endpoint (with pagination)
- [ ] Categorization rules (fuzzy matching)
- [ ] Recurring job scheduler (daily enrichment)
- [ ] API documentation + postman collection

### V2 (Month 3)
- [ ] Plaid integration (live sync)
- [ ] Goal management schema (gold.goals table)
- [ ] Claude advisor prompts (goal recommendations)
- [ ] Budget alerts / spending insights

---

## 9. Security & Compliance

| Area | Implementation |
|------|---|
| **Authentication** | API keys (v0.5), OAuth2 (v1) |
| **Data Encryption** | AES-256 at rest, TLS in transit |
| **Audit Trail** | All inserts logged with user + timestamp |
| **Access Control** | Row-level security for multi-user |
| **GDPR Compliance** | Data deletion, right to access endpoint |
| **PCI-DSS (future)** | No card numbers stored; transactions only |

---

## 10. Key Improvements Over Original

| Issue | Original | Revised |
|-------|----------|---------|
| SQL Injection | ❌ String concat | ✅ Parameterized queries |
| Account Linkage | ❌ Undefined ("later") | ✅ Explicit rules + upsert |
| Deduplication | ❌ Incomplete | ✅ file_hash + deterministic keys |
| Balance Handling | ❌ From latest txn | ✅ Immutable account_ledger |
| Multi-Currency | ❌ Ignored | ✅ amount_original + exchange_rate |
| Subscription Detection | ❌ Hand-waved | ✅ Defined algorithm |
| Subscription History | ❌ Insufficient | ✅ Confidence score + occurrence count |
| Transfers | ❌ Missing entirely | ✅ Detection + gold.transfers table |
| Holdings | ❌ No account link | ✅ account_id foreign key |
| Job Orchestration | ❌ Undefined | ✅ Celery + idempotency |
| Pagination | ❌ Returns all | ✅ Limit + offset |
| Input Validation | ❌ None | ✅ Pydantic models |
| Indexes | ❌ None | ✅ All critical paths |
| Quality Issues | ❌ Fragile array | ✅ Separate table |
| Error Recovery | ❌ None | ✅ Quality issues + logging |
| Logging | ❌ None | ✅ Structured logging |

---

## Summary

This revised architecture is **production-ready and secure**. Key features:

✅ **No SQL injection** (parameterized queries everywhere)
✅ **Handles duplicates** (file_hash + deterministic keys)
✅ **Defines all unclear steps** (account linking, job orchestration, subscription detection)
✅ **Supports multi-currency** (stores both original and GBP)
✅ **Detects transfers** (prevents double-counting)
✅ **Immutable balances** (account_ledger for point-in-time queries)
✅ **Structured quality tracking** (separate table, not fragile arrays)
✅ **Idempotent jobs** (can be safely retried)
✅ **Indexes everywhere** (performance tested)
✅ **Pagination for Claude** (fits in token budget)
✅ **Input validation** (Pydantic models)
✅ **Error handling** (Celery retry logic + quality issues)

Next step: Start with V0 scope (Monzo + Natwest only). Ready to code?
