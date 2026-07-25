# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Personal finance dashboard using a **medallion data lake architecture**. Ingests bank statements (CSV + PDF) from 11 source_types across 7 financial institutions, normalizes them through Bronze→Silver→Gold layers, and enables SQL analytics via DuckDB.

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
uv run black adapters/ models/ transformers/ tests/ cli.py              # Format code
uv run ruff check adapters/ models/ transformers/ tests/ cli.py --fix   # Lint + auto-fix
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
uv run python cli.py accounts coverage                    # Per-account statement periods ingested + flagged gaps
```

---

## Architecture & Data Flow

### Medallion Layers (Bronze → Silver → Gold)

**Bronze Layer** (`data/bronze/{source_type}/`):
- Raw, immutable Parquet files (one per upload)
- Created by `adapters/` parsing CSV/PDF files
- Stores as-is: Monzo fields, Natwest fields, etc. (no normalization yet)
- Deduplication via deterministic `source_key` (prevents re-import duplicates)
- `cli.py ingest` also archives a copy of the original uploaded file to `data/raw/{source_type}/`, so every statement ever ingested lives in one place instead of scattered wherever it was downloaded from (`_archive_raw_file()` in `cli.py`; never overwrites a differently-named file, suffixes with a short hash on a genuine name collision)

**What Bronze guarantees** (hardened per `BRONZE_SILVER_HARDENING_PLAN.md`'s B1/B2/B3 — see Gotcha #14):
- No double-counting on re-upload (`bronze_source_key` dedup, plus same-file/cross-format dedup — Gotchas #11, #13)
- Full audit trail: original file archived to `data/raw/`, every field the adapter saw preserved untouched in the `raw_data` column
- Friendly, typed errors on an unrecognized/ambiguous statement format: `AdapterFactory.detect_adapter()` raises `UnrecognizedFormatError`/`AmbiguousFormatError` (subclasses of `AdapterDetectionError`, itself a `ValueError` — `adapters/factory.py`), not a bare `ValueError` with no actionable message. `cli.py ingest` distinguishes "format not recognized" from "recognized but failed to parse" and exits nonzero if any file in the batch fails (still processes every file in the batch first — one bad file doesn't stop the rest)
- **When the source statement itself prints a balance anchor** (Previous/New/Closing Balance, or equivalent): a structured, persisted self-check that parsed transactions actually reconcile — `reconciliation_check`/`reconciliation_expected_closing`/`reconciliation_derived_closing`/`reconciliation_matches` columns on the Bronze row (`models/datalake.py::write_bronze`), echoed by `cli.py ingest` (not just a log line anymore). Currently implemented: Amex, First Direct, Natwest Statement, Kroo
- **When the source statement itself prints a coverage period** ("From X to Y", "Period Covered", etc.): `statement_period_from`/`statement_period_to` columns on the Bronze row, queryable per-account via `cli.py accounts coverage` (`transformers/coverage.py::find_statement_periods()`/`find_coverage_gaps()`). Currently implemented: Amex, `natwest-pdf`, `natwest-statement`

**What Bronze does *not* guarantee:**
- That parsed numbers are correct for a source with no balance anchor to self-check against — reconciliation only catches errors for the 4 source_types listed above; see Gotcha #8's history of silent parsing bugs that predate this
- Reconciliation/statement-period columns for every source_type — only present when the adapter actually set them (see "Reaching 'proper Bronze' for a new adapter" below); absent (not `NaN`-filled) for sources with no such concept in the source document at all (`vanguard-pdf`, `monzo-pdf`, `chase`, all CSV adapters, and `natwest-pdf` specifically for reconciliation — that document has no balance data anywhere, Gotcha #6)

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

**PDF Adapters:** Kroo, Natwest, Natwest Statement, First Direct, AmEx, Vanguard, Monzo, Chase (`*_pdf_adapter.py`)
- Inherit from `PdfAdapter` base (handles PyMuPDF text extraction)
- Parse bytes content (PDF files)
- Implement bank-specific regex patterns to extract transactions from extracted text
- Handle multi-line transactions (PDF text extraction breaks table cells into separate lines)
- Natwest has **two** unrelated PDF adapters: `natwest_pdf_adapter.py` (source_type `"natwest-pdf"`) for the online "Transactions" export, and `natwest_statement_pdf_adapter.py` (source_type `"natwest-statement"`) for the quarterly Statement PDF. They share almost no structure (different section markers, different column layout, masked vs full account number) — see Gotcha #10.

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

### Reaching "proper Bronze" for a new PDF adapter

The steps above (`RawRecord`, `generate_source_key` folding in `account_identifier`) are required for every adapter and are enough to *ship*. Two more whole-file facts feed Bronze's reconciliation/coverage guarantees (see above) — both optional, both keyed off what the *source statement itself* prints, not something to fabricate or infer:

**Reconciliation** — if the statement prints a balance anchor (a "Previous Balance"/"Closing Balance"/"New Balance" pair, or similar):
1. Extract the anchor value(s) with a regex against the extracted text — see `_PREVIOUS_BALANCE_RE`/`_NEW_BALANCE_RE` in `first_direct_pdf_adapter.py` or the column-flattened `_ACCOUNT_SUMMARY_RE` in `amex_pdf_adapter.py` for two different real layouts.
2. Roll a `Decimal` running balance forward through parsed transactions from the anchor. Mind the sign convention — the printed balance is typically a liability/asset that moves *opposite* to the signed `amount` field (`balance -= amount`, not `+=`) — see Gotcha #6.
3. Set `self.last_reconciliation = ReconciliationResult(check_name=..., expected_closing=..., derived_closing=..., matches=...)` (dataclass in `adapters/base.py`) — **reset to `None` at the very top** of whichever method computes it, not only inside the "anchor found" branch (see Gotcha #14).
4. If the source has no rolled-forward balance concept at all but a per-row balance is a *direct read* (not derived) and a separate closing anchor is printed, a lighter check comparing the last transaction's own balance to that anchor still counts — see `KrooPdfAdapter._check_reconciliation`.

**Statement period** — if the statement prints its own coverage range ("From X to Y", "Period Covered", etc.):
1. Extract it with a regex — see `_PERIOD_RE` in `amex_pdf_adapter.py`/`natwest_pdf_adapter.py`, or `_PERIOD_COVERED_RE` in `natwest_statement_pdf_adapter.py`.
2. Set `self.last_statement_period = StatementPeriod(from_date, to_date)` (dataclass in `adapters/base.py`) — same reset-at-top discipline as reconciliation.
3. If the period is *also* needed to resolve missing years on individual transaction dates (see Gotcha #7), that's a separate, already-existing use of the same extracted period via `resolve_year_in_period()` — don't conflate the two call sites, they serve different purposes even when they parse the same text.

Neither is required to ship a working adapter — most PDF adapters (Vanguard, Monzo, Chase) have neither, and that's fine: Bronze simply won't have those columns for that source_type, not `NaN`-filled placeholders. But when the source *does* print the anchor/period, capturing it is what makes Bronze's guarantees actually apply to that source — and Bronze's biggest historical class of bug (Gotcha #8) is exactly the kind of thing a reconciliation check catches automatically instead of needing another manual statement-by-statement audit.

Add matching tests per the pattern in e.g. `tests/unit/adapters/test_amex_pdf_adapter.py`'s `TestAmexReconciliation`/`TestAmexStatementPeriod`: a match case, a mismatch/missing-anchor case, and a reset-between-parses regression test (adapter instances are reused across files within one `cli.py ingest` invocation — see Gotcha #14).

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

**PDF date-year handling:** see Gotcha #7 — Natwest PDF and AmEx now stamp a real year onto transaction dates at the adapter level when the statement's period header is found; `_infer_dated_with_year()` here is the fallback for when it isn't (or for Bronze rows ingested before that existed). Natwest Statement has no adapter-level fix yet and always uses this fallback.

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
| `adapters/natwest_statement_pdf_adapter.py` | Natwest quarterly Statement PDF (`"natwest-statement"`) — distinct from `natwest_pdf_adapter.py`'s online export; see Gotcha #10 |
| `adapters/pdf_adapter.py` | Shared PDF base class (PyMuPDF text extraction, `_parse_decimal()` helper, `resolve_year_in_period()` — see Gotcha #7) |
| `models/datalake.py` | `DataLake` singleton for Parquet I/O + DuckDB queries |
| `config.py` | Paths, logging level, Celery/Redis config (read-only at runtime) |
| `logging_config.py` | Structured logging setup (dictConfig-based) |
| `tests/conftest.py` | Shared pytest fixtures (sample CSV strings) |
| `tests/unit/adapters/` | Unit tests for adapters (CSV + all 8 PDF adapters have coverage, plus the shared `PdfAdapter` base class in `test_pdf_adapter.py`) |
| `cli.py` | `ingest` (Bronze ingestion) + `accounts list-unmapped/register/register-fallback` — CLI for the account map |
| `transformers/silver_transformer.py` | `SilverTransformer` + `run_bronze_to_silver()` — Bronze→Silver normalization (Phase 2) |
| `transformers/account_config.py` | `get_account_id()`/`find_unmapped_accounts()`/`register_account()` — resolves against `data/account_map.json` (user data, not code) |
| `transformers/coverage.py` | `find_statement_periods()`/`find_coverage_gaps()` — Bronze statement-period coverage tracking (`cli.py accounts coverage`), see Bronze guarantees above |
| `tests/unit/transformers/` | Unit tests for the Silver transformer + account config |
| `tasks/` | (Empty, Phase 3) Celery task definitions go here |
| `ARCHITECTURE.md` | Design philosophy, data flow diagrams, Phase roadmap |

---

## Current Status

**Phase 1 ✅ DONE:** Adapters (CSV + PDF parsing)
- 11 source_types working: Monzo, Natwest, Vanguard (CSV); Kroo, Natwest, Natwest Statement, First Direct, AmEx, Vanguard, Monzo, Chase (PDF)
- CSV adapters are currently disabled by default (`AdapterFactory(disabled_source_types=AdapterFactory.CSV_SOURCE_TYPES)`) — real exports are PDF-only for this user; CSV code still works and is tested, just not in the default routing path. Note: `cli.py ingest` itself does *not* disable CSV — Monzo has no CSV counterpart otherwise excluded, so the plain `AdapterFactory()` is used there
- ⚠️ AmEx and Vanguard PDF adapters were rewritten after validating against real statements — the original implementations didn't actually work against real exports despite being marked "tested manually". See Gotcha #8 before trusting a "tested manually" claim on a PDF adapter
- Monzo PDF adapter added and validated against a real "Personal Account statement" export (73 txns) — see Gotcha #10 for a false-positive bug this surfaced in the Kroo adapter. Chase PDF adapter added, covering both the current account and Chase Saver statements (distinguished by `account_identifier`, same two-accounts-one-source_type pattern as Amex — see Gotcha #5)
- ✅ Amex's `_select_amount_block`-class bugs (amount mispairing/dropped transactions, wrong credit signs on "OTHER ACCOUNT TRANSACTIONS", an Account Summary regex that broke on Plan-It-active statements, and a missing Plan It balance component) — root-caused and fixed by switching transaction extraction to PyMuPDF's `sort=True` mode. All 7 real Amex statements available now reconcile exactly against their printed Closing Balance (previously 0 of 7 did). Full investigation trail in `AMEX_BUG_HANDOFF.md`.

**Phase 2 ✅ DONE: Silver Transformations**
- Account linking via hashed statement identifiers resolved against `data/account_map.json` (`transformers/account_config.py`) — distinguishes multiple accounts of the same source_type (e.g. two Amex cards, Natwest current vs credit), not just cross-source dedup
- Schema normalization for all 11 source_types → `transactions`, `holdings`, `account_ledger` (`transformers/silver_transformer.py`)
- Deduplication by `bronze_source_key`, idempotent reruns (`_dedupe_with_existing`); Natwest's two PDF formats additionally get cross-format dedup by `(account_id, transaction_date, amount)` since they can cover overlapping periods (`_dedupe_natwest_cross_format`, see Gotcha #11)
- New accounts registered via `cli.py accounts register`, not hand-edited into `account_map.json`
- `account_ledger` now covers Natwest CSV, Vanguard CSV, Kroo, AmEx, First Direct, and Natwest Statement. Still excluded: `natwest-pdf` (the online export has no balance data in the source document at all) and `vanguard-pdf` (its per-line `cash_balance` is a different metric from Portfolio Value — kept in `raw_data` but not wired into the ledger). See Gotcha #6.
- Natwest PDF and AmEx transaction dates get a real year at parse time whenever the statement's own "From X to Y" period header is found (`resolve_year_in_period()`, `_extract_statement_period()` per adapter) — falls back to upload-timestamp inference (`_infer_dated_with_year()` in `silver_transformer.py`) for Bronze rows ingested before this existed, or if the period header isn't present. Natwest Statement still relies on upload-timestamp inference only — see Gotcha #7.

**Bronze Hardening (B1/B2/B3) ✅ DONE:** see `BRONZE_SILVER_HARDENING_PLAN.md` for the full rationale/history — structured per-file reconciliation status (B1), friendly typed detection errors (B2), and statement coverage tracking (B3) are implemented; see "What Bronze guarantees" above and Gotcha #14. The plan's Silver section (S1-S4: same-day ledger ordering, a first-class current-balance/net-worth query, queryable reconciliation status) is still pending — it depends on this Bronze work per the plan's own sequencing.

**Phase 3 (Next): Celery Orchestration** (configured but not wired up)
- Bronze→Silver transformation job — should just wrap `run_bronze_to_silver()`
- Silver→Gold enrichment job
- Job chaining + error recovery

**Phase 4: Testing**
- ✅ Unit tests for all PDF adapters (all 8 have coverage)
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
- **A fresh git worktree has no `data/account_map.json`:** it's gitignored user data, so `tests/unit/transformers/test_silver_transformer.py` (whose fixtures resolve real account IDs, not just source_type fallbacks) will fail with `KeyError: No account mapping for ...` until it's copied in from another checkout that already has one. Safe to copy — it's just hashed-identifier → account_id/display_name/type mappings, no financial figures.

---

## Configuration & Environment

Read-only at runtime (defined in `config.py`):
- `DATA_DIR` — root data lake directory (default: `./data`)
- `RAW_DIR`, `BRONZE_DIR`, `SILVER_DIR`, `GOLD_DIR` — medallion layer paths, plus `RAW_DIR` (`data/raw/{source_type}/`), where `cli.py ingest` archives a copy of every original uploaded file (see Architecture)
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

6. **`account_ledger` balance coverage (mostly resolved):** Kroo, AmEx, First Direct, Natwest Statement (`natwest-statement`), and Monzo PDF (`monzo-pdf`) all now capture a `balance` field in `raw_data` and are wired into `LEDGER_SOURCE_TYPES`/`_LEDGER_NORMALIZERS`. Two PDF sources are still excluded, for different reasons:
   - `natwest-pdf` (the online "Transactions" export): verified against a real download — this document genuinely has **no balance data anywhere**, not even an opening balance. Nothing to capture; this isn't fixable without a different export format from Natwest.
   - `vanguard-pdf`: its per-line "Cash balance" (captured as `raw_data["cash_balance"]`) is uninvested cash sitting in a wrapper (ISA/pension), not the wrapper's total value — a different metric from "Portfolio Value" (what the Vanguard CSV ledger normalizer uses). Deliberately kept separate rather than mixed into `account_ledger.balance`.

   Kroo, Vanguard PDF, and Monzo PDF's balance capture is a direct read (the adapter already parsed it as a `(GBP) Balance` column, just wasn't returning it). AmEx and First Direct don't print a per-transaction balance at all — only a single "Previous Closing Balance"/"Previous Balance" anchor in an Account Summary block — so their `balance` is *derived*: roll a `Decimal` accumulator forward through transactions from that anchor (`balance -= amount`, not `+=` — the anchor is a liability that moves opposite to the signed cash-received `amount` convention used elsewhere). Both log a non-blocking warning if the derived final balance doesn't reconcile with the statement's own printed closing figure — this is what surfaced the AmEx bug fixed in Phase 1 status above (see `AMEX_BUG_HANDOFF.md`).

   Watch for this class of bug recurring: `_ledger_from_amex` assumed AmEx's `date` was always a bare `"DD Mmm"` (no year) and fed it straight to `_infer_dated_with_year()`; once the AmEx adapter started stamping a real year onto `date` itself (Gotcha #7), that assumption broke silently (`_infer_dated_with_year` mis-parses a string that already has a year appended, returning `None` for `as_of_date`) even though the sibling `_normalize_pdf_no_year()` used for the `transactions` table already handled both cases correctly. Fixed by giving `_ledger_from_amex` the same dual-path check. Any future *_LEDGER_NORMALIZERS entry for a source whose date format the adapter itself might change needs to make the same check, not assume the transactions-table normalizer's date-handling automatically covers the ledger one too — they're separate functions.

7. **PDF date-year inference (mostly resolved for Natwest PDF / AmEx):** Natwest PDF, Natwest Statement, and AmEx statement text never includes a year on individual transaction dates (e.g. `"15 Jan"`, `"26 FEB"`). Natwest PDF and AmEx now extract the statement's own "From X to Y" period header at parse time and stamp the real year onto every transaction date (`resolve_year_in_period()` in `pdf_adapter.py`, `_extract_statement_period()` per adapter) — a period-boundary tolerance (±3 days) absorbs a transaction printed just outside the declared range. `_infer_dated_with_year()` in `silver_transformer.py` (guesses the year from the Bronze `upload_timestamp`, preferring the most recent year that doesn't land after the upload time) is kept as a fallback for Bronze rows ingested before this existed, or wherever the period header isn't found in the text. Natwest Statement doesn't have this fix yet and still relies solely on the upload-timestamp fallback for per-transaction date-year inference. (Note: Natwest Statement's `_extract_period_covered()`, added for B3 statement coverage tracking — sets `self.last_statement_period`, see Gotcha #14 — reads the same "Period Covered" text but is *not* wired into per-transaction date-year inference; this Gotcha's gap is still open, don't assume B3's addition closed it.)

8. **"Tested manually" isn't proof a PDF adapter works:** AmEx and Vanguard's Phase 1 adapters looked reasonable but silently didn't work against real statements. AmEx: PyMuPDF extracts real tables column-by-column (all dates, then all descriptions, then all amounts as one block at the end) — one-line-per-transaction regex can't parse that; rows must be reconstructed by zipping the blocks back together positionally, and boilerplate text (e.g. an interest-rate worked example) can contain decoy numbers that must be filtered by matching the expected transaction count. Vanguard: real statements cover multiple product wrappers (e.g. ISA + Personal Pension) under one account, each with its own holdings table and activity section — a single flat "Activity" assumption merges them. Validate PDF adapter regex against an actual downloaded statement, not just a hand-written fixture, especially for table-heavy layouts.

9. **`PdfAdapter._extract_text()` page boundaries:** pages are joined with `"\n\x0c\n"` (form-feed), not just `"\n"`, so adapters needing per-page structure (e.g. Amex's column reconstruction) can `text.split("\x0c")`. Existing line-based adapters are unaffected — `\x0c` strips to `""` and gets skipped by their blank-line checks.

10. **A loose `validate_text()` can silently swallow another bank's statement:** Kroo's original check was `"Kroo Current Account" in text or ("Kroo" in text and "Sort code" in text)`. A real Monzo PDF statement satisfied the `or` branch purely by coincidence — one transaction's reference read *"Sent from Kroo"* (a transfer from the user's actual Kroo account) and Monzo's own account-details footer happens to also use a `"Sort code:"` label. Since the factory picks whichever adapter validates when there's no competing match, the Monzo statement got routed to Kroo, its transaction regex found nothing, and `factory.ingest()` returned 0 records with no error — a fully wrong routing that looked like a formatting quirk instead. Fixed by dropping the loose branch and requiring the literal `"Kroo Current Account"` header (real statements always have it - see the fixture in `tests/unit/adapters/test_kroo_pdf_adapter.py`). General lesson: a PDF `validate_text()` built on words rather than a structural header string is at risk of matching another source's file whenever a transaction description happens to mention the bank by name.

11. **Two unrelated Natwest PDF formats:** `natwest_pdf_adapter.py` (`"natwest-pdf"`) and `natwest_statement_pdf_adapter.py` (`"natwest-statement"`) both parse Natwest PDFs but target structurally different real documents — the online "Transactions" export vs. the quarterly Statement. Confirmed by testing the *old* adapter's `validate_text` against a real quarterly Statement file: it returned `True` (its old check was just `"NatWest" in text and "Transaction" in text`, both of which happen to appear in the Statement's footer/transaction descriptions too), producing an exact-confidence tie with the new adapter that would make `AdapterFactory.detect_adapter()` raise an ambiguous-match error. Fixed by narrowing `natwest_pdf_adapter.py`'s check to `"Your transactions"`, a section heading unique to the online export. If you add a third Natwest document type, verify `validate_text` against real files from *all* existing Natwest adapters, not just the new one — a validator that looks reasonable in isolation can still collide.

    Because the two formats can cover overlapping date ranges, uploading both risks double-counting the same real transaction under two different `bronze_source_key`s (they differ by construction across adapters, so the usual `bronze_source_key`-based dedup can't catch it). `transformers/silver_transformer.py::_dedupe_natwest_cross_format()` handles this separately, matching by `(account_id, transaction_date, amount)` — not description, which differs materially in wording between the two formats for the same real transaction — and preferring the `natwest-statement` row (has balance data) via count-based (multiset) removal, so genuinely repeated same-day/same-amount transactions within one format aren't mistakenly dropped.

12. **Natwest Statement's amount is *derived from* balance, not the other way round:** its transaction table has separate `Paid In(£)`/`Withdrawn(£)`/`Balance(£)` columns, but plain-text PDF extraction loses column position — a lone number after a description could be either Paid In or Withdrawn. Rather than guess, `natwest_statement_pdf_adapter.py` treats the trailing (unambiguous) `Balance(£)` figure as the primary parsed value and derives the signed `amount` as `balance − previous_balance`. This is the opposite of every other adapter (which derive balance from amount, when they derive anything at all) and is correct by construction rather than a heuristic. The date is also only printed once per calendar day in this format — a second same-day transaction omits the date line entirely, so parsing carries the last-seen date forward rather than requiring one per row.

13. **Same-file transaction collisions need disambiguation, not just cross-file dedup:** `generate_source_key()` builds a content-based key (date/description/amount), which correctly dedupes the same real transaction re-appearing across overlapping statements — but two genuinely distinct transactions *within one statement* (e.g. two identical £5 Deliveroo Gold Benefit credits on different days that happen to collide after truncation, or any same-day/same-amount/same-description repeat) collide on that same key too. `PdfAdapter.parse()` now tracks per-file occurrence counts and suffixes the Nth+1 same-key repeat within a single file (`_dup1`, `_dup2`, ...) — the *first* occurrence keeps the plain key, so genuine cross-statement dedup still works, only same-file repeats get disambiguated.

14. **Per-file (not per-record) facts need instance-attribute reset discipline:** `DataSourceAdapter.__init__` (`adapters/base.py`) gives every adapter two attributes, `self.last_reconciliation`/`self.last_statement_period` (dataclasses `ReconciliationResult`/`StatementPeriod`, also in `base.py`) — the channel whole-file facts use to escape `parse()`'s `List[RawRecord]`-only return contract, added for B1/B2/B3 (see "What Bronze guarantees" above). `AdapterFactory.ingest()` reads both off the adapter instance right after `parse()` returns and packages them into the `IngestResult` it returns (`adapters/factory.py`) — no other code touches these attributes directly. Because `cli.py ingest` builds one `AdapterFactory()` and reuses its adapter instances across every file in a batch, any method that sets one of these attributes **must reset it to `None` at the very top of the method**, not only inside the "anchor/period found" branch — otherwise a file with no anchor of its own silently inherits the previous file's result, which would misreport reconciliation/coverage for a file that has neither. Every adapter that implements either (Amex, First Direct, Natwest Statement, Kroo for reconciliation; Amex, `natwest-pdf`, Natwest Statement for period) has an explicit reset-between-parses regression test guarding this — e.g. `TestAmexReconciliation::test_reconciliation_and_period_reset_between_parses` in `tests/unit/adapters/test_amex_pdf_adapter.py`. Add the same style of test for any new adapter that sets either attribute (see "Reaching 'proper Bronze' for a new adapter" above).

