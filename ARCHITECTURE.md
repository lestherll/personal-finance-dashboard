# Personal Finance Dashboard Architecture (File-Based Data Lake)

## Overview

**File-based data lake** with **DuckDB** query engine and **Celery** task orchestration.

No PostgreSQL. All data stored as **Parquet files** organized in medallion layers (Bronze/Silver/Gold).

```
CSVs from Banks
      ↓
   Adapters (auto-detect format)
      ↓
   Bronze Layer (raw Parquet, immutable)
      ↓
   Celery Job: Bronze→Silver
      ↓
   Silver Layer (normalized Parquet)
      ↓
   Celery Job: Silver→Gold
      ↓
   Gold Layer (enriched Parquet)
      ↓
   DuckDB Queries (in-process, no server)
```

---

## Core Components

### 1. **Adapters** (CSV Parsers)
- `MonzoAdapter` — Monzo export format
- `NatwelstAdapter` — Natwest export format
- `VanguardAdapter` — Vanguard holdings export
- `AdapterFactory` — Auto-detect + route to correct adapter

**Output:** List of `RawRecord` objects (no schema transformation)

### 2. **Data Lake Layers (Parquet Files)**

**Bronze:** `/data/bronze/{source_type}/{filename}.parquet`
- Raw, immutable records
- One Parquet file per upload
- Schema-free (read-parquet queries handle any structure)

**Silver:** `/data/silver/{entity_type}.parquet`
- Normalized records (transactions, accounts, holdings, account_ledger)
- Standardized columns
- Deduplicated by bronze_source_key

**Gold:** `/data/gold/{entity_type}.parquet`
- Enriched records (transactions, subscriptions, transfers, snapshots)
- Business logic applied
- Ready for analysis

### 3. **Celery Tasks** (Orchestration)
- `transform_bronze_to_silver` — Normalize raw data
- `enrich_silver_to_gold` — Add business logic
- Retry logic, error logging, scheduling

### 4. **DuckDB** (Query Engine)
- In-process SQL engine
- Queries Parquet files directly
- No schema management needed
- Fast OLAP queries

---

## Data Flow

```
1. USER UPLOADS CSV
   ↓
2. ADAPTER DETECTS FORMAT
   (MonzoAdapter, NatwelstAdapter, or VanguardAdapter)
   ↓
3. PARSE TO RAWRECORDS
   (source_key, source_type, raw_data, filename, file_hash)
   ↓
4. WRITE TO BRONZE PARQUET
   data/bronze/monzo/export_20240115.parquet
   ↓
5. CELERY JOB: transform_bronze_to_silver
   - Link accounts (get_or_create_account)
   - Normalize schema (SilverTransformer)
   - Write to Silver Parquet
   ↓
6. CELERY JOB: enrich_silver_to_gold
   - Categorize transactions
   - Detect subscriptions
   - Detect transfers
   - Write to Gold Parquet
   ↓
7. DUCKDB QUERY
   SELECT * FROM read_parquet('data/gold/transactions.parquet')
   WHERE transaction_date >= '2024-01-01'
```

---

## File Structure

```
data/
├── personal_finance.duckdb    # DuckDB database (metadata, indices)
├── bronze/
│   ├── monzo/
│   │   ├── export_20240115.parquet
│   │   └── export_20240116.parquet
│   ├── natwest/
│   │   └── export_20240115.parquet
│   └── vanguard/
│       └── holdings_20240115.parquet
├── silver/
│   ├── transactions.parquet       # Normalized transactions
│   ├── accounts.parquet           # Account registry
│   ├── holdings.parquet           # Normalized holdings
│   └── account_ledger.parquet     # Balance history
└── gold/
    ├── transactions.parquet       # Enriched transactions
    ├── subscriptions.parquet      # Detected subscriptions
    ├── transfers.parquet          # Detected transfers
    └── account_snapshots.parquet  # Daily aggregates
```

---

## Key Design Decisions

### Why File-Based (Parquet)?
- ✅ Immutable, append-only (Bronze)
- ✅ Self-describing schema
- ✅ Columnar format (fast OLAP queries)
- ✅ No schema migrations needed
- ✅ Easy to version control
- ✅ Portable (copy files anywhere)

### Why DuckDB (No PostgreSQL)?
- ✅ In-process (no server to manage)
- ✅ Queries Parquet directly (no ETL)
- ✅ Fast OLAP engine (built for analytics)
- ✅ Simple (one file: personal_finance.duckdb)
- ✅ Local development (no Docker required)

### Why Celery (Not Sync)?
- ✅ Schedules medallion transformations (Bronze→Silver→Gold)
- ✅ Retries failed jobs automatically
- ✅ Monitors task progress
- ✅ Logs errors to quality_issues
- ✅ Enables background processing

---

## V0 Scope (Done ✅)

✅ **Phase 1:** Adapter pattern + factory
- Monzo, Natwest, Vanguard adapters
- Auto-detection by confidence scoring
- Error handling for ambiguous formats

**Phase 2 (Next):** Data transformations
- Account linking (source-specific rules)
- Silver transformer (normalization)
- Subscription/transfer detection

**Phase 3:** Celery orchestration
- Bronze→Silver transformation job
- Silver→Gold enrichment job
- Job chaining + error recovery

**Phase 4:** Testing
- Unit tests for adapters
- Integration tests for transformations
- E2E tests with real CSV files

**Phase 5:** CLI tool
- Manual ingestion
- Pipeline inspection
- Demo script

---

## Configuration

```python
# config.py
DATA_DIR = "./data"
BRONZE_DIR = "./data/bronze"
SILVER_DIR = "./data/silver"
GOLD_DIR = "./data/gold"
DUCKDB_PATH = "./data/personal_finance.duckdb"
REDIS_URL = "redis://localhost:6379/0"
CELERY_CONFIG = {...}
```

---

## Examples

### Parse CSV and Write to Bronze
```python
from adapters.factory import AdapterFactory
from models.datalake import get_datalake
import pandas as pd

factory = AdapterFactory()
datalake = get_datalake()

with open("export.csv") as f:
    csv_content = f.read()

# Auto-detect format
records = factory.ingest(csv_content, "export.csv", file_hash="abc123")

# Convert to DataFrame
df = pd.DataFrame([r.__dict__ for r in records])

# Write to Bronze
datalake.write_bronze("monzo", "export.csv", df)
```

### Query Gold Layer with DuckDB
```python
from models.datalake import get_datalake

datalake = get_datalake()

# Query enriched transactions
result = datalake.query("""
    SELECT 
        transaction_date,
        merchant_name,
        amount_gbp,
        category
    FROM read_parquet('data/gold/transactions.parquet')
    WHERE transaction_date >= '2024-01-01'
    ORDER BY transaction_date DESC
    LIMIT 100
""")

print(result)
```

---

## Next Steps

1. **Phase 2:** Implement transformers (account linking, normalization, detection)
2. **Phase 3:** Create Celery tasks for medallion transitions
3. **Phase 4:** Write comprehensive tests
4. **Phase 5:** Build CLI for manual testing
5. **V1:** Add REST API + Claude integration

---

## Tech Stack

| Layer | Technology | Why |
|-------|------------|-----|
| CSV Parsing | Python + Adapters | Type-safe, extensible |
| Storage | Parquet + DuckDB | Immutable, fast, portable |
| Orchestration | Celery + Redis | Task scheduling, retry logic |
| Query | DuckDB SQL | In-process, no server |
| CLI | Click | User-friendly commands |
| Testing | pytest | Comprehensive coverage |

---

## Success Metrics

✅ Adapters auto-detect format with confidence scoring
✅ All data flows through medallion layers (Bronze→Silver→Gold)
✅ No data loss on re-upload (deterministic source_keys)
✅ Celery jobs are idempotent and retryable
✅ DuckDB queries return results in < 1 second
✅ Full audit trail (every record traced back to source file)
