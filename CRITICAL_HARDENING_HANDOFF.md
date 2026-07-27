# Critical Hardening Handoff

Status snapshot for continuing `CRITICAL_HARDENING_PLAN.md`. Written at the
point Milestone 1 was checkpointed; not re-verified after that point, so
confirm current state before trusting specifics below.

## Where this lives

- Worktree: `/Users/lestherll/.codex/worktrees/e8d5/personal-finance-dashboard`
  (registered as a worktree of the main repo at
  `/Users/lestherll/Projects/personal-finance-dashboard`, alongside other
  worktrees under `.claude/worktrees/`).
- Branch: `critical-hardening-plan`. This worktree was originally on a
  **detached HEAD** at `e4a33c5` with the hardening work already uncommitted
  in the tree; the branch was created specifically to give that work a safe
  home before committing (detached-HEAD commits risk becoming unreachable).
- Checkpoint commit: `5339160` — "Make raw archival and Bronze publication
  immutable and content-addressed."
- `data/account_map.json` was copied in from the
  `.claude/worktrees/fix-reconciliation-mismatch-gate` worktree to unblock
  tests — it's gitignored user data (hashed identifiers only, no financial
  figures), a fresh worktree never has it (documented in this repo's
  CLAUDE.md as a known gotcha).

## What's done (Milestone 1 — complete, tested, committed)

Implements immutable, content-addressed raw archival + Bronze publication:

- `models/ingestion.py` (new): `IngestionManifest` dataclass with status
  states `archived` → `parse_failed` / `bronze_failed` / `complete`;
  `start_ingestion()` archives raw bytes and creates/reuses the manifest;
  raw artifacts are content-addressed at
  `data/raw/sha256/{first-2-hex-chars}/{sha256}{suffix}` (sharded by hash
  prefix, **not** by `source_type` — deliberate deviation from the plan
  doc's literal `data/raw/{source_type}/{sha256}{suffix}` path, because
  `source_type` isn't known yet at archive time, which happens before
  parsing). Writes go through temp-file + `os.replace` atomic rename, same
  pattern for the manifest JSON.
- `models/datalake.py::write_bronze`: signature changed from
  `(source_type, filename, df, ...)` to `(ingestion: IngestionManifest, df,
  ...)`. Bronze files are now keyed by `ingestion_id`
  (`data/bronze/{source_type}/{ingestion_id}.parquet`) instead of original
  filename, written via temp-file + validate (`pq.read_table`) + atomic
  rename, and the write is skipped entirely (idempotent no-op) if that
  ingestion's Bronze file already exists. `read_bronze` needed **no
  change** — it already globs all `*.parquet` in the source_type directory.
- `cli.py ingest`: rewritten to drive the manifest lifecycle —
  `start_ingestion()` first (before parsing, since source_type is unknown
  until the adapter runs); early-return with "already ingested" if the
  manifest is already `complete`; on `AdapterDetectionError`/`ValueError`/
  zero-records/Bronze-write-exception, sets the manifest to the matching
  failed status with an error message and does **not** publish Bronze; on
  success, sets `complete` with `record_count`/`bronze_path`. The old
  `_archive_raw_file()` filename-based archiving helper was deleted —
  replaced entirely by `start_ingestion()`.
- `config.py`: added `INGESTIONS_DIR = DATA_DIR / "ingestions"`, created
  alongside the other data dirs at import time.
- Groundwork threaded through for Milestone 2 (see below), because Bronze
  row identity had to change shape anyway to support this:
  `adapters/base.py::make_bronze_record_id(ingestion_id, record_type,
  source_ordinal)` — content-independent hash, **not** based on parsed
  transaction content, so a parser fix that changes a date/description
  doesn't change the Bronze row's identity. Added `bronze_record_id` /
  `source_ordinal` fields to `RawRecord`, set in `adapters/pdf_adapter.py`'s
  shared parse loop and `adapters/monzo_adapter.py`. Added
  `PARSER_VERSION` class attr to `DataSourceAdapter` (default `"1"`,
  currently not bumped by any concrete adapter). `adapters/factory.py`'s
  `IngestResult` now also carries `source_type`/`adapter`/`parser_version`
  read off the adapter instance after `parse()`.
- `transformers/silver_transformer.py`: `bronze_record_id` threaded into
  all four normalized Silver tables (`transactions`, `holdings`,
  `account_ledger`, `plan_it_instalments`), falling back to
  `bronze_source_key` if absent (defensive, for old-shape Bronze rows).
  `run_bronze_to_silver()` no longer calls `_dedupe_with_existing()` for
  any of the five Silver tables — it's now a **full rebuild** from the
  current Bronze set every time, not a merge with whatever Silver already
  had on disk. `_dedupe_with_existing()` itself was left in place (still
  covered by its own direct unit tests, `TestDedupeWithExisting`) — it's
  just no longer called from the main pipeline. Worth a decision later:
  either delete it as dead code, or keep it if something else is meant to
  use it.

### Acceptance criteria status (per `CRITICAL_HARDENING_PLAN.md` Milestone 1)

All four are covered by `tests/unit/models/test_ingestion.py` and pass:

- Two byte-distinct inputs sharing a filename → two raw artifacts, two
  Bronze files. ✅ `test_same_filename_with_different_bytes_creates_two_immutable_ingestions`
- Re-ingesting identical bytes changes neither Bronze nor manifest count. ✅
  `test_exact_repeat_is_idempotent`
- A simulated parse/write failure produces no published Bronze file or
  partial manifest. ✅ `test_failed_bronze_write_never_publishes_a_partial_file`
  (this one tests `datalake.write_bronze` failing directly, not the
  `cli.py` failure-status wiring end to end — that end-to-end path is
  exercised by `tests/unit/test_cli.py` but wasn't independently
  re-verified against this exact acceptance wording)
- Every Bronze row joins to exactly one manifest/raw artifact by
  `ingestion_id`. ✅ by construction (Bronze filename *is* `ingestion_id`,
  manifest keyed the same way) — not separately asserted by a dedicated
  join test.

At last check: **394 tests passed**, coverage **90%** (threshold 85%),
`ruff check` clean except two **pre-existing** issues unrelated to this
work (`cli.py`'s unused `numpy` import in `accounts breakdown`, and
`models/datalake.py`'s `import pyarrow as pa` not at top of file — both
predate this changeset and are already documented in this repo's
CLAUDE.md "Common Gotchas" #1).

## What's not done

### Milestone 2 — Stable source identity and rebuildable Silver (partial)

Done (see groundwork above): content-independent `bronze_record_id`
threaded through `RawRecord` → Bronze → all Silver tables; full-rebuild
Silver instead of merge.

Not done:

- **Separate Silver matching interface.** The plan calls for preferring a
  bank-provided transaction identifier when available, otherwise falling
  back to a full fingerprint (account + normalized date + signed minor
  amount + normalized description + occurrence number), with ambiguous
  matches treated as reviewable rather than auto-dropped. Silver dedup
  today is still the pre-existing `bronze_source_key`-based approach
  (`_dedupe_natwest_cross_format` for the Natwest overlap case, plus
  whatever `_dedupe_with_existing` did before it was unwired from the main
  pipeline). No bank-provided-ID concept exists in any adapter currently.
- **Silver build manifest.** Nothing yet records, per Silver build, which
  parser versions or which set of input `ingestion_id`s produced it. There
  also isn't an explicit "rebuild" CLI command/API distinct from just
  calling `run_bronze_to_silver()` again — that function rebuilding fully
  each time is arguably "good enough" for the plan's stated goal (no
  manual Parquet deletion needed after a parser fix), but there's no
  audit trail of *which* Bronze set went into a given Silver build.
- Ambiguous-match review handling doesn't exist at all yet — no concept of
  a "reviewable" (vs. silently dropped or silently kept) duplicate.

### Milestone 3 — Exact-money schema (not started)

Everything here is still open: no `Decimal`-based parsing utilities, no
`_minor`/`currency` fields anywhere, all adapters still parse to `float`,
reconciliation math is still float-based, no FX/foreign-currency policy,
`_parse_money(...) -> 0.0`-style silent-zero fallbacks (if any still exist)
haven't been audited or removed. Every task under Milestone 3 in
`CRITICAL_HARDENING_PLAN.md` is untouched.

### Also deferred (per `TODO.md`, unchanged by this work)

Items 4–6 in `TODO.md`'s "Critical data-correctness hardening" list —
gating Silver promotion on data quality/quarantine, correcting
snapshot/net-worth semantics, and atomic hermetic-tested Bronze→Silver
publication — are explicitly sequenced *after* Milestones 1–3 finish, per
the plan doc's own "Delivery sequence" section. Not started, not
attempted.

## Suggested next step

Pick up Milestone 2's two remaining pieces (matching interface, build
manifest) as their own focused commit(s) on the `critical-hardening-plan`
branch, per the plan doc's stated delivery sequence (finish Milestone 2
before starting Milestone 3, since Milestone 3's adapter changes will
touch the same normalization code paths Milestone 2's matching interface
needs to land in first).
