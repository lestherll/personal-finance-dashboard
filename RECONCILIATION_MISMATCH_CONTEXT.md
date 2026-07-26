# Context: Amex/First Direct reconciliation-mismatch handling

## The issue

Amex and First Direct don't print a per-transaction balance — only a single
anchor pair (Previous/New Balance, or Amex's Account Summary box). Their
adapters derive a `balance` for every transaction by rolling that anchor
forward through the signed `amount`s (`_attach_derived_balances` in
`adapters/first_direct_pdf_adapter.py`; the equivalent logic lives in
`adapters/amex_pdf_adapter.py::parse()`).

**The `balance` field is written to every transaction unconditionally,
before the reconciliation check even runs.** The check only compares the
*final* rolled-forward total against the statement's own printed closing
anchor, and if it doesn't match:

- `cli.py ingest` prints an advisory warning (`⚠ balance mismatch: ...`)
- `self.last_reconciliation.matches = False` is persisted to Bronze and
  queryable via `cli.py accounts reconciliation`

Neither of these **stops** anything. The file is still written to Bronze
with its (possibly wrong) derived balances, those balances still flow into
Silver's `account_ledger` via `_ledger_from_amex`/`_ledger_from_firstdirect`
in `transformers/silver_transformer.py`, and `get_current_balances()`/
`get_net_worth()` will use them exactly as if they'd reconciled. Nothing
currently distinguishes a verified balance from an unverified one once it's
in the ledger.

Two other things worth knowing:

- The check validates the aggregate only, not each row: if two transactions
  were swapped, or one dropped while another was duplicated with the same
  net amount, the final total could coincidentally still match while every
  intermediate per-transaction balance is wrong.
- This is exactly the mechanism that caught the real historical Amex bug
  (see `AMEX_BUG_HANDOFF.md`) — before the fix, all 7 real Amex statements
  failed reconciliation, and that's the only reason the bug (wrong
  amount-run selection, wrong credit sign, a regex that broke on a 5th
  balance column, a missing Plan-It component) was ever caught at all: it
  produced "plausible-looking wrong data" with no error otherwise.

## Direction discussed, not yet decided or implemented

Recommendation on the table (not committed to): on a reconciliation
mismatch, **suppress the derived `balance` from `account_ledger` while still
keeping the transactions themselves** (date/description/amount aren't
invalidated by this check — only the aggregate balance is). This means
`get_current_balances()` would fall back to the last *known-good*
reconciling balance instead of silently surfacing a wrong one — stale but
correct, rather than current but wrong. Also discussed: making `cli.py
ingest` exit non-zero on a mismatch (it already does for unrecognized
formats), so it's harder to miss in a batch ingest.

Explicitly rejected: hard-failing the whole ingest on any mismatch. Per the
Amex history above, that would have made the tool completely unable to
ingest Amex data for the entire period the bug existed, even though the
transactions themselves (dates/descriptions/amounts) were mostly fine and
only the derived balance was off.

## Where to look

- `adapters/amex_pdf_adapter.py` (`parse()`, `_extract_account_summary`)
- `adapters/first_direct_pdf_adapter.py` (`_attach_derived_balances`)
- `transformers/silver_transformer.py` (`_ledger_from_amex`,
  `_ledger_from_firstdirect`, `normalize_account_ledger`)
- `transformers/balance.py` (`get_current_balances`, `get_net_worth`)
- `cli.py` (`ingest` command's `had_failure` handling, `_echo_reconciliation`)
- `AMEX_BUG_HANDOFF.md` for the historical incident that makes a hard-fail
  policy risky

No implementation plan has been written yet — this file is just enough
context to start from.
