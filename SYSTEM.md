# Personal Finance System Architecture
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
│  │ - Timestamps, source IDs             │    │
│  │ - No transformation                  │    │
│  └──────────────────────────────────────┘    │
│                                                │
│  ┌──────────────────────────────────────┐    │
│  │ SILVER LAYER (Normalized)            │    │
│  │ - Schema validation & normalization  │    │
│  │ - Standard column names              │    │
│  │ - Data quality checks                │    │
│  │ - Deduplicated transactions          │    │
│  └──────────────────────────────────────┘    │
│                                                │
│  ┌──────────────────────────────────────┐    │
│  │ GOLD LAYER (Domain Models)           │    │
│  │ - Transactions (normalized)          │    │
│  │ - Accounts (merged state)            │    │
│  │ - Holdings (enriched)                │    │
│  │ - Recurring patterns                 │    │
│  │ - Account ledger (immutable log)     │    │
│  └──────────────────────────────────────┘    │
└────────────────────────┬─────────────────────┘
                         │
    ┌────────────────────┼──────────────────┐
    │                    │                  │
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
    source_key: str  # "monzo_transactions_20240601"
    source_type: str  # "monzo", "natwest", "amex", "vanguard"
    raw_data: Dict[str, Any]  # Entire row as dict
    filename: str
    upload_timestamp: datetime
    line_number: int

class DataSourceAdapter(ABC):
    """All adapters inherit from this"""
    
    @abstractmethod
    def validate(self, file_content: str) -> bool:
        """Check if file format matches this adapter"""
        pass
    
    @abstractmethod
    def parse(self, file_content: str) -> List[RawRecord]:
        """Parse file, return raw records (minimal transformation)"""
        pass
    
    @abstractmethod
    def detect_source_type(self) -> str:
        """Return: 'monzo', 'natwest', 'amex', 'vanguard'"""
        pass
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
    
    def validate(self, file_content: str) -> bool:
        """Check first row contains Monzo headers"""
        lines = file_content.split('\n')
        if not lines:
            return False
        headers = [h.strip() for h in lines[0].split(',')]
        return all(col in headers for col in self.EXPECTED_COLUMNS[:5])
    
    def parse(self, file_content: str) -> List[RawRecord]:
        """Convert CSV to RawRecord list"""
        import csv
        from io import StringIO
        
        records = []
        reader = csv.DictReader(StringIO(file_content))
        
        for idx, row in enumerate(reader, start=2):
            source_key = f"monzo_txn_{row['Transaction ID']}"
            records.append(RawRecord(
                source_key=source_key,
                source_type='monzo',
                raw_data=dict(row),  # Keep all fields as-is
                filename='monzo_export.csv',
                upload_timestamp=datetime.now(),
                line_number=idx
            ))
        
        return records
    
    def detect_source_type(self) -> str:
        return 'monzo'


# adapters/natwest_adapter.py
class NatwuestAdapter(DataSourceAdapter):
    """Natwest CSV export (different format from Monzo)"""
    
    EXPECTED_COLUMNS = [
        'Transaction Type', 'Transaction Date', 'Transaction Amount',
        'Transaction Narrative', 'Balance', 'Balance Date'
    ]
    
    def validate(self, file_content: str) -> bool:
        lines = file_content.split('\n')
        if not lines:
            return False
        headers = [h.strip() for h in lines[0].split(',')]
        return any(col in headers for col in self.EXPECTED_COLUMNS[:3])
    
    def parse(self, file_content: str) -> List[RawRecord]:
        import csv
        from io import StringIO
        
        records = []
        reader = csv.DictReader(StringIO(file_content))
        
        for idx, row in enumerate(reader, start=2):
            # Natwest has no explicit transaction ID; use date + amount + narrative
            source_key = f"natwest_txn_{row['Transaction Date']}_{row['Transaction Amount']}_{idx}"
            records.append(RawRecord(
                source_key=source_key,
                source_type='natwest',
                raw_data=dict(row),
                filename='natwest_export.csv',
                upload_timestamp=datetime.now(),
                line_number=idx
            ))
        
        return records
    
    def detect_source_type(self) -> str:
        return 'natwest'


# adapters/vanguard_adapter.py
class VanguardAdapter(DataSourceAdapter):
    """Vanguard holdings export (often PDF → parsed to CSV)"""
    
    EXPECTED_COLUMNS = [
        'ISIN', 'Fund Name', 'Quantity', 'Price', 'Value'
    ]
    
    def validate(self, file_content: str) -> bool:
        headers = [h.strip() for h in file_content.split('\n')[0].split(',')]
        return any(col in headers for col in self.EXPECTED_COLUMNS)
    
    def parse(self, file_content: str) -> List[RawRecord]:
        import csv
        from io import StringIO
        
        records = []
        reader = csv.DictReader(StringIO(file_content))
        
        for idx, row in enumerate(reader, start=2):
            source_key = f"vanguard_holding_{row['ISIN']}_{row['Quantity']}"
            records.append(RawRecord(
                source_key=source_key,
                source_type='vanguard',
                raw_data=dict(row),
                filename='vanguard_holdings.csv',
                upload_timestamp=datetime.now(),
                line_number=idx
            ))
        
        return records
    
    def detect_source_type(self) -> str:
        return 'vanguard'
```

### 2.3 Adapter Factory & Registry

```python
# adapters/factory.py
class AdapterFactory:
    """Auto-detect and route to the right adapter"""
    
    def __init__(self):
        # Registry of all adapters—add new ones here
        self.adapters = [
            MonzoAdapter(),
            NatwuestAdapter(),
            AmexAdapter(),  # To implement
            VanguardAdapter(),
        ]
    
    def detect_adapter(self, file_content: str) -> DataSourceAdapter:
        """Try each adapter until one validates"""
        for adapter in self.adapters:
            if adapter.validate(file_content):
                return adapter
        
        raise ValueError("File format not recognized. Supported: Monzo, Natwest, Amex, Vanguard")
    
    def ingest(self, file_content: str, filename: str) -> List[RawRecord]:
        """Single entry point: detect + parse"""
        adapter = self.detect_adapter(file_content)
        records = adapter.parse(file_content)
        
        # Log ingestion event
        print(f"✓ Ingested {len(records)} records from {adapter.detect_source_type()}")
        
        return records
```

---

## 3. Data Lake Layers (Medallion Architecture)

### 3.1 Bronze Layer (Immutable Raw)

**Purpose:** Store everything exactly as uploaded. No transformations.

```sql
-- bronze.raw_file_uploads
CREATE TABLE raw_file_uploads (
    id UUID PRIMARY KEY,
    source_key VARCHAR UNIQUE,  -- "monzo_txn_abc123"
    source_type VARCHAR NOT NULL,  -- "monzo", "natwest", "amex", "vanguard"
    raw_data JSONB NOT NULL,  -- Entire row as JSON
    filename VARCHAR,
    upload_timestamp TIMESTAMP NOT NULL,
    line_number INT,
    file_hash VARCHAR,  -- Detect duplicate uploads
    
    -- Lineage
    processed BOOLEAN DEFAULT FALSE,
    processed_at TIMESTAMP,
    
    -- Audit
    created_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT unique_source_key UNIQUE (source_key, source_type)
);

-- Why JSONB?
-- - Flexible schema (Monzo has different columns than Natwest)
-- - Can add new sources without schema migration
-- - Preserves raw data fidelity
```

**Write Pattern:** Adapters → Bronze (append-only)

**Read Pattern:** Never read Bronze directly. Always go through Silver/Gold.

### 3.2 Silver Layer (Normalized)

**Purpose:** Standardize schema, deduplicate, validate quality.

```sql
-- silver.transactions
-- Source-agnostic normalized view
CREATE TABLE transactions (
    id UUID PRIMARY KEY,
    
    -- Lineage
    bronze_source_key VARCHAR NOT NULL,  -- Link back to bronze
    source_type VARCHAR NOT NULL,  -- monzo, natwest, amex
    
    -- Standardized fields (normalized from any adapter)
    account_id UUID NOT NULL,  -- Links to silver.accounts
    transaction_date DATE NOT NULL,
    transaction_time TIME,
    amount_gbp DECIMAL(12, 2) NOT NULL,
    direction VARCHAR NOT NULL,  -- 'credit', 'debit'
    merchant_name VARCHAR,
    
    -- Derived (deterministic, from adapter rules)
    category VARCHAR,  -- "Groceries", "Transport", "Subscription", etc.
    is_recurring BOOLEAN DEFAULT FALSE,
    
    -- Data quality
    data_quality_flags VARCHAR[],  -- ["missing_time", "negative_balance_jump"]
    
    -- Audit
    ingested_at TIMESTAMP NOT NULL,
    ingested_from_file VARCHAR,
    
    CONSTRAINT fk_account FOREIGN KEY (account_id) REFERENCES silver.accounts(id),
    CONSTRAINT unique_txn_per_source UNIQUE (source_type, bronze_source_key)
);

-- silver.accounts
CREATE TABLE accounts (
    id UUID PRIMARY KEY,
    source_type VARCHAR NOT NULL,  -- monzo, natwest, amex, vanguard
    external_account_id VARCHAR,  -- Monzo: account_id, Natwest: sort code + account
    account_name VARCHAR,
    account_type VARCHAR,  -- "checking", "savings", "investment", "isa"
    currency VARCHAR DEFAULT 'GBP',
    
    -- Derived from latest transaction balance
    latest_balance DECIMAL(12, 2),
    balance_as_of TIMESTAMP,
    
    -- Metadata
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW(),
    
    CONSTRAINT unique_account_per_source UNIQUE (source_type, external_account_id)
);

-- silver.holdings
CREATE TABLE holdings (
    id UUID PRIMARY KEY,
    source_type VARCHAR NOT NULL,  -- vanguard, etc.
    isin VARCHAR NOT NULL,
    fund_name VARCHAR,
    quantity DECIMAL(12, 6) NOT NULL,
    unit_price DECIMAL(12, 4) NOT NULL,
    total_value DECIMAL(12, 2) NOT NULL,
    
    as_of_date DATE NOT NULL,
    ingested_at TIMESTAMP NOT NULL,
    
    CONSTRAINT unique_holding_per_source UNIQUE (source_type, isin, as_of_date)
);
```

**Transformation Rules (Silver):**

```python
# transformers/silver_transformer.py
class SilverTransformer:
    """
    Convert Bronze RawRecords → Silver normalized schema
    Encodes source-specific business rules
    """
    
    @staticmethod
    def normalize_monzo(raw_record: RawRecord) -> dict:
        """Monzo-specific normalization"""
        data = raw_record.raw_data
        
        return {
            'bronze_source_key': raw_record.source_key,
            'source_type': 'monzo',
            'account_id': None,  # Will be linked later
            'transaction_date': datetime.strptime(data['Date'], '%d/%m/%Y').date(),
            'transaction_time': data.get('Time'),
            'amount_gbp': float(data['Amount']),
            'direction': 'credit' if float(data['Amount']) > 0 else 'debit',
            'merchant_name': data.get('Name', '').strip(),
            'category': data.get('Category', 'Uncategorized'),
            'is_recurring': False,  # Monzo doesn't flag this; use pattern detection
            'data_quality_flags': [],
            'ingested_at': datetime.now(),
        }
    
    @staticmethod
    def normalize_natwest(raw_record: RawRecord) -> dict:
        """Natwest-specific normalization"""
        data = raw_record.raw_data
        
        return {
            'bronze_source_key': raw_record.source_key,
            'source_type': 'natwest',
            'account_id': None,
            'transaction_date': datetime.strptime(data['Transaction Date'], '%d/%m/%Y').date(),
            'transaction_time': None,  # Natwest doesn't export time
            'amount_gbp': float(data['Transaction Amount']),
            'direction': 'credit' if float(data['Transaction Amount']) > 0 else 'debit',
            'merchant_name': data.get('Transaction Narrative', '').strip(),
            'category': None,  # Natwest doesn't provide categories
            'is_recurring': False,
            'data_quality_flags': ['missing_time', 'missing_category'] if not data.get('Transaction Narrative') else [],
            'ingested_at': datetime.now(),
        }
    
    @staticmethod
    def to_silver(raw_record: RawRecord) -> dict:
        """Dispatch to source-specific normalizer"""
        if raw_record.source_type == 'monzo':
            return SilverTransformer.normalize_monzo(raw_record)
        elif raw_record.source_type == 'natwest':
            return SilverTransformer.normalize_natwest(raw_record)
        # ... other sources
        else:
            raise ValueError(f"Unknown source type: {raw_record.source_type}")
```

### 3.3 Gold Layer (Domain Models)

**Purpose:** Business logic, enrichment, aggregations.

```sql
-- gold.transaction_ledger
-- Immutable, append-only event log
CREATE TABLE transaction_ledger (
    id UUID PRIMARY KEY,
    
    -- Core fields
    account_id UUID NOT NULL,
    transaction_date DATE NOT NULL,
    amount_gbp DECIMAL(12, 2) NOT NULL,
    direction VARCHAR NOT NULL,
    merchant_name VARCHAR,
    
    -- Enrichment (computed)
    category VARCHAR,
    merchant_category_code VARCHAR,  -- Via fuzzy matching / Claude
    tags TEXT[],
    is_subscription BOOLEAN,
    
    -- Metadata
    silver_transaction_id UUID NOT NULL,
    source_type VARCHAR,
    
    -- Audit
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    
    CONSTRAINT fk_silver FOREIGN KEY (silver_transaction_id) REFERENCES silver.transactions(id)
);

-- gold.recurring_subscriptions
-- Detected patterns
CREATE TABLE recurring_subscriptions (
    id UUID PRIMARY KEY,
    account_id UUID NOT NULL,
    merchant_name VARCHAR,
    expected_frequency VARCHAR,  -- "monthly", "quarterly", "annual"
    amount_gbp DECIMAL(12, 2),
    last_occurrence DATE,
    next_expected_date DATE,
    confidence_score FLOAT,  -- 0-1, based on pattern consistency
    is_active BOOLEAN DEFAULT TRUE,
    
    created_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT fk_account FOREIGN KEY (account_id) REFERENCES silver.accounts(id)
);

-- gold.account_snapshots
-- Point-in-time snapshots for historical queries
CREATE TABLE account_snapshots (
    id UUID PRIMARY KEY,
    account_id UUID NOT NULL,
    snapshot_date DATE NOT NULL,
    balance DECIMAL(12, 2),
    transaction_count INT,
    total_inflow DECIMAL(12, 2),
    total_outflow DECIMAL(12, 2),
    created_at TIMESTAMP DEFAULT NOW(),
    
    CONSTRAINT fk_account FOREIGN KEY (account_id) REFERENCES silver.accounts(id),
    CONSTRAINT unique_snapshot UNIQUE (account_id, snapshot_date)
);
```

---

## 4. Data Flow: File Upload → Lake

```
1. FILE UPLOAD
   User uploads: "monzo_export.csv"
   ↓
2. ADAPTER DETECTION
   AdapterFactory.detect_adapter(file_content)
   → Returns MonzoAdapter
   ↓
3. PARSE → BRONZE
   MonzoAdapter.parse() → List[RawRecord]
   → Inserted into bronze.raw_file_uploads (immutable)
   ↓
4. BRONZE → SILVER (Transformation Job)
   FOR EACH raw_file_uploads WHERE processed = FALSE:
     SilverTransformer.to_silver(raw_record)
     → Insert into silver.transactions
     → Insert into silver.accounts
     → Mark bronze as processed
   ↓
5. SILVER → GOLD (Enrichment Job)
   FOR EACH silver.transactions with NO gold.transaction_ledger entry:
     - Apply categorization rules
     - Detect subscriptions (pattern matching)
     - Link to accounts
     → Insert into gold.transaction_ledger
     → Insert into gold.recurring_subscriptions (if detected)
   ↓
6. QUERY / ANALYTICS
   Dashboard queries gold layer
   Claude context pulls from gold layer
```

---

## 5. API Layer (Built on Data Lake)

### 5.1 REST Endpoints

```python
# api/transactions.py
from fastapi import FastAPI, UploadFile, File
from datetime import date

app = FastAPI()

@app.post("/api/v1/ingest")
async def ingest_file(file: UploadFile = File(...)):
    """
    Upload a CSV file. Auto-detect source type.
    
    Returns:
    {
        "status": "success",
        "source_type": "monzo",
        "records_inserted": 127,
        "bronze_records": 127,
        "silver_records": 127,
        "warnings": []
    }
    """
    content = await file.read()
    
    factory = AdapterFactory()
    raw_records = factory.ingest(content.decode(), file.filename)
    
    # Insert to bronze
    bronze_count = db.insert_bronze(raw_records)
    
    # Transform to silver
    silver_records = [SilverTransformer.to_silver(r) for r in raw_records]
    silver_count = db.insert_silver(silver_records)
    
    return {
        "status": "success",
        "source_type": raw_records[0].source_type,
        "records_inserted": len(raw_records),
        "bronze_records": bronze_count,
        "silver_records": silver_count,
    }


@app.get("/api/v1/transactions")
async def get_transactions(
    account_id: str = None,
    start_date: date = None,
    end_date: date = None,
    category: str = None
):
    """
    Query transactions from gold layer.
    
    Returns normalized transaction records across all sources.
    """
    query = "SELECT * FROM gold.transaction_ledger WHERE 1=1"
    
    if account_id:
        query += f" AND account_id = '{account_id}'"
    if start_date:
        query += f" AND transaction_date >= '{start_date}'"
    if end_date:
        query += f" AND transaction_date <= '{end_date}'"
    if category:
        query += f" AND category = '{category}'"
    
    return db.query(query)


@app.get("/api/v1/accounts")
async def get_accounts():
    """List all accounts from silver layer"""
    return db.query("SELECT * FROM silver.accounts WHERE is_active = TRUE")


@app.get("/api/v1/holdings")
async def get_holdings():
    """List all holdings from silver layer"""
    return db.query("SELECT * FROM silver.holdings ORDER BY as_of_date DESC")


@app.get("/api/v1/subscriptions")
async def get_subscriptions():
    """List detected recurring subscriptions"""
    return db.query("""
        SELECT 
            merchant_name,
            amount_gbp,
            expected_frequency,
            last_occurrence,
            next_expected_date,
            confidence_score
        FROM gold.recurring_subscriptions
        WHERE is_active = TRUE
        ORDER BY amount_gbp DESC
    """)


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
    return db.query(f"""
        SELECT 
            SUM(CASE WHEN direction = 'credit' THEN amount_gbp ELSE 0 END) as total_inflow,
            SUM(CASE WHEN direction = 'debit' THEN amount_gbp ELSE 0 END) as total_outflow,
            COUNT(*) as transaction_count
        FROM gold.transaction_ledger
        WHERE transaction_date BETWEEN '{start_date}' AND '{end_date}'
    """)
```

### 5.2 Claude Context API

```python
# api/claude_context.py

@app.get("/api/v1/claude-context")
async def get_claude_context(goal_id: str = None):
    """
    Fetch data for Claude to analyze.
    
    Returns:
    {
        "accounts": [{...}],
        "transactions_6mo": [{...}],
        "subscriptions": [{...}],
        "holdings": [{...}],
        "goals": [{...}],
        "summary": {
            "net_worth": 150000,
            "monthly_savings": 1700,
            "subscription_total": 245,
            "portfolio_allocation": {...}
        }
    }
    """
    
    # Last 6 months of transactions
    six_months_ago = date.today() - timedelta(days=180)
    transactions = db.query(f"""
        SELECT * FROM gold.transaction_ledger
        WHERE transaction_date >= '{six_months_ago}'
        ORDER BY transaction_date DESC
    """)
    
    # Current accounts
    accounts = db.query("SELECT * FROM silver.accounts WHERE is_active = TRUE")
    
    # Current holdings
    latest_holdings_date = db.query("""
        SELECT DISTINCT as_of_date FROM silver.holdings
        ORDER BY as_of_date DESC LIMIT 1
    """)[0]['as_of_date']
    
    holdings = db.query(f"""
        SELECT * FROM silver.holdings WHERE as_of_date = '{latest_holdings_date}'
    """)
    
    # Subscriptions
    subscriptions = db.query("""
        SELECT * FROM gold.recurring_subscriptions WHERE is_active = TRUE
    """)
    
    # Aggregates
    summary_data = db.query(f"""
        SELECT 
            SUM(balance) as total_balance,
            AVG(balance) as avg_balance
        FROM silver.accounts
    """)[0]
    
    return {
        "accounts": accounts,
        "transactions_6mo": transactions,
        "subscriptions": subscriptions,
        "holdings": holdings,
        "summary": summary_data
    }
```

---

## 6. Extensibility Points (How to Add New Sources)

### Case: Adding American Express in the Future

**Step 1: Create Adapter**
```python
# adapters/amex_adapter.py
class AmexAdapter(DataSourceAdapter):
    EXPECTED_COLUMNS = ['Transaction Date', 'Description', 'Amount', 'Extended Details']
    
    def validate(self, file_content: str) -> bool: ...
    def parse(self, file_content: str) -> List[RawRecord]: ...
    def detect_source_type(self) -> str:
        return 'amex'
```

**Step 2: Register in Factory**
```python
# adapters/factory.py
self.adapters = [
    MonzoAdapter(),
    NatwuestAdapter(),
    AmexAdapter(),  # ← Add here
    VanguardAdapter(),
]
```

**Step 3: Add Normalization Rule**
```python
# transformers/silver_transformer.py
@staticmethod
def normalize_amex(raw_record: RawRecord) -> dict: ...

# In to_silver():
elif raw_record.source_type == 'amex':
    return SilverTransformer.normalize_amex(raw_record)
```

**That's it.** No database migration. Bronze already has flexibility via JSONB. Silver schema already has source_type column.

### Case: Adding Plaid API in V1

**Step 1: Create API Adapter (parallel to file adapter)**
```python
# adapters/plaid_adapter.py
class PlaidAdapter(DataSourceAdapter):
    def __init__(self, api_key: str):
        self.client = plaid.ApiClient(...)
    
    def validate(self, file_content: str) -> bool:
        # Not applicable for API
        return True
    
    def parse(self, account_ids: List[str]) -> List[RawRecord]:
        # Fetch from Plaid API instead of parsing file
        records = []
        for account_id in account_ids:
            txns = self.client.get_transactions(account_id)
            for txn in txns:
                records.append(RawRecord(
                    source_key=f"plaid_txn_{txn['transaction_id']}",
                    source_type='plaid',
                    raw_data=txn,
                    ...
                ))
        return records
```

**Step 2: Webhook Handler**
```python
# api/webhooks.py
@app.post("/api/v1/webhooks/plaid")
async def plaid_webhook(event: dict):
    """
    Plaid sends TRANSACTIONS_UPDATED event
    → Trigger Bronze insert → Silver transform → Done
    """
    adapter = PlaidAdapter(api_key=config.plaid_key)
    raw_records = adapter.parse([event['account_id']])
    db.insert_bronze(raw_records)
    # Silver transformation runs async job
    return {"status": "received"}
```

**Zero breaking changes.** Same Bronze, Silver, Gold layers.

---

## 7. Tech Stack Recommendation

| Component | Technology | Why |
|-----------|------------|-----|
| **API** | FastAPI (Python) | Type-safe, auto OpenAPI docs, great for data pipelines |
| **Database** | PostgreSQL + TimescaleDB | JSONB for flexibility, time-series for snapshots |
| **File Storage (Bronze)** | PostgreSQL JSONB + S3 backup | Immutable, versioned, queryable |
| **Job Queue** | Celery + Redis | Async Bronze→Silver→Gold transformations |
| **Frontend** | React + TypeScript | Not critical for V0; focus on API first |
| **Deployment** | Docker + Docker Compose (dev), Render/Railway (prod) | Simple, scalable, cost-effective |
| **Monitoring** | Prometheus + Grafana | Track ingestion pipeline health |

---

## 8. Implementation Roadmap

### V0 (Weeks 1-2)
- [ ] Core adapter pattern + 4 adapters (Monzo, Natwest, Amex, Vanguard)
- [ ] PostgreSQL setup (Bronze, Silver, Gold tables)
- [ ] File upload API (`POST /api/v1/ingest`)
- [ ] Basic transformation (Bronze → Silver)
- [ ] Query API (`GET /api/v1/transactions`)
- [ ] Manual testing with your own exports

### V0.5 (Week 3)
- [ ] Subscription detection (pattern matching in gold layer)
- [ ] Account summary API
- [ ] Data quality dashboard (# of warnings, coverage by source)
- [ ] Simple React dashboard (list transactions, accounts)

### V1 (Month 2)
- [ ] Claude integration endpoint (`GET /api/v1/claude-context`)
- [ ] Goal management schema (gold.goals table)
- [ ] Recurring job for Bronze→Silver→Gold pipeline
- [ ] API documentation + postman collection

### V2 (Month 3)
- [ ] Plaid integration (API instead of CSV)
- [ ] Goal scenario modeling API
- [ ] Claude advisor prompts (goal adjustment recommendations)

---

## 9. Key Design Principles

| Principle | Implementation |
|-----------|---|
| **Immutability** | Bronze is append-only; transactions are immutable events |
| **Auditability** | Every record links back to bronze source + timestamps |
| **Source Flexibility** | New sources = new adapter; no schema changes |
| **Separation of Concerns** | Bronze (raw), Silver (normalized), Gold (domain logic) |
| **Schema Evolution** | JSONB in bronze; Silver schema is source-agnostic; Gold can be extended |
| **Scalability** | Adapters are stateless; jobs are parallel; no monolithic transform |
| **Debuggability** | Trace any transaction back to original file + raw data |

---

## 10. Error Handling & Data Quality

```python
# transformers/validation.py
class ValidationError(Exception):
    def __init__(self, record: RawRecord, reason: str):
        self.record = record
        self.reason = reason
        self.source_key = record.source_key

# Bronze insertion includes validation
try:
    silver_record = SilverTransformer.to_silver(raw_record)
except ValidationError as e:
    # Log to data_quality_issues table
    db.log_quality_issue(
        source_key=raw_record.source_key,
        severity='warning',  # or 'error'
        reason=e.reason
    )
    # Still insert; mark with flag
    silver_record['data_quality_flags'].append(e.reason)
    db.insert_silver(silver_record)
```

**Data Quality Dashboard:**
- Tracks failed validations per source
- Flags incomplete fields (e.g., Natwest missing transaction times)
- Shows coverage % per account type

---

## 11. Querying the Data Lake

### For Dashboard
```sql
-- Total balance by account type
SELECT 
    a.account_type,
    COUNT(*) as account_count,
    SUM(a.latest_balance) as total_balance
FROM silver.accounts a
GROUP BY a.account_type;

-- Spending by category (last 30 days)
SELECT 
    category,
    SUM(amount_gbp) as total_spent,
    COUNT(*) as transaction_count
FROM gold.transaction_ledger
WHERE direction = 'debit'
  AND transaction_date >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY category
ORDER BY total_spent DESC;
```

### For Claude
```python
# Single query returns all context Claude needs
context = {
    'accounts': fetch_accounts(),
    'recent_spending': fetch_spending_6mo(),
    'portfolio': fetch_holdings(),
    'subscriptions': fetch_subscriptions(),
    'summary': {
        'net_worth': calculate_net_worth(),
        'monthly_savings_rate': calculate_savings_rate(),
    }
}

# Pass to Claude as JSON
response = client.messages.create(
    model="claude-opus",
    system="You are a personal finance advisor...",
    messages=[{
        "role": "user",
        "content": f"Here's my financial data: {json.dumps(context)}\n\nI want to buy a house in 3 years. What should I do?"
    }]
)
```

---

## 12. Security Considerations

- **No plaintext passwords:** All credentials in env variables (Plaid API key, DB password)
- **JSONB fields encrypted at rest:** PostgreSQL native encryption or application-level
- **Immutable bronze:** Prevents accidental deletion of source data
- **Audit trail:** Every insertion logged with timestamp + source
- **Access control (future):** API keys / JWT for multi-user setup

---

## Summary

This design gives you:
1. ✅ **Flexibility:** Add sources without code rewrites
2. ✅ **Scalability:** Layers decouple Bronze (raw) from Gold (analysis)
3. ✅ **Debuggability:** Trace any record back to source file
4. ✅ **LLM-Ready:** Gold layer has everything Claude needs
5. ✅ **V0-Compatible:** File uploads → PostgreSQL (no fancy infrastructure)
6. ✅ **Future-Proof:** Adapt to Plaid, Stripe, crypto exchanges, etc.