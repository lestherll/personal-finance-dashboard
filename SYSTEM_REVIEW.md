# SYSTEM.md Architecture Review

## Critical Issues (Fix Before Implementation)

### 1. **SQL Injection Vulnerability in API Layer**
**Location:** Section 5.1, `get_transactions()` endpoint

```python
# CURRENT (UNSAFE)
query += f" AND account_id = '{account_id}'"
query += f" AND transaction_date >= '{start_date}'"
```

**Problem:** String concatenation allows SQL injection. User could pass `'; DROP TABLE transactions; --` as `category`.

**Fix:** Use parameterized queries (FastAPI + SQLAlchemy ORM):
```python
from sqlalchemy import select
query = select(TransactionLedger).where(
    (TransactionLedger.account_id == account_id),
    (TransactionLedger.transaction_date >= start_date)
)
```

---

### 2. **Account Linkage is Undefined**
**Location:** Section 3.2, Silver transformer

```python
'account_id': None,  # Will be linked later
```

**Problem:** 
- The doc says "Will be linked later" but never explains HOW or WHEN
- Is there a lookup table mapping external_account_id → UUID?
- What if the same bank account appears in multiple files with different naming?
- If account_id stays NULL, foreign key constraint fails

**Impact:** Silver insertion will fail. This is a blocker.

**Fix:** Define account linking rules BEFORE insertion:
- For Monzo: use Monzo's `account_id` field as external_account_id
- For Natwest: use sort_code + account_number
- Upsert into `silver.accounts` first, get UUID, then link transactions

---

### 3. **Incomplete Deduplication Strategy**
**Location:** Section 3.1, Bronze layer

**Problem:**
- `UNIQUE (source_key, source_type)` prevents same record from being inserted twice
- But what if user re-uploads the same file? Natwest source_key includes line_number:
  ```python
  f"natwest_txn_{date}_{amount}_{idx}"  # idx is line number!
  ```
  A re-upload will have different line numbers → treated as new records
- Monzo's source_key uses Transaction ID (good), but Natwest's doesn't (bad)
- The doc mentions "file_hash" but never implements it

**Fix:**
- Add `file_hash VARCHAR` to bronze.raw_file_uploads
- Check hash before insert: if file already uploaded, skip or update
- For Natwest, use deterministic key: `f"natwest_txn_{date}_{amount}_{merchant}"`

---

### 4. **Transaction Balance Derivation is Fragile**
**Location:** Section 3.2, `silver.accounts.latest_balance`

```sql
-- Derived from latest transaction balance
latest_balance DECIMAL(12, 2),
```

**Problem:**
- If Natwest provides a balance field but transactions arrive out-of-order from Monzo, the "latest" balance is ambiguous
- Natwest exports include balance per transaction (good), but Monzo doesn't (missing)
- Recalculating balance by summing transactions requires assuming starting balance = 0
- If you delete a transaction from bronze, balance becomes wrong—immutability breaks

**Fix:** 
- Create an immutable `silver.account_ledger` (append-only) with (date, running_balance)
- Derive `latest_balance` from ledger, not from transactions
- For Natwest: use the balance_per_txn field
- For Monzo: require manual initial balance or external balance snapshot

---

### 5. **Multi-Currency Handling is Missing**
**Location:** Section 3.2, Silver schema

```sql
CREATE TABLE transactions (
    amount_gbp DECIMAL(12, 2) NOT NULL,  -- Always GBP
    ...
);

CREATE TABLE accounts (
    currency VARCHAR DEFAULT 'GBP',  -- Single currency per account
);
```

**Problem:**
- Monzo supports `Local Currency` (EUR, USD, etc.)
- Natwest supports multi-currency
- Schema forces everything to GBP but doesn't show conversion logic
- What exchange rate? What date? This is critical for accuracy

**Fix:**
```sql
-- Store both original and converted
amount_original DECIMAL(12, 2) NOT NULL,
original_currency VARCHAR NOT NULL,
amount_gbp DECIMAL(12, 2) NOT NULL,  -- Converted
exchange_rate DECIMAL(6, 4),
exchange_rate_source VARCHAR,  -- 'provider', 'ecb', 'manual'
```

---

### 6. **Subscription Detection is Hand-Waved**
**Location:** Section 3.3, `gold.recurring_subscriptions` + Section 4

```python
# Detect subscriptions (pattern matching)
```

**Problem:**
- No algorithm defined
- "Pattern matching" is vague
- Do you look for same merchant + same amount + regular intervals?
- What's "confidence_score"? How's it calculated?
- If this is first upload, no history to detect patterns
- What about subscriptions that vary (Netflix with different tiers)?

**Fix:** Define the algorithm:
```python
def detect_recurring(account_id, transaction_history):
    """
    For each merchant:
      1. Group transactions by merchant_name (fuzzy matching needed)
      2. Check if amounts are within 10% of each other
      3. Calculate interval between successive transactions
      4. If interval is consistent (±3 days), flag as recurring
      5. confidence = consistency_score * history_depth
    
    Only flag if:
      - At least 3 occurrences
      - Last occurrence within 60 days (is it still active?)
    """
```

---

### 7. **Adapter Factory Resolution is Ambiguous**
**Location:** Section 2.3, `detect_adapter()`

```python
def detect_adapter(self, file_content: str) -> DataSourceAdapter:
    for adapter in self.adapters:
        if adapter.validate(file_content):
            return adapter  # Returns FIRST match
```

**Problem:**
- If multiple adapters return True (format overlap), first in list wins
- No scoring/confidence mechanism
- Monzo and Natwest both export CSV—could overlap if headers aren't strict

**Fix:**
```python
def detect_adapter(self, file_content: str) -> DataSourceAdapter:
    matches = [(adapter, adapter.validate_score(file_content)) 
               for adapter in self.adapters]
    if not matches:
        raise ValueError("...")
    matches.sort(key=lambda x: x[1], reverse=True)
    if matches[0][1] >= 0.8:  # Confidence threshold
        return matches[0][0]
    raise ValueError(f"Ambiguous format: {[(a, s) for a, s in matches]}")
```

---

### 8. **Holdings Have No Account Linkage**
**Location:** Section 3.2 & 3.3, Holdings tables

```sql
CREATE TABLE holdings (
    id UUID PRIMARY KEY,
    source_type VARCHAR NOT NULL,  -- vanguard, etc.
    isin VARCHAR NOT NULL,
    -- NO account_id field!
);
```

**Problem:**
- Holdings are linked to accounts but schema doesn't reflect this
- If you have multiple Vanguard accounts, which one owns which fund?
- The Claude context API returns holdings without account context

**Fix:**
```sql
CREATE TABLE holdings (
    id UUID PRIMARY KEY,
    account_id UUID NOT NULL,  -- New!
    source_type VARCHAR NOT NULL,
    isin VARCHAR NOT NULL,
    ...
    CONSTRAINT fk_account FOREIGN KEY (account_id) REFERENCES silver.accounts(id)
);
```

---

## Major Architectural Gaps

### 9. **Job Orchestration is Missing**
**Location:** Section 4, "Data Flow"

**Problem:**
- When does Bronze → Silver transformation run?
- Is it immediate (blocking request) or async (background job)?
- What if job crashes partway through? How do we resume?
- No idempotency defined

**Fix:** Define execution model:
```python
# Option A: Synchronous (simple, slow)
POST /ingest → parse → insert_bronze → transform_to_silver → return

# Option B: Async (recommended)
POST /ingest → parse → insert_bronze → return immediately
Celery job: FOR EACH unprocessed bronze record:
  - Try transform_to_silver
  - If success: mark processed=True
  - If failure: log to data_quality_issues, mark processed=True + failed=True
  - Idempotent: if already exists in silver, skip
```

---

### 10. **Transfer Transactions Between Owned Accounts**
**Location:** None—this is completely missing

**Problem:**
- If you transfer £100 from Monzo → Natwest, this appears as:
  - Monzo: -£100 debit
  - Natwest: +£100 credit
- Both are reported separately to the system
- When aggregating net worth, this £100 appears twice
- Need to link them as a single "transfer" event

**Fix:** Add gold layer:
```sql
CREATE TABLE transfers (
    id UUID PRIMARY KEY,
    from_account_id UUID NOT NULL,
    to_account_id UUID NOT NULL,
    amount_gbp DECIMAL(12, 2),
    from_txn_id UUID,  -- Link to gold.transaction_ledger
    to_txn_id UUID,
    detected_date DATE,  -- When we matched them
    confidence_score FLOAT,
    CONSTRAINT fk_from FOREIGN KEY (from_account_id) REFERENCES silver.accounts(id),
    CONSTRAINT fk_to FOREIGN KEY (to_account_id) REFERENCES silver.accounts(id)
);
```

---

### 11. **Pending vs. Posted Transactions**
**Location:** None—missing entirely

**Problem:**
- Credit card transactions are often pending for days before posting
- Current schema has no status field
- If you query "balance as of today", pending transactions might/might not be included

**Fix:**
```sql
ALTER TABLE gold.transaction_ledger ADD COLUMN status VARCHAR;
-- Values: 'posted', 'pending', 'cancelled', 'reversed'
```

---

### 12. **Claude Context Endpoint Lacks Pagination**
**Location:** Section 5.2, `/api/v1/claude-context`

```python
# Last 6 months of transactions
transactions = db.query(f"""
    SELECT * FROM gold.transaction_ledger
    WHERE transaction_date >= '{six_months_ago}'
""")
```

**Problem:**
- If user has 5 years of data, this returns thousands of rows as JSON
- Response is huge; Claude has 200K token limit (but wastes it on verbose JSON)
- No filtering by category/account; everything is included
- No cursor pagination or limit

**Fix:**
```python
@app.get("/api/v1/claude-context")
async def get_claude_context(
    months: int = 6,
    accounts: List[str] = None,
    categories: List[str] = None,
    limit: int = 1000
):
    # More granular; Claude can request specific slices
```

---

## Data Integrity Issues

### 13. **Foreign Key Constraints Too Strict**
**Location:** Section 3.2 & 3.3, `CONSTRAINT fk_account`

**Problem:**
- If an account is deleted (e.g., closed bank account), ON DELETE RESTRICT prevents it
- Immutable audit trail means you can't delete historical data anyway
- These constraints prevent orphaned records, which might be necessary

**Fix:**
```sql
-- Use ON DELETE RESTRICT for now, document the implication:
-- Accounts can never be deleted; only marked is_active = FALSE
CONSTRAINT fk_account FOREIGN KEY (account_id) REFERENCES silver.accounts(id) ON DELETE RESTRICT
```

---

### 14. **No Schema Versioning**
**Location:** Schema design (all layers)

**Problem:**
- If you add a new column to silver.transactions later, old raw_records in bronze won't have it
- Raw data is immutable, so you can't back-fill
- How do you handle version drift?

**Fix:**
- Add `schema_version INT` to all silver/gold tables
- Document migration path
- Keep transformers backward-compatible

---

### 15. **Data Quality Flags as VARCHAR[]**
**Location:** Section 3.2, `data_quality_flags VARCHAR[]`

**Problem:**
- Storing structured data as array of strings is fragile
- Can't query "all transactions with 'missing_time' flag" without array operators
- Unclear what valid flags are

**Fix:**
```sql
-- Create separate table
CREATE TABLE transaction_quality_issues (
    id UUID PRIMARY KEY,
    transaction_id UUID NOT NULL,
    issue_type VARCHAR NOT NULL,  -- 'missing_time', 'negative_balance_jump', etc.
    severity VARCHAR,  -- 'warning', 'error'
    description VARCHAR,
    created_at TIMESTAMP,
    CONSTRAINT fk_txn FOREIGN KEY (transaction_id) REFERENCES silver.transactions(id)
);
```

---

## Security & Compliance Issues

### 16. **No Data Retention Policy**
**Location:** Section 12, Security Considerations

**Problem:**
- How long do you keep raw data?
- GDPR: users can request deletion; do you support this?
- No mention of data archival or purging strategy

**Fix:**
- Define retention: e.g., "Keep bronze + silver for 7 years (tax), archive annually"
- Document GDPR/data deletion process

---

### 17. **JSONB Encryption "TBD"**
**Location:** Section 12

> JSONB fields encrypted at rest: PostgreSQL native encryption or application-level

**Problem:**
- This is too vague; never decided
- Impacts performance and recovery

**Fix:**
- Choose: PostgreSQL native encryption (simpler) OR application-level (more control)
- Document in README

---

### 18. **No API Rate Limiting**
**Location:** Section 5 (APIs)

**Problem:**
- If this becomes a product, API is open to abuse
- No mention of auth, API keys, or rate limits

**Fix:**
```python
from slowapi import Limiter
limiter = Limiter(key_func=get_remote_address)

@app.post("/api/v1/ingest")
@limiter.limit("10/hour")
async def ingest_file(...):
```

---

## Design Issues & Smell Tests

### 19. **Typo: "NatwuestAdapter"**
**Location:** Section 2.2, line 151

Should be "NatwelstAdapter" or wait—is it "Natwest"? Current code says "Natwuest" which is wrong.

---

### 20. **Amex is Marked "To implement"**
**Location:** Section 2.3, line 239

```python
AmexAdapter(),  # To implement
```

But Amex is a core source in the roadmap. This is a red flag—are you committing to V0 or not?

---

### 21. **Vanguard is Holdings, Not Transactions**
**Location:** Section 2.2, VanguardAdapter

**Problem:**
- Vanguard data is holdings (stock/fund positions), not bank transactions
- But the schema treats everything as "transactions"
- This is a domain confusion: transactions are cashflow; holdings are inventory

**Fix:**
- Keep adapters generic, but the parse() method should return different types
- Or separate adapter interface for holdings: `HoldingsAdapter`

---

### 22. **Missing Input Validation in APIs**
**Location:** Section 5.1

```python
async def get_transactions(
    account_id: str = None,
    start_date: date = None,  # Strings? dates? validation?
    end_date: date = None,
):
```

**Problem:**
- No validation: is start_date a valid ISO date?
- No bounds checking: what if start_date > end_date?
- No error messages

**Fix:**
```python
from pydantic import BaseModel, validator

class TransactionQuery(BaseModel):
    account_id: Optional[UUID] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    
    @validator('end_date')
    def end_after_start(cls, v, values):
        if v and 'start_date' in values and v < values['start_date']:
            raise ValueError('end_date must be >= start_date')
        return v
```

---

### 23. **No Indexes Defined**
**Location:** Section 3 (all schema)

**Problem:**
- Schema has no indexes
- Queries like "all transactions in last 6 months by category" will be O(n)
- This will be slow with 10K+ transactions

**Fix:**
```sql
CREATE INDEX idx_txn_date ON gold.transaction_ledger(transaction_date DESC);
CREATE INDEX idx_txn_account_date ON gold.transaction_ledger(account_id, transaction_date DESC);
CREATE INDEX idx_txn_category ON gold.transaction_ledger(category);
CREATE INDEX idx_holding_account ON silver.holdings(account_id, as_of_date DESC);
```

---

### 24. **No Logging/Monitoring for Jobs**
**Location:** Section 4, Bronze→Silver job

**Problem:**
- "Transformation job" is mentioned but zero instrumentation
- If a job fails, how do you know?
- No logs, no metrics, no alerts

**Fix:**
```python
import logging
logger = logging.getLogger(__name__)

try:
    silver_record = SilverTransformer.to_silver(raw_record)
    db.insert_silver(silver_record)
    logger.info(f"✓ Transformed {raw_record.source_key}")
except Exception as e:
    logger.error(f"✗ Failed to transform {raw_record.source_key}: {e}")
    db.log_quality_issue(
        source_key=raw_record.source_key,
        severity='error',
        reason=str(e)
    )
```

---

## Minor Issues & Roadmap Red Flags

### 25. **Unrealistic V0 Timeline**
**Location:** Section 8, Roadmap

> V0 (Weeks 1-2): Core adapter + 4 adapters + PostgreSQL + file upload + transformations + testing

**Reality check:**
- Adapter pattern: 1-2 days
- PostgreSQL setup + schema: 1-2 days
- File upload API: 1 day
- Transformations: 2-3 days
- Testing: 2-3 days
- Realistic: 1.5-2 weeks for one developer (tight)
- Amex adapter: NOT included (marked "to implement")

**Recommendation:** Tighten scope for V0. Ship Monzo + Natwest only. Amex/Vanguard in V0.5.

---

### 26. **Claude Integration is Late (V1)**
**Location:** Section 8, Roadmap

**Problem:**
- Claude context endpoint is core value prop (personalized financial advice)
- Delaying to V1 means you ship data pipeline without LLM until week 3+
- Suggests unclear feature priority

**Recommendation:** Move `/api/v1/claude-context` to V0. It's simple once Gold layer exists.

---

### 27. **Recurring Subscription Detection in V0.5**
**Location:** Section 8, Roadmap

**Problem:**
- Pattern detection requires multi-month history
- If it's V0.5 (first 3 weeks), users don't have enough data yet
- This timing mismatch suggests roadmap isn't user-flow aware

---

## Positive Aspects (Don't Lose These)

✅ **Medallion architecture** is sound—isolation of concerns is great
✅ **Adapter pattern** is extensible
✅ **Immutable bronze** prevents data corruption
✅ **Auditability** via source_key + lineage
✅ **JSONB flexibility** for evolving schemas

---

## Summary Table

| Issue | Severity | Fix Time |
|-------|----------|----------|
| SQL Injection | 🔴 Critical | 2h |
| Account linkage undefined | 🔴 Critical | 4h |
| Deduplication incomplete | 🔴 Critical | 3h |
| Job orchestration missing | 🔴 Critical | 2h |
| Balance derivation fragile | 🟠 Major | 4h |
| Multi-currency handling | 🟠 Major | 3h |
| Subscription detection vague | 🟠 Major | 4h |
| Transfer transactions missing | 🟠 Major | 6h |
| Holdings account linkage | 🟠 Major | 1h |
| No input validation | 🟠 Major | 2h |
| No indexes | 🟡 Minor | 1h |
| No pagination on Claude endpoint | 🟡 Minor | 2h |
| Typo in adapter name | 🟡 Minor | 0.5h |
| Missing logging/monitoring | 🟡 Minor | 3h |

---

## Recommended Action Plan

1. **Before writing ANY code:**
   - Fix critical issues (1-4 above)
   - Define account linking rules
   - Define job orchestration model
   - Resolve SQL injection throughout

2. **During schema design:**
   - Add indexes
   - Add transfer detection table
   - Fix holdings linkage
   - Add status/quality_issues tables

3. **During implementation:**
   - Add input validation to all APIs
   - Add logging/monitoring to jobs
   - Test duplicate/re-upload scenarios
   - Document data retention policy

4. **Roadmap adjustment:**
   - Move Claude context to V0 (low effort, high value)
   - Reduce V0 to Monzo + Natwest only
   - Move Amex/Vanguard to V0.5
   - Add "subscription detection" only if users have 3mo+ data
