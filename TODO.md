# TODO

## Next up

- [ ] **Ingest real statements into the actual data store.** `data/bronze/`, `silver/`, `gold/` are all empty — every ingestion so far has run against temp directories for testing. The pipeline is proven end-to-end but hasn't touched real data yet.
- [ ] **Phase 3: Celery orchestration.** Wrap `run_bronze_to_silver()` in a `transform_bronze_to_silver` task; add `enrich_silver_to_gold`; job chaining + retry/error recovery. `CELERY_CONFIG` exists in `config.py` but no `@app.task` yet.

## Phase 4: Testing gaps

- [ ] Disk-backed integration test for the full Bronze→Silver pipeline (`run_bronze_to_silver()` itself is only verified manually via a temp `DATA_DIR`, not under pytest)
- [ ] E2E test(s) with real files

## Known limitations (see CLAUDE.md "Common Gotchas" for detail)

- [ ] `account_ledger` only covers Natwest CSV + Vanguard CSV — PDF adapters discard running balance during parsing (Gotcha #6)
- [ ] Natwest PDF / AmEx transaction dates have no year in source text; year is inferred, not extracted (Gotcha #7)
- [ ] `import pyarrow as pa` at the bottom of `models/datalake.py` instead of the top (Gotcha #1)

## Phase 5+ (not started)

- [ ] CLI beyond account mapping: manual ingestion command, pipeline inspection/status
- [ ] Gold layer enrichment: subscription detection, transfer detection, account snapshots
- [ ] REST API + dashboard (V1 scope, per ARCHITECTURE.md)
