# TODO

## Critical data-correctness hardening (before Gold)

The items below are ordered by dependency and risk. Detailed implementation
planning for items 1–3 is in `CRITICAL_HARDENING_PLAN.md`.

- [x] **1. Make raw and Bronze ingestion immutable.** Archive source files by
  SHA-256 before publication, record an ingestion manifest, and write Bronze
  to content-addressed paths. A same-named but different statement must never
  overwrite prior Bronze data.
- [x] **2. Separate immutable source-record identity from transaction
  deduplication.** Use a file-hash + record-type + line-number Bronze record
  ID; replace semantic `source_key` identity with an explicit, traceable
  cross-file matching policy; rebuild Silver from a selected Bronze set.
- [x] **3. Adopt an exact money data contract.** Replace persisted financial
  floats with signed integer minor units and currency; preserve source text;
  reject malformed monetary fields instead of coercing them to zero.
- [x] **4. Gate Silver promotion on data quality.** Persist parse and
  reconciliation results per ingestion, quarantine failed/inconclusive files
  by default, and require a recorded override before they affect balances.
- [x] **5. Correct snapshot and net-worth semantics.** Use the latest complete
  holdings snapshot per investment account, account for investment cash, and
  expose data freshness alongside canonical current balances.
- [x] **6. Publish Bronze→Silver atomically with a hermetic test suite.** Build
  Silver into a staging run, validate it, publish all tables together, and add
  clean-checkout end-to-end tests for every critical failure mode.

## Existing next up (deferred until critical hardening is complete)

- [ ] **Ingest real statements into the actual data store.** Do this only after
  the immutable-ingestion and exact-money work above; `data/bronze/`,
  `silver/`, `gold/` are currently empty.
- [ ] **Phase 3: Celery orchestration.** Wrap `run_bronze_to_silver()` in a `transform_bronze_to_silver` task; add `enrich_silver_to_gold`; job chaining + retry/error recovery. `CELERY_CONFIG` exists in `config.py` but no `@app.task` yet.

## Phase 4: Testing gaps

- [ ] Disk-backed integration test for the full Bronze→Silver pipeline (`run_bronze_to_silver()` itself is only verified manually via a temp `DATA_DIR`, not under pytest)
- [ ] E2E test(s) with real files

## Known limitations (resolved by critical hardening)

- [x] `account_ledger` now covers Kroo, AmEx, First Direct, Natwest Statement, Monzo PDF, Chase, Monzo Flex (was: Gotcha #6)
- [x] Natwest PDF / AmEx transaction dates have year inferred via `resolve_year_in_period()` (was: Gotcha #7)
- [x] `import pyarrow as pa` moved to the top of `models/datalake.py` (was: Gotcha #1)
- [x] `_parse_money(...) -> 0.0` silent-zero removed (now `parse_money_minor` with `MoneyParseError`)
- [x] `coverage.py`/`reconciliation_status.py` dedup keys switched from `filename` to `ingestion_id`
- [x] `models/schema.py` stub removed

## Phase 5+ (not started)

- [ ] CLI beyond account mapping: manual ingestion command, pipeline inspection/status
- [ ] Gold layer enrichment: subscription detection, transfer detection, account snapshots
- [ ] REST API + dashboard (V1 scope, per ARCHITECTURE.md)
