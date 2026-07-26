# Design: deriving a balance for `natwest-transactions`

## Context

`natwest-transactions` (the on-demand online "Transactions" export) has no
balance data anywhere in the source document at all — a structural gap,
not a bug (see CLAUDE.md Gotcha #6). It exists specifically to cover the
period since the last quarterly `natwest-statement`, which does print a
real closing balance. Today, `acc_natwest_current`'s "current balance"
in `get_current_balances()`/`cli.py accounts breakdown` is stuck at
whichever date the last real Statement covers (e.g. 2026-05-13), even
though a `natwest-transactions` export may already cover activity well
past that (e.g. through 2026-05-31) — because there's no anchor to roll
forward from.

This doc captures a design discussed and refined in conversation for
closing that gap. **Not implemented. Not decided to implement.** Written
up per the same "options/design, no code yet" convention as
`BALANCE_HANDOFF.md`.

## The proposed workflow

1. User uploads a `natwest-statement` — real closing-balance anchor.
2. User uploads `natwest-transactions` for the gap since — no printed
   balance, but derivable by rolling the last statement's anchor forward
   through these transactions.
3. ~3 months later, NatWest generates the next `natwest-statement`.
4. User uploads it. The new statement's own transactions win over the
   transactions-export's duplicates (already true today via
   `_dedupe_natwest_cross_format`) — and separately, the *derived* balance
   computed in step 2 can now be checked against this new real anchor.

## Design: generic Bronze-level relationship, not Natwest-specific Silver code

The natural trap is hardcoding this as Natwest-specific logic bolted into
Silver, breaking the current clean invariant that every
`_TRANSACTION_NORMALIZERS`/`_LEDGER_NORMALIZERS` entry is a pure per-row,
per-source-type function with zero cross-source-type awareness. Instead:

**A declared registry** (same spirit as `data/account_map.json` — a fact
declared as data, not bespoke code — so a future bank with the same
"on-demand export vs. formal statement" split reuses this for free):

```python
# transformers/silver_transformer.py

# Source_types with no balance anchor of their own, but that recognize
# another source_type as the authority for the same account - rolled
# forward from that anchor's latest confirmed balance.
_DERIVED_BALANCE_ANCHORS = {
    "natwest-transactions": {
        "anchor_source_type": "natwest-statement",
        "rollforward_sign": 1,  # balance += amount, asset account (Gotcha #12)
    },
}
```

**One generic function**, called once from `run_bronze_to_silver()` after
both `normalize_transactions()` (which already produces the deduped,
unified transaction set via `_dedupe_natwest_cross_format`) and
`normalize_account_ledger()` have run — `normalize_account_ledger()` itself
never changes:

```python
def _derive_rollforward_ledger_rows(transactions_df, ledger_df):
    """For each dependent->anchor pair in _DERIVED_BALANCE_ANCHORS, take the
    anchor source's latest confirmed balance per account and roll it
    forward through the dependent source's own transactions dated after
    that anchor - producing extra ledger rows tagged balance_source
    "derived" instead of "printed"."""
```

Output rows get appended to `ledger_df` before the final Silver write.

**Provenance**: add a `balance_source` column to `_LEDGER_COLUMNS`
(`"printed"` by default — one line in the existing per-row loop —
`"derived"` only for these synthesized rows), so `get_current_balances()`/
`cli.py accounts breakdown` can distinguish a confirmed figure from a
provisional one without any change to existing tie-break logic.

**Self-correcting for free**: Silver is fully recomputed from Bronze on
every rebuild (not incrementally patched). When a new `natwest-statement`
arrives, its closing balance becomes the new latest anchor, so derivation
naturally only rolls forward from that point on — no explicit
invalidation/cleanup needed for the old derived rows.

**Reframing step 4**: not "check our own arithmetic" but a **continuity
check between two consecutive real anchors** — given anchor₁ (old
statement) and anchor₂ (new statement), does
`anchor₁.balance + Σ(deduped transaction amounts between the two dates)
== anchor₂.balance`? A mismatch here is informative, not just a bug
signal — it likely means the online export missed something real
(interest, a fee, a pending item settling differently) that the statement
caught.

**Architectural note**: this can't be a Bronze/B1-style check (computed
once, at single-file ingest time, self-contained per CLAUDE.md Gotcha
#14) — it inherently needs two sibling files/source_types to exist
together, so it's necessarily a Silver-time computation, closer in spirit
to the S1-S3 Silver hardening items than another per-adapter B1.
`cli.py accounts reconciliation`/`find_reconciliation_status()` (which
reads Bronze reconciliation columns directly) would need a sibling code
path, not a simple extension.

## Edge case: partial/gapped coverage

Raised and resolved in discussion: what if the `natwest-transactions`
data available doesn't fully, contiguously cover the window between
anchor₁ and anchor₂ (or between anchor₁ and "now")? Two bad outcomes if
unhandled: derived balances get produced for dates we shouldn't have any
confidence about, and the step-4 continuity check would report a false
**mismatch** for windows that were never fully covered in the first place
— indistinguishable from a genuine discrepancy.

**Fix — reuse existing infrastructure, don't invent new gap detection**:
`natwest-transactions` already captures `statement_period_from`/
`statement_period_to` per file (B4), and
`transformers/coverage.py::find_coverage_gaps()` already answers "is this
account's period coverage contiguous?". Before rolling anything forward:

1. Check contiguity of this account's `natwest-transactions` periods,
   anchoring the expected start at `anchor₁.as_of_date`.
2. If contiguous: derive normally across the whole span; the continuity
   check against a later anchor is meaningful.
3. If there's a gap: only derive up to the edge of contiguous coverage —
   nothing past the gap gets a synthesized balance (individual transaction
   rows past the gap are unaffected; only the *cumulative ledger balance*
   is gap-sensitive). `get_current_balances()` then naturally stops
   advancing `as_of_date` at the last point of contiguous derivation
   rather than silently extrapolating across a hole it doesn't know is
   there.
4. The continuity check becomes **tri-state**, reusing the existing
   `ReconciliationResult.matches: Optional[bool]` pattern (`None` already
   means "inconclusive" for a missing anchor; it would also mean "known
   coverage gap, not comparable" here) — so a real mismatch stays a
   genuine, actionable signal instead of being buried under expected gap
   noise.

## Status

Exploration/design only, not scheduled. Revisit this file when picking
the work up — it should be a fairly mechanical implementation given the
design above, mirroring `_dedupe_natwest_cross_format`'s existing
precedent for cross-format Natwest handling.
