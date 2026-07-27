# Follow-up work: Bronze/Silver hardening

Branch: `bronze-silver-followups` (based on `bronze-silver-enhancements`).

These came out of a full codebase audit. Each item below is a **verified**
defect, not a hypothesis — the reproduction is given so you can confirm it
before fixing and prove it after.

---

## Working constraints (read first)

**There is no real data in this environment and there never will be.**
`data/` is gitignored: no statement PDFs, no Bronze/Silver Parquet, no
`data/account_map.json`, no DuckDB file. You cannot run `cli.py ingest`,
`cli.py silver rebuild`, or `cli.py accounts breakdown`.

**Verification is `uv run pytest` only.** Every task below is chosen to be
provable with synthetic fixtures. Where a task needs data-shaped input,
build it in the test — do not reach for real files.

```bash
uv sync --extra dev          # pytest/black/ruff are NOT in the base sync
uv run pytest tests/ -q
```

**Baseline: 550 tests pass — but only with a real `data/account_map.json`.**
On a clean checkout you will see **27 failures** in
`tests/unit/transformers/test_silver_transformer.py`. That is P0 below, not
something you broke. Fix P0 first; then the suite is self-contained and every
later task has a trustworthy baseline.

**Do not run `uv run black` across the repo.** The pinned `black==23.12.1`
disagrees with whatever version actually formatted this codebase, so it
rewrites unrelated files (12 of them in one commit during the audit). Format
only files you changed, and check `git status` before committing. See P3.3.

**Money invariant:** all monetary values are signed integer minor units
(pence). Never introduce a `float` into a financial path. See `models/money.py`.

---

## P0 — Tests depend on gitignored user data (blocks everything)

**File:** `tests/unit/transformers/test_silver_transformer.py`

27 tests fail on any checkout without `data/account_map.json`. The file
hardcodes the maintainer's real hashed account identifiers and relies on the
real, gitignored map resolving them:

```python
# Real (test-only) entries so get_account_id resolves without hitting the fallback.
_KROO_ID = "fd7a2651d39e"
_NATWEST_ID = "43ae9e53d8a2"
...
```

`get_account_id()` (`transformers/account_config.py`) reads
`config.ACCOUNT_MAP_PATH` and raises `KeyError: "No account mapping for
account_identifier=..."` when it's absent.

**Reproduce:** `mv data/account_map.json /tmp/ && uv run pytest tests/ -q`
→ 27 failed. (In cloud, the file is simply never there.)

**Fix:** add a fixture — `tests/conftest.py` is the natural home — that writes
a synthetic account map to `tmp_path` and monkeypatches `ACCOUNT_MAP_PATH` to
it, then have the test module use it. The identifiers can stay as-is (they're
truncated one-way hashes) or become obviously-fake constants; either is fine,
but the tests must stop depending on a file that isn't in the repo.

**Done when:** the full suite passes with no `data/` directory at all.

---

## P1 — Correctness

### P1.1 `account_ledger` has no currency column; net worth sums blindly

**Files:** `transformers/silver_transformer.py` (`_LEDGER_COLUMNS`),
`transformers/balance.py`

`_LEDGER_COLUMNS` has no `currency` field, so account balances carry no
currency at all. `get_net_worth()` sums `balance_minor` across every account
and adds `total_value_minor` from holdings with no currency check anywhere
(`grep -n currency transformers/balance.py` → no matches).

Latent while everything is GBP, but it is silently wrong the moment it isn't —
and Amex statements already contain foreign transactions today (the adapter
just hardcodes `"currency": "GBP"`, a separate known gap).

**Fix:** add `currency` to the ledger schema and normalizers, then make
`get_net_worth`/`get_net_worth_breakdown` refuse to sum mixed currencies —
raise a clear error rather than returning a meaningless number. Do not build
FX conversion.

**Test:** synthetic ledger with two currencies → raises; single-currency →
unchanged behaviour.

### P1.2 `get_net_worth` is annotated `-> Decimal` but returns `int`

**File:** `transformers/balance.py:157-199`

Signature says `Decimal`; body ends `return total_minor`, an `int`. Callers
type-hinting on it are wrong. The value is correct (minor units) — only the
annotation lies.

**Fix:** change the annotation to `int`. Do **not** start returning `Decimal`
— that would reintroduce non-integer money into the core path.

---

## P2 — Performance (all currently correct, just wasteful)

### P2.1 Account map re-read from disk 1,477× per rebuild

**File:** `transformers/account_config.py`

`get_account_id()` calls `_load()` on **every row**, and `_load()` opens and
`json.load`s the map each time.

**Measured:** 1,477 `_load()` calls for a 697-transaction rebuild (0.86s).
Scales linearly with rows — 100k transactions means 100k file reads.

**Fix:** cache the parsed map (keyed by resolved path). It must stay
invalidatable: `register_account()` writes the file, and tests monkeypatch
`ACCOUNT_MAP_PATH` and pass explicit `path=` arguments — a naive
`@lru_cache` will make those stale.

**Test:** counter/spy asserting one load for N lookups, plus a test that
registering a new account is visible immediately afterwards.

### P2.2 `read_bronze` called 46× per rebuild for 9 sources

**Files:** `transformers/silver_transformer.py`
(`_read_bronze_frames`, `_contiguous_coverage_end`), `models/datalake.py`

Each `read_bronze()` globs a directory and concatenates every Parquet file in
it. It runs ~5× per source_type per rebuild, largely because
`_contiguous_coverage_end()` re-reads Bronze inside a per-account loop while
the caller already holds the frames.

**Fix:** pass the already-loaded frames down, or memoize per rebuild.

**Test:** spy on `read_bronze`, assert at most once per source_type.

### P2.3 O(n²) provenance rewrite in cross-source dedup

**File:** `transformers/matching.py`, `_dedupe_cross_source`

For each absorbed row it rescans the entire `provenance_rows` list to redirect
ids. Fine at today's 697 rows; quadratic as history grows.

**Fix:** index provenance by `silver_transaction_id` instead of rescanning.
Behaviour must not change — `tests/unit/transformers/test_matching.py` covers
it.

---

## P3 — Hygiene

### P3.1 Dead float money parser
`transformers/silver_transformer.py:290` — `_parse_money()` returns `float` and
has **no callers**. It is a live contradiction of the "no float in any
financial path" invariant sitting inside the financial module. Delete it.

### P3.2 Dead assignment
`cli.py:543` — `overload = False` is assigned and never read (`ruff` F841).
Confirm it isn't a dropped code path (the surrounding block builds
`override_flag`), then delete.

### P3.3 `black` version mismatch corrupts unrelated files
`pyproject.toml` pins `black==23.12.1`, which formats differently from whatever
version this codebase was formatted with — so the CLAUDE.md-documented
`uv run black ...` rewrites files you never touched.

**Fix (pick one, in its own commit, touching nothing else):** bump the pin to a
version consistent with the existing style, **or** run one deliberate
repo-wide normalisation. Do not mix this with any other change.

---

## Out of scope

- **Monzo CSV × Monzo PDF double-counting.** Both map to `acc_monzo_current`
  with no cross-source policy in `matching.py`, so overlapping ingests double
  every transaction (net worth stays right — it reads printed ledger balances
  — so it is silent). Accepted risk: the CSV export is effectively unused.
  Don't fix without asking.
- **Gold layer, categorization, merchant normalization.** Separate, larger
  work.
- **Currency extraction in adapters** (Amex forex). Related to P1.1 but needs
  real statements to develop against — not doable here.

---

## Commit guidance

One concern per commit, with the reproduction in the message. Run the full
suite before each. After P0, `uv run pytest tests/ -q` must be green with no
`data/` directory present — that is the standing bar for this branch.
