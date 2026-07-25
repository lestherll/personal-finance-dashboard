# Handoff: Amex transaction parsing produces wrong amounts / drops transactions (FIXED 2026-07-25)

## Resolution

All bugs described below (1-4, plus the CR-marker misattribution found
while implementing) are now fixed in `adapters/amex_pdf_adapter.py`. The
fix switches transaction extraction from PyMuPDF's default flattened text
mode to `sort=True` mode (already used for the Account Summary box, see
Bug 3), which keeps each transaction on one line and eliminates the whole
class of column-reconstruction bugs at the root rather than patching each
one - see the class docstring and `_parse_page`/`_parse_main_table`/
`_parse_plan_it_created` for the current implementation.

Verified by reconciling `AmexPdfAdapter.parse()`'s derived closing balance
against the statement's own printed Closing Balance for **all 7 real
statements** referenced below (`~/Downloads/20_*_202*.pdf`) - every one now
matches exactly, including the 3 Plan-It-active statements and the
previously-unparseable Dec 2025–Jan 2026 opening statement. Unit tests
rewritten in `tests/unit/adapters/test_amex_pdf_adapter.py` to match the
new single-line fixture format.

One more thing found and fixed while implementing bug 2 (not in the
original list below): the "OTHER ACCOUNT TRANSACTIONS" section isn't a
blanket-credit section as first assumed - it can hold real debits too
(e.g. an annual card "MEMBERSHIP FEE"). The reliable signal turned out to
be a "CR" marker (either a standalone line, or appended as a trailing
token on an annotation line like `"DeliverooGoldBenefit ... CR"`) checked
uniformly for every row, not section membership.

The rest of this document is kept as-is below for historical context on
how each bug was found.

---


## Context

Found while adding balance tracking (see `BALANCE_HANDOFF.md` / the
`add-balance-tracking` branch work). Deriving a per-transaction running
balance for Amex required rolling forward from the statement's own
"Previous Closing Balance", then cross-checking the result against the
statement's own printed "Closing Balance". That cross-check fails on
**every real Amex statement tested** — the underlying transaction amounts
`AmexPdfAdapter` produces don't actually sum correctly, which is a
pre-existing bug in `adapters/amex_pdf_adapter.py`, not something the
balance work introduced. It affects the existing `transactions` Silver
table today, silently, for anyone who's ingested Amex PDFs before this was
noticed.

A non-blocking warning was added (`AmexPdfAdapter.parse()`, logs when
derived-vs-printed closing balance mismatch) so this is now visible instead
of silent, but the root cause in the parser itself is unfixed. This doc is
the handoff for whoever picks up the actual fix — findings only, no
prescribed solution, since only one of several mismatching cases has been
fully traced.

## Where the bug lives

`adapters/amex_pdf_adapter.py`:
- `_parse_page()` (line 133) scans for `(transaction_date, process_date,
  description)` triples via `_DATE_RE`, then calls `_select_amount_block()`
  (line 182) to find the matching amount block, and `zip()`s them together
  (line 162).
- `_select_amount_block()` groups amount-shaped lines (`_AMOUNT_RE`) into
  contiguous runs, and returns the run whose length equals
  `expected_count` (the number of triples found). **If no run matches
  exactly, it falls back to `runs[-1]`** (line 204) — the *last* run on the
  page, regardless of length.
- Because `zip()` stops at the shorter of the two sequences, a
  shorter-than-expected fallback run silently **drops trailing
  transactions** and **mispairs amounts to the wrong triples** for however
  many pairs `zip()` does produce. No error, no warning (before this
  session) — just wrong data.

## Root-caused example (one case, fully traced)

Statement: `20 Jan 2026 - 19 Feb 2026` (real file, in `~/Downloads/20_Jan_2026_-_19_Feb_2026.pdf` on the machine this was investigated on).

Last page's raw extracted text (page 5 of 8) ends with:

```
Feb 19 / Feb 19 / TESCO STORE 5312 5312TE GLASGOW    <- triple 1
Jan 26 / Jan 26 / DELIVEROO                          <- triple 2
Feb 11 / Feb 11 / DELIVEROO                          <- triple 3
DeliverooGoldBenefit
DeliverooGoldBenefit
...
2.15                                                  <- Tesco's real amount
Total new spend transactions for ... 1,437.34         <- summary line, not a txn
OTHER ACCOUNT TRANSACTIONS
5.00                                                   <- Deliveroo credit 1
5.00                                                   <- Deliveroo credit 2
Total of other account transactions
10.00                                                  <- summary line, not a txn
```

`expected_count = 3` (three triples). The amount-shaped runs on this page
are `[2.15]`, `[1,437.34]`, `[5.00, 5.00]`, `[10.00]` — **none has length
3**, so `_select_amount_block` falls back to `runs[-1]` = `["10.00"]`
(length 1). `zip(triples, ["10.00"])` only produces one pair: Tesco paired
with `10.00` (should have been `2.15`), and the two Deliveroo credits are
dropped entirely.

Verified this is the *complete* explanation for this statement: correcting
Tesco to `-2.15` and adding back the two missing `+5.00` credits changes the
computed total by exactly `-2.15`, which makes the derived closing balance
match the statement's printed Closing Balance (`1,427.34`) exactly.

## Broader evidence (not root-caused)

Tested against all 7 real monthly statements available
(`~/Downloads/20_*_2026*.pdf` and `20_Dec_2025...pdf`) via:

```python
from decimal import Decimal
from adapters.amex_pdf_adapter import AmexPdfAdapter
adapter = AmexPdfAdapter()
with open(path, "rb") as f:
    content = f.read()
anchors = adapter._extract_account_summary(content)  # (previous, printed_closing)
records = adapter.parse_transactions(adapter._extract_text(content))
total = sum(Decimal(str(r["amount"])) for r in records)
computed_closing = anchors[0] - total
# compare computed_closing against anchors[1]
```

Results (previous_balance / printed_closing / computed_closing, all £):

| Statement | n_txns | previous | printed closing | computed closing | mismatch |
|---|---|---|---|---|---|
| 20 Jan – 19 Feb 2026 | 70 | 769.58 | 1427.34 | 1425.19 | 2.15 (root-caused above) |
| 20 Feb – 19 Mar 2026 | 42 | 1427.34 | 787.51 | 357.41 | 430.10 |
| 20 Mar – 19 Apr 2026 | 10 | 787.51 | 569.36 | 1801.17 | 1231.81 |
| 20 Apr – 19 May 2026 | 58 | 2537.57 | 569.36 | 897.67 | 328.31 |
| 20 May – 19 Jun 2026 | 42 | 1467.03 | 569.35 | 438.56 | 130.79 |
| 20 Jun – 19 Jul 2026 | 52 | 1198.01 | 863.04 | 2679.54 | 1816.50 |
| 20 Dec 2025 – 19 Jan 2026 | 22 | — | — | — | anchor extraction returned `None` for this one — likely the first/opening statement has a different Account Summary layout; not investigated |

Every statement mismatches. Only the first row's cause is confirmed; the
others are presumed to be the same class of bug (a run-length mismatch
somewhere on some page triggered by different boilerplate/summary text per
statement) but this is **not verified** — could also be a distinct bug.
Worth checking each page of each statement individually, the same way the
Jan–Feb case was traced, before assuming they're all one root cause.

Also note: also-real file `~/Downloads/P_AMEXUK00009949.pdf` is **not** a
card statement — it's an Amex Travel Insurance certificate. Grabbed it by
mistake once during investigation; don't waste time on it again.

## A second, separate wrinkle noticed (same-day ordering)

Once real data was ingested through the full Bronze→Silver pipeline (see
`data/silver/account_ledger.parquet` in this worktree, or re-run
`transformers.silver_transformer.run_bronze_to_silver()`), a query for
"the latest balance per account" using
`ROW_NUMBER() OVER (PARTITION BY account_id ORDER BY as_of_date DESC)`
returned an **arbitrary** row when multiple transactions share the same
`as_of_date` (very common — Amex transaction dates have no time component,
so several same-day transactions all get identical `as_of_date` values).
For the 20 Jun–19 Jul statement's last day (`2026-07-13`), three different
balance values exist (`2679.54`, `2864.54`, `2856.59`) all dated the same
day, and which one a naive "latest" query returns depends on DuckDB's
unspecified tie-break order, not print/chronological order within the day.

This is somewhat independent of the amount-matching bug above (it's a
consequence of `transaction_date` having day-only granularity, not a
parsing error per se), but compounds it: even *if* amounts were parsed
correctly, "current balance" queries need a stable secondary sort key
(e.g. a preserved parse-order sequence number) to resolve same-day ties
correctly. Currently nothing in the Silver schema provides one.

## What's already in place (not a fix, just visibility)

`AmexPdfAdapter.parse()` (added this session) rolls a `Decimal` balance
forward from the statement's "Previous Closing Balance" and logs a
`logger.warning(...)` when the result doesn't match the statement's own
printed "Closing Balance". This doesn't correct the transaction data — it
just means the mismatch is now visible in logs instead of silent. The
`balance` field on Amex `raw_data` (and therefore `account_ledger` rows
sourced from `amex`) should be treated as **unreliable** until this is
fixed. The statement's own printed Closing Balance (via
`AmexPdfAdapter._extract_account_summary()`) is trustworthy and was used
as a manual workaround when reporting a real balance figure during this
session.

## Update: root cause fully traced, plus three more distinct bugs found (2026-07-25)

Continued the investigation above across all 6 available real statements
(all except the Dec 2025–Jan 2026 opening statement, which has its own
separate issue, see Bug 4 below). Conclusion: what looked like one bug
(`_select_amount_block`'s `runs[-1]` fallback) is actually **four separate
bugs**, three of them newly found this session. Listing them because they
need different fixes and some are much lower-risk than others.

### Bug 1 (the one already root-caused above, now generalized): amount-run selection

Confirmed the same root cause — a page's real transaction amounts split
across multiple non-contiguous runs — on every mismatching page in 5 of
the 6 statements (Jan–Feb, Feb–Mar, Mar–Apr, May–Jun, Jun–Jul). The
splitting is always caused by one of:
- A `"Total ..."` summary line (e.g. `"Total new spend transactions for
  ..."`, `"Total of other account transactions"`) sitting between two
  runs that both belong to the real transaction list.
- A section header like `"OTHER ACCOUNT TRANSACTIONS"` starting a second,
  separate run of real amounts (credits/benefits) after the main spend
  run.
- An unrelated informational section — `"New Plan It Instalments
  Created"` — whose amount is *not* a new transaction at all (see Bug 3
  below) and has no corresponding date-pair triple, so it must be
  excluded rather than zipped in.

Verified fix approach: tag each contiguous amount-run by its immediately
preceding non-amount line. Only include runs preceded by `"Amount  £"`
(the main table's column header — note: not always the *literal*
immediate predecessor, since first-page statements interleave an
unrelated Credit Summary / Rates-of-Interest sidebar box in between due
to PyMuPDF's default flattening — the numbers from those boxes never
collide with real amounts in every statement checked, but this hasn't
been proven in general) or `"OTHER ACCOUNT TRANSACTIONS"`, then
concatenate those runs in page order and zip against the triples list
positionally (which preserves the same section ordering). This reproduced
the correct amount for every triple checked by hand (Tesco `2.15`, the
two Deliveroo `5.00` credits, all 12 triples on Mar–Apr page 3, etc.) — a
full page-by-page dump script is in the session transcript if needed
again, not saved to a file.

### Bug 2 (new): "OTHER ACCOUNT TRANSACTIONS" sign is wrong even once Bug 1 is fixed

`_parse_page`'s `is_credit = i > 0 and lines[i - 1] == "CR"` only fires
when a literal `"CR"` marker line sits immediately before the date pair.
Checked every "OTHER ACCOUNT TRANSACTIONS" entry across all statements
(e.g. the two Deliveroo Gold Benefit credits) — **none of them have an
adjacent `"CR"` marker** in the extracted text, even though the section
itself is semantically all credits (confirmed independently: the
Jan–Feb root-cause trace above required treating them as `+5.00`, not
`-5.00`, to reconcile). So even after fixing Bug 1, these would still be
mis-signed as debits.

Fix needs to track, while scanning the page, whether a triple's line
index falls after an `"OTHER ACCOUNT TRANSACTIONS"` header (until the
next `"Total of other account transactions"` line) and treat those as
credits regardless of an adjacent `"CR"` marker.

### Bug 3 (new, separate from parsing entirely): Account Summary regex breaks on Plan It statements

`_extract_account_summary`'s `_ACCOUNT_SUMMARY_RE` hardcodes exactly four
`£` values in the formula row (`Previous - New Credits + New Debits =
Closing`). Three of the six statements (Mar–Apr, Apr–May, May–Jun) have
an *active Plan It installment plan*, and Amex prints a **fifth** `£`
value for those: `Previous - New Credits + New Debits + Plan It
Instalments Due = Closing`. The regex's fixed two-skip count then
silently captures the wrong (4th of 5) figure as `closing_balance` — in
practice it grabs the *Plan It Instalments Due* amount (~£569, the
recurring monthly installment) instead of the real Closing Balance.

This means **most of the "mismatch" figures in the table above for
Mar–Apr, Apr–May, and May–Jun are not measuring real transaction-parsing
error** — they were compared against the wrong target balance to begin
with. Confirmed real printed values by dumping the Account Summary row
directly (`doc[0].get_text(sort=True)`, search for `"Previous Closing
Balance"`):

| Statement | Previous | New Credits | New Debits | Plan It Due | **Real Closing** |
|---|---|---|---|---|---|
| Dec 2025 – Jan 2026 | 2592.93 | 2597.93 | 774.58 | — | 769.58 |
| Jan – Feb 2026 | 769.58 | 779.58 | 1437.34 | — | 1427.34 |
| Feb – Mar 2026 | 1427.34 | 1432.34 | 792.51 | — | 787.51 |
| Mar – Apr 2026 | 787.51 | 2453.90 | 3634.60 | 569.36 | **2537.57** |
| Apr – May 2026 | 2537.57 | 2537.57 | 897.67 | 569.36 | **1467.03** |
| May – Jun 2026 | 1467.03 | 1472.03 | 633.66 | 569.35 | **1198.01** |
| Jun – Jul 2026 | 1198.01 | 1208.01 | 873.04 | — | 863.04 |

A related, smaller formatting bug in the same regex: it assumes `£`
is never followed by a space before the digits, but Amex isn't
consistent about this (`"£ 769.58"` on the Dec–Jan statement's *only*
match target, vs `"£1,427.34"` elsewhere) — this alone is why Dec–Jan's
`_extract_account_summary` returns `None` today (regex fails to match
at all, not a different-layout issue as originally guessed). Easy fix:
`£\s*([\d,]+\.\d{2})` instead of `£([\d,]+\.\d{2})` in both spots.

### Bug 4 (new, structural — not a parsing bug, a scope gap): Plan It statements need a different balance formula entirely

Even with Bugs 1–3 fixed, `AmexPdfAdapter.parse()`'s balance rolling
(`running -= amount` per transaction, starting from Previous Closing
Balance) **cannot reconcile any statement with an active Plan It plan**,
by construction. Traced why on the Mar–Apr statement: a `MALAYSIA
AIRLINES` purchase (`£1,656.39`) gets converted into an installment plan
mid-statement. This produces:
- The original purchase, a normal debit in the main transaction table.
- A `"New Plan It Instalments Created"` entry for the *same* `£1,656.39`,
  printed as a credit — this is Amex removing the lump sum from what you
  owe this month (see Bug 1, this entry has no triple and must be
  excluded from the zip, not added as a transaction).
- A separate `"Plan It Instalments Summary"` table (page 3 of this
  statement) showing this specific plan's `Instalment £ / Fee £ / Total
  Amount £` for the current period (`552.13 / 17.23 / 569.36`) — **this
  £569.36 "Plan It Instalments Due" figure is what actually gets added
  to what you owe this month**, and it is never printed as a line in the
  main transaction table at all.

So the true formula is `Closing = Previous - New Credits + New Debits +
Plan It Instalments Due`, and that last term requires parsing an entirely
different table (`"Plan It Instalments Summary"`) that nothing in
`amex_pdf_adapter.py` currently touches. Rolling a per-transaction balance
forward from a flat transaction list structurally cannot produce a
correct number for these months — this isn't fixable by better
zipping/pairing, it needs new parsing scope. Also noticed one unresolved
oddity worth flagging for whoever picks this up: the same Mar–Apr
statement prints *two* near-duplicate `MALAYSIA AIRLINES` lines a page
apart (`£1,656.39` and `£1,656.40`, one penny apart) — not yet traced
whether the second is a genuine second line item, a foreign-currency
display artifact (the transaction also shows a `"MALAYSIAN RINGGIT"`
marker nearby), or something else.

### Net effect / suggested order of attack

Bugs 1–3 are self-contained, verified against real statement text, and
independent of each other and of Bug 4 — worth fixing together as a first
pass (should make Jan–Feb, Feb–Mar, and Jun–Jul reconcile exactly, and
get Mar–Apr/Apr–May/May–Jun close, modulo Bug 4). Bug 4 (Plan It
Instalments Due) is a separate, larger scope addition — needs a product
decision on whether per-transaction `balance` should even attempt to
model Plan It months, or whether it's acceptable to keep logging the
non-blocking reconciliation warning for those specific statements.

## Suggested starting points for investigation (not a plan)

- Page-by-page audit of `_select_amount_block`'s runs against real
  statements — for each mismatching statement, find which page(s) have a
  `runs[-1]` fallback firing (add a temporary log/assert when
  `len(run) != expected_count` at the point of fallback) and inspect that
  page's raw text the way the Jan–Feb case was traced above.
- Consider whether `_select_amount_block` should raise/fail loudly instead
  of silently falling back when no run matches exactly, at least during
  investigation — easier to find every occurrence than to reconstruct them
  from statement-level reconciliation deltas alone.
- The `is_credit = i > 0 and lines[i - 1] == "CR"` check (line 150) is
  itself fragile — it assumes the "CR" marker line is *immediately*
  adjacent to the date-pair it belongs to, with nothing in between. Worth
  checking whether any of the mismatching statements have a CR marker
  separated from its transaction by an intervening boilerplate line,
  causing a payment/credit to be mis-signed as a debit (or vice versa) —
  which would also throw off the running total without necessarily being
  caught by `_select_amount_block`'s run-length check at all.
- Once amounts are trustworthy, revisit the same-day-ordering wrinkle above
  — likely needs a stable per-row sequence number preserved through
  Bronze→Silver rather than relying on `transaction_date` alone for
  "latest" queries.
