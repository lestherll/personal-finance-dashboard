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

### 1. **Adapters** (CSV + PDF Parsers)

**CSV Adapters:**
- `MonzoAdapter` — Monzo export format
- `NatwestAdapter` — Natwest export format
- `VanguardAdapter` — Vanguard holdings export

**PDF Adapters (PyMuPDF-based text extraction):**
- `KrooPdfAdapter` — Kroo current account statements
- `NatwestPdfAdapter` — Natwest bank statements
- `FirstDirectPdfAdapter` — First Direct credit card statements
- `AmexPdfAdapter` — American Express statements
- `VanguardPdfAdapter` — Vanguard investment statements
- `PdfAdapter` — Shared base class (text extraction, validation template)

**Factory:**
- `AdapterFactory` — Auto-detect + route to correct adapter (branches on content type: CSV adapters for `str`, PDF adapters for `bytes`)

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
   (CSV: Monzo/Natwest/Vanguard; PDF: Kroo/Natwest/First Direct/Amex/Vanguard)
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
│   │   ├── export_20240115.parquet
│   │   └── statement_20260501.pdf.parquet
│   ├── vanguard/
│   │   ├── holdings_20240115.parquet
│   │   └── statement_20260708.pdf.parquet
│   ├── kroo/
│   │   └── statement_20260501.pdf.parquet
│   ├── firstdirect/
│   │   └── statement_20260505.pdf.parquet
│   └── amex/
│       └── statement_20260401.pdf.parquet
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

## V0 Scope

✅ **Phase 1:** Adapter pattern + factory (DONE)
- CSV adapters: Monzo, Natwest, Vanguard
- PDF adapters: Kroo, Natwest, First Direct, AmEx, Vanguard
- Auto-detection by confidence scoring (95%+ confidence validates adapter match)
- Error handling for ambiguous formats
- ⚠️ **Test coverage:** CSV adapters have full unit tests; PDF adapters validated on real statements but lack unit tests (need to be added in Phase 4)

✅ **Phase 2:** Data transformations (DONE)
- Account linking via a static `source_type → account_id` mapping (`transformers/account_config.py`) — deliberately not runtime heuristics, since the account set is small and known in advance
- Silver transformer (`transformers/silver_transformer.py`) — normalizes all 8 adapters' `RawRecord.raw_data` fields to a common schema across `transactions`, `holdings`, `account_ledger`
- Deduplication via `bronze_source_key`, idempotent reruns (`_dedupe_with_existing`)
- ⚠️ `account_ledger` only covers Natwest CSV + Vanguard CSV (PDF adapters discard balance in Phase 1); Natwest PDF/AmEx dates need year inference since source text omits the year
- Subscription/transfer detection deferred to Phase 3 (Silver→Gold enrichment, not Silver normalization)

**Phase 3 (Next):** Celery orchestration
- Bronze→Silver transformation job
- Silver→Gold enrichment job
- Job chaining + error recovery + retry logic

**Phase 4:** Testing (expand)
- Unit tests for all PDF adapters
- Integration tests for transformations
- E2E tests with real CSV + PDF files

**Phase 5:** CLI tool
- Manual ingestion command
- Pipeline inspection/status
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

1. ✅ **Phase 2:** Implement transformers (account linking, normalization) — DONE
2. **Phase 3:** Create Celery tasks for medallion transitions (wrap `run_bronze_to_silver()`; add Silver→Gold enrichment)
3. **Phase 4:** Write comprehensive tests (PDF adapter unit tests, disk-backed pipeline integration tests)
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
