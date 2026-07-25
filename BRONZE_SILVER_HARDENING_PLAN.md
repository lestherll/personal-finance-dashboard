# Plan: Hardening Bronze & Silver before Gold

> **Status update:** the Bronze section (B1 structured reconciliation, B2
> friendly detection errors, B3 statement coverage tracking) has since been
> implemented in full, and extended by a B4 follow-up (statement-period
> capture broadened from 3 to all 8 PDF source_types) — see `CLAUDE.md`'s
> "What Bronze guarantees" (under Architecture & Data Flow → Bronze Layer)
> and Gotcha #14/#15. The Silver section below (S1-S3) has also since been
> implemented in full — see `CLAUDE.md`'s "Current Status" →
> "Silver Hardening (S1-S3)". Only S4 (merchant normalization) remains
> deferred, per this doc's own recommendation. The rest of this document is
> left as originally written, for history/rationale.

## Why this doc exists

Written after a product-level step-back: the project is functionally at the
Silver stage (Phase 1 adapters + Phase 2 Silver transformations both done,
see `CLAUDE.md`), Gold enrichment (subscriptions, transfers, snapshots) is
next per `ARCHITECTURE.md`'s phase plan. Before building Gold, this plan
argues for hardening what Bronze and Silver actually *guarantee* — because
Gold's output (categorized spending, "are you on track") is only ever as
trustworthy as what it's built on, and several real, user-facing gaps exist
today that no amount of Gold-layer cleverness will paper over.

This is a plan for **another agent to pick up and execute**, not a record of
work already done. Read `CLAUDE.md` (especially "Common Gotchas") and
`AMEX_BUG_HANDOFF.md` / `BALANCE_HANDOFF.md` first — this plan assumes that
context and doesn't repeat it.

## What each layer actually guarantees today (baseline, for context)

**Bronze guarantees:** no double-counting on re-upload (`bronze_source_key`
+ same-file/cross-format dedup), full audit trail (`data/raw/{source_type}/`
+ raw `raw_data`), auto-detection of source bank.

**Bronze does *not* guarantee:** that parsed numbers are correct. Five
distinct classes of silent parsing bugs were found and fixed in adapters
this project has shipped (Amex amount mispairing/sign errors/Plan-It
formula — see `AMEX_BUG_HANDOFF.md`; Kroo swallowing a Monzo statement;
Natwest format ambiguity — see `CLAUDE.md` Gotchas #8, #10, #11). None of
these threw an error; they produced plausible-looking wrong data. The only
existing safety net is a non-blocking `logger.warning()` in
`AmexPdfAdapter.parse()` when its derived balance doesn't reconcile against
the statement's printed Closing Balance — nobody sees this unless they're
tailing logs.

**Silver guarantees:** a unified SQL-queryable schema across 11
source_types (`transactions`, `holdings`, `account_ledger` — see
`transformers/silver_transformer.py`), idempotent deduplicated reruns, and
balance history for 6 of 8 PDF sources (`LEDGER_SOURCE_TYPES`).

**Silver does *not* guarantee:** a correct answer to "what's my current
balance" from a naive query. `account_ledger`'s schema
(`bronze_source_key, account_id, source_type, balance, as_of_date`) has no
stable secondary sort key, and `as_of_date` is day-granularity — multiple
same-day transactions produce multiple same-day balance rows with no
defined order. `ORDER BY as_of_date DESC LIMIT 1` can silently return the
wrong one. This was hit for real this session: for the Amex account, a
naive "latest balance" query returned £678.04 when the true figure
(confirmed against the statement's printed Closing Balance) was £863.04.
It was only caught by chance, cross-checking against the source PDF. Given
`PRODUCT.md`'s entire premise ("are you on track") starts from "what do you
have right now," this is the highest-leverage gap in the whole pipeline.

Also missing from Silver: statement-period/coverage tracking (no way to
detect a user skipped uploading a month), and any queryable reconciliation
status (the Amex check exists but dies in a log line, isn't stored
anywhere).

## Non-goals

This plan is scoped to Bronze and Silver correctness/trustworthiness. It
deliberately does **not** cover:
- Gold enrichment itself (categorization, subscriptions, transfers,
  snapshots) — next phase, out of scope here.
- Celery orchestration (Phase 3, `TODO.md`) — orthogonal, can land before
  or after this work.
- New bank adapters (e.g. Monzo Flex) — separate effort.

---

## Bronze improvements

### B1. Structured reconciliation status per ingested statement (highest priority)

**Problem:** the Amex balance-reconciliation check
(`AmexPdfAdapter.parse()`, compares derived running balance against the
statement's printed Closing Balance) is the only correctness signal that
exists anywhere in Bronze, and it only reaches a `logger.warning()`. No
other adapter has an equivalent check at all (Kroo, First Direct, Natwest
Statement all *could* do the same thing — they parse or derive a balance
too).

**Why it matters:** this is the difference between "the pipeline silently
gives wrong numbers" and "the pipeline tells you it's not sure." Given how
many real bugs have already been found this way, treat statement-level
reconciliation as a first-class feature, not a debugging side effect.

**Suggested approach:**
- Define a small structured result (e.g. a `ReconciliationResult` dataclass
  with `expected_closing: Optional[Decimal]`, `derived_closing:
  Optional[Decimal]`, `matches: Optional[bool]`) that adapters which can
  self-check (Amex now, extend to Kroo/First Direct/Natwest Statement —
  they all have a printed closing/previous balance to check against)
  return alongside their records, rather than just logging.
- Persist this per Bronze file — either as a sibling small parquet/JSON
  next to the Bronze parquet, or as extra columns/metadata on the Bronze
  write itself (`models/datalake.py::write_bronze`) so it survives and is
  queryable, not just visible in a terminal at ingest time.
- Surface it via `cli.py ingest`'s output (currently just prints record
  count) — e.g. `✓ file.pdf: 76 records, reconciles against printed
  closing balance (£863.04)` or a loud warning line when it doesn't.

### B2. Friendly handling of unsupported/unrecognized statement formats

**Problem:** uploading a format with no adapter (e.g. the Monzo Flex
statement encountered this session) currently surfaces as a raw
`ValueError` from `AdapterFactory.detect_adapter()` — not a message a
non-technical user could act on.

**Why it matters:** the product's entire ingestion loop is "export →
upload." A failed upload with an opaque error is the most direct churn
risk in the whole product.

**Suggested approach:**
- Catch the "no adapter matched" and "ambiguous match" cases in `cli.py
  ingest` (eventually wherever the real upload entry point lands) and
  produce a specific, actionable message distinct from a generic parse
  failure — something like "we don't recognize this statement format yet"
  vs. "we recognized the bank but couldn't parse this file."
- Consider logging unrecognized-format uploads somewhere so it's visible
  which banks/products are actually being attempted vs. supported — useful
  input for adapter prioritization.

### B3. Statement coverage / gap tracking

**Problem:** nothing tracks which calendar periods, per account, have been
ingested. A skipped month is invisible.

**Why it matters:** any Gold-layer projection ("are you on track") is only
as good as its input completeness. A silent gap undermines trust in the
eventual product surface without anyone noticing until numbers look wrong.

**Suggested approach:**
- Most adapters already extract (or could extract) a statement period —
  Amex and Natwest PDF do via `_extract_statement_period()` /
  `resolve_year_in_period()` (`adapters/pdf_adapter.py`). Capture this
  (from/to dates) as part of `RawRecord` metadata or a lightweight
  per-upload record, per account_id.
- Build a simple query/report: per account, list statement periods
  ingested, and flag gaps between consecutive periods. Doesn't need to be
  automatic/alerting yet — a `cli.py accounts coverage` command that prints
  gaps would already deliver most of the value.

---

## Silver improvements

### S1. Fix the same-day ordering gap (highest priority)

**Problem:** `account_ledger` has no stable secondary sort key. Multiple
transactions on the same calendar day (very common for Amex especially,
per `AMEX_BUG_HANDOFF.md`'s "second wrinkle" section) produce multiple
same-`as_of_date` balance rows with database-unspecified tie-break order.

**Why it matters:** "current balance" is the single most basic query the
product needs, and today it can be silently wrong. This was hit for real
this session (see baseline section above) — the workaround was manually
joining `account_ledger` back to Bronze on `bronze_source_key` to recover
`line_number` as a tiebreaker. That's not something that should need to be
re-derived by hand every time.

**Suggested approach:**
- Add a stable ordering column through the pipeline. `RawRecord` already
  has `line_number` (parse-order sequence within a file) — thread it (or
  an equivalent monotonic sequence) through
  `transformers/silver_transformer.py`'s normalization into both
  `transactions` and `account_ledger` as a real column (e.g. `sequence` or
  `parse_order`).
- Update `_dedupe_with_existing()` and any "latest balance" logic to sort
  by `(as_of_date, sequence)`, not `as_of_date` alone.
- This needs to work *across* Bronze files for the same account too (not
  just within one statement) — a global sequence isn't meaningful across
  separately-parsed files, so the ordering key likely needs to be
  `(as_of_date, upload_timestamp, line_number)` or similar — worth
  designing carefully rather than bolting on the first thing that works,
  since getting this wrong is exactly how the current bug happened.

### S2. First-class "current balance" (and net worth) query

**Problem:** there is no canonical, tested way to ask "what's my current
balance per account" or "what's my total net worth right now." Every time
this has come up, it's been hand-written ad hoc SQL with manual tie-break
logic.

**Why it matters:** this is arguably the single most basic product query,
and today it doesn't exist as a reusable, correct, tested thing.

**Suggested approach (depends on S1 landing first):**
- A small helper — either a DuckDB view definition, or a Python function in
  `models/datalake.py` or a new `queries.py` — that returns latest balance
  per `account_id`, using the stable ordering from S1.
- A net-worth rollup: sum current balances across accounts, accounting for
  sign convention differences between asset accounts (current/savings/
  investment) and liability accounts (credit cards) — check
  `transformers/account_config.py`'s `account_type` field, which already
  distinguishes these.
- Add unit tests using the same same-day-tie scenario that caused the
  original bug, so this can't silently regress.

### S3. Queryable reconciliation status

**Problem:** even once B1 (Bronze-level reconciliation) exists, it's not
useful if it can't be joined against Silver data.

**Why it matters:** lets a future UI (or just a developer) ask "which of my
accounts have unreconciled/uncertain balance data right now" directly via
SQL, rather than grepping logs.

**Suggested approach:**
- Carry B1's reconciliation result through into Silver — either as
  additional columns on `account_ledger` (e.g. `reconciled: Optional[bool]`
  per batch) or a separate small `ingestion_quality` table keyed by
  `bronze_source_key`/account/statement-period.
- Depends on B1 landing first; sequence these together.

### S4. (Lower priority, flagged not planned) Merchant/description normalization

**Problem:** the same merchant appears differently across banks and even
within one bank's statements (e.g. `SAINSBURY'S SPRMRKTS LT` vs
`SAINSBURY'S` vs `Sainsbury's`). Currently `transactions.description` is
raw statement text.

**Why it matters:** blocks "spending by category" until Gold does this
work — flagging here only because it's a genuine open question whether
this belongs in Silver (as data cleanup) or Gold (as business logic), and
whoever picks up Gold should make that call explicitly rather than by
default. Not scoped as a concrete task in this plan.

---

## Suggested order of attack

1. **S1** (same-day ordering) — blocks S2 and S3, and is the single most
   impactful fix given how central "current balance" is.
2. **B1** (structured reconciliation) — independent of S1, can happen in
   parallel; needed before S3.
3. **S2** (current balance / net worth query) — depends on S1.
4. **S3** (queryable reconciliation) — depends on B1.
5. **B2** (friendly unsupported-format handling) and **B3** (coverage
   tracking) — independent, lower urgency, can slot in anywhere.
6. **S4** — explicitly deferred; resolve the Silver-vs-Gold placement
   question when Gold work starts, don't build it speculatively here.

## Open questions for whoever picks this up

- Should B1's reconciliation check be a required part of every adapter's
  contract going forward (i.e. added to `DataSourceAdapter`/`PdfAdapter`
  base class), or opt-in per adapter where a printed anchor exists? Kroo,
  First Direct, and Natwest Statement all have the data to support it;
  Vanguard/Monzo (direct-read balance, no anchor to check against) don't
  need it the same way.
- For S1's ordering key: is `upload_timestamp` reliable enough as a
  cross-file tiebreaker, or does it need to be the statement's own period
  end date instead (to avoid depending on *when* a user happened to upload
  a file)?
- Does coverage tracking (B3) belong as its own CLI command, or as an
  extension of `cli.py accounts list-unmapped`'s spirit — a
  "cli.py accounts list-gaps" sibling?
