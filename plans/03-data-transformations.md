# Plan: Data Transformations

## Goal
Implement core transformation logic: account linking, Silver normalization, subscription detection, transfer detection.

## Scope
- Account linking rules
- Silver transformer (per-source normalization)
- Account ledger builder
- Subscription detection algorithm
- Transfer detection algorithm

---

## Phase 1: Account Linking

### File: `transformers/account_linker.py`

**Function: `get_or_create_account(source_type, raw_data) -> UUID`**

```python
def get_or_create_account(source_type: str, raw_data: Dict[str, Any]) -> UUID:
    """
    Map source-specific data to silver.accounts.
    Idempotent: same input always returns same account UUID.
    
    Linking rules:
    - Monzo: external_account_id = raw['account_id']
    - Natwest: external_account_id = f"{sort_code}_{account_number}"
    - Vanguard: external_account_id = raw['account_reference']
    """
```

**Implementation Details:**

```python
# Monzo CSV has column: 'account_id'
MONZO_EXTERNAL_ID_FIELD = 'account_id'

# Natwest CSV has columns: 'Sort Code', 'Account Number'
NATWEST_EXTERNAL_ID_FIELDS = ('Sort Code', 'Account Number')

# Vanguard CSV has column: 'Account Reference'
VANGUARD_EXTERNAL_ID_FIELD = 'Account Reference'

def get_or_create_account(source_type: str, raw_data: Dict[str, Any]) -> UUID:
    # 1. Determine external_account_id based on source
    if source_type == 'monzo':
        if MONZO_EXTERNAL_ID_FIELD not in raw_data:
            raise ValidationError(f"Missing {MONZO_EXTERNAL_ID_FIELD} for Monzo")
        external_id = raw_data[MONZO_EXTERNAL_ID_FIELD]
        account_type = 'checking'  # Monzo is always a checking account
    
    elif source_type == 'natwest':
        sort_code = raw_data.get('Sort Code', '').strip()
        acct_num = raw_data.get('Account Number', '').strip()
        if not sort_code or not acct_num:
            raise ValidationError("Missing Sort Code or Account Number for Natwest")
        external_id = f"{sort_code}_{acct_num}"
        account_type = infer_natwest_account_type(raw_data)
    
    elif source_type == 'vanguard':
        if VANGUARD_EXTERNAL_ID_FIELD not in raw_data:
            raise ValidationError(f"Missing {VANGUARD_EXTERNAL_ID_FIELD} for Vanguard")
        external_id = raw_data[VANGUARD_EXTERNAL_ID_FIELD]
        account_type = 'general_investment'
    
    else:
        raise ValueError(f"Unknown source type: {source_type}")
    
    # 2. Try to find existing account
    session = get_db_session()
    existing_account = session.query(Account).filter(
        Account.source_type == source_type,
        Account.external_account_id == external_id
    ).first()
    
    if existing_account:
        return existing_account.id
    
    # 3. Create new account
    new_account = Account(
        source_type=source_type,
        external_account_id=external_id,
        account_name=external_id,  # Initially; can be updated
        account_type=account_type,
        is_active=True
    )
    
    session.add(new_account)
    session.commit()
    
    logger.info(f"Created new account: {source_type}/{external_id}")
    return new_account.id

def infer_natwest_account_type(raw_data: Dict) -> str:
    """Guess account type from transaction data"""
    # Heuristic: if narrative contains 'ISA', it's likely ISA
    narrative = raw_data.get('Transaction Narrative', '').upper()
    if 'ISA' in narrative:
        return 'investment_isa'
    # Default: checking
    return 'checking'
```

**Tests:**

```python
def test_monzo_account_linking():
    """Monzo transaction links to correct account"""
    raw_data = {'account_id': 'acc_123456'}
    account_id = get_or_create_account('monzo', raw_data)
    assert account_id is not None
    
    # Idempotency: calling again returns same UUID
    account_id_2 = get_or_create_account('monzo', raw_data)
    assert account_id == account_id_2

def test_natwest_account_linking():
    """Natwest uses sort_code + account_number"""
    raw_data = {'Sort Code': '40-44-44', 'Account Number': '12345678'}
    account_id = get_or_create_account('natwest', raw_data)
    
    account = session.query(Account).filter(Account.id == account_id).first()
    assert account.external_account_id == '40-44-44_12345678'

def test_vanguard_account_linking():
    """Vanguard uses account reference"""
    raw_data = {'Account Reference': 'VA123456'}
    account_id = get_or_create_account('vanguard', raw_data)
    
    account = session.query(Account).filter(Account.id == account_id).first()
    assert account.account_type == 'general_investment'

def test_missing_required_field():
    """Linking fails gracefully if required field missing"""
    raw_data = {}  # Missing account_id
    with pytest.raises(ValidationError):
        get_or_create_account('monzo', raw_data)
```

---

## Phase 2: Silver Transformer

### File: `transformers/silver_transformer.py`

**Class: `SilverTransformer`**

```python
class SilverTransformer:
    """Convert RawRecord → Silver normalized schema"""
    
    @staticmethod
    def to_silver(raw_record: RawRecord, account_id: UUID) -> Dict[str, Any]:
        """Dispatch to source-specific normalizer"""
        if raw_record.source_type == 'monzo':
            return SilverTransformer.normalize_monzo(raw_record, account_id)
        elif raw_record.source_type == 'natwest':
            return SilverTransformer.normalize_natwest(raw_record, account_id)
        elif raw_record.source_type == 'vanguard':
            # Holdings, not transactions
            return SilverTransformer.normalize_vanguard_holding(raw_record, account_id)
        else:
            raise ValueError(f"Unknown source: {raw_record.source_type}")
```

**Monzo Normalization:**

```python
@staticmethod
def normalize_monzo(raw_record: RawRecord, account_id: UUID) -> Dict:
    """
    Monzo CSV format:
    - Date: DD/MM/YYYY
    - Time: HH:MM:SS
    - Amount: Signed decimal (positive = credit, negative = debit)
    - Category: Already provided
    - Currency: Always GBP (or Local Currency for foreign)
    """
    data = raw_record.raw_data
    
    # Parse date
    try:
        txn_date = datetime.strptime(data['Date'], '%d/%m/%Y').date()
    except ValueError as e:
        raise ValidationError(f"Invalid Monzo date: {data['Date']}") from e
    
    # Parse time (may be missing)
    txn_time = None
    if data.get('Time'):
        try:
            txn_time = datetime.strptime(data['Time'], '%H:%M:%S').time()
        except ValueError:
            logger.warning(f"Invalid Monzo time: {data['Time']}, skipping")
    
    # Amount and direction
    try:
        amount = float(data['Amount'])
    except (ValueError, TypeError) as e:
        raise ValidationError(f"Invalid Monzo amount: {data['Amount']}") from e
    
    direction = 'credit' if amount > 0 else 'debit'
    amount_abs = abs(amount)
    
    # Currency
    currency = data.get('Currency', 'GBP').upper()
    local_currency = data.get('Local Currency', currency).upper()
    
    # If foreign transaction, store both
    if local_currency != 'GBP':
        amount_original = float(data.get('Local Amount', amount_abs))
        exchange_rate = amount_abs / amount_original if amount_original > 0 else None
    else:
        amount_original = amount_abs
        exchange_rate = None
    
    return {
        'bronze_source_key': raw_record.source_key,
        'source_type': 'monzo',
        'account_id': account_id,
        'transaction_date': txn_date,
        'transaction_time': txn_time,
        'amount_original': amount_original,
        'original_currency': local_currency if local_currency != 'GBP' else 'GBP',
        'amount_gbp': amount_abs,
        'exchange_rate': exchange_rate,
        'exchange_rate_source': 'provider' if local_currency != 'GBP' else 'identity',
        'direction': direction,
        'merchant_name': data.get('Name', '').strip(),
        'merchant_category_code': None,  # Monzo doesn't provide this
        'status': 'posted',  # Monzo exports are posted transactions
        'ingested_at': datetime.now(),
    }
```

**Natwest Normalization:**

```python
@staticmethod
def normalize_natwest(raw_record: RawRecord, account_id: UUID) -> Dict:
    """
    Natwest CSV format:
    - Transaction Date: DD/MM/YYYY (NO TIME)
    - Transaction Amount: Signed decimal
    - Balance: Account balance after transaction (useful!)
    - Transaction Narrative: Free text
    - No categories, no currency field (always GBP)
    """
    data = raw_record.raw_data
    
    # Parse date
    try:
        txn_date = datetime.strptime(data['Transaction Date'], '%d/%m/%Y').date()
    except ValueError as e:
        raise ValidationError(f"Invalid Natwest date: {data['Transaction Date']}") from e
    
    # Amount and direction
    try:
        amount = float(data['Transaction Amount'])
    except (ValueError, TypeError) as e:
        raise ValidationError(f"Invalid Natwest amount: {data['Transaction Amount']}") from e
    
    direction = 'credit' if amount > 0 else 'debit'
    amount_abs = abs(amount)
    
    return {
        'bronze_source_key': raw_record.source_key,
        'source_type': 'natwest',
        'account_id': account_id,
        'transaction_date': txn_date,
        'transaction_time': None,  # Natwest doesn't provide time
        'amount_original': amount_abs,
        'original_currency': 'GBP',
        'amount_gbp': amount_abs,
        'exchange_rate': None,
        'exchange_rate_source': 'identity',
        'direction': direction,
        'merchant_name': data.get('Transaction Narrative', '').strip(),
        'merchant_category_code': None,  # Natwest doesn't provide
        'status': 'posted',
        'ingested_at': datetime.now(),
    }
```

**Vanguard Holdings Normalization:**

```python
@staticmethod
def normalize_vanguard_holding(raw_record: RawRecord, account_id: UUID) -> Dict:
    """
    Vanguard is holdings (not transactions).
    Return structure for silver.holdings table (not transactions).
    
    This returns a different structure than transactions!
    Caller must handle differently (insert into holdings table, not transactions).
    """
    data = raw_record.raw_data
    
    return {
        'record_type': 'holding',  # Flag to caller: use holdings table
        'account_id': account_id,
        'source_type': 'vanguard',
        'isin': data.get('ISIN', '').strip(),
        'fund_name': data.get('Fund Name', '').strip(),
        'quantity': float(data.get('Quantity', 0)),
        'unit_price': float(data.get('Price', 0)),
        'total_value': float(data.get('Value', 0)),
        'as_of_date': date.today(),  # Vanguard doesn't provide date; assume today
        'ingested_at': datetime.now(),
    }
```

**Tests:**

```python
def test_monzo_normalization():
    """Monzo transaction normalized correctly"""
    raw_record = RawRecord(
        source_key='monzo_txn_abc',
        source_type='monzo',
        raw_data={
            'Date': '15/01/2024',
            'Time': '14:30:00',
            'Amount': '-25.50',
            'Name': 'Tesco Groceries',
            'Currency': 'GBP',
            'Category': 'Groceries'
        },
        filename='export.csv',
        file_hash='abc',
        upload_timestamp=datetime.now(),
        line_number=2
    )
    
    account_id = UUID('12345678-1234-5678-1234-567812345678')
    normalized = SilverTransformer.normalize_monzo(raw_record, account_id)
    
    assert normalized['merchant_name'] == 'Tesco Groceries'
    assert normalized['amount_gbp'] == 25.50
    assert normalized['direction'] == 'debit'
    assert normalized['transaction_date'] == date(2024, 1, 15)

def test_natwest_no_time():
    """Natwest transactions don't have time"""
    raw_record = RawRecord(
        source_key='natwest_txn_...',
        source_type='natwest',
        raw_data={
            'Transaction Date': '15/01/2024',
            'Transaction Amount': '-100.00',
            'Transaction Narrative': 'Card Payment',
            'Balance': '500.00'
        },
        filename='export.csv',
        file_hash='abc',
        upload_timestamp=datetime.now(),
        line_number=2
    )
    
    normalized = SilverTransformer.normalize_natwest(raw_record, account_id)
    
    assert normalized['transaction_time'] is None

def test_monzo_foreign_transaction():
    """Monzo foreign transaction stores exchange rate"""
    raw_record = RawRecord(
        source_key='monzo_txn_...',
        source_type='monzo',
        raw_data={
            'Date': '15/01/2024',
            'Time': '14:30:00',
            'Amount': '-50.00',
            'Currency': 'EUR',
            'Local Currency': 'EUR',
            'Local Amount': '-50.00',
            'Name': 'Paris Cafe'
        },
        filename='export.csv',
        file_hash='abc',
        upload_timestamp=datetime.now(),
        line_number=2
    )
    
    normalized = SilverTransformer.normalize_monzo(raw_record, account_id)
    
    assert normalized['original_currency'] == 'EUR'
    assert normalized['exchange_rate'] is not None
```

---

## Phase 3: Account Ledger Builder

### File: `transformers/account_ledger.py`

**Function: `build_account_ledger(account_id, raw_records) -> List[Dict]`**

```python
def build_account_ledger(account_id: UUID, raw_records: List[RawRecord]) -> List[Dict]:
    """
    Extract balance snapshots from raw records.
    
    Only Natwest provides balance; use it.
    Monzo and Vanguard: skip (no balance field).
    """
    ledger_entries = []
    
    for record in raw_records:
        if record.source_type == 'natwest':
            # Natwest has 'Balance' field
            balance_str = record.raw_data.get('Balance')
            if not balance_str:
                continue
            
            try:
                balance = float(balance_str)
            except ValueError:
                logger.warning(f"Invalid Natwest balance: {balance_str}")
                continue
            
            # Extract date
            date_str = record.raw_data.get('Balance Date') or record.raw_data.get('Transaction Date')
            if not date_str:
                continue
            
            try:
                snapshot_date = datetime.strptime(date_str, '%d/%m/%Y').date()
            except ValueError:
                logger.warning(f"Invalid Natwest date: {date_str}")
                continue
            
            ledger_entries.append({
                'account_id': account_id,
                'snapshot_date': snapshot_date,
                'closing_balance': balance,
                'source_type': 'natwest',
                'source_field': 'Balance',
            })
    
    return ledger_entries
```

**Tests:**

```python
def test_natwest_balance_extracted():
    """Natwest balance field extracted correctly"""
    raw_records = [
        RawRecord(
            source_key='natwest_txn_...',
            source_type='natwest',
            raw_data={
                'Transaction Date': '15/01/2024',
                'Balance': '1000.00',
                'Balance Date': '15/01/2024',
                ...
            },
            ...
        )
    ]
    
    ledger = build_account_ledger(account_id, raw_records)
    
    assert len(ledger) == 1
    assert ledger[0]['closing_balance'] == 1000.00
    assert ledger[0]['snapshot_date'] == date(2024, 1, 15)

def test_monzo_no_balance():
    """Monzo records skipped (no balance field)"""
    raw_records = [
        RawRecord(
            source_key='monzo_txn_...',
            source_type='monzo',
            raw_data={...},
            ...
        )
    ]
    
    ledger = build_account_ledger(account_id, raw_records)
    
    assert len(ledger) == 0  # Nothing added
```

---

## Phase 4: Subscription Detection

### File: `transformers/subscription_detector.py`

**Algorithm:**

```
For each account:
  1. Fetch transactions from last N months (default: 6)
  2. Group by (merchant_name, amount ±5%)
  3. For each group with >= 2 occurrences:
     a. Calculate intervals between successive dates
     b. Calculate mean interval and std_dev
     c. If std_dev <= 3 days: flag as subscription
     d. Calculate confidence: min(1.0, occurrences / expected_occurrences)
  4. Only insert if confidence >= 0.6
```

**Implementation:**

```python
def detect_subscriptions(account_id: UUID, lookback_months: int = 6) -> List[Dict]:
    """
    Detect recurring transactions within account.
    Returns list of detected subscriptions (not yet inserted).
    """
    start_date = date.today() - timedelta(days=lookback_months * 30)
    
    # Fetch transactions
    session = get_db_session()
    transactions = session.query(SilverTransaction).filter(
        SilverTransaction.account_id == account_id,
        SilverTransaction.transaction_date >= start_date,
        SilverTransaction.direction == 'debit'  # Usually subscriptions are debits
    ).all()
    
    if len(transactions) < 2:
        return []
    
    # Group by (merchant, amount)
    merchant_groups = {}
    
    for txn in transactions:
        merchant_key = txn.merchant_name or 'Unknown'
        
        if merchant_key not in merchant_groups:
            merchant_groups[merchant_key] = []
        
        merchant_groups[merchant_key].append((txn.transaction_date, txn.amount_gbp))
    
    # Analyze patterns
    detected = []
    
    for merchant, txn_list in merchant_groups.items():
        # Group by amount (within ±5%)
        amount_buckets = group_by_amount_tolerance(txn_list, tolerance=0.05)
        
        for amount, dates in amount_buckets.items():
            if len(dates) < 2:
                continue
            
            dates.sort()
            intervals = [(dates[i+1] - dates[i]).days for i in range(len(dates) - 1)]
            
            avg_interval = sum(intervals) / len(intervals)
            variance = sum((x - avg_interval) ** 2 for x in intervals) / len(intervals)
            std_dev = variance ** 0.5
            
            # Regular if std_dev <= 3 days
            if std_dev > 3:
                continue
            
            # Classify frequency
            expected_frequency = classify_frequency(avg_interval)
            
            # Calculate confidence
            expected_occurrences = lookback_months / frequency_to_months(expected_frequency)
            confidence = min(1.0, len(dates) / max(1, expected_occurrences))
            
            if confidence < 0.6:
                continue
            
            # Next expected date
            next_expected = dates[-1] + timedelta(days=int(avg_interval))
            
            detected.append({
                'account_id': account_id,
                'merchant_name': merchant,
                'amount_gbp': float(amount),
                'amount_tolerance_pct': 5.0,
                'expected_frequency': expected_frequency,
                'first_occurrence': dates[0],
                'last_occurrence': dates[-1],
                'next_expected_date': next_expected,
                'occurrence_count': len(dates),
                'confidence_score': float(confidence),
                'is_active': True,
            })
    
    return detected


def group_by_amount_tolerance(txn_list: List[tuple], tolerance: float = 0.05) -> Dict[float, List[date]]:
    """Group transactions by amount with tolerance"""
    buckets = {}
    
    for txn_date, amount in txn_list:
        # Find matching bucket
        bucket_amount = None
        for existing_amount in buckets.keys():
            if abs(amount - existing_amount) / existing_amount <= tolerance:
                bucket_amount = existing_amount
                break
        
        if bucket_amount is None:
            bucket_amount = amount
            buckets[bucket_amount] = []
        
        buckets[bucket_amount].append(txn_date)
    
    return buckets


def classify_frequency(avg_interval_days: int) -> str:
    """Map interval (in days) to frequency"""
    if 6 <= avg_interval_days <= 8:
        return 'weekly'
    elif 13 <= avg_interval_days <= 15:
        return 'bi-weekly'
    elif 27 <= avg_interval_days <= 32:
        return 'monthly'
    elif 85 <= avg_interval_days <= 95:
        return 'quarterly'
    elif 360 <= avg_interval_days <= 370:
        return 'annual'
    else:
        return 'irregular'


def frequency_to_months(frequency: str) -> float:
    """Convert frequency to months"""
    mapping = {
        'weekly': 0.23,
        'bi-weekly': 0.46,
        'monthly': 1.0,
        'quarterly': 3.0,
        'annual': 12.0,
        'irregular': 0.0,
    }
    return mapping.get(frequency, 0.0)
```

**Tests:**

```python
def test_monthly_subscription_detected():
    """Regular monthly subscription detected"""
    account_id = UUID('...')
    
    # Create 4 monthly transactions (Jan, Feb, Mar, Apr)
    dates = [date(2024, 1, 15), date(2024, 2, 15), date(2024, 3, 15), date(2024, 4, 15)]
    for d in dates:
        create_transaction(account_id, d, -9.99, 'Netflix')
    
    detected = detect_subscriptions(account_id)
    
    assert len(detected) == 1
    sub = detected[0]
    assert sub['merchant_name'] == 'Netflix'
    assert sub['amount_gbp'] == 9.99
    assert sub['expected_frequency'] == 'monthly'
    assert sub['occurrence_count'] == 4

def test_irregular_not_detected():
    """Irregular transactions not flagged"""
    # Create transactions with varying intervals (10, 20, 40 days)
    dates = [date(2024, 1, 1), date(2024, 1, 11), date(2024, 1, 31), date(2024, 3, 12)]
    for d in dates:
        create_transaction(account_id, d, -50.00, 'Irregular Store')
    
    detected = detect_subscriptions(account_id)
    
    assert len(detected) == 0  # Std dev too high

def test_amount_variance_allowed():
    """Subscription with ±5% amount variance detected"""
    # Same merchant, amount between 9.99 and 10.49
    dates = [date(2024, 1, 15), date(2024, 2, 15), date(2024, 3, 15)]
    amounts = [9.99, 10.25, 10.49]
    
    for d, a in zip(dates, amounts):
        create_transaction(account_id, d, -a, 'Variable Subscription')
    
    detected = detect_subscriptions(account_id)
    
    assert len(detected) == 1
```

---

## Phase 5: Transfer Detection

### File: `transformers/transfer_detector.py`

**Function: `detect_transfers(lookback_days=30) -> List[Dict]`**

```python
def detect_transfers(lookback_days: int = 30) -> List[Dict]:
    """
    Find transfers between owned accounts.
    
    Pattern:
    - Debit from AccountA + Credit to AccountB
    - Same amount
    - Within 2 days (max)
    - Merchant names suggest transfer
    """
    start_date = date.today() - timedelta(days=lookback_days)
    
    session = get_db_session()
    
    # Get all accounts (to filter transfers between owned accounts)
    all_accounts = session.query(Account).filter(Account.is_active == True).all()
    account_ids = {acc.id for acc in all_accounts}
    
    # Fetch debits and credits
    debits = session.query(SilverTransaction).filter(
        SilverTransaction.direction == 'debit',
        SilverTransaction.transaction_date >= start_date
    ).all()
    
    credits = session.query(SilverTransaction).filter(
        SilverTransaction.direction == 'credit',
        SilverTransaction.transaction_date >= start_date
    ).all()
    
    transfers = []
    
    for debit in debits:
        for credit in credits:
            # Amount match?
            if debit.amount_gbp != credit.amount_gbp:
                continue
            
            # Within 2 days?
            days_apart = (credit.transaction_date - debit.transaction_date).days
            if not (-2 <= days_apart <= 2):
                continue
            
            # Both from owned accounts?
            if debit.account_id not in account_ids or credit.account_id not in account_ids:
                continue
            
            # Merchant names suggest transfer?
            if not is_transfer_like(debit.merchant_name, credit.merchant_name):
                continue
            
            # Confidence: higher if same day
            confidence = 0.95 if days_apart == 0 else 0.75
            
            transfers.append({
                'from_account_id': debit.account_id,
                'to_account_id': credit.account_id,
                'amount_gbp': float(debit.amount_gbp),
                'from_txn_id': debit.id,
                'to_txn_id': credit.id,
                'confidence_score': confidence,
            })
    
    return transfers


def is_transfer_like(debit_merchant: str, credit_merchant: str) -> bool:
    """Check if transaction names suggest a transfer"""
    keywords = ['transfer', 'payment to', 'send money', 'received from', 'monzo', 'natwest', 'amex']
    
    debit_lower = (debit_merchant or '').lower()
    credit_lower = (credit_merchant or '').lower()
    
    # At least one merchant should mention transfer keywords
    return any(kw in debit_lower or kw in credit_lower for kw in keywords)
```

**Tests:**

```python
def test_transfer_detected():
    """Transfer between two owned accounts detected"""
    # Create accounts
    monzo_account = create_account('monzo', 'monzo_123')
    natwest_account = create_account('natwest', 'natwest_456')
    
    # Create matching debit/credit
    debit = create_transaction(monzo_account, date(2024, 1, 15), -100.00, 'Transfer to Natwest')
    credit = create_transaction(natwest_account, date(2024, 1, 15), 100.00, 'Received from Monzo')
    
    transfers = detect_transfers()
    
    assert len(transfers) == 1
    t = transfers[0]
    assert t['from_account_id'] == monzo_account
    assert t['to_account_id'] == natwest_account
    assert t['amount_gbp'] == 100.00

def test_transfer_within_2_days():
    """Transfers within 2 days detected"""
    # Debit on Jan 15, Credit on Jan 16
    debit = create_transaction(acc1, date(2024, 1, 15), -50.00, 'Transfer')
    credit = create_transaction(acc2, date(2024, 1, 16), 50.00, 'Received')
    
    transfers = detect_transfers()
    
    assert len(transfers) == 1

def test_transfer_beyond_2_days_ignored():
    """Transfers > 2 days apart not flagged"""
    # Debit on Jan 15, Credit on Jan 18 (3 days)
    debit = create_transaction(acc1, date(2024, 1, 15), -50.00, 'Transfer')
    credit = create_transaction(acc2, date(2024, 1, 18), 50.00, 'Received')
    
    transfers = detect_transfers()
    
    assert len(transfers) == 0
```

---

## Success Criteria

✅ Account linking idempotent (same input → same UUID)
✅ All three sources (Monzo, Natwest, Vanguard) normalized
✅ Multi-currency amounts handled (amount_original + amount_gbp)
✅ Account ledger extracted from Natwest balances
✅ Subscriptions detected with confidence scoring
✅ Transfers detected between owned accounts
✅ All transformation logic unit tested
✅ No data loss or corruption during transformations

---

## Next Step
Implement transformation jobs in Celery for Bronze→Silver→Gold pipeline.
