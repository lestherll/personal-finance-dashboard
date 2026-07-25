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

### CLI (ingestion + account mapping)
```bash
uv run python cli.py ingest <file> [<file> ...]           # Parse statement file(s), write to Bronze
uv run python cli.py accounts list-unmapped              # Bronze accounts with no mapping yet
uv run python cli.py accounts register <hash> <account_id> <display_name> <current|credit|investment|savings>
uv run python cli.py accounts register-fallback <source_type> <account_id> <display_name> <account_type>
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
- Account linking applied via hashed statement identifiers (sort code+account number, masked card number, etc.) resolved against `data/account_map.json` — see `transformers/account_config.py` and Gotcha #5
- Created by `transformers/silver_transformer.py::run_bronze_to_silver()` (Phase 2 — done)

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
- `generate_source_key(txn, line_num, account_identifier=None)` → `str` — Deterministic key for dedup; PDF adapters fold `account_identifier` in so two accounts of the same source_type (e.g. two Amex cards) never collide

`RawRecord` also carries `account_identifier` (hashed, see Gotcha #5) and `record_type` ("transaction" | "holding" — only Vanguard PDF produces both).

**CSV Adapters:** Monzo, Natwest, Vanguard (`*_adapter.py`)
- Parse string content (CSV text)
- Handle format variations (e.g., Monzo has both "full export" and "search export" formats)

**PDF Adapters:** Kroo, Natwest, First Direct, AmEx, Vanguard, Monzo (`*_pdf_adapter.py`)
- Inherit from `PdfAdapter` base (handles PyMuPDF text extraction)
- Parse bytes content (PDF files)
- Implement bank-specific regex patterns to extract transactions from extracted text
- Handle multi-line transactions (PDF text extraction breaks table cells into separate lines)

**Factory:** `factory.py` auto-detects + routes:
- Branches on content type: `str` → try CSV adapters, `bytes` → try PDF adapters
- Scores each adapter, picks highest confidence
- Raises if no valid match or ambiguous tie (< 0.05 confidence gap at top)
- Supports disabling adapters: `AdapterFactory(disabled_source_types=AdapterFactory.CSV_SOURCE_TYPES)`

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

Silver transformers normalize multi-source raw data to a common schema. Implemented in `transformers/silver_transformer.py` (Phase 2 — done).

Pattern used:
1. `SilverTransformer` class with `normalize_transactions(bronze_frames)`, `normalize_holdings(bronze_frames)`, `normalize_account_ledger(bronze_frames)`:
   - Each takes a `dict[source_type, Bronze DataFrame]`
   - A per-`source_type` normalizer function (`_TRANSACTION_NORMALIZERS` dict) maps that source's `raw_data` fields to the common schema
   - Returns a normalized DataFrame ready for `write_silver`
2. Account linking: `transformers/account_config.py::get_account_id(account_identifier, source_type)` — resolves against `data/account_map.json`, a data file, not code (see Gotcha #5)
3. `run_bronze_to_silver(datalake)` is the orchestration entry point — pre-flight checks every Bronze account is mapped (raises `UnmappedAccountsError` listing *all* unmapped accounts at once if not), then normalizes, merges with existing Silver data via `_dedupe_with_existing()` (dedup by `bronze_source_key`, idempotent reruns), and writes all four Silver tables
4. To add a new adapter's transactions to Silver: add a normalizer function to `_TRANSACTION_NORMALIZERS`. To register a new physical account: `uv run python cli.py accounts list-unmapped` then `accounts register` — never hand-edit `account_map.json`

**Known limitation:** Natwest PDF and AmEx statements never capture a year in their transaction dates (e.g. `"15 Jan"`). `_infer_dated_with_year()` guesses the year from the Bronze `upload_timestamp` rather than fixing this at the source — a proper fix belongs in the Phase 1 adapters, not the transformer.

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
| `tests/unit/adapters/` | Unit tests for adapters (CSV + all 6 PDF adapters have coverage) |
| `cli.py` | `ingest` (Bronze ingestion) + `accounts list-unmapped/register/register-fallback` |
| `transformers/silver_transformer.py` | `SilverTransformer` + `run_bronze_to_silver()` — Bronze→Silver normalization (Phase 2) |
| `transformers/account_config.py` | `get_account_id()`/`find_unmapped_accounts()`/`register_account()` — resolves against `data/account_map.json` (user data, not code) |
| `tests/unit/transformers/` | Unit tests for the Silver transformer + account config |
| `tasks/` | (Empty, Phase 3) Celery task definitions go here |
| `ARCHITECTURE.md` | Design philosophy, data flow diagrams, Phase roadmap |

---

## Current Status

**Phase 1 ✅ DONE:** Adapters (CSV + PDF parsing)
- 9 bank sources working: Monzo, Natwest, Vanguard (CSV); Kroo, Natwest, First Direct, AmEx, Vanguard, Monzo (PDF)
- CSV adapters are currently disabled by default (`AdapterFactory(disabled_source_types=AdapterFactory.CSV_SOURCE_TYPES)`) — real exports are PDF-only for this user; CSV code still works and is tested, just not in the default routing path. Note: `cli.py ingest` itself does *not* disable CSV — Monzo has no CSV counterpart otherwise excluded, so the plain `AdapterFactory()` is used there
- ⚠️ AmEx and Vanguard PDF adapters were rewritten after validating against real statements — the original implementations didn't actually work against real exports despite being marked "tested manually". See Gotcha #8 before trusting a "tested manually" claim on a PDF adapter
- Monzo PDF adapter added and validated against a real "Personal Account statement" export (73 txns) — see Gotcha #10 for a false-positive bug this surfaced in the Kroo adapter

**Phase 2 ✅ DONE: Silver Transformations**
- Account linking via hashed statement identifiers resolved against `data/account_map.json` (`transformers/account_config.py`) — distinguishes multiple accounts of the same source_type (e.g. two Amex cards, Natwest current vs credit), not just cross-source dedup
- Schema normalization for all 9 source_types → `transactions`, `holdings`, `account_ledger` (`transformers/silver_transformer.py`)
- Deduplication by `bronze_source_key`, idempotent reruns (`_dedupe_with_existing`)
- New accounts registered via `cli.py accounts register`, not hand-edited into `account_map.json`
- ⚠️ `account_ledger` only covers Natwest CSV + Vanguard CSV — PDF adapters discard balance data during Phase 1 parsing, so they're excluded (see Gotcha below)
- ⚠️ Natwest PDF / AmEx transaction dates have no year in source text; year is inferred from upload time, not fixed at the source
- ⚠️ `tests/unit/transformers/test_silver_transformer.py` resolves `get_account_id()` against the *real* `data/account_map.json` (no path override/fixture) — in a fresh checkout without that gitignored file, 14 of its tests raise `KeyError` rather than fail meaningfully. Pre-existing gap, not yet fixed; `tests/unit/transformers/test_account_config.py`'s `account_map_file` (tmp_path) fixture is the pattern to follow when this gets addressed

**Phase 3 (Next): Celery Orchestration** (configured but not wired up)
- Bronze→Silver transformation job — should just wrap `run_bronze_to_silver()`
- Silver→Gold enrichment job
- Job chaining + error recovery

**Phase 4: Testing**
- Add unit tests for all PDF adapters
- Integration tests for the full Bronze→Silver→Gold pipeline (transformer unit tests exist; no disk-backed integration test yet)
- E2E tests with real files

---

## Testing Notes

- **Unit tests** live in `tests/unit/adapters/` and use sample CSV strings from `conftest.py`
- **PDF adapters** have pytest coverage (`tests/unit/adapters/test_*_pdf_adapter.py`, fabricated fixtures mirroring real structural patterns) and were validated against real downloaded statements during development — see Gotcha #8
- **Silver transformer unit tests** live in `tests/unit/transformers/` — normalize_* methods are tested purely in-memory (no disk I/O); `run_bronze_to_silver()` itself has only been verified manually end-to-end (via a temp `DATA_DIR`), not under pytest
- **CLI (`cli.py`) tests** live in `tests/unit/test_cli.py`, using `click.testing.CliRunner`
- **No integration tests yet** (disk-backed Bronze→Silver→Gold pipeline)
- Dev dependencies require `uv sync --extra dev` (pytest, black, ruff not auto-installed with base `uv sync`)

---

## Configuration & Environment

Read-only at runtime (defined in `config.py`):
- `DATA_DIR` — root data lake directory (default: `./data`)
- `BRONZE_DIR`, `SILVER_DIR`, `GOLD_DIR` — medallion layer paths
- `DUCKDB_PATH` — DuckDB database file path
- `LOG_LEVEL` — logging level (default: `INFO`)
- `REDIS_URL` — Redis broker for Celery (default: `redis://localhost:6379/0`)
- `ACCOUNT_MAP_PATH` — account_identifier → account mapping data file (default: `data/account_map.json`)

Overridable via env vars: `DATA_DIR`, `DUCKDB_PATH`, `LOG_LEVEL`, `REDIS_URL`, `ACCOUNT_MAP_PATH`.

`data/` is gitignored — raw statements, Bronze/Silver/Gold parquet, the DuckDB file, and `account_map.json` are all personal financial data and never committed.

---

## Common Gotchas

1. **PyArrow import placement:** In `models/datalake.py`, `import pyarrow as pa` is at the bottom (line 177) instead of top. This works at runtime but is unconventional; consider moving it to the top.

2. **PDF adapter field normalization:** ✅ Resolved in Phase 2 — `transformers/silver_transformer.py`'s `_TRANSACTION_NORMALIZERS` maps each source_type's `raw_data` shape (Kroo's already-signed `amount`, Natwest CSV's signed string, Monzo's full-vs-search export field names, etc.) to the common `transactions` schema.

3. **Re-upload deduplication:** Based on `source_key` (deterministic hash of date + description + amount). If parsing logic changes and produces different `source_key` for the same transaction, it could be imported twice. Document the generation rule clearly per adapter.

4. **Celery not yet wired:** Celery/Redis dependencies are installed and `CELERY_CONFIG` dict exists, but no Celery app instance or `@app.task` decorators exist yet. Phase 3 will wire this up.

5. **Account linking:** PDF adapters extract a real identifier from the statement (sort code+account number, masked card number, Vanguard account+wrapper), hash it (`adapters/base.py::hash_account_identifier` — SHA256 truncated, never stores the raw number), and resolve it against `data/account_map.json`. That file is a data store, not code — it's user data and deliberately kept out of source control/hardcoding. New accounts go through `uv run python cli.py accounts register`, not hand-editing JSON. `run_bronze_to_silver()` pre-flight-checks every account is mapped and raises `UnmappedAccountsError` listing *all* unmapped accounts at once (not just the first) before writing anything.

6. **`account_ledger` balance coverage is partial:** PDF adapters (Kroo, Natwest PDF, First Direct, AmEx, Vanguard PDF) already discard running-balance data during Phase 1 parsing — only `date`/`description`/`amount` survive. `normalize_account_ledger()` can therefore only populate the ledger from Natwest CSV (`Balance`/`Balance Date`) and Vanguard CSV (`Portfolio Value`). Extending balance history to PDF sources requires going back and modifying those Phase 1 adapters, not just the transformer.

7. **PDF date-year inference:** Natwest PDF and AmEx statement text never includes a year (e.g. `"15 Jan"`). `_infer_dated_with_year()` in `silver_transformer.py` guesses the year from the Bronze `upload_timestamp`, preferring the most recent year that doesn't land after the upload time (handles Dec/Jan boundaries). This is a best-effort workaround, not a fix — the correct fix is extracting the statement period from the PDF itself at the adapter level.

8. **"Tested manually" isn't proof a PDF adapter works:** AmEx and Vanguard's Phase 1 adapters looked reasonable but silently didn't work against real statements. AmEx: PyMuPDF extracts real tables column-by-column (all dates, then all descriptions, then all amounts as one block at the end) — one-line-per-transaction regex can't parse that; rows must be reconstructed by zipping the blocks back together positionally, and boilerplate text (e.g. an interest-rate worked example) can contain decoy numbers that must be filtered by matching the expected transaction count. Vanguard: real statements cover multiple product wrappers (e.g. ISA + Personal Pension) under one account, each with its own holdings table and activity section — a single flat "Activity" assumption merges them. Validate PDF adapter regex against an actual downloaded statement, not just a hand-written fixture, especially for table-heavy layouts.

9. **`PdfAdapter._extract_text()` page boundaries:** pages are joined with `"\n\x0c\n"` (form-feed), not just `"\n"`, so adapters needing per-page structure (e.g. Amex's column reconstruction) can `text.split("\x0c")`. Existing line-based adapters are unaffected — `\x0c` strips to `""` and gets skipped by their blank-line checks.

10. **A loose `validate_text()` can silently swallow another bank's statement:** Kroo's original check was `"Kroo Current Account" in text or ("Kroo" in text and "Sort code" in text)`. A real Monzo PDF statement satisfied the `or` branch purely by coincidence — one transaction's reference read *"Sent from Kroo"* (a transfer from the user's actual Kroo account) and Monzo's own account-details footer happens to also use a `"Sort code:"` label. Since the factory picks whichever adapter validates when there's no competing match, the Monzo statement got routed to Kroo, its transaction regex found nothing, and `factory.ingest()` returned 0 records with no error — a fully wrong routing that looked like a formatting quirk instead. Fixed by dropping the loose branch and requiring the literal `"Kroo Current Account"` header (real statements always have it - see the fixture in `tests/unit/adapters/test_kroo_pdf_adapter.py`). General lesson: a PDF `validate_text()` built on words rather than a structural header string is at risk of matching another source's file whenever a transaction description happens to mention the bank by name.

