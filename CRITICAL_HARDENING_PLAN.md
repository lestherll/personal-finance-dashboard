# Critical Hardening Plan: Immutable Ingestion, Identity, and Exact Money

## Purpose and scope

This plan addresses the first three critical correctness gaps identified
before Gold-layer work begins:

1. Bronze can overwrite a different file with the same filename.
2. `bronze_source_key` mixes immutable source identity with an approximate
   transaction-deduplication key.
3. Financial values are persisted as binary floating-point numbers.

The data store is currently empty, so this is the right time to make a
breaking schema change without a production migration. Do not ingest real
statements until these three milestones have passed their acceptance tests.

## Shared design decisions

### Terms

- **Raw artifact:** the original uploaded PDF or CSV bytes, stored unchanged.
- **Ingestion:** one exact raw artifact, identified by its SHA-256 hash.
- **Bronze record:** one parser output row from an ingestion.
- **Silver build:** a reproducible materialization from an explicit set of
  eligible Bronze ingestions.

### Required provenance fields

Every Bronze row must include the following fields. `bronze_record_id` is the
row's immutable primary key; neither a parser change nor a change in parsed
description/date/amount may alter it.

| Field | Rule |
| --- | --- |
| `ingestion_id` | SHA-256 of the original bytes (the file hash) |
| `bronze_record_id` | SHA-256 of `ingestion_id`, `record_type`, and source line/ordinal |
| `source_type` | Adapter's stable source type |
| `record_type` | `transaction`, `holding`, or another declared type |
| `source_ordinal` | One-based parser ordinal within the artifact |
| `parser_version` | Explicit adapter/parser version, bumped for semantic parsing changes |
| `raw_artifact_path` | Content-addressed local raw-artifact location |

`source_key` may remain temporarily for compatibility, but must be renamed
to `legacy_transaction_fingerprint` and must not be used as a Bronze primary
key.

### Monetary representation

All currency quantities must be stored as a signed integer count of minor
units plus ISO currency code. GBP examples: `£12.34 -> 1234`, `-£0.01 -> -1`.

- Transactions: `amount_minor`, `currency`
- Ledger: `balance_minor`, `currency`
- Holdings: `unit_price_minor`, `total_value_minor`, `currency`
- Quantities are not currency; use a declared decimal precision rather than
  `float`.

Parsing must return either an exact value or a structured parsing error.
Invalid/missing text must never become a numeric zero unless `-` is defined
by that source as a genuine zero.

## Milestone 1 — Immutable raw and Bronze ingestion

### Implementation tasks

1. Add an `IngestionManifest` model and an `ingestions` dataset (Parquet or
   JSON is acceptable initially). It records `ingestion_id`, original
   filename, source type, raw artifact path, timestamps, adapter name/version,
   parse status, and record count.
2. Replace filename-based raw storage with
   `data/raw/{source_type}/{sha256}{original_suffix}`. Copy the raw artifact
   before parsing or writing Bronze, via a temporary file and atomic rename.
3. Replace `write_bronze(source_type, filename, df)` with an ingestion-aware
   write. Bronze data should live at
   `data/bronze/{source_type}/{ingestion_id}.parquet`; a duplicate ingestion
   is a no-op after verifying the manifest.
4. Write files through a sibling temporary path and atomically rename only
   after the Parquet file and manifest are valid.
5. Update `read_bronze` to read every content-addressed file and return the
   required provenance fields.
6. Change the CLI sequence to archive → parse → write Bronze → mark manifest
   complete. Parse/write errors must mark the manifest failed without a
   published Bronze file.

### Tests and acceptance criteria

- Two byte-distinct inputs named `statement.pdf` create two raw artifacts and
  two Bronze files.
- Re-ingesting identical bytes changes neither Bronze nor the manifest count.
- A simulated parse/write failure produces no published Bronze file or partial
  manifest entry.
- Every Bronze row joins to exactly one manifest/raw artifact by
  `ingestion_id`.

## Milestone 2 — Stable source identity and rebuildable Silver

### Implementation tasks

1. Add `ingestion_id`, `bronze_record_id`, and `source_ordinal` to `RawRecord`
   and construct them in the common PDF path and the Monzo CSV adapter.
2. Make `bronze_record_id` content-independent: use the raw file hash,
   record type, and parser ordinal. Preserve the old semantic key only as
   diagnostic/matching data while migration is underway.
3. Introduce a separate Silver matching interface:
   - Prefer a bank-provided transaction identifier when available.
   - Otherwise use a full fingerprint including account, normalized date,
     signed minor amount, full normalized description, and occurrence number.
   - Treat ambiguous matches as reviewable, not automatically dropped.
4. Keep all Bronze records. Store Silver provenance as either a
   `bronze_record_id` or a canonical-record-to-source mapping table, so a
   deduplicated Silver transaction remains auditable.
5. Replace merge-with-existing Silver writes with a full rebuild from selected
   eligible ingestion IDs. The build manifest must include parser versions and
   its input ingestion IDs.
6. Add an explicit rebuild command/API; do not require manual deletion of
   Parquet files after parser fixes.

### Tests and acceptance criteria

- Two distinct same-day, same-amount purchases survive even when descriptions
  share a prefix.
- A parser change that changes a date/description leaves old Bronze provenance
  intact and a rebuild produces only the intended current Silver view.
- A re-upload of one exact file contributes no extra Silver transaction.
- A deduplicated cross-statement transaction can be traced to every source
  Bronze record that supplied it.

## Milestone 3 — Exact-money schema and parser migration

### Implementation tasks

1. Add shared parsing utilities that convert statement text to `Decimal`,
   validate scale against currency metadata, and produce integer minor units.
   They must return an error/result type rather than silently returning zero.
2. Update every adapter to build raw parsed monetary fields in minor units.
   Keep the exact source string in `raw_data` for audit/reparse purposes.
3. Update reconciliation math to operate entirely on `Decimal` or integer
   minor units. Persist expected/derived reconciliation values as minor units,
   never `float`.
4. Replace Silver `amount`, `balance`, `unit_price`, and `total_value` fields
   with their `_minor` equivalents. Update account-ledger and net-worth code
   to sum integers and format only at the UI/CLI boundary.
5. Define an explicit treatment for foreign-currency transactions: preserve
   their source currency and minor value; do not aggregate them into GBP until
   an FX policy and rate source exist.
6. Remove float fallbacks such as `_parse_money(...)->0.0`; emit an ingestion
   quality failure for values that cannot be parsed exactly.

### Tests and acceptance criteria

- `10p + 20p` equals exactly `30p` after parser, Parquet, Silver rebuild, and
  net-worth aggregation.
- Values at the boundary (`£0.01`, `-£0.01`, `£1,234,567.89`) round-trip with
  no change.
- Invalid currency text fails/quarantines the ingestion; it cannot become a
  zero-value transaction or holding.
- Reconciliation compares exact minor units and correctly distinguishes a
  one-penny mismatch.
- No financial schema or calculation path uses `float`; enforce this with a
  targeted static test or lint rule.

## Delivery sequence

1. Agree the shared field names and file-layout contract in this document.
2. Implement and test Milestone 1 in one focused change set.
3. Implement Milestone 2 immediately after it; its rebuild model depends on
   immutable ingestion IDs.
4. Implement Milestone 3 against the new Bronze/Silver contracts.
5. Only then begin the quality-gate, snapshot, and atomic-publication tasks
   listed in `TODO.md`.
