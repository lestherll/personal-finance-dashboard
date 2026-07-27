"""Regression tests for adapter-level source-key identity.

Both properties asserted here were absent from the original hand-rolled
per-adapter keys, which used `str(abs(amount_minor))` and truncated the
description to 10 characters. See
adapters/base.py::make_transaction_source_key.
"""

import pytest

from adapters.amex_pdf_adapter import AmexPdfAdapter
from adapters.base import make_snapshot_source_key, make_transaction_source_key
from adapters.chase_pdf_adapter import ChasePdfAdapter
from adapters.first_direct_pdf_adapter import FirstDirectPdfAdapter
from adapters.kroo_pdf_adapter import KrooPdfAdapter
from adapters.monzo_flex_pdf_adapter import MonzoFlexPdfAdapter
from adapters.monzo_pdf_adapter import MonzoPdfAdapter
from adapters.natwest_statement_pdf_adapter import NatwestStatementPdfAdapter
from adapters.natwest_transactions_pdf_adapter import NatwestTransactionsPdfAdapter
from adapters.vanguard_pdf_adapter import VanguardPdfAdapter

# Every PDF adapter, with a date string in the format that adapter emits.
PDF_ADAPTERS = [
    (KrooPdfAdapter, "15 May 2026"),
    (AmexPdfAdapter, "May 15"),
    (ChasePdfAdapter, "15 May 2026"),
    (FirstDirectPdfAdapter, "15 May 26"),
    (MonzoPdfAdapter, "15/05/2026"),
    (MonzoFlexPdfAdapter, "15/05/2026"),
    (NatwestStatementPdfAdapter, "15 May"),
    (NatwestTransactionsPdfAdapter, "15 May"),
    (VanguardPdfAdapter, "15/05/2026"),
]


class TestSignIsPreserved:
    """A £50 payment out and a £50 repayment in, same day, same counterparty,
    must not share a source key - the old `abs()` key made them identical and
    relied entirely on same-file _dup1/_dup2 ordinal suffixing to stay apart."""

    @pytest.mark.parametrize("adapter_cls,date_str", PDF_ADAPTERS)
    def test_debit_and_credit_differ(self, adapter_cls, date_str):
        adapter = adapter_cls()
        debit = {
            "date": date_str,
            "description": "JOHN SMITH TRANSFER",
            "amount_minor": -5000,
        }
        credit = {
            "date": date_str,
            "description": "JOHN SMITH TRANSFER",
            "amount_minor": 5000,
        }

        key_debit = adapter.generate_source_key(debit, 1, "acct1")
        key_credit = adapter.generate_source_key(credit, 2, "acct1")

        assert (
            key_debit != key_credit
        ), f"{adapter_cls.__name__} collapses sign: {key_debit}"


class TestLongDescriptionsDoNotCollide:
    """Two different merchants sharing a long common prefix, same day, same
    amount: the old key truncated the description to 10 characters, so these
    produced an identical key."""

    @pytest.mark.parametrize("adapter_cls,date_str", PDF_ADAPTERS)
    def test_shared_prefix_differs(self, adapter_cls, date_str):
        adapter = adapter_cls()
        a = {
            "date": date_str,
            "description": "AMAZON MARKETPLACE LONDON",
            "amount_minor": -2599,
        }
        b = {
            "date": date_str,
            "description": "AMAZON MARKETPLACE BERLIN",
            "amount_minor": -2599,
        }

        assert adapter.generate_source_key(
            a, 1, "acct1"
        ) != adapter.generate_source_key(
            b, 2, "acct1"
        ), f"{adapter_cls.__name__} collides on a shared 10-char description prefix"


class TestKeysRemainDeterministicAndScoped:
    @pytest.mark.parametrize("adapter_cls,date_str", PDF_ADAPTERS)
    def test_same_input_same_key(self, adapter_cls, date_str):
        adapter = adapter_cls()
        txn = {"date": date_str, "description": "TESCO STORES", "amount_minor": -1250}
        assert adapter.generate_source_key(
            txn, 1, "acct1"
        ) == adapter.generate_source_key(txn, 1, "acct1")

    @pytest.mark.parametrize("adapter_cls,date_str", PDF_ADAPTERS)
    def test_account_identifier_scopes_the_key(self, adapter_cls, date_str):
        """Two accounts of the same source_type (e.g. two Amex cards) must
        never collide - see DataSourceAdapter.generate_source_key contract."""
        adapter = adapter_cls()
        txn = {"date": date_str, "description": "TESCO STORES", "amount_minor": -1250}
        assert adapter.generate_source_key(
            txn, 1, "acct1"
        ) != adapter.generate_source_key(txn, 1, "acct2")

    def test_no_two_adapters_produce_the_same_key(self):
        """Keys must be source-attributable: identical transaction content
        parsed from two different banks' statements must never collide.

        Asserted as cross-adapter uniqueness rather than a prefix match on
        detect_source_type(), because the key prefixes are deliberately not
        identical to source_types (`vanguard-pdf` emits `vanguard_txn`).
        """
        keys = {}
        for adapter_cls, date_str in PDF_ADAPTERS:
            adapter = adapter_cls()
            txn = {
                "date": date_str,
                "description": "TESCO STORES",
                "amount_minor": -1250,
            }
            key = adapter.generate_source_key(txn, 1, "acct1")
            assert (
                key not in keys
            ), f"{adapter_cls.__name__} collides with {keys[key]}: {key}"
            keys[key] = adapter_cls.__name__


class TestSnapshotKeys:
    """Vanguard holdings and Amex Plan-It rows are re-printed snapshots keyed
    by identity + statement date, with no amount - same truncation hazard."""

    def test_vanguard_holdings_with_shared_fund_prefix_differ(self):
        adapter = VanguardPdfAdapter()
        a = {
            "record_type": "holding",
            "fund_name": "FTSE Global All Cap Index Fund Acc",
            "as_of_date": "08 July 2026",
        }
        b = {
            "record_type": "holding",
            "fund_name": "FTSE Global All Cap Index Fund Inc",
            "as_of_date": "08 July 2026",
        }
        assert adapter.generate_source_key(a, 1, "v1") != adapter.generate_source_key(
            b, 2, "v1"
        )

    def test_vanguard_holding_differs_across_statement_dates(self):
        adapter = VanguardPdfAdapter()
        base = {"record_type": "holding", "fund_name": "FTSE Global All Cap"}
        a = {**base, "as_of_date": "08 July 2026"}
        b = {**base, "as_of_date": "08 April 2026"}
        assert adapter.generate_source_key(a, 1, "v1") != adapter.generate_source_key(
            b, 1, "v1"
        )

    def test_amex_plan_it_with_shared_description_prefix_differ(self):
        adapter = AmexPdfAdapter()
        a = {
            "record_type": "plan_it_instalment",
            "start_date": "Apr 12 2026",
            "description": "MALAYSIA AIRLINES KUALA LUMPUR",
            "as_of_date": "19 Jul 2026",
        }
        b = {
            "record_type": "plan_it_instalment",
            "start_date": "Apr 12 2026",
            "description": "MALAYSIA AIRLINES SINGAPORE",
            "as_of_date": "19 Jul 2026",
        }
        assert adapter.generate_source_key(
            a, 1, "amex1"
        ) != adapter.generate_source_key(b, 2, "amex1")

    def test_amex_plan_it_differs_across_statements(self):
        """The same plan re-printed next month must get its own key."""
        adapter = AmexPdfAdapter()
        base = {
            "record_type": "plan_it_instalment",
            "start_date": "Apr 12 2026",
            "description": "MALAYSIA AIRLINES",
        }
        a = {**base, "as_of_date": "19 Jul 2026"}
        b = {**base, "as_of_date": "19 Jun 2026"}
        assert adapter.generate_source_key(
            a, 1, "amex1"
        ) != adapter.generate_source_key(b, 1, "amex1")


class TestHelpersDirectly:
    def test_transaction_key_omits_account_part_when_unmapped(self):
        key = make_transaction_source_key(
            "kroo_txn", "15 May 2026", "TESCO", -100, None
        )
        assert key.startswith("kroo_txn_15May2026_")

    def test_transaction_key_normalizes_both_date_separator_styles(self):
        """'15 May 2026' and '15/05/2026' are different dates, but the
        separator stripping must not itself introduce a collision."""
        slash = make_transaction_source_key("x", "15/05/2026", "A", -1, None)
        space = make_transaction_source_key("x", "15 05 2026", "A", -1, None)
        assert slash == space  # same digits, same date - intended

    def test_empty_description_is_handled(self):
        assert make_transaction_source_key("x", "15 May 2026", "", -1, None)
        assert make_snapshot_source_key("y", "", "08 July 2026", None)
