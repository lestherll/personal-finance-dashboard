# Context: two further Vanguard capture gaps, found during the coverage/net-worth investigation

Neither of these is implemented - this is handoff context for a future session, not a
committed plan. Companion to `VANGUARD_COVERAGE_AND_NETWORTH_GAPS.md` (the two bugs
documented there - coverage's `drop_duplicates("filename")` and the breakdown's hardcoded
`as_of_date: None` - are fixed on this branch: `transformers/coverage.py` +
`transformers/balance.py`). These two are additional, separate findings from reading the
real statement text end-to-end (`data/raw/vanguard-pdf/Vanguard_Statement.pdf`) against what
`adapters/vanguard_pdf_adapter.py` actually parses.

A third finding from the same investigation - `data/raw/vanguard-pdf/Vanguard_Portfolio_Valuation.pdf`,
a structurally unrelated "Portfolio valuation" snapshot document (no Activity/transactions
section at all, never successfully run through `cli.py ingest` - no matching Bronze parquet
ever existed for it) - has been deleted. It wasn't a real export format in active use and
isn't in scope for this or any future session.

## 1. "Your Vanguard account summary" table isn't captured - and it's a real reconciliation anchor

**Where:** page 1 of every real statement, before either wrapper's holdings/activity
sections:

```
Your Vanguard account summary
Product                       Value on 09 April 2026   Value on 08 July 2026
ISA                           £986.49                   £1,630.43
Vanguard Personal Pension     £0.00                      £3,310.42
Account total                 £986.49                    £4,940.85
```

`adapters/vanguard_pdf_adapter.py::parse_transactions()`'s main loop only recognizes two
section starts - `_WRAPPER_INVESTMENTS_RE` (`"Your X investments at DATE"`) and lines
starting `"Activity from"`. This table falls between the statement header and the first of
those, so every line in it is walked over one at a time and discarded (`i += 1`, no branch
matches).

**Confirmed against real data - this is an exact reconciliation anchor, not just a
duplicate total:**

```
ISA:      Value on 08 July 2026  £1,630.43  ==  holdings fund £1,630.28 + cash £0.15
Pension:  Value on 08 July 2026  £3,310.42  ==  holdings fund £3,309.42 + cash £1.00
Account total £4,940.85 == ISA (£1,630.43) + Pension (£3,310.42)
```

This directly contradicts the current CLAUDE.md/Gotcha #6 claim that "Vanguard PDF has no
reconciliation - a direct-read balance with no anchor to check against." There **is** a
printed anchor; it's simply never been parsed. If implemented, CLAUDE.md's "What Bronze does
not guarantee" section and the vanguard-pdf bullet in Gotcha #6 both need updating to drop
Vanguard from the "no anchor" list.

**Likely fix direction (not committed to):**
- Extract the table with a regex over the joined per-line text (same per-line matching style
  as `_ACTIVITY_PERIOD_RE`), keyed on the wrapper name strings that already appear verbatim
  elsewhere (`"ISA"`, `"Vanguard Personal Pension"`) so it maps onto the same `wrapper` value
  used by `_parse_holdings_block`/`_parse_activity_block`.
- Add a lighter reconciliation check per wrapper - same "direct-read total vs. separately
  printed closing anchor" pattern as `KrooPdfAdapter._check_reconciliation`/
  `MonzoPdfAdapter._check_reconciliation` - comparing that wrapper's holdings-block total
  (fund `total_value` + cash `total_value`) against this table's `Value on [end date]` figure.

**Open design question for whoever picks this up:** every other adapter reconciles once per
file (`self.last_reconciliation` is a single `ReconciliationResult`). Vanguard would need
**two** reconciliation targets per statement, one per wrapper - `ReconciliationResult`/
`self.last_reconciliation` as currently defined (`adapters/base.py`) doesn't obviously support
that. Needs a look at how `AdapterFactory.ingest()`/`IngestResult` consume
`last_reconciliation` before deciding whether to extend the dataclass to a list, run it
per-wrapper another way, or something else - don't guess at this without reading that code
path first.

**Where to look:** `adapters/vanguard_pdf_adapter.py` (main parse loop, wrapper-name
matching), `adapters/base.py` (`ReconciliationResult`, `self.last_reconciliation` contract,
Gotcha #14's reset-at-top discipline), `KrooPdfAdapter._check_reconciliation`/
`MonzoPdfAdapter._check_reconciliation` as the closest existing pattern to copy,
`tests/unit/adapters/test_vanguard_pdf_adapter.py` (no reconciliation tests exist yet for
this adapter - would need the standard match/mismatch/reset-between-parses trio per Gotcha
#14's existing convention).

## 2. "Payments in" summary table - deliberately deferred, not planned for now

**Where:** page 2 (ISA) / page 3 (Pension), under `"Your X summary"` → `"Payments in"` - a
categorized, undated list of cash-in line items for the period:

```
ISA:      External fee payment for ISA  £12.00
          Regular Deposit                £150.00  (x3)
          Deposit for Investment Purchases £200.00
Pension:  Wrap                           £3,280.81
```

Currently unparsed entirely - it falls between the two section headers the main loop
recognizes, same blind spot as gap #1 above.

**Decision (explicit, from this investigation, 2026-07-26): not implementing capture for
this section.** Every amount in "Payments in" already appears as an individual dated entry
in that wrapper's Activity table (already captured into Silver `transactions`) - this table
is a pre-categorized, undated view of data Bronze already has, not new information.

The one thing that *would* make this worth capturing directly rather than deriving it: if
the cash account inside each wrapper got its own reconciliation step. Confirmed against real
data that this would actually be a near-trivial check, not new machinery - each wrapper's
Activity table's *last row's* `cash_balance` already equals that wrapper's holdings-table
`Cash account` value exactly:

```
ISA:      last Activity row (23/06/2026) cash_balance = £0.15  ==  holdings Cash account £0.15
Pension:  last Activity row (01/06/2026) cash_balance = £1.00  ==  holdings Cash account £1.00
```

That's essentially the same underlying idea as gap #1's fund-value anchor, just for the cash
sub-account instead of the fund. **Explicitly out of scope for now** - noting it here so a
future session doesn't have to rediscover the tradeoff (derive vs. capture) from scratch, and
so the near-trivial reconciliation check above isn't lost if cash-account reconciliation ever
does become worth doing alongside gap #1.

## Status

Neither gap implemented. `data/raw/vanguard-pdf/Vanguard_Portfolio_Valuation.pdf` deleted
(unrelated document type, out of scope, never successfully ingested).
