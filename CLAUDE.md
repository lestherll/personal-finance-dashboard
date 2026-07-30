# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Personal finance dashboard using a **medallion data lake architecture**. Ingests bank statements (CSV + PDF) from 10 source_types across 7 financial institutions, normalizes them through Bronze→Silver→Gold layers, and enables SQL analytics via DuckDB.

- **Storage:** File-based Parquet files (not a database)
- **Query Engine:** DuckDB (in-process, no server)
- **Orchestration:** Celery + Redis (configured but not yet wired up)
- **Python Version:** 3.13+ (use `uv` to manage)
- **Monetary values:** All financial data is stored as **signed integer minor units** (e.g. GBP pence) with explicit currency — never `float`. See `models/money.py`.

See `ARCHITECTURE.md` for the full design philosophy and data flow.

---

## Development Commands

All commands use `uv` (not pip or conda). Dev dependencies require `uv sync --extra dev`.

### Environment & Setup
```bash
uv python list                    # Check available Python versions (3.13 recommended)
uv sync --extra dev               # Install all deps + dev tools (pytest, ruff)
uv run python -m pytest tests/    # Run all tests with Python auto-selected from pyproject.toml
```

### Running Tests
```bash
uv run pytest tests/unit/adapters/ -v              # Test CSV/PDF adapter parsing
uv run pytest tests/unit/adapters/test_monzo_adapter.py -v  # Single adapter test
uv run pytest tests/ -k "test_kroo" -v             # Run tests matching pattern
uv run pytest --collect-only                       # List all tests without running
uv run pytest --cov=adapters tests/                # Coverage report (adapters module only)
uv run pytest --cov --cov-report=term-missing      # Full coverage checker (adapters/models/transformers/cli), fails if under 85% (tool.coverage.report.fail_under in pyproject.toml)
```

### Code Quality
```bash
uv run ruff format adapters/ models/ transformers/ tests/ cli.py              # Format code
uv run ruff check adapters/ models/ transformers/ tests/ cli.py --fix   # Lint + auto-fix
uv run mypy                                                           # Type-check adapters/, models/, transformers/, cli.py (config in [tool.mypy] of pyproject.toml)
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

### CLI (ingestion + account mapping + Silver management)
```bash
uv run python cli.py ingest <file> [<file> ...]           # Parse statement file(s), write to Bronze
uv run python cli.py accounts list-unmapped               # Bronze accounts with no mapping yet
uv run python cli.py accounts register <hash> <account_id> <display_name> <current|credit|investment|savings>
uv run python cli.py accounts register-fallback <source_type> <account_id> <display_name> <account_type>
uv run python cli.py accounts coverage                    # Per-account statement periods ingested + flagged gaps
uv run python cli.py accounts reconciliation              # Per-account balance-reconciliation status (B1 self-checks)
uv run python cli.py accounts breakdown                   # Net-worth breakdown by account/holding
uv run python cli.py silver rebuild                       # Full rebuild of Silver from current Bronze
uv run python cli.py silver builds                        # List published Silver builds
uv run python cli.py ingestions list                      # List all ingestion manifests
uv run python cli.py ingestions show <ingestion_id>       # Show a full ingestion manifest
uv run python cli.py ingestions quarantined               # List quarantined ingestions (mismatch, no override)
uv run python cli.py ingestions override <id> --allow --reason "..."  # Override quarantine for an ingestion
```

---

## Architecture & Data Flow

### Medallion Layers (Bronze → Silver → Gold)

**Bronze Layer** (`data/bronze/{source_type}/{ingestion_id}.parquet`):
- Raw, immutable Parquet files — one per ingestion, content-addressed by SHA-256 of the original uploaded bytes
- Created by `adapters/` parsing CSV/PDF files
- Original source files archived to `data/raw/sha256/{xx}/{sha256}.pdf` (content-addressed, immutable — never overwritten)
- Every Bronze row carries `ingestion_id` (file hash), `bronze_record_id` (immutable hash of `ingestion_id:record_type:source_ordinal`), `source_type`, `parser_version`, and the adapter's `raw_data` dict preserved untouched
- Idempotent re-ingest: same file → same ingestion_id → manifest already complete → skip (never overwrites)
- Ingestion manifest (`data/ingestions/{sha256}.json`) tracks every file's status through the lifecycle: `archived` → parsing → `complete` / `parse_failed` / `bronze_failed`
- All monetary values in `raw_data` are integer minor units (`amount_minor`, `balance_minor`), not float — see Gotcha #18

**What Bronze guarantees:**
- No double-counting on re-upload (content-addressed, idempotent; two distinct files with the same original filename → two separate ingestions)
- Full audit trail: original file archived to `data/raw/`, every field the adapter saw preserved untouched in `raw_data`, ingestion manifest records lifecycle status
- Friendly, typed errors on unrecognized/ambiguous formats: `UnrecognizedFormatError`/`AmbiguousFormatError` (subclasses of `AdapterDetectionError`, itself a `ValueError` — `adapters/factory.py`). `cli.py ingest` distinguishes "format not recognized" from "recognized but failed to parse" and exits nonzero if any file in the batch fails (still processes every file in the batch first)
- **Reconciliation self-check:** for sources whose statement prints a balance anchor (Previous/New/Closing Balance), a structured per-file verification that parsed transactions reconcile — persisted as `reconciliation_check`/`reconciliation_expected_closing_minor`/`reconciliation_derived_closing_minor`/`reconciliation_matches` columns on the Bronze row, echoed by `cli.py ingest`, and queryable via `cli.py accounts reconciliation`. Implemented for 8 of 10 source_types (all PDF sources except `natwest-transactions`, which has no balance data anywhere — see Gotcha #6)
- **Statement period:** `statement_period_from`/`statement_period_to` columns, queryable via `cli.py accounts coverage`. Implemented for all 9 PDF source_types. First Direct only prints a single Statement Date, so its `from_date` is derived (one calendar month earlier)
- **Quality gate:** reconciliation-mismatched ingestions (`matches=False`) are quarantined from Silver promotion unless overridden via `cli.py ingestions override`. Inconclusive (`matches=None`, e.g. natwest-transactions with no anchor) is not quarantined

**What Bronze does *not* guarantee:**
- That parsed numbers are correct for a source with no balance anchor to self-check against — `natwest-transactions` structurally has no balance data; see Gotcha #8 for history
- The Monzo CSV adapter has no balance anchor or statement period concept at all

**Silver Layer** (`data/silver/builds/` + `data/silver/current` symlink):
- **Versioned builds with atomic publication:** each rebuild creates a new build directory under `data/silver/builds/<build_id>/` with all Parquet tables + `build.json` manifest. The `data/silver/current` symlink is swapped atomically (temp symlink + `os.replace`) only after all tables are written and validated. Old builds pruned (keep last 2).
- Normalized records: `transactions`, `accounts`, `holdings`, `account_ledger`, `transaction_sources`, `plan_it_instalments`
- **Exact-money schema:** all monetary columns are integer minor units (`amount_minor`, `balance_minor`, `total_value_minor`, `unit_price_minor` int64) + `currency` column
- **Two-tier matching engine** (`transformers/matching.py`): same-source dedup by full content fingerprint (account, date, amount_minor, normalized description, occurrence); declared cross-source pairs (Natwest Transactions+Statement) match on loose key (account, date, amount_minor) with multiset counting, preferring the statement. `silver_transaction_id` is minted as `source_type:fingerprint_occurrence` (source-scoped so two sources with identical content can't collide). There is deliberately **no** bank-transaction-id tier — the only source with a genuine bank id is the effectively-unused Monzo CSV export; see the matching.py docstring for why a synthesised id wouldn't earn top rank
- **Transaction provenance:** `transaction_sources` table maps each canonical `silver_transaction_id` to every ingested `bronze_record_id` it subsumed, with match policy attribution
- Unmapped-account pre-flight check raises `UnmappedAccountsError` listing all unmapped accounts at once before writing anything
- Created by `transformers/silver_transformer.py::run_bronze_to_silver()`, exposed via `cli.py silver rebuild`

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
- `PARSER_VERSION` class attr (default `"1"`, bumped to `"2"` by all adapters after exact-money migration) — tracked per ingestion in the manifest and build manifest

`RawRecord` carries `bronze_record_id` (immutable identity), `source_ordinal`, `account_identifier` (hashed), and `record_type` ("transaction" | "holding" | "plan_it_instalment").

**Adapter contract for amounts:** `raw_data` dict must carry `amount_minor` (int) — never `amount` (float). Reconciliation accumulators use integer minor arithmetic. `ReconciliationResult` uses `expected_closing_minor`/`derived_closing_minor` (int). See `models/money.py::parse_money_minor` / `try_parse_money_minor`.

**CSV Adapters:** Monzo (`*_adapter.py`)
- Parse string content (CSV text)
- Handle format variations (e.g., Monzo has both "full export" and "search export" formats)
- Emits `bank_transaction_id` from the Monzo native "id"/"Transaction ID" field

**PDF Adapters:** Kroo, Natwest, Natwest Statement, First Direct, AmEx, Vanguard, Monzo, Chase, Monzo Flex (`*_pdf_adapter.py`)
- Inherit from `PdfAdapter` base (handles PyMuPDF text extraction)
- Parse bytes content (PDF files)
- Implement bank-specific regex patterns to extract transactions from extracted text
- Handle multi-line transactions (PDF text extraction breaks table cells into separate lines)
- Natwest has **two** unrelated PDF adapters (see Gotcha #10)

**Factory:** `factory.py` auto-detects + routes:
- Branches on content type: `str` → try CSV adapters, `bytes` → try PDF adapters
- Scores each adapter, picks highest confidence
- Raises if no valid match or ambiguous tie (< 0.05 confidence gap at top)
- Supports disabling adapters: `AdapterFactory(disabled_source_types=AdapterFactory.CSV_SOURCE_TYPES)`
- `IngestResult` carries records + reconciliation verdict (singular or plural for Vanguard per-wrapper) + statement period + source_type/adapter/parser_version

### Data Lake I/O

Located in `models/datalake.py`:

```python
from models.datalake import get_datalake

datalake = get_datalake()

# Write raw records to Bronze (content-addressed, immutable)
datalake.write_bronze(ingestion_manifest, df, reconciliation=..., statement_period=...)

# Read all Bronze records for a source
df = datalake.read_bronze("monzo")

# Read Silver through the current/ build symlink. Raises StaleSilverError
# if the build layout is broken (dangling/missing current) - never silently
# falls back to stale data. Flat data/silver/*.parquet is legacy-only
# (no builds/ at all) and warns.
df = datalake.read_silver("transactions")

# Query across Parquet files with DuckDB SQL
df = datalake.query("SELECT * FROM transactions LIMIT 10")  # named views over the current build + bronze_<source_type>
```

Singleton pattern: `get_datalake()` returns cached connection; safe to call multiple times.

`models/ingestion.py`: `IngestionManifest`, `start_ingestion()`, `load_manifest()`, `write_manifest()`, `archive_raw_artifact()` — content-addressed immutable ingestion lifecycle.

`models/money.py`: `parse_money_minor()` / `try_parse_money_minor()` — text→int minor units; `minor_to_decimal()`, `format_minor()` — display. `MoneyParseError` raised on unparseable input (never silently returns 0).

`models/build.py`: `publish_silver_build()` — versioned builds with atomic symlink swap; `list_builds()`, `current_build_id()`.

---

## Extending the System

### Adding a New Bank Adapter

1. **Create the adapter file** (`adapters/newbank_adapter.py`):
   ```python
   from adapters.base import DataSourceAdapter, RawRecord
   from models.money import parse_money_minor

   class NewBankAdapter(DataSourceAdapter):
       PARSER_VERSION = "2"

       def validate(self, file_content: str) -> tuple[bool, float]:
           if "NewBank" in file_content and "Transaction" in file_content:
               return True, 0.95
           return False, 0.0

       def parse(self, file_content: str, filename: str, file_hash: str) -> List[RawRecord]:
           records = []
           for idx, row in enumerate(csv.DictReader(file_content.splitlines()), start=1):
               records.append(RawRecord(
                   source_key=self.generate_source_key(...),
                   source_type="newbank",
                   raw_data={
                       "date": row["Date"],
                       "description": row["Description"],
                       "amount_minor": parse_money_minor(row["Amount"]),
                       "amount_text": row["Amount"],
                   },
                   filename=filename,
                   file_hash=file_hash,
                   upload_timestamp=datetime.now(),
                   line_number=idx,
                   bronze_record_id=make_bronze_record_id(file_hash, "transaction", idx),
                   source_ordinal=idx,
               ))
           return records

       def generate_source_key(self, txn: dict, line_num: int, account_identifier=None) -> str:
           return f"newbank_txn_{txn['date']}_{txn['amount_minor']}"

       def detect_source_type(self) -> str:
           return "newbank"
   ```
   Key: emit `amount_minor` (int minor units) in `raw_data`, not `amount` (float). Construct `bronze_record_id` via `make_bronze_record_id`. Bump `PARSER_VERSION` on semantic changes.

2. **Register in factory** (`adapters/factory.py`):
   - Add import: `from adapters.newbank_adapter import NewBankAdapter`
   - Add to `self.csv_adapters` list (or `self.pdf_adapters` if PDF)

3. **Add a Silver normalizer** in `transformers/silver_transformer.py` — add to `_TRANSACTION_NORMALIZERS` dict. Emit `amount_minor`, `currency`, `bank_transaction_id`.

4. **Add tests** (`tests/unit/adapters/test_newbank_adapter.py`):
   - Test validation, parsing, source key generation
   - If the adapter has a balance anchor, add reconciliation tests (match, mismatch, reset-between-parses per Gotcha #14)

### Reaching "proper Bronze" for a new PDF adapter

See the full guide at Gotcha #14 and in the relevant adapter files. Key contract points:
- Reconciliation: if the statement prints a balance anchor, compute a `ReconciliationResult(check_name, expected_closing_minor, derived_closing_minor, matches)`, set it on `self.last_reconciliation` (or `self.last_reconciliations` for multi-account files like Vanguard), and **reset to None/[] at the top** of the method
- Statement period: extract from text, set `self.last_statement_period`, same reset discipline
- Multi-account files: use `self.last_reconciliations: List[ReconciliationResult]` with per-result `account_identifier` — see `VanguardPdfAdapter._check_reconciliation`
- All monetary values in `ReconciliationResult` are integer minor units

### Writing a Silver Transformer

Pattern (already implemented, extend when adding adapters):
- `SilverTransformer` class with `normalize_transactions()`, `normalize_holdings()`, `normalize_account_ledger()`, `normalize_plan_it_instalments()`
- `run_bronze_to_silver()` orchestrates: pre-flight unmapped-account check → read Bronze → normalize → match (two-tier dedup) → publish via versioned build
- To add a new adapter's transactions: add a normalizer function to `_TRANSACTION_NORMALIZERS` that reads `amount_minor`/`balance_minor` from `raw_data`
- To register a new physical account: `uv run python cli.py accounts list-unmapped` then `accounts register` — never hand-edit `account_map.json`

---

## Key Files & Patterns

| File | Purpose |
|------|---------|
| `adapters/base.py` | `DataSourceAdapter` ABC, `RawRecord`, `ReconciliationResult`, `StatementPeriod` dataclasses, `make_bronze_record_id()`, `hash_account_identifier()` |
| `adapters/factory.py` | `AdapterFactory.detect_adapter()` + `ingest()` — main entry point; `IngestResult` carries records + reconciliation + period |
| `adapters/*_adapter.py` | Concrete adapters for each bank/format (10 total) |
| `adapters/pdf_adapter.py` | Shared PDF base class (PyMuPDF text extraction, `resolve_year_in_period()`) |
| `adapters/natwest_transactions_pdf_adapter.py` | Natwest on-demand online "Transactions" export — covers the gap since the last quarterly Statement; see Gotcha #10 |
| `adapters/natwest_statement_pdf_adapter.py` | Natwest quarterly Statement PDF — distinct from the Transactions export; see Gotcha #10 |
| `adapters/vanguard_pdf_adapter.py` | Vanguard PDF — multi-wrapper (ISA+Pension), per-wrapper reconciliation via `last_reconciliations` |
| `adapters/monzo_flex_pdf_adapter.py` | Monzo Flex (BNPL/credit) — newest-first, single-account fallback registration; see Gotcha #16 |
| `models/datalake.py` | `DataLake` singleton for Parquet I/O + DuckDB queries; `write_bronze` content-addressed + atomic |
| `models/ingestion.py` | `IngestionManifest` dataclass + lifecycle (start_ingestion, load/write_manifest, archive_raw_artifact) |
| `models/money.py` | Exact-money utilities: `parse_money_minor`, `format_minor`, `MoneyParseError` |
| `models/build.py` | Versioned Silver builds with atomic symlink swap: `publish_silver_build`, `list_builds`, `current_build_id` |
| `config.py` | Paths, logging level, Celery/Redis config (read-only at runtime) |
| `logging_config.py` | Structured logging setup (dictConfig-based) |
| `cli.py` | `ingest`, `accounts *`, `silver rebuild/builds`, `ingestions *` — full CLI surface |
| `transformers/silver_transformer.py` | `SilverTransformer` + `run_bronze_to_silver()` — Bronze→Silver normalization with quality-gate quarantining |
| `transformers/matching.py` | Two-tier transaction dedup (fingerprint + cross-source policies) + `transaction_sources` provenance |
| `transformers/account_config.py` | Account mapping via `data/account_map.json` (user data, not code) |
| `transformers/coverage.py` | `find_statement_periods()`/`find_coverage_gaps()` — per-account statement period coverage |
| `transformers/balance.py` | `get_current_balances()`/`get_net_worth()`/`get_net_worth_breakdown()` — exact int minor arithmetic |
| `transformers/reconciliation_status.py` | `find_reconciliation_status()` — queryable per-file reconciliation status |
| `tests/conftest.py` | Shared pytest fixtures |
| `tests/unit/adapters/` | Unit tests for all 10 adapters + base adapter |
| `tests/unit/transformers/` | Unit tests for Silver transformer, balance, coverage, reconciliation, matching |
| `tests/unit/models/` | Unit tests for ingestion, datalake |
| `tests/integration/natwest_overlap/` | Disk-backed full-pipeline integration test for Natwest cross-format dedup |
| `tests/e2e/` | Hermetic clean-checkout end-to-end tests (ingest→Bronze→Silver→net_worth, idempotency, money round-trip) |
| `tasks/` | (Empty, Phase 3) Celery task definitions go here |
| `ARCHITECTURE.md` | Design philosophy, data flow diagrams, Phase roadmap |

---

## Current Status

**Critical Hardening (Milestones 1–3 + Items 4–6) ✅ DONE** — see `CRITICAL_HARDENING_PLAN.md` and `CRITICAL_HARDENING_HANDOFF.md`:
- M1: Immutable content-addressed Bronze ingestion with per-file manifest lifecycle
- M2: Stable source-record identity (`bronze_record_id`), two-tier Silver matching interface with provenance, versioned atomic Silver builds
- M3: Exact-money schema (all monetary values are integer minor units, all adapters bumped to `PARSER_VERSION="2"`)
- Item 4: Quality gate — reconciliation verdict in manifest, quarantine + override CLI
- Item 5: Holdings snapshot semantics — per-account latest complete snapshot used for net worth
- Item 6: Atomic Silver publication, hermetic E2E tests, cleanup (dedup key fixes, pyarrow import, schema.py removed)
- Real statement data ingested and verified: 28 files, 697 transactions, £10,084.76 net worth, all reconciling files pass

**Phase 1 ✅ DONE:** Adapters (CSV + PDF parsing) — 10 source_types

**Phase 2 ✅ DONE:** Silver Transformations — full normalization, matching, provenance, versioned builds

**Bronze Hardening (B1/B2/B3) ✅ DONE, extended (B4):** reconciliation, detection errors, statement coverage

**Silver Hardening (S1-S3) ✅ DONE:** same-day ordering, current balance/net worth, queryable reconciliation. S4 (merchant normalization) deferred.

**Phase 3 (Next): Celery Orchestration** (configured but not wired up)

**Phase 4: Testing** — ✅ Unit + integration + E2E tests exist

---

## Testing Notes

- **Unit tests** cover adapters, models, transformers, CLI
- **Integration tests** in `tests/integration/natwest_overlap/` exercise the disk-backed pipeline (real Bronze→Silver, cross-format dedup)
- **E2E tests** in `tests/e2e/` run on clean `tmp_path` checkouts: ingest→Silver→net_worth, idempotent re-ingest, same-name different bytes, money round-trip
- **567 tests pass, 88% coverage** (threshold 85%)
- Dev dependencies require `uv sync --extra dev` (pytest, black, ruff not auto-installed with base `uv sync`)
- **A fresh git worktree has no `data/account_map.json`:** it's gitignored user data. Copy from another checkout that has one — safe, it's just hashed-identifier → account_id/display_name/type mappings, no financial figures.

---

## Configuration & Environment

Read-only at runtime (defined in `config.py`):
- `DATA_DIR` — root data lake directory (default: `./data`)
- `RAW_DIR`, `BRONZE_DIR`, `SILVER_DIR`, `GOLD_DIR` — medallion layer paths
- `INGESTIONS_DIR` — per-ingestion manifest JSON files (default: `data/ingestions/`)
- `DUCKDB_PATH` — DuckDB database file path
- `LOG_LEVEL` — logging level (default: `INFO`)
- `REDIS_URL` — Redis broker for Celery (default: `redis://localhost:6379/0`)
- `ACCOUNT_MAP_PATH` — account_identifier → account mapping data file (default: `data/account_map.json`)

Overridable via env vars: `DATA_DIR`, `DUCKDB_PATH`, `LOG_LEVEL`, `REDIS_URL`, `ACCOUNT_MAP_PATH`.

`data/` is gitignored — raw statements, Bronze/Silver/Gold parquet, ingestion manifests, the DuckDB file, and `account_map.json` are all personal financial data and never committed.

---

## Common Gotchas

1. ~~**PyArrow import placement**~~ ✅ Fixed — `import pyarrow as pa` is now at the top of `models/datalake.py`.

2. **PDF adapter field normalization:** ✅ Resolved — `_TRANSACTION_NORMALIZERS` / `_LEDGER_NORMALIZERS` normalize per-adapter shapes to a common schemas with `amount_minor`/`balance_minor`.

3. **Re-upload deduplication:** Content-addressed by SHA-256. Same file → same `ingestion_id` → manifest already complete → skip. Different files with the same original filename → two separate ingestion_ids, no collision. Cross-file transaction matching is handled by the two-tier matching engine (`transformers/matching.py`).

4. **Celery not yet wired:** Celery/Redis dependencies are installed and `CELERY_CONFIG` dict exists, but no Celery app instance or `@app.task` decorators exist yet. Phase 3 will wire this up.

5. **Account linking:** PDF adapters extract a real identifier from the statement (sort code+account number, masked card number, Vanguard account+wrapper), hash it (`hash_account_identifier` — SHA256 truncated, never stores the raw number), and resolve it against `data/account_map.json`. That file is a data store, not code — it's user data and deliberately kept out of source control. New accounts go through `uv run python cli.py accounts register`, not hand-editing JSON. `run_bronze_to_silver()` pre-flight-checks every account is mapped and raises `UnmappedAccountsError` listing *all* unmapped accounts at once.

6. **`account_ledger` balance coverage:** Kroo, AmEx, First Direct, Natwest Statement, Monzo PDF, Chase, and Monzo Flex all capture `balance_minor` (int minor) and are wired into `LEDGER_SOURCE_TYPES`/`_LEDGER_NORMALIZERS`. Two PDF sources are excluded, for structural reasons:
   - `natwest-transactions` (online "Transactions" export): the document has **no balance data anywhere** — not even an opening balance. Nothing to capture; this isn't fixable without a different export format.
   - `vanguard-pdf`: per-line `cash_balance` is uninvested cash in a wrapper, not the wrapper's total value. Deliberately kept separate. Vanguard's reconciliation is per-wrapper (ISA + Pension), checking the "Your Vanguard account summary" table's "Value on <end date>" against each wrapper's holdings total (fund + cash).

   Kroo, Vanguard, Monzo PDF, and Chase's balance capture is a direct read of a printed column. AmEx and First Direct don't print per-transaction balances — only a printed anchor in an Account Summary block — so their `balance_minor` is derived by rolling a minoe-unit accumulator through transactions from the anchor. Both log a non-blocking warning if the derived balance doesn't reconcile.

   Watch for this class of bug: `_ledger_from_amex` / `_ledger_from_natwest_statement` use a dual-path date check (4-digit year already present → parse directly; otherwise infer) because the adapter may have already stamped a real year via `resolve_year_in_period()`.

7. **PDF date-year inference:** ✅ Resolved — Natwest Transactions, Natwest Statement, and AmEx now stamp a real year onto transaction dates at the adapter level via `resolve_year_in_period()` whenever the statement's period header is found. `_infer_dated_with_year()` is kept as a fallback for Bronze rows ingested before the fix.

   **Rebuilding Silver is required after a date/description/amount fix:** changing what an adapter produces changes `generate_source_key()`'s output for already-ingested transactions. Since Silver is now a full rebuild (not a merge), just `cli.py silver rebuild` — Bronze doesn't need touching.

8. **"Tested manually" isn't proof a PDF adapter works:** AmEx and Vanguard's Phase 1 adapters looked reasonable but silently didn't work against real statements. Validate PDF adapter regex against an actual downloaded statement, not just a hand-written fixture, especially for table-heavy layouts.

9. **`PdfAdapter._extract_text()` page boundaries:** pages are joined with `"\n\x0c\n"` (form-feed), not just `"\n"`. Existing line-based adapters are unaffected — `\x0c` strips to `""` and gets skipped by blank-line checks.

10. **A loose `validate_text()` can silently swallow another bank's statement:** Kroo's original check matched a Monzo statement by coincidence (a "Sent from Kroo" transaction reference + Monzo's own "Sort code:" footer). Fixed by requiring the literal `"Kroo Current Account"` header. General lesson: validate on a structural header string, not individual words.

11. **Two unrelated Natwest PDF formats:** `natwest_transactions_pdf_adapter.py` and `natwest_statement_pdf_adapter.py` both parse Natwest PDFs but target structurally different documents. The Transactions export covers the gap since the last quarterly Statement, which only comes every ~3 months. They share almost no structure.

    Because the two formats can cover overlapping date ranges, Silver's matching engine has a declared cross-source policy: match on `(account_id, transaction_date, amount_minor)` with multiset counting, preferring the statement row. Verified end-to-end against real overlapping files.

12. **Natwest Statement's amount is *derived from* balance:** its transaction table loses column position in plain-text extraction — a lone number after a description could be Paid In or Withdrawn. Rather than guess, the adapter treats the trailing `Balance(£)` as the primary values and derives amount as `balance_minor − previous_balance_minor`. Dates are only printed once per calendar day.

13. **Same-file transaction collisions need disambiguation:** `PdfAdapter.parse()` suffixes the Nth+1 same-key repeat within a single file (`_dup1`, `_dup2`, …). The first occurrence keeps the plain key for cross-file dedup; only same-file repeats get disambiguated.

14. **Per-file facts need instance-attribute reset discipline:** `last_reconciliation` / `last_reconciliations` / `last_statement_period` must be reset to `None`/`[]`/`None` at the very top of the method that sets them. Adapter instances are reused across files within one `cli.py ingest` invocation, so a file with no anchor would otherwise silently inherit the previous file's result. Every adapter that implements any of these has a reset-between-parses regression test.

15. **A PDF label's layout doesn't necessarily match its neighbor's:** First Direct's `Previous Balance` is label-then-newline-then-value, but `Statement Date` in the same Account Summary block is inline. Validate each regex against real data individually.

16. **A newest-first PDF adapter needs registering in `_REVERSE_CHRONOLOGICAL_SOURCE_TYPES`:** Monzo PDF and Monzo Flex print transactions newest-first. They must be in `transformers/balance.py::_REVERSE_CHRONOLOGICAL_SOURCE_TYPES` or same-day balance queries will silently pick the wrong row.

17. **Reconciliation mismatches are gated at both build time and query time:** Silver's `account_ledger` carries a `reconciled` flag per row. `run_bronze_to_silver` quarantines ingestions with `reconciliation_matches=False` unless overridden (build-time gate). `get_current_balances()` additionally filters `reconciled != False` at query time (defense-in-depth). A mismatched file's balance is never used for net worth.

18. **All monetary values are integer minor units:** Everything in `raw_data`, Bronze columns, Silver schema, and `ReconciliationResult` uses signed integer minor units (e.g. GBP pence). `models/money.py::parse_money_minor()` raises `MoneyParseError` on unparseable input — never silently returns zero. `format_minor()` converts at the CLI/SQL display boundary only. There is no `float` in any financial path. See `CRITICAL_HARDENING_PLAN.md` Milestone 3.
