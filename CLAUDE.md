# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Personal finance dashboard using a **medallion data lake architecture**. Ingests bank statements (CSV + PDF) from 8 financial sources, normalizes them through Bronze→Silver→Gold layers, and enables SQL analytics via DuckDB.

- **Storage:** File-based Parquet files (not a database)
- **Query Engine:** DuckDB (in-process, no server)
- **Orchestration:** Celery + Redis (configured but not yet wired up)
- **Python Version:** 3.13+ (use `uv` to manage)

See `ARCHITECTURE.md` for the full design philosophy and data flow.

---

## Development Commands

All commands use `uv` (not pip or conda). Dev dependencies require `uv sync --extra dev`.

### Environment & Setup
```bash
uv python list                    # Check available Python versions (3.13 recommended)
uv sync --extra dev               # Install all deps + dev tools (pytest, black, ruff)
uv run python -m pytest tests/    # Run all tests with Python auto-selected from pyproject.toml
```

### Running Tests
```bash
uv run pytest tests/unit/adapters/ -v              # Test CSV/PDF adapter parsing
uv run pytest tests/unit/adapters/test_monzo_adapter.py -v  # Single adapter test
uv run pytest tests/ -k "test_kroo" -v             # Run tests matching pattern
uv run pytest --collect-only                       # List all tests without running
uv run pytest --cov=adapters tests/                # Coverage report (adapters module only)
```

### Code Quality
```bash
uv run black adapters/ models/ tests/              # Format code
uv run ruff check adapters/ models/ tests/ --fix   # Lint + auto-fix
```

### Running the App
```bash
uv run python -c "from adapters.factory import AdapterFactory; print('OK')"  # Quick import test
uv run python << 'EOF'
from adapters.factory import AdapterFactory
factory = AdapterFactory()
# ... test code ...
EOF
```

---

## Architecture & Data Flow

### Medallion Layers (Bronze → Silver → Gold)

**Bronze Layer** (`data/bronze/{source_type}/`):
- Raw, immutable Parquet files (one per upload)
- Created by `adapters/` parsing CSV/PDF files
- Stores as-is: Monzo fields, Natwest fields, etc. (no normalization yet)
- Deduplication via deterministic `source_key` (prevents re-import duplicates)

**Silver Layer** (`data/silver/`):
- Normalized records: `transactions.parquet`, `accounts.parquet`, `holdings.parquet`, `account_ledger.parquet`
- Unified schema across all sources (e.g., all transaction amounts are single signed `amount` column, not Kroo's `out`/`in` split)
- Account linking applied (cross-source dedup: "this Natwest account is the same as this Monzo account")
- Created by `transformers/` (not yet implemented; Phase 2 scope)

**Gold Layer** (`data/gold/`):
- Enriched records: `transactions.parquet`, `subscriptions.parquet`, `transfers.parquet`, `account_snapshots.parquet`
- Business logic applied: subscription detection (recurring patterns), transfer detection (matched debit/credit pairs), account snapshots (daily balances)
- Ready for dashboard/analytics
- Created by enrichment tasks (Phase 3 scope, not yet implemented)

### Adapter Pattern (CSV & PDF)

Located in `adapters/`:

**Base:** `base.py` defines `DataSourceAdapter` ABC:
- `validate(file_content)` → `(is_valid: bool, confidence: float)` — Parse + score whether this file matches this adapter
- `parse(file_content, filename, file_hash)` → `List[RawRecord]` — Extract transactions/holdings
- `detect_source_type()` → `str` — Return adapter name (e.g., "monzo", "kroo")
- `generate_source_key(txn, line_num)` → `str` — Deterministic key for dedup

**CSV Adapters:** Monzo, Natwest, Vanguard (`*_adapter.py`)
- Parse string content (CSV text)
- Handle format variations (e.g., Monzo has both "full export" and "search export" formats)

**PDF Adapters:** Kroo, Natwest, First Direct, AmEx, Vanguard (`*_pdf_adapter.py`)
- Inherit from `PdfAdapter` base (handles PyMuPDF text extraction)
- Parse bytes content (PDF files)
- Implement bank-specific regex patterns to extract transactions from extracted text
- Handle multi-line transactions (PDF text extraction breaks table cells into separate lines)

**Factory:** `factory.py` auto-detects + routes:
- Branches on content type: `str` → try CSV adapters, `bytes` → try PDF adapters
- Scores each adapter, picks highest confidence
- Raises if no valid match or ambiguous tie (< 0.05 confidence gap at top)

### Data Lake I/O

Located in `models/datalake.py`:

```python
from models.datalake import get_datalake

datalake = get_datalake()

# Write raw records to Bronze
datalake.write_bronze("monzo", "export_20260724.csv", df)

# Read all Bronze records for a source
df = datalake.read_bronze("monzo")

# Query across Parquet files with DuckDB SQL
df = datalake.query("SELECT * FROM read_parquet('data/silver/transactions.parquet') LIMIT 10")
```

Singleton pattern: `get_datalake()` returns cached connection; safe to call multiple times.

---

## Extending the System

### Adding a New Bank Adapter

1. **Create the adapter file** (`adapters/newbank_adapter.py`):
   ```python
   from adapters.base import DataSourceAdapter, RawRecord
   
   class NewBankAdapter(DataSourceAdapter):
       def validate(self, file_content: str) -> tuple[bool, float]:
           # Check for bank-specific markers in CSV text
           if "NewBank" in file_content and "Transaction" in file_content:
               return True, 0.95
           return False, 0.0
       
       def parse(self, file_content: str, filename: str, file_hash: str) -> List[RawRecord]:
           # Parse CSV, extract transactions
           records = []
           for row in csv.DictReader(file_content.splitlines()):
               records.append(RawRecord(
                   source_key=self.generate_source_key(...),
                   source_type="newbank",
                   raw_data={"date": row["Date"], "amount": row["Amount"], ...},
                   filename=filename,
                   file_hash=file_hash,
                   upload_timestamp=datetime.now(),
                   line_number=idx,
               ))
           return records
       
       def generate_source_key(self, txn: dict, line_num: int) -> str:
           return f"newbank_txn_{txn['date']}_{txn['amount']}"
       
       def detect_source_type(self) -> str:
           return "newbank"
   ```

2. **Register in factory** (`adapters/factory.py`):
   - Add import: `from adapters.newbank_adapter import NewBankAdapter`
   - Add to `self.csv_adapters` list (or `self.pdf_adapters` if PDF)

3. **Add tests** (`tests/unit/adapters/test_newbank_adapter.py`):
   - Use fixtures in `tests/conftest.py` to create sample data
   - Test validation, parsing, source key generation

### Writing a Silver Transformer

Silver transformers normalize multi-source raw data to a common schema. Not yet implemented (Phase 2 scope).

Pattern to follow:
1. Create `transformers/silver_transformer.py` with class like `SilverTransformer`
2. Methods like `normalize_transactions(bronze_df)` that:
   - Take a Bronze DataFrame with mixed source types
   - Map source-specific fields to common schema
   - Return normalized DataFrame ready for write to Silver
3. Handle account linking: identify which accounts across sources are duplicates
4. Write to Silver via `datalake.write_silver("transactions", normalized_df)`

### Adding a Gold Enrichment

Gold enrichments add business logic (subscription detection, transfer matching, etc.). Not yet implemented (Phase 3 scope).

Pattern to follow:
1. Create task in `tasks/gold_tasks.py` (once Celery is wired up)
2. Read Silver layer, apply heuristics, write to Gold layer
3. E.g., `detect_subscriptions(silver_transactions_df)` → identify recurring monthly charges

---

## Key Files & Patterns

| File | Purpose |
|------|---------|
| `adapters/base.py` | `DataSourceAdapter` ABC; all adapters inherit from this |
| `adapters/factory.py` | `AdapterFactory.detect_adapter()` + `ingest()` — main entry point |
| `adapters/*_adapter.py` | Concrete adapters for each bank/format |
| `adapters/pdf_adapter.py` | Shared PDF base class (PyMuPDF text extraction) |
| `models/datalake.py` | `DataLake` singleton for Parquet I/O + DuckDB queries |
| `config.py` | Paths, logging level, Celery/Redis config (read-only at runtime) |
| `logging_config.py` | Structured logging setup (dictConfig-based) |
| `tests/conftest.py` | Shared pytest fixtures (sample CSV strings) |
| `tests/unit/adapters/` | Unit tests for adapters (CSV only; PDF tests are missing — Phase 4) |
| `transformers/` | (Empty, Phase 2) Silver normalization logic goes here |
| `tasks/` | (Empty, Phase 3) Celery task definitions go here |
| `ARCHITECTURE.md` | Design philosophy, data flow diagrams, Phase roadmap |

---

## Current Status

**Phase 1 ✅ DONE:** Adapters (CSV + PDF parsing)
- 8 bank sources working: Monzo, Natwest, Vanguard (CSV); Kroo, Natwest, First Direct, AmEx, Vanguard (PDF)
- Tested on real user data
- ⚠️ PDF adapters lack unit tests (only CSV adapters have test coverage)

**Phase 2 (Next): Silver Transformations**
- Account linking logic (identify duplicate accounts across sources)
- Schema normalization (map source-specific fields → common schema)
- Deduplication by `source_key`
- Lives in `transformers/` (currently empty)

**Phase 3: Celery Orchestration** (configured but not wired up)
- Bronze→Silver transformation job
- Silver→Gold enrichment job
- Job chaining + error recovery

**Phase 4: Testing**
- Add unit tests for all PDF adapters
- Integration tests for transformers
- E2E tests with real files

---

## Testing Notes

- **Unit tests** live in `tests/unit/adapters/` and use sample CSV strings from `conftest.py`
- **PDF adapters tested manually** on real statements (Kroo: 14 txns, Natwest: 12 txns, First Direct: 1 txn, Vanguard: 10 txns) but lack pytest coverage
- **No integration tests yet** (for Silver transformations or end-to-end pipelines)
- Dev dependencies require `uv sync --extra dev` (pytest, black, ruff not auto-installed with base `uv sync`)

---

## Configuration & Environment

Read-only at runtime (defined in `config.py`):
- `DATA_DIR` — root data lake directory (default: `./data`)
- `BRONZE_DIR`, `SILVER_DIR`, `GOLD_DIR` — medallion layer paths
- `DUCKDB_PATH` — DuckDB database file path
- `LOG_LEVEL` — logging level (default: `INFO`)
- `REDIS_URL` — Redis broker for Celery (default: `redis://localhost:6379/0`)

Overridable via env vars: `DATA_DIR`, `DUCKDB_PATH`, `LOG_LEVEL`, `REDIS_URL`.

Note: `.gitignore` does not explicitly exclude `data/`, so raw statement files *will* be committed if not careful — consider adding `data/` to `.gitignore` before first commit if you don't want raw files in git.

---

## Common Gotchas

1. **PyArrow import placement:** In `models/datalake.py`, `import pyarrow as pa` is at the bottom (line 177) instead of top. This works at runtime but is unconventional; consider moving it to the top.

2. **PDF adapter field normalization:** Kroo uses `out`/`in` columns, Natwest PDF uses signed `amount`, Monzo uses different field names entirely. Silver transformer will need to map all of these to a common schema.

3. **Re-upload deduplication:** Based on `source_key` (deterministic hash of date + description + amount). If parsing logic changes and produces different `source_key` for the same transaction, it could be imported twice. Document the generation rule clearly per adapter.

4. **Celery not yet wired:** Celery/Redis dependencies are installed and `CELERY_CONFIG` dict exists, but no Celery app instance or `@app.task` decorators exist yet. Phase 3 will wire this up.

5. **Account linking complexity:** Identifying that "this Natwest account ending in 1234 is the same as this Monzo account" requires heuristics (IBAN matching, account numbers, balance matching at a point in time). This is currently not implemented and will be in Phase 2.

