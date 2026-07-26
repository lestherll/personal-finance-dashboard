"""Cross-checks the source_type "which adapters get this guarantee" registries
that are otherwise maintained as separately-edited, parallel sets across four
files:

- transformers/reconciliation_status.py::_RECONCILIATION_SOURCE_TYPES
- transformers/coverage.py::_PERIOD_SOURCE_TYPES
- transformers/silver_transformer.py::LEDGER_SOURCE_TYPES/TRANSACTION_SOURCE_TYPES
  (and their _LEDGER_NORMALIZERS/_TRANSACTION_NORMALIZERS dicts)
- transformers/balance.py::_REVERSE_CHRONOLOGICAL_SOURCE_TYPES

Nothing else keeps these in sync with what each adapter actually does - the
monzo-flex adapter genuinely implemented reconciliation and statement-period
capture, but wasn't added to _RECONCILIATION_SOURCE_TYPES/_PERIOD_SOURCE_TYPES,
and nothing failed: `cli.py accounts reconciliation`/`coverage` just silently
never showed it. This module exists so that class of drift fails a test
instead of quietly producing a wrong CLI/query result.
"""

import importlib
import inspect

import pytest
from dateutil import parser as date_parser

from adapters.factory import AdapterFactory
from transformers.balance import _REVERSE_CHRONOLOGICAL_SOURCE_TYPES
from transformers.coverage import _PERIOD_SOURCE_TYPES
from transformers.reconciliation_status import _RECONCILIATION_SOURCE_TYPES
from transformers.silver_transformer import (
    _LEDGER_NORMALIZERS,
    _TRANSACTION_NORMALIZERS,
    LEDGER_SOURCE_TYPES,
    TRANSACTION_SOURCE_TYPES,
)

_RECONCILIATION_MARKER = "self.last_reconciliation = ReconciliationResult("
_STATEMENT_PERIOD_MARKER = "self.last_statement_period = StatementPeriod("


def _pdf_adapters():
    return AdapterFactory().pdf_adapters


def _adapter_ids(adapter):
    return adapter.detect_source_type()


class TestReconciliationRegistration:
    """An adapter "implements reconciliation" iff its class source ever sets
    self.last_reconciliation = ReconciliationResult(...) somewhere (any
    method - Amex's lives in an overridden parse(), not parse_transactions()).
    That must match _RECONCILIATION_SOURCE_TYPES exactly, both directions:
    implements-but-unregistered silently hides real data (the monzo-flex
    bug); registered-but-not-implemented would silently query a source that
    can never produce a result."""

    @pytest.mark.parametrize("adapter", _pdf_adapters(), ids=_adapter_ids)
    def test_registration_matches_implementation(self, adapter):
        source_type = adapter.detect_source_type()
        implements = _RECONCILIATION_MARKER in inspect.getsource(type(adapter))
        registered = source_type in _RECONCILIATION_SOURCE_TYPES
        assert implements == registered, (
            f"{source_type}: implements reconciliation={implements} but "
            f"registered in _RECONCILIATION_SOURCE_TYPES={registered} - "
            "these must match (transformers/reconciliation_status.py)"
        )


class TestStatementPeriodRegistration:
    """Same bidirectional check as above, for statement-period capture
    (B4) against transformers/coverage.py::_PERIOD_SOURCE_TYPES."""

    @pytest.mark.parametrize("adapter", _pdf_adapters(), ids=_adapter_ids)
    def test_registration_matches_implementation(self, adapter):
        source_type = adapter.detect_source_type()
        implements = _STATEMENT_PERIOD_MARKER in inspect.getsource(type(adapter))
        registered = source_type in _PERIOD_SOURCE_TYPES
        assert implements == registered, (
            f"{source_type}: implements statement-period capture="
            f"{implements} but registered in _PERIOD_SOURCE_TYPES="
            f"{registered} - these must match (transformers/coverage.py)"
        )


class TestNormalizerSetConsistency:
    """LEDGER_SOURCE_TYPES/TRANSACTION_SOURCE_TYPES and their normalizer
    dicts are two independent places to list the same source_types within
    silver_transformer.py itself - a mismatch here means either a
    source_type with no normalizer function (KeyError risk) or a normalizer
    function nothing ever calls (dead code silently excluded)."""

    def test_transaction_source_types_match_normalizers(self):
        assert TRANSACTION_SOURCE_TYPES == set(_TRANSACTION_NORMALIZERS)

    def test_ledger_source_types_match_normalizers(self):
        assert LEDGER_SOURCE_TYPES == set(_LEDGER_NORMALIZERS)


class TestReverseChronologicalRegistration:
    """Whether an adapter's own parse order is newest-first is a genuine
    runtime/data property (see commit 419c097 - the original Monzo PDF bug),
    not something greppable from source. This dynamically parses each
    adapter's own existing test fixtures (no new fixtures needed) and infers
    direction from the actual dates returned, asserting it against
    transformers/balance.py::_REVERSE_CHRONOLOGICAL_SOURCE_TYPES. Only
    asserts when a fixture gives an unambiguous (strictly monotonic,
    multi-date) example - fixtures with too few distinct dates are skipped,
    not treated as a pass.
    """

    def test_registration_matches_actual_parse_order(self):
        checked = {}
        for adapter in _pdf_adapters():
            source_type = adapter.detect_source_type()
            test_module_name = adapter.__class__.__module__.replace(
                "adapters.", "tests.unit.adapters.test_", 1
            )
            try:
                test_module = importlib.import_module(test_module_name)
            except ImportError:
                continue

            direction = self._detect_direction(adapter, test_module)
            if direction is None:
                continue

            checked[source_type] = direction
            is_reverse = source_type in _REVERSE_CHRONOLOGICAL_SOURCE_TYPES
            if direction == "descending":
                assert is_reverse, (
                    f"{source_type}: its own test fixture parses with dates "
                    "in descending order (newest-first) but it's not in "
                    "_REVERSE_CHRONOLOGICAL_SOURCE_TYPES - "
                    "get_current_balances()'s same-day tie-break would pick "
                    "the wrong row (see commit 419c097)"
                )
            else:
                assert not is_reverse, (
                    f"{source_type}: its own test fixture parses with dates "
                    "in ascending order but it IS in "
                    "_REVERSE_CHRONOLOGICAL_SOURCE_TYPES - remove it, or "
                    "the same-day tie-break will pick the wrong row"
                )

        # Guard against this silently checking nothing: the two known
        # reverse-chronological sources must always be conclusively
        # verified, or this test has gone blind.
        assert checked.get("monzo-pdf") == "descending"
        assert checked.get("monzo-flex") == "descending"

    @staticmethod
    def _detect_direction(adapter, test_module):
        candidates = [
            name
            for name in dir(test_module)
            if name.isupper()
            and isinstance(getattr(test_module, name), str)
            and len(getattr(test_module, name)) > 100
        ]
        for name in candidates:
            text = getattr(test_module, name)
            try:
                txns = adapter.parse_transactions(text)
            except Exception:
                continue
            # Vanguard PDF's parse_transactions() returns both "transaction"
            # and "holding" dicts in one list - holdings have no "date" key
            # (they use "as_of_date" instead) and aren't part of the
            # chronological transaction table this check cares about.
            txns = [t for t in txns if t.get("record_type", "transaction") == "transaction"]
            if len(txns) < 2:
                continue

            dates = []
            for txn in txns:
                try:
                    dates.append(date_parser.parse(txn["date"], dayfirst=True))
                except (KeyError, ValueError, TypeError, OverflowError):
                    dates = []
                    break
            if len(dates) < 2 or len(set(dates)) < 2:
                continue

            # Non-strict (>=/<=) since same-day transactions are common and
            # legitimately tie - only the overall trend needs to be
            # unambiguous, not every consecutive pair distinct.
            if all(a >= b for a, b in zip(dates, dates[1:])):
                return "descending"
            if all(a <= b for a, b in zip(dates, dates[1:])):
                return "ascending"
        return None
