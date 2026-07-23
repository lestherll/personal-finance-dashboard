# Implementation Plans: Personal Finance Dashboard

## Overview

This directory contains detailed implementation plans for building the **core engine** of the personal finance dashboard. The plans are organized by component and focus on the data pipeline—no APIs, no UI, just the heart of the system.

**Out of scope for V0 (core engine):**
- ❌ REST APIs
- ❌ React dashboard
- ❌ Claude integration
- ❌ Deployment/infrastructure

**In scope for V0 (core engine):**
- ✅ Adapter pattern (Monzo, Natwest, Vanguard)
- ✅ Medallion data lake (Bronze, Silver, Gold)
- ✅ Data transformations
- ✅ Async job orchestration (Celery)
- ✅ Quality tracking
- ✅ End-to-end testing

---

## Plans

### 1. **[Core Engine](01-core-engine.md)** (5-6 days)
The master plan: high-level roadmap broken into 6 phases.

**Phases:**
1. Adapter pattern + core infra (1.5 days)
2. Database schema + models (1 day)
3. Data transformations (1.5 days)
4. Job orchestration (1 day)
5. CLI tool for testing (0.5 days)
6. Integration tests (0.5 days)

**Deliverables:**
- Adapters for Monzo, Natwest, Vanguard
- PostgreSQL schema (Bronze, Silver, Gold)
- Transformation logic (account linking, normalization, enrichment)
- Celery jobs (idempotent, with error recovery)
- CLI for manual testing

**Success:** All unit + integration tests pass, pipeline runs end-to-end

---

### 2. **[Database Schema](02-database-schema.md)** (1 day)
Detailed SQL migrations and SQLAlchemy models.

**Deliverables:**
- 7 migration files (Bronze, Silver, Gold)
- All tables with constraints, indexes, foreign keys
- SQLAlchemy ORM models
- Alembic setup for version control

**Key decisions:**
- JSONB for raw data (schema flexibility)
- Immutable Bronze layer (append-only)
- UNIQUE constraints for deduplication
- Foreign key constraints (referential integrity)
- Indexes on all common query paths

**Success:** Migrations run cleanly, schema matches architecture doc

---

### 3. **[Data Transformations](03-data-transformations.md)** (1.5 days)
Transformation logic: account linking, normalization, detection algorithms.

**Modules:**
- `account_linker.py` — Map source-specific account IDs to silver.accounts
- `silver_transformer.py` — Normalize Monzo/Natwest/Vanguard to common schema
- `account_ledger.py` — Extract balance history
- `subscription_detector.py` — Pattern matching for recurring transactions
- `transfer_detector.py` — Find transfers between owned accounts

**Key algorithms:**
- Account linking: source-specific rules per adapter
- Subscription detection: interval consistency + confidence scoring
- Transfer detection: debit/credit matching (same amount, within 2 days)

**Success:** All transformation unit tests pass, handles edge cases

---

### 4. **[Job Orchestration](04-job-orchestration.md)** (1 day)
Celery + Redis async pipeline with idempotency and error handling.

**Jobs:**
- `transform_bronze_to_silver` — Normalize raw data (idempotent)
- `enrich_silver_to_gold` — Add business logic + detect patterns
- Job chaining: Bronze→Silver→Gold
- Error recovery: failures logged, pipeline continues

**Features:**
- Idempotent transforms (safe to rerun)
- Soft/hard time limits (graceful timeout)
- Retry logic with exponential backoff
- Quality issues tracking
- Scheduled runs (Celery Beat)

**Success:** Jobs run reliably, errors logged cleanly, no data loss

---

### 5. **[Testing & Verification](05-testing-verification.md)** (1 day)
Comprehensive test suite: unit, integration, end-to-end.

**Test structure:**
- Unit tests (adapters, transformers, models)
- Integration tests (layer interactions)
- End-to-end tests (real CSV → gold)
- Data integrity tests (no orphans, no duplicates)
- Performance tests (1000 records < 5s)

**Coverage target:** > 80% on critical paths

**Success:** All tests pass, pipeline verified end-to-end

---

## Execution Timeline

| Day | Phase | Deliverable |
|-----|-------|-------------|
| 1 | 1 | Adapter pattern + factory |
| 2 | 2 | Database schema + models |
| 3-4 | 3 | Transformations (all 5 modules) |
| 5 | 4 | Celery jobs + orchestration |
| 5 | 5 | CLI tool for testing |
| 6 | 6 | Integration + E2E tests |

**Total: 5-6 days for one developer**

---

## Dependencies

### Python Packages
- `fastapi` — API framework (future)
- `sqlalchemy` — ORM
- `psycopg2-binary` — PostgreSQL driver
- `celery` — Task queue
- `redis` — Message broker
- `pydantic` — Data validation
- `pytest` — Testing
- `click` — CLI

### Services
- PostgreSQL 15+
- Redis 7+

### Files to Create
```
adapters/
  __init__.py
  base.py
  factory.py
  monzo_adapter.py
  natwest_adapter.py
  vanguard_adapter.py

transformers/
  __init__.py
  account_linker.py
  silver_transformer.py
  account_ledger.py
  subscription_detector.py
  transfer_detector.py

tasks/
  __init__.py
  celery_config.py
  transform_bronze_to_silver.py
  enrich_silver_to_gold.py
  pipeline.py
  monitoring.py

models/
  __init__.py
  schema.py

tests/
  conftest.py
  unit/
  integration/
  e2e/
  fixtures/
    sample_monzo.csv
    sample_natwest.csv
    sample_vanguard.csv

migrations/
  001_create_bronze_layer.sql
  002_create_silver_accounts.sql
  ...
  007_create_gold_transfers.sql

cli.py
config.py
db.py
logging.py
```

---

## Key Design Decisions

### 1. Adapter Pattern
- **Why:** Different sources have different schemas; adapters decouple
- **Trade-off:** Slightly more code, but extensible (easy to add Plaid, etc.)

### 2. Medallion Data Lake
- **Why:** Clear separation of concerns (raw → normalized → enriched)
- **Trade-off:** More tables, but auditability and debuggability are excellent

### 3. Celery + Redis
- **Why:** Async, reliable, retry-safe
- **Trade-off:** Need to run Redis; slightly more infrastructure

### 4. Deterministic Source Keys
- **Why:** Prevent duplicates on re-upload without central ID assignment
- **Trade-off:** Keys are longer, less readable (but still debuggable)

### 5. Idempotent Transforms
- **Why:** Jobs can be safely rerun; no data loss on failure
- **Trade-off:** Must check for existing records before insert (slight perf cost)

---

## Critical Success Factors

✅ **No SQL injection** — All queries parameterized (SQLAlchemy ORM)
✅ **No duplicates** — Deterministic keys + idempotent transforms
✅ **Auditability** — Every record traced back to source file
✅ **Error resilience** — Failures logged, pipeline continues
✅ **Extensibility** — New adapters/sources don't require schema changes

---

## Gotchas to Avoid

❌ Don't hardcode file paths; use environment variables
❌ Don't skip migrations; they're your schema versioning
❌ Don't catch all exceptions silently; log them to quality_issues
❌ Don't assume transactions are sorted by date
❌ Don't mix business logic with persistence; use transformers
❌ Don't skip the integration tests; they catch layer-interaction bugs

---

## After V0: Next Steps

Once the core engine is solid (all tests passing), build in this order:

1. **API Layer** (2-3 days)
   - REST endpoints for ingestion, queries
   - Input validation + error responses
   - Pagination for large datasets

2. **CLI Tool** (1 day)
   - Manual ingestion
   - Pipeline inspection
   - Demo script

3. **Claude Integration** (2-3 days)
   - `/api/v1/claude-context` endpoint
   - Goal planning prompts
   - Financial advice generation

4. **Dashboard** (variable)
   - React frontend
   - Charts, tables, insights
   - File upload UI

5. **Deployment** (1-2 days)
   - Docker containers
   - Environment setup
   - Monitoring (Prometheus + Grafana)

---

## Questions Before Starting?

- ❓ **Data:** Do you have real CSV exports to test with?
- ❓ **Scope:** Confirm Monzo + Natwest for V0? (Vanguard can be V0.5)
- ❓ **Timeline:** 5-6 days realistic, or need to adjust?
- ❓ **Infrastructure:** Should I set up Docker Compose for local dev (PostgreSQL + Redis)?

---

## Running the Plans

Each plan is self-contained but builds on the previous one:

1. **Start with [01-core-engine.md](01-core-engine.md)** for high-level phases
2. **Refer to [02-database-schema.md](02-database-schema.md)** while designing tables
3. **Follow [03-data-transformations.md](03-data-transformations.md)** for business logic
4. **Use [04-job-orchestration.md](04-job-orchestration.md)** for async pipeline
5. **Check [05-testing-verification.md](05-testing-verification.md)** for test cases

Each plan has:
- Clear **Goals** (what you're building)
- **Scope** (in/out of V0)
- **Phases/Modules** (broken into chunks)
- **Implementation Details** (code structure + pseudocode)
- **Tests** (what to verify)
- **Success Criteria** (how to know you're done)

---

## Architecture Documents (Reference)

- `SYSTEM_REVISED.md` — Full architecture (everything)
- `SYSTEM_REVIEW.md` — Critical issues from original (why we revised)
- These plans — Focused execution (how to build it)

---

**Ready to start building? Pick a plan and go!** 🚀
