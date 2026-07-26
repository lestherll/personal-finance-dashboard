# Context: two Vanguard-related gaps found while checking `cli.py accounts coverage`

**Status: both fixed.** `transformers/coverage.py::find_statement_periods()` now dedupes on
`["filename", "account_identifier"]`, and `transformers/balance.py::get_net_worth_breakdown()`
now reads `holding_row.as_of_date` instead of hardcoding `None`. Both changes are covered by
new regression tests (`test_multi_wrapper_statement_keeps_both_accounts` in
`tests/unit/transformers/test_coverage.py`, `test_holdings_as_of_date_is_populated_not_blank`
in `tests/unit/transformers/test_balance.py`) and verified against real data. See
`VANGUARD_ACCOUNT_SUMMARY_AND_PAYMENTS_IN_GAPS.md` for two further Vanguard gaps found
during the same investigation (not fixed - deliberately deferred/handed off).

## 1. `cli.py accounts coverage` silently drops Vanguard Pension

**Where:** `transformers/coverage.py::find_statement_periods()`

```python
per_file = df.dropna(
    subset=["statement_period_from", "statement_period_to"]
).drop_duplicates("filename")
```

This dedupes by `filename` alone. That's fine for every other PDF source_type
(one file = one account), but Vanguard's PDF statement legitimately bundles
*two* accounts under one file - confirmed against real Bronze data:

```
                 filename account_identifier statement_period_from statement_period_to
0  Vanguard_Statement.pdf       992add198186 (ISA)      2026-04-09          2026-07-08
2  Vanguard_Statement.pdf       e1b3455b82b0 (Pension)  2026-04-09          2026-07-08
```

`drop_duplicates("filename")` keeps only the first row (whichever
`account_identifier` happens to sort first in the DataFrame - currently the
ISA), so `acc_vanguard_pension` never appears in `cli.py accounts coverage`
output at all, regardless of whether it's actually covered. **This is not a
real coverage gap** - Bronze has the period data for Pension too, same as
ISA - it's purely a reporting bug in the dedup key.

**Fixed:** dedupes on `["filename", "account_identifier"]` instead of `"filename"`
alone. `find_coverage_gaps()` needed no change - it just consumes whatever rows
`find_statement_periods()` returns, so it started seeing Pension's periods automatically.

**Where to look:** `transformers/coverage.py::find_statement_periods()`
(the `drop_duplicates` call), `adapters/vanguard_pdf_adapter.py` (confirms
one file produces both account_identifiers), `cli.py`'s `coverage` command.

## 2. `as_of_date` is always blank for holdings in `get_net_worth_breakdown()`

**Where:** `transformers/balance.py::get_net_worth_breakdown()`, the
holdings-row loop:

```python
for holding_row in holdings.itertuples():
    value = Decimal(str(holding_row.total_value))
    rows.append(
        {
            "account_id": holding_row.account_id,
            "source": holding_row.fund_name,
            "balance_or_value": holding_row.total_value,
            "as_of_date": None,          # <- hardcoded, never reads holding_row.as_of_date
            "contribution_to_net_worth": value,
            "balance_may_be_stale": False,
        }
    )
```

`as_of_date` is hardcoded to `None` unconditionally. But Silver's `holdings`
table (built by `normalize_holdings()`) already carries a real, populated
`as_of_date` per row - confirmed against real data:

```
             account_id                                             fund_name  total_value as_of_date
      acc_vanguard_isa  Vanguard FTSE Global All Cap Index Fund Accumulation      1630.28 2026-07-08
      acc_vanguard_isa                                          Cash account         0.15 2026-07-08
  acc_vanguard_pension   Vanguard Target Retirement 2060 Fund - Accumulation      3309.42 2026-07-08
  acc_vanguard_pension                                          Cash account         1.00 2026-07-08
```

`holding_row.as_of_date` is available and correct - it's just never read.
This has been the case since `get_net_worth_breakdown()` was first added
(commit `64d06da`), so it's not something the reconciliation-mismatch fix
introduced or touched.

**Effect:** every holdings row (currently: all Vanguard ISA/Pension funds -
the only source that produces holdings at all) shows a blank/`NaT`
`as_of_date` in `cli.py accounts breakdown`, and since the breakdown sorts
by `as_of_date` descending (`na_position="last"`), all holdings rows sink to
the bottom of the output regardless of how current their valuation actually
is - visible directly in a real `cli.py accounts breakdown` run.

**Fixed:** uses `holding_row.as_of_date` instead of the hardcoded `None`.
`balance_may_be_stale`-style staleness logic was left out of scope for holdings, consistent
with holdings having no reconciliation concept at all (Gotcha #6/#17) - the holdings loop
still sets `balance_may_be_stale` to a fixed `False` for every row.

**Where to look:** `transformers/balance.py::get_net_worth_breakdown()`
(the holdings loop), `transformers/silver_transformer.py::normalize_holdings()`
/ `_normalize_vanguard_pdf_holding()` (confirms `as_of_date` is populated
upstream).
