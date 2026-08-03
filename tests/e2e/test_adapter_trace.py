"""Adapter transparency traces: run one statement through the *real*
disk-backed pipeline (ingest -> Bronze -> Silver) and dump the exact JSON
shape of a representative transaction at every stage.

Purpose: `CLAUDE.md` describes each layer in prose; this module produces a
concrete, checked-in artifact (`docs/adapter_traces/<source_type>.json`)
that shows it happening to real values. The test both regenerates and
verifies that artifact, so it can't silently drift from what the pipeline
actually produces.

To add a new source_type's trace: copy `TestMonzoFlexTrace`, swap in that
adapter's own synthetic-but-structurally-faithful fixture text (most
adapters already have one in their own `tests/unit/adapters/test_*.py`),
and pick 3-5 transactions that cover its interesting cases (a plain debit,
a plain credit, and whatever edge case that adapter's docstring calls out).

To regenerate the committed JSON after an intentional pipeline change: delete
`docs/adapter_traces/<source_type>.json` and re-run this test - a missing
trace file is written fresh (and the run fails once, on purpose, so you
review the diff before trusting it).
"""

import hashlib
import json
import re
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import fitz
import numpy as np
import pandas as pd
import pytest

from adapters.amex_pdf_adapter import AmexPdfAdapter
from adapters.base import hash_account_identifier
from adapters.factory import AdapterFactory
from models.datalake import DataLake
from models.ingestion import STATUS_COMPLETE, start_ingestion, write_manifest
from tests.unit.adapters.test_monzo_flex_pdf_adapter import SAMPLE_TEXT
from transformers.account_config import register_account, register_source_type_fallback
from transformers.silver_transformer import run_bronze_to_silver

_TRACES_DIR = Path(__file__).resolve().parent.parent.parent / "docs" / "adapter_traces"

_REDACTED = "<non-deterministic: differs on every real run, e.g. wall-clock time or a tmp-dir path>"
_VOLATILE_COLUMNS = {"upload_timestamp", "ingested_at", "raw_artifact_path"}


def _jsonable(value: Any) -> Any:
    """Recursively coerce pandas/numpy/dataclass leftovers to plain JSON types."""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _scrub(row: Dict[str, Any]) -> Dict[str, Any]:
    """Replace columns that vary run-to-run with a fixed, documented placeholder
    so the committed trace is byte-stable, without hiding that the field exists."""
    row = dict(row)
    for key in _VOLATILE_COLUMNS:
        if key in row:
            row[key] = _REDACTED
    return row


def _ingest(datalake: DataLake, pdf_bytes: bytes, filename: str, tmp_dir: Path):
    """Same archive -> parse -> write_bronze flow `cli.py ingest` runs, kept
    inline (not imported from cli.py) so this test exercises the library
    functions directly, matching tests/e2e/test_full_pipeline.py's pattern."""
    filepath = tmp_dir / filename
    filepath.write_bytes(pdf_bytes)
    file_hash = hashlib.sha256(pdf_bytes).hexdigest()
    manifest = start_ingestion(filepath, file_hash)

    factory = AdapterFactory()
    result = factory.ingest(pdf_bytes, filename, file_hash)

    df = pd.DataFrame(
        [
            {
                "source_key": r.source_key,
                "raw_data": r.raw_data,
                "account_identifier": r.account_identifier,
                "record_type": r.record_type,
                "file_hash": r.file_hash,
                "line_number": r.line_number,
                "bronze_record_id": r.bronze_record_id,
                "source_ordinal": r.source_ordinal,
            }
            for r in result.records
        ]
    )
    manifest.source_type = result.source_type
    manifest.adapter = result.adapter
    manifest.parser_version = result.parser_version
    datalake.write_bronze(
        manifest,
        df,
        reconciliation=result.reconciliation,
        statement_period=result.statement_period,
        reconciliations=result.reconciliations,
    )
    manifest.status = STATUS_COMPLETE
    manifest.record_count = len(result.records)
    write_manifest(manifest)
    return result, file_hash


def _assert_trace_matches_committed(trace: Dict[str, Any], filename: str) -> None:
    trace_path = _TRACES_DIR / filename
    if not trace_path.exists():
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text(json.dumps(trace, indent=2) + "\n")
        pytest.fail(
            f"No committed trace yet - wrote a fresh one to {trace_path}. "
            "Review it, then re-run to confirm it's stable."
        )

    committed = json.loads(trace_path.read_text())
    assert trace == committed, (
        "The live pipeline's output no longer matches "
        f"docs/adapter_traces/{filename}. If this change is intentional, review "
        "the diff and overwrite the committed file with this test's freshly "
        "generated `trace` dict."
    )


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    for sub in ("bronze", "silver", "gold", "ingestions", "raw"):
        (tmp_path / sub).mkdir(parents=True)
    monkeypatch.setattr("models.datalake.BRONZE_DIR", tmp_path / "bronze")
    monkeypatch.setattr("models.datalake.SILVER_DIR", tmp_path / "silver")
    monkeypatch.setattr("models.datalake.GOLD_DIR", tmp_path / "gold")
    monkeypatch.setattr(
        "transformers.silver_transformer._SILVER_DIR", tmp_path / "silver"
    )
    monkeypatch.setattr("models.build._DEFAULT_SILVER_DIR", tmp_path / "silver")
    monkeypatch.setattr("models.ingestion.INGESTIONS_DIR", tmp_path / "ingestions")
    monkeypatch.setattr("models.ingestion.RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(
        "adapters.pdf_adapter.PdfAdapter._extract_text",
        staticmethod(lambda content: content.decode("utf-8")),
    )
    acct_path = tmp_path / "account_map.json"
    monkeypatch.setattr("transformers.account_config.ACCOUNT_MAP_PATH", acct_path)
    return DataLake(db_path=str(tmp_path / "test.duckdb")), acct_path


# Newest-first order, matching how MonzoFlexPdfAdapter.parse_transactions
# actually emits them from SAMPLE_TEXT - see that adapter's test file for
# the corresponding unit-level assertions this trace is consistent with.
_TRACED_LABELS = [
    "debit_only_purchase",  # Corner Shop, 30/06/2026
    "foreign_currency_multiline_description",  # Foreign Cafe, 28/06/2026
    "credit_only_payment",  # Monthly payment, 01/06/2026
    "page_boundary_scramble_donor_row",  # Old Purchase, 15/04/2026
    "page_boundary_scramble_victim_row",  # bare 01/04/2026 row
]

_NOTES = {
    "page_boundary_scramble_donor_row": (
        "Known, accepted limitation (see MonzoFlexPdfAdapter._parse_transaction_lines "
        "docstring): a description split across a real PDF's page boundary can land on "
        "the wrong row. This row's own description absorbed the *next* row's trailing "
        "'Corner Bakery' merchant line. Amounts/dates/balances stay correct for both rows."
    ),
    "page_boundary_scramble_victim_row": (
        "The other half of the same scramble: this row's own merchant name ('Corner "
        "Bakery') was lost upstream to page_boundary_scramble_donor_row above, leaving "
        "only its foreign-currency annotation as the description."
    ),
}


class TestMonzoFlexTrace:
    """Traces every transaction in the Monzo Flex sample statement through:

    stage 1 - adapter.parse_transactions() output (per-transaction dict)
    stage 2 - the RawRecord parse() wraps it in
    stage 3 - the row as actually persisted to Bronze Parquet
    stage 4 - the canonical row in Silver's `transactions` table
    stage 5 - the corresponding row in Silver's `account_ledger` table

    plus the whole-file facts (reconciliation, statement period, account
    resolution) that apply to the file rather than any one transaction.
    """

    ACCOUNT_ID = "acc_monzo_flex_credit"
    FILENAME = "monzo_flex_2026-04-01_2026-06-30.pdf"

    def _build_trace(self, isolated, tmp_path) -> Dict[str, Any]:
        datalake, acct_path = isolated
        register_source_type_fallback(
            "monzo-flex", self.ACCOUNT_ID, "Monzo Flex Test", "credit", path=acct_path
        )

        pdf_bytes = SAMPLE_TEXT.encode("utf-8")
        result, file_hash = _ingest(datalake, pdf_bytes, self.FILENAME, tmp_path)

        silver = run_bronze_to_silver(datalake)
        bronze = datalake.read_bronze("monzo-flex")
        assert bronze is not None and len(bronze) == 5

        bronze_by_id = {row["bronze_record_id"]: row for _, row in bronze.iterrows()}
        sources = silver["transaction_sources"]
        silver_id_by_bronze_id = dict(
            zip(sources["bronze_record_id"], sources["silver_transaction_id"])
        )
        txns_by_silver_id = {
            row["silver_transaction_id"]: row
            for _, row in silver["transactions"].iterrows()
        }
        ledger_by_bronze_id = {
            row["bronze_record_id"]: row
            for _, row in silver["account_ledger"].iterrows()
        }

        transactions = []
        for record, label in zip(result.records, _TRACED_LABELS):
            bronze_row = bronze_by_id[record.bronze_record_id]
            silver_id = silver_id_by_bronze_id[record.bronze_record_id]
            entry = {
                "label": label,
                "stage_1_parsed_transaction": _jsonable(record.raw_data),
                "stage_2_raw_record": _jsonable(_scrub(asdict(record))),
                "stage_3_bronze_row": _jsonable(_scrub(bronze_row.to_dict())),
                "stage_4_silver_transaction": _jsonable(
                    _scrub(txns_by_silver_id[silver_id].to_dict())
                ),
                "stage_5_silver_account_ledger_row": _jsonable(
                    _scrub(ledger_by_bronze_id[record.bronze_record_id].to_dict())
                ),
            }
            if label in _NOTES:
                entry["note"] = _NOTES[label]
            transactions.append(entry)

        return {
            "_generated_by": "tests/e2e/test_adapter_trace.py::TestMonzoFlexTrace",
            "source_type": result.source_type,
            "adapter_class": result.adapter,
            "parser_version": result.parser_version,
            "input_file": {
                "filename": self.FILENAME,
                "file_hash_sha256": file_hash,
                "note": (
                    "ingestion_id and every bronze_record_id below are deterministic "
                    "functions of this hash - re-running this exact fixture always "
                    "reproduces the same ids."
                ),
            },
            "account_resolution": {
                "mechanism": "source_type_fallback",
                "account_id": self.ACCOUNT_ID,
                "note": (
                    "Monzo Flex statements print no account identifier anywhere (no "
                    "sort code, account number, or masked digits), so every "
                    "transaction's account_identifier is None. Resolution falls back "
                    "to a single source_type-wide mapping registered via "
                    "'cli.py accounts register-fallback monzo-flex ...', which assumes "
                    "exactly one Flex account in practice."
                ),
            },
            "file_level_facts": {
                "statement_period": _jsonable(asdict(result.statement_period))
                if result.statement_period
                else None,
                "reconciliation": _jsonable(asdict(result.reconciliation))
                if result.reconciliation
                else None,
            },
            "raw_pdf_text": SAMPLE_TEXT,
            "transactions": transactions,
        }

    def test_trace_matches_committed_json(self, isolated, tmp_path):
        trace = self._build_trace(isolated, tmp_path)
        _assert_trace_matches_committed(trace, "monzo-flex.json")

    def test_five_transactions_traced(self, isolated, tmp_path):
        trace = self._build_trace(isolated, tmp_path)
        assert len(trace["transactions"]) == 5
        assert [t["label"] for t in trace["transactions"]] == _TRACED_LABELS

    def test_reconciliation_and_period_captured(self, isolated, tmp_path):
        trace = self._build_trace(isolated, tmp_path)
        assert trace["file_level_facts"]["reconciliation"]["matches"] is True
        assert trace["file_level_facts"]["statement_period"] is not None


# Fake masked card - Amex's own regex only ever sees the "xxxx-xxxxxx-NNNNN"
# shape, never a real number, but this isn't the user's actual card either.
_AMEX_CARD = "xxxx-xxxxxx-99887"

_AMEX_TRACED_LABELS = [
    "debit_purchase",
    "credit_payment_standalone_cr",
    "other_account_transactions_trailing_cr_credit",
]

_AMEX_DESCRIPTION_BY_LABEL = {
    "debit_purchase": "COFFEE SHOP LONDON",
    "credit_payment_standalone_cr": "PAYMENT RECEIVED - THANK YOU",
    "other_account_transactions_trailing_cr_credit": "DELIVEROO",
}

_AMEX_NOTES = {
    "credit_payment_standalone_cr": (
        "Amex marks a credit two different ways depending on section (see "
        "AmexPdfAdapter._is_credit_marked). This is the main-table form: a "
        "standalone 'CR' line immediately after the transaction row."
    ),
    "other_account_transactions_trailing_cr_credit": (
        "The other form: inside 'OTHER ACCOUNT TRANSACTIONS', the 'CR' marker "
        "is a trailing token on the *annotation* line that follows the "
        "transaction, not a standalone line of its own - "
        "'DeliverooGoldBenefit ... CR'. Section membership alone isn't a "
        "reliable credit signal (that section can hold plain debits too, e.g. "
        "a membership fee) - only this CR-marker check decides the sign."
    ),
}


_PDF_ID_RE = re.compile(rb"/ID\[<([0-9A-Fa-f]+)><([0-9A-Fa-f]+)>\]")


def _deterministic_pdf_bytes(raw: bytes) -> bytes:
    """PyMuPDF's `tobytes()` stamps the trailer's `/ID` with two freshly
    random hex strings on every single call, even for byte-identical input -
    the *only* part of the output that varies (verified: re-generating the
    same document twice differs in nothing else). Left alone, that would
    make `file_hash`/`ingestion_id`/every `bronze_record_id` below
    non-reproducible, defeating the whole point of this trace. Substituting
    a fixed placeholder of the same hex-digit length leaves every byte
    offset elsewhere in the file (and thus the xref table) untouched."""
    return _PDF_ID_RE.sub(
        lambda m: b"/ID[<"
        + b"AB" * (len(m.group(1)) // 2)
        + b"><"
        + b"CD" * (len(m.group(2)) // 2)
        + b">]",
        raw,
    )


def _build_amex_pdf_bytes(lines_with_y) -> bytes:
    """A real, minimal PDF built with PyMuPDF's own text-insertion API -
    mirrors tests/unit/adapters/test_amex_pdf_adapter.py's own
    `_build_pdf_bytes` helper. Needed (not a plain-text stub) because
    AmexPdfAdapter overrides `_extract_text` to use PyMuPDF's `sort=True`
    mode, and its balance-anchor lookup (`_extract_account_summary`) always
    reopens the raw PDF bytes directly - see the adapter's class/parse()
    docstrings."""
    doc = fitz.open()
    page = doc.new_page()
    for text, y in lines_with_y:
        page.insert_text((50, y), text)
    return _deterministic_pdf_bytes(doc.tobytes())


def _amex_statement_pdf() -> bytes:
    """Synthetic Account Summary: Previous Closing Balance 100.00, one
    ordinary debit, and both of Amex's two distinct credit-marker forms.
    Previous(100.00) - Credits(20.00 + 5.00) + Debits(3.85) = Closing(78.85),
    reconciling exactly with AmexPdfAdapter.parse()'s independently derived
    rollforward."""
    return _build_amex_pdf_bytes(
        [
            ("American Express", 50),
            ("Preferred Rewards Gold Credit Card", 70),
            ("Mr Test Cardholder", 85),
            (_AMEX_CARD, 100),
            ("From  20 April to 19 May 2026", 115),
            ("Account Summary", 130),
            ("Previous Closing Balance New Credits New Debits Closing Balance", 145),
            ("£100.00 - £25.00 + £3.85 = £78.85", 160),
            ("Apr 19   Apr 19   COFFEE SHOP LONDON                       3.85", 200),
            ("Total new spend transactions for Test                     3.85", 220),
            ("OTHER ACCOUNT TRANSACTIONS", 240),
            ("May 1   May 1   PAYMENT RECEIVED - THANK YOU              20.00", 260),
            ("CR", 280),
            ("May 7   May 7   DELIVEROO                                    5.00", 300),
            ("DeliverooGoldBenefit                                          CR", 320),
            ("Total of other account transactions                       25.00", 340),
            ("Amount  £", 460),
        ]
    )


class TestAmexTrace:
    """Same 5-stage trace as TestMonzoFlexTrace, for a source_type whose
    pipeline differs in every interesting way: a real per-card account
    identifier (hashed, not a source_type-wide fallback), two visually
    distinct credit markers, and a balance that's *derived* by rolling a
    running total forward from the printed "Previous Closing Balance"
    anchor rather than read directly off the page - see
    AmexPdfAdapter.parse() and CLAUDE.md Gotcha #6.

    Also captures a stage the Monzo Flex trace has no equivalent for:
    AmexPdfAdapter.parse() does a *second* pass over parse_transactions()'s
    own output to attach balance_minor, so stage_1 (parse_transactions()
    alone) and stage_2 (RawRecord, post-parse()) genuinely differ here.
    """

    ACCOUNT_ID = "acc_amex_credit_test"
    FILENAME = "amex_2026-04-20_2026-05-19.pdf"

    def _build_trace(self, isolated, tmp_path) -> Dict[str, Any]:
        datalake, acct_path = isolated
        hashed_card = hash_account_identifier(_AMEX_CARD)
        register_account(
            hashed_card, self.ACCOUNT_ID, "Amex Test", "credit", path=acct_path
        )

        pdf_bytes = _amex_statement_pdf()
        # The literal output of the real overridden _extract_text() (PyMuPDF's
        # sort=True mode) against these bytes - not typed statement text.
        raw_text = AmexPdfAdapter._extract_text(pdf_bytes)
        pre_balance_by_description = {
            t["description"]: t for t in AmexPdfAdapter().parse_transactions(raw_text)
        }

        result, file_hash = _ingest(datalake, pdf_bytes, self.FILENAME, tmp_path)

        silver = run_bronze_to_silver(datalake)
        bronze = datalake.read_bronze("amex")
        assert bronze is not None and len(bronze) == 3

        bronze_by_id = {row["bronze_record_id"]: row for _, row in bronze.iterrows()}
        sources = silver["transaction_sources"]
        silver_id_by_bronze_id = dict(
            zip(sources["bronze_record_id"], sources["silver_transaction_id"])
        )
        txns_by_silver_id = {
            row["silver_transaction_id"]: row
            for _, row in silver["transactions"].iterrows()
        }
        ledger_by_bronze_id = {
            row["bronze_record_id"]: row
            for _, row in silver["account_ledger"].iterrows()
        }

        transactions = []
        for label in _AMEX_TRACED_LABELS:
            description = _AMEX_DESCRIPTION_BY_LABEL[label]
            record = next(
                r for r in result.records if r.raw_data["description"] == description
            )
            bronze_row = bronze_by_id[record.bronze_record_id]
            silver_id = silver_id_by_bronze_id[record.bronze_record_id]
            entry = {
                "label": label,
                "stage_1_parsed_transaction": _jsonable(
                    pre_balance_by_description[description]
                ),
                "stage_2_raw_record": _jsonable(_scrub(asdict(record))),
                "stage_3_bronze_row": _jsonable(_scrub(bronze_row.to_dict())),
                "stage_4_silver_transaction": _jsonable(
                    _scrub(txns_by_silver_id[silver_id].to_dict())
                ),
                "stage_5_silver_account_ledger_row": _jsonable(
                    _scrub(ledger_by_bronze_id[record.bronze_record_id].to_dict())
                ),
            }
            if label in _AMEX_NOTES:
                entry["note"] = _AMEX_NOTES[label]
            transactions.append(entry)

        return {
            "_generated_by": "tests/e2e/test_adapter_trace.py::TestAmexTrace",
            "source_type": result.source_type,
            "adapter_class": result.adapter,
            "parser_version": result.parser_version,
            "input_file": {
                "filename": self.FILENAME,
                "file_hash_sha256": file_hash,
                "note": (
                    "ingestion_id and every bronze_record_id below are deterministic "
                    "functions of this hash - re-running this exact fixture always "
                    "reproduces the same ids."
                ),
            },
            "account_resolution": {
                "mechanism": "identifier_lookup",
                "account_id": self.ACCOUNT_ID,
                "hashed_account_identifier": hashed_card,
                "note": (
                    "Unlike Monzo Flex, every Amex statement prints a real masked "
                    f"card number ('{_AMEX_CARD}' here, itself already fake). "
                    "adapters/base.py::hash_account_identifier() SHA256-truncates "
                    "it before it's ever persisted, so only this hash reaches "
                    "Bronze - resolved against 'cli.py accounts register' "
                    "(per-identifier lookup, not a source_type-wide fallback)."
                ),
            },
            "file_level_facts": {
                "statement_period": _jsonable(asdict(result.statement_period))
                if result.statement_period
                else None,
                "reconciliation": _jsonable(asdict(result.reconciliation))
                if result.reconciliation
                else None,
            },
            "raw_pdf_text": raw_text,
            "raw_pdf_text_note": (
                "Unlike the Monzo Flex trace's hand-typed fixture, this is the "
                "literal output of AmexPdfAdapter._extract_text() (PyMuPDF's real "
                "sort=True extraction) run against a real, synthetically-authored "
                "PDF built with fitz.Page.insert_text() - not typed statement "
                "text. Amex's balance-anchor lookup always reopens the raw PDF "
                "bytes directly, so a plain-text fixture can't stand in for it "
                "the way it can for Monzo Flex."
            ),
            "transactions": transactions,
        }

    def test_trace_matches_committed_json(self, isolated, tmp_path):
        trace = self._build_trace(isolated, tmp_path)
        _assert_trace_matches_committed(trace, "amex.json")

    def test_three_transactions_traced(self, isolated, tmp_path):
        trace = self._build_trace(isolated, tmp_path)
        assert len(trace["transactions"]) == 3
        assert [t["label"] for t in trace["transactions"]] == _AMEX_TRACED_LABELS

    def test_reconciliation_and_period_captured(self, isolated, tmp_path):
        trace = self._build_trace(isolated, tmp_path)
        assert trace["file_level_facts"]["reconciliation"]["matches"] is True
        assert trace["file_level_facts"]["statement_period"] is not None
