"""Disk-backed Bronze -> Silver integration test for a specific real-world
scenario: two Natwest quarterly Statements with two overlapping online
"Transactions" exports bridging the gap between them (and beyond the most
recent statement) - the exact shape of files a user uploads in the
"statement -> transactions export -> next statement" workflow described in
NATWEST_TRANSACTIONS_BALANCE_DESIGN.md.

This is intentionally isolated in its own subfolder (`tests/integration/`,
sibling `__init__.py`) rather than folded into `tests/unit/` - the goal is a
dedicated home for narrow, real-scenario regression tests that exercise the
*actual* disk-backed pipeline (real Bronze Parquet writes, a real
`run_bronze_to_silver()` call, real Silver Parquet reads) end-to-end,
distinct from the purely in-memory unit tests elsewhere. Add future
specific-scenario tests as sibling subfolders under `tests/integration/`,
following this same fixture-text -> real Bronze -> real Silver -> assert
shape.

Fixture text mirrors the exact structural templates already validated in
`tests/unit/adapters/test_natwest_statement_pdf_adapter.py` /
`test_natwest_transactions_pdf_adapter.py`, with invented but internally
consistent numbers (each statement's own closing balance reconciles, and
the two statements chain: statement 1's New Balance == statement 2's
Previous Balance).

Scenario timeline (one real account, `acc_natwest_test`):
    Statement 1   14 Nov 2025 - 13 Feb 2026   Previous 800.00 -> New 1,000.00
    Export A      13 Feb 2026 - 13 May 2026   (covers the gap after Stmt 1)
    Statement 2   14 Feb 2026 - 13 May 2026   Previous 1,000.00 -> New 950.00
    Export B      26 Apr 2026 - 26 Jul 2026   (covers the gap after Stmt 2,
                                                and extends past it to "now")

Export A's transaction and one of Export B's transactions each duplicate a
transaction the relevant statement's own table also prints (same date +
amount, different wording) - real-world cross-format overlap, resolved by
`transformers/silver_transformer.py::_dedupe_natwest_cross_format()`.
Export B's second transaction (GYM MEMBERSHIP) has no statement counterpart
at all - it's in the still-uncovered gap after Statement 2, which is
exactly the case `NATWEST_TRANSACTIONS_BALANCE_DESIGN.md` is about.
"""

import pandas as pd
import pytest

from adapters.base import hash_account_identifier
from adapters.factory import AdapterFactory
from models.datalake import DataLake
from models.ingestion import IngestionManifest
from transformers.account_config import register_account
from transformers.balance import get_current_balances
from transformers.coverage import find_coverage_gaps, find_statement_periods
from transformers.silver_transformer import LEDGER_SOURCE_TYPES, run_bronze_to_silver

ACCOUNT_ID = "acc_natwest_test"

_STATEMENT_HEADER = """Account Name
Account No
Sort Code
Page No
MR TEST PERSON
12345678
11-22-33
1 of 1
CURRENT ACCOUNT
Summary"""

STATEMENT_1_TEXT = f"""{_STATEMENT_HEADER}
Statement Date
13 FEB 2026
Period Covered
14 NOV 2025 to 13 FEB 2026
Previous Balance
£800.00
Paid In
£200.00
Withdrawn
£0.00
New Balance
£1,000.00
Date
Description
Paid In(£)
Withdrawn(£)
Balance(£)

14 NOV 2025

BROUGHT FORWARD


800.00

26 DEC

SALARY PAYMENT

200.00

1,000.00
Interest (variable) you currently pay us on overdrawn balances
"""

STATEMENT_2_TEXT = f"""{_STATEMENT_HEADER}
Statement Date
13 MAY 2026
Period Covered
14 FEB 2026 to 13 MAY 2026
Previous Balance
£1,000.00
Paid In
£0.00
Withdrawn
£50.00
New Balance
£950.00
Date
Description
Paid In(£)
Withdrawn(£)
Balance(£)

14 FEB 2026

BROUGHT FORWARD


1,000.00

10 MAR

CARD PURCHASE COFFEE SHOP

10.00

990.00

01 MAY

CARD PURCHASE ONLINE SHOP

40.00

950.00
Interest (variable) you currently pay us on overdrawn balances
"""

_TRANSACTIONS_HEADER = """NatWest
Account details
*****227 · 11-22-33"""

TRANSACTIONS_A_TEXT = f"""{_TRANSACTIONS_HEADER}
From
13/02/2026
To
13/05/2026
Your transactions
Date
Description
Type
Paid in
Paid out
10 Mar
COFFEE SHOP
Mobile/Online Transaction
-£10.00
Downloaded from the NatWest online transactions service.
"""

TRANSACTIONS_B_TEXT = f"""{_TRANSACTIONS_HEADER}
From
26/04/2026
To
26/07/2026
Your transactions
Date
Description
Type
Paid in
Paid out
01 May
ONLINE SHOP
Mobile/Online Transaction
-£40.00
15 Jun
GYM MEMBERSHIP
Direct Debit
-£30.00
Downloaded from the NatWest online transactions service.
"""


@pytest.fixture
def datalake(tmp_path, monkeypatch):
    """A fully isolated DataLake: real Parquet files on disk and a real
    DuckDB connection, rooted under pytest's tmp_path so this never reads
    or writes the user's real (gitignored) data/ directory.

    BRONZE_DIR/SILVER_DIR/GOLD_DIR are imported into models.datalake's own
    namespace at module-load time (`from config import ...`), so patching
    config.BRONZE_DIR wouldn't affect the already-bound name there -
    models.datalake's own attributes must be patched directly. Likewise
    DataLake.__init__'s `db_path` default is bound at function-definition
    time, so it's passed explicitly rather than patched.
    """
    monkeypatch.setattr("models.datalake.BRONZE_DIR", tmp_path / "bronze")
    monkeypatch.setattr("models.datalake.SILVER_DIR", tmp_path / "silver")
    monkeypatch.setattr("models.datalake.GOLD_DIR", tmp_path / "gold")
    monkeypatch.setattr(
        "transformers.silver_transformer._SILVER_DIR", tmp_path / "silver"
    )

    # PdfAdapter.parse()/validate() start from real PDF bytes and call
    # PyMuPDF to extract text - this fixture's "raw data" is plain text
    # standing in for a real PDF (same as every unit test's SAMPLE_TEXT),
    # so the bytes<->PDF boundary is the one thing stubbed here. Everything
    # downstream of it (validate_text, parse_transactions, account
    # identifier hashing, source_key generation, reconciliation, statement
    # period capture) still runs for real, unstubbed.
    monkeypatch.setattr(
        "adapters.pdf_adapter.PdfAdapter._extract_text",
        staticmethod(lambda file_content: file_content.decode("utf-8")),
    )

    account_map_path = tmp_path / "account_map.json"
    monkeypatch.setattr(
        "transformers.account_config.ACCOUNT_MAP_PATH", account_map_path
    )

    dl = DataLake(db_path=str(tmp_path / "test.duckdb"))

    # Identifier-based registration, not source_type fallback - both
    # Natwest adapters DO extract a real account_identifier (unlike, say,
    # Monzo CSV), and find_unmapped_accounts() only consults
    # source_type_fallback when no identifier was extracted at all (see
    # transformers/account_config.py::find_unmapped_accounts - it doesn't
    # fall through to source_type_fallback for an identifier it doesn't
    # recognize, unlike get_account_id(), which does). Registering only a
    # fallback here would leave both source_types looking unmapped to
    # run_bronze_to_silver()'s pre-flight check even though get_account_id()
    # would have resolved them - so the two raw identifiers this fixture
    # text produces are hashed the same way PdfAdapter.parse() does, and
    # registered directly, mirroring how a real Natwest account is
    # registered in this system (`cli.py accounts register <identifier>`).
    statement_identifier = hash_account_identifier("12345678_11-22-33")
    transactions_identifier = hash_account_identifier("*****227_11-22-33")
    register_account(
        statement_identifier,
        ACCOUNT_ID,
        "Test Natwest Account",
        "current",
        path=account_map_path,
    )
    register_account(
        transactions_identifier,
        ACCOUNT_ID,
        "Test Natwest Account",
        "current",
        path=account_map_path,
    )

    yield dl
    dl.close()


def _ingest(datalake, factory, text, filename):
    """Mirrors cli.py's `ingest` command body exactly (detect + parse via
    AdapterFactory, build a Bronze-shaped DataFrame, write_bronze) so this
    exercises the real adapter detection/parsing/reconciliation/period
    capture path - not a shortcut that bypasses the adapters."""
    file_hash = f"testhash_{filename}"
    result = factory.ingest(text.encode(), filename, file_hash)
    records = result.records
    assert records, f"{filename} parsed 0 records - fixture text is broken"

    df = pd.DataFrame(
        [
            {
                "source_key": r.source_key,
                "raw_data": r.raw_data,
                "account_identifier": r.account_identifier,
                "record_type": r.record_type,
                "file_hash": r.file_hash,
                "line_number": r.line_number,
            }
            for r in records
        ]
    )
    manifest = IngestionManifest(
        ingestion_id=file_hash,
        original_filename=filename,
        raw_artifact_path=f"/test/raw/{file_hash}.pdf",
        status="archived",
        created_at="2026-01-01T00:00:00+00:00",
        source_type=result.source_type,
        adapter=result.adapter,
        parser_version=result.parser_version,
    )
    datalake.write_bronze(
        manifest,
        df,
        reconciliation=result.reconciliation,
        statement_period=result.statement_period,
    )
    return result


@pytest.fixture
def ingested(datalake):
    """Ingests all four files (order: both statements interleaved with both
    transactions exports, oldest to newest) and runs the real, full
    Bronze -> Silver pipeline once. Returns the Silver tables dict from
    run_bronze_to_silver() for assertions."""
    factory = AdapterFactory()
    _ingest(datalake, factory, STATEMENT_1_TEXT, "Statement_1_13_Feb_2026.pdf")
    _ingest(datalake, factory, TRANSACTIONS_A_TEXT, "TXN_13052026_13022026.pdf")
    _ingest(datalake, factory, STATEMENT_2_TEXT, "Statement_2_13_May_2026.pdf")
    _ingest(datalake, factory, TRANSACTIONS_B_TEXT, "TXN_26072026_26042026.pdf")

    return run_bronze_to_silver(datalake)


class TestBronzeIngestion:
    """Confirms each file's own per-file B1/B4 self-checks fired correctly
    before Silver even gets involved - if these fail, the fixture text
    itself is wrong, not the cross-file logic under test."""

    def test_both_statements_reconcile_individually(self, datalake):
        factory = AdapterFactory()
        result_1 = _ingest(datalake, factory, STATEMENT_1_TEXT, "stmt1.pdf")
        result_2 = _ingest(datalake, factory, STATEMENT_2_TEXT, "stmt2.pdf")

        assert result_1.reconciliation.matches is True
        assert result_1.reconciliation.expected_closing_minor == 100000
        assert result_2.reconciliation.matches is True
        assert result_2.reconciliation.expected_closing_minor == 95000

    def test_statements_chain_previous_to_new_balance(self, datalake):
        """Statement 1's New Balance must equal Statement 2's Previous
        Balance for this fixture to represent a real, internally
        consistent two-statement timeline - not just two unrelated files
        that happen to reconcile independently."""
        factory = AdapterFactory()
        result_1 = _ingest(datalake, factory, STATEMENT_1_TEXT, "stmt1.pdf")
        result_2 = _ingest(datalake, factory, STATEMENT_2_TEXT, "stmt2.pdf")

        assert (
            result_1.reconciliation.derived_closing_minor
            == result_2.reconciliation.derived_closing_minor + 5000
        )  # net movement in statement 2: -10.00 - 40.00


class TestCoverage:
    def test_no_gaps_across_all_four_files(self, ingested, datalake):
        """The four files' declared periods union to one unbroken span from
        the oldest statement's start to the most recent export's end -
        find_coverage_gaps() must report zero gaps for this account."""
        periods = find_statement_periods(datalake)
        account_periods = periods[periods["account_id"] == ACCOUNT_ID]
        assert len(account_periods) == 4

        gaps = find_coverage_gaps(periods)
        account_gaps = gaps[gaps["account_id"] == ACCOUNT_ID]
        assert account_gaps.empty

    def test_coverage_extends_to_latest_transactions_export(self, ingested, datalake):
        periods = find_statement_periods(datalake)
        account_periods = periods[periods["account_id"] == ACCOUNT_ID]
        assert account_periods["period_to"].max() == pd.Timestamp("2026-07-26")


class TestCrossFormatDedup:
    """The core thing this scenario is designed to exercise:
    _dedupe_natwest_cross_format() collapsing the same real-world
    transaction reported under both source_types down to one row, while
    leaving genuinely unique transactions (on either side) untouched."""

    def test_four_distinct_transactions_survive(self, ingested):
        transactions = ingested["transactions"]
        account_txns = transactions[transactions["account_id"] == ACCOUNT_ID]
        assert len(account_txns) == 4

    def test_statement_row_wins_each_duplicate(self, ingested):
        """COFFEE SHOP and ONLINE SHOP are each reported once by a
        transactions export and once by a statement - the statement's row
        (real balance data) must be the one that survives dedup."""
        transactions = ingested["transactions"]
        account_txns = transactions[transactions["account_id"] == ACCOUNT_ID]

        coffee = account_txns[account_txns["description"].str.contains("COFFEE SHOP")]
        assert len(coffee) == 1
        assert coffee.iloc[0]["source_type"] == "natwest-statement"
        assert coffee.iloc[0]["amount_minor"] == -1000

        online = account_txns[account_txns["description"].str.contains("ONLINE SHOP")]
        assert len(online) == 1
        assert online.iloc[0]["source_type"] == "natwest-statement"
        assert online.iloc[0]["amount_minor"] == -4000

    def test_unmatched_transactions_export_row_survives(self, ingested):
        """GYM MEMBERSHIP has no statement counterpart at all (it's in the
        gap after the most recent statement) - must survive untouched,
        still tagged as coming from natwest-transactions."""
        transactions = ingested["transactions"]
        account_txns = transactions[transactions["account_id"] == ACCOUNT_ID]

        gym = account_txns[account_txns["description"].str.contains("GYM")]
        assert len(gym) == 1
        assert gym.iloc[0]["source_type"] == "natwest-transactions"
        assert gym.iloc[0]["amount_minor"] == -3000

    def test_non_overlapping_statement_row_survives(self, ingested):
        """SALARY PAYMENT is only ever in Statement 1 - no transactions
        export covers Nov/Dec 2025 - so it must be completely unaffected
        by cross-format dedup."""
        transactions = ingested["transactions"]
        account_txns = transactions[transactions["account_id"] == ACCOUNT_ID]

        salary = account_txns[account_txns["description"].str.contains("SALARY")]
        assert len(salary) == 1
        assert salary.iloc[0]["source_type"] == "natwest-statement"
        assert salary.iloc[0]["amount_minor"] == 20000


class TestCurrentBalanceKnownLimitation:
    """Documents present behavior, not desired behavior - see
    NATWEST_TRANSACTIONS_BALANCE_DESIGN.md. `natwest-transactions` still
    isn't in LEDGER_SOURCE_TYPES, so even though this scenario has fully
    contiguous transaction coverage all the way to 2026-07-26 (Export B),
    the ledger-derived "current balance" is still stuck at the most recent
    *statement's* own closing balance and date. If this test starts
    failing because the derived-rollforward design got implemented, that's
    good news - update these assertions to match the new, better behavior
    instead of trying to preserve them.
    """

    def test_natwest_transactions_still_excluded_from_ledger(self):
        assert "natwest-transactions" not in LEDGER_SOURCE_TYPES

    def test_current_balance_stuck_at_last_statement_not_last_transaction(
        self, ingested, datalake
    ):
        balances = get_current_balances(datalake)
        account_balance = balances[balances["account_id"] == ACCOUNT_ID].iloc[0]

        # Statement 2's own closing balance/date - correct, but stale: it
        # predates Export B's GYM MEMBERSHIP transaction by over a month.
        assert account_balance["balance_minor"] == 95000
        assert account_balance["as_of_date"] == pd.Timestamp("2026-05-13")
