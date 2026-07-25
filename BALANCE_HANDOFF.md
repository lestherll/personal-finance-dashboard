# Handoff: capturing account balance

## Context

Asked "can we get a snapshot of the user's balance / financial health" from
the current data lake. Answer: no, not reliably. This doc is the handoff for
whoever picks up fixing that - options only, no implementation yet.

## Current state (as of `worktree-ingest-real-statements`, commit `b70c211`)

- `account_ledger` (the Silver table meant for this): **0 rows.** Only
  Natwest CSV and Vanguard CSV populate it, and neither is in the default
  ingestion path (CSV adapters disabled by default; PDF is primary).
- PDF adapters (Kroo, Natwest, First Direct, Amex, Vanguard, Monzo, Chase)
  all **parse and then discard** the running-balance column - only
  `date`/`description`/`amount` survive into `raw_data`. This is deliberate
  (documented as a known limitation), not a bug.
- Vanguard holdings *do* give a real point-in-time value (`total_value` as
  of the statement date) - the one source that's actually usable today.
- Summing transaction amounts is **not** a substitute - verified: one
  account's net sum landed on exactly £0.00 purely by coincidence (one
  month of a brand-new account), and none of the others have a known
  opening-balance anchor or guaranteed-continuous statement coverage.

## Options

### A. Capture the per-transaction running balance PDF adapters already parse
Every adapter already sees a running balance while parsing a transaction
row (e.g. Kroo's Out/In/**Balance** columns, Chase's Amount/**Balance**) -
it's just dropped before `RawRecord.raw_data`. Keep it.
- **Effort:** touch all 7 PDF adapters (`_parse_transaction_lines` /
  equivalent), extend the Silver `account_ledger` normalizer to read it.
- **Gives you:** full balance history, one point per transaction.
- **Downside:** most work; still only "balance as of last statement
  uploaded," not "balance right now" - statements are periodic.

### B. Capture only the statement's closing balance (one number per upload)
Every statement already prints an explicit closing balance summary
(Kroo: "Total closing balance", Chase: "Closing balance", Amex: "Closing
Balance", Monzo: "Total balance"). Parse just that one line per statement
instead of per-transaction running balance.
- **Effort:** small, mechanical - one regex per adapter, no schema change
  beyond what `account_ledger` already expects (`balance` + `as_of_date`).
- **Gives you:** one ledger point per statement upload - enough for "as of
  your last statement, your balance was X."
- **Downside:** same "not real-time" ceiling as A, but far less parsing
  work to get there. Probably the best effort/value ratio of the four.

### C. Manual seed balance + roll forward from transactions
User provides one known balance-as-of-date via a CLI command; derive
current balance by summing transactions since that anchor.
- **Effort:** small (one CLI command + arithmetic), no adapter changes.
- **Downside:** fragile - drifts silently if a statement gap exists or a
  transaction is missed/miscounted; needs the user to keep re-anchoring;
  the exact trap already hit when just summing transactions blind (see
  Current state above), just pushed one step further out.

### D. Gold-layer `account_snapshots` enrichment job
The architecture already reserves a slot for this
(`ARCHITECTURE.md` / `data/gold/account_snapshots.parquet`, Phase 3,
not started). Would consume whichever of A/B/C above produces the
ledger data, and turn it into daily balance snapshots (carrying the last
known balance forward across gaps, netting in transactions).
- **Effort:** the "proper" home for this, but only meaningful once there's
  actual ledger data (A or B) to feed it - not a substitute for those.

### E. Open Banking / bank API integration
`PRODUCT.md` already lists this as "coming soon." The only option that
gets an actual real-time balance instead of "as of the last statement
uploaded."
- **Effort:** large - auth, per-bank API integration, compliance. Out of
  scope for a quick follow-up; noted for completeness.

## Recommendation (opinion, not decided)

B first (cheap, unblocks "balance as of last statement" for every account
immediately), then D once there's ledger data worth turning into
snapshots. A is strictly more work than B for not much more value unless
per-transaction balance granularity turns out to matter later. C is a trap
disguised as a shortcut - skip it. E is a different project.

## Open questions for whoever picks this up

- Does "financial health snapshot" need to be *as of today*, or is "as of
  your most recent statement" good enough for v1?
- Should `account_ledger` re-ingestion of already-archived statements
  (`data/raw/`) happen automatically once B lands, or is a manual re-run
  of `cli.py ingest` against the archive acceptable?
