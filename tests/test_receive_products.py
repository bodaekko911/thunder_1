"""Tests for the Receive Products feature.

Covers:
  - ReceiptCreate schema validation (pure Python, no DB)
  - create_receipt: stock increase, StockMove creation
  - create_receipt: no expense when unit_cost is absent/zero
  - create_receipt: expense + journal created when unit_cost > 0
  - create_receipt: raises 404 when product not found
  - list_receipts: returns empty result set
"""
import asyncio
from datetime import date

import pytest

from app.models.accounting import Account, Journal, JournalEntry
from app.models.expense import Expense, ExpenseCategory
from app.models.inventory import StockMove
from app.models.product import Product
from app.models.receipt import ProductReceipt
from app.models.user import User
from app.services.receive_service import (
    BatchReceiptCreate,
    BatchReceiptItem,
    ReceiptCreate,
    create_receipt,
    create_receipt_batch,
    list_receipts,
)


# ── Fake infrastructure ───────────────────────────────────────────────────────

class FakeScalarResult:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value if isinstance(self._value, list) else []

    def scalar(self):
        return self._value


class FakeReceiveSession:
    """
    Queue-based fake AsyncSession for testing receive_service.

    Responses are consumed in the order execute() is called.
    Every object add()ed gets an auto-assigned id if it doesn't have one.
    """

    def __init__(self, responses: list):
        self._responses = list(responses)
        self._call_idx  = 0
        self.added: list = []
        self.flush_count = 0
        self.committed   = False

    async def execute(self, _stmt):
        if self._call_idx < len(self._responses):
            val = self._responses[self._call_idx]
            self._call_idx += 1
            if isinstance(val, FakeScalarResult):
                return val
            return FakeScalarResult(val)
        return FakeScalarResult(None)

    def add(self, obj):
        self.added.append(obj)
        if getattr(obj, "id", None) is None:
            obj.id = len(self.added)

    async def flush(self):
        self.flush_count += 1
        # Ensure all added objects have IDs (simulates DB autoincrement).
        for i, obj in enumerate(self.added, start=1):
            if getattr(obj, "id", None) is None:
                obj.id = i

    async def commit(self):
        self.committed = True

    async def refresh(self, _obj):
        pass


def _make_product(**kwargs) -> Product:
    defaults = dict(id=1, sku="SKU-001", name="Olive Oil", unit="ltr", stock=10, cost=5)
    defaults.update(kwargs)
    return Product(**defaults)


def _make_user(**kwargs) -> User:
    defaults = dict(id=99, name="Test User", role="admin")
    defaults.update(kwargs)
    return User(**defaults)


# ── Schema validation ─────────────────────────────────────────────────────────

def test_receipt_create_rejects_zero_qty():
    with pytest.raises(Exception):
        ReceiptCreate(product_id=1, qty=0, receive_date=date.today())


def test_receipt_create_rejects_negative_qty():
    with pytest.raises(Exception):
        ReceiptCreate(product_id=1, qty=-5, receive_date=date.today())


def test_receipt_create_rejects_negative_cost():
    with pytest.raises(Exception):
        ReceiptCreate(product_id=1, qty=5, unit_cost=-1, receive_date=date.today())


def test_receipt_create_rejects_zero_product_id():
    with pytest.raises(Exception):
        ReceiptCreate(product_id=0, qty=5, receive_date=date.today())


def test_receipt_create_accepts_no_cost():
    r = ReceiptCreate(product_id=1, qty=3.5, receive_date=date.today())
    assert r.unit_cost is None


def test_receipt_create_accepts_zero_cost_as_no_expense():
    # unit_cost=0 is technically ≥ 0 per schema, treated as "no expense" by service
    r = ReceiptCreate(product_id=1, qty=2, unit_cost=0, receive_date=date.today())
    assert r.unit_cost == 0


# ── create_receipt: no-cost path ──────────────────────────────────────────────

def test_create_receipt_increases_stock():
    product = _make_product(stock=10)
    user    = _make_user()

    # execute() call sequence (no-cost path):
    #   1. select(Product)              → product
    #   2. select(max(ProductReceipt.id)) → None  (first receipt)
    db = FakeReceiveSession([
        FakeScalarResult(product),
        FakeScalarResult(None),
    ])

    data   = ReceiptCreate(product_id=1, qty=5, receive_date=date(2026, 4, 13))
    result = asyncio.run(create_receipt(db, data, user))

    assert result["qty"] == 5.0
    assert result["ref_number"] == "RCV-00001"
    assert result["expense_id"]  is None
    assert result["expense_ref"] is None
    assert float(product.stock) == 15.0
    assert db.committed is True


def test_create_receipt_creates_stock_move():
    product = _make_product(stock=20)
    user    = _make_user()

    db = FakeReceiveSession([
        FakeScalarResult(product),
        FakeScalarResult(None),
    ])

    data = ReceiptCreate(product_id=1, qty=3, receive_date=date(2026, 4, 13))
    asyncio.run(create_receipt(db, data, user))

    moves = [o for o in db.added if isinstance(o, StockMove)]
    assert len(moves) == 1
    move = moves[0]
    assert move.type     == "in"
    assert move.ref_type == "receipt"
    assert float(move.qty)        == 3.0
    assert float(move.qty_before) == 20.0
    assert float(move.qty_after)  == 23.0


def test_create_receipt_no_expense_when_no_cost():
    product = _make_product(stock=5)
    user    = _make_user()

    db = FakeReceiveSession([
        FakeScalarResult(product),
        FakeScalarResult(None),
    ])

    data = ReceiptCreate(product_id=1, qty=2, receive_date=date(2026, 4, 13))
    asyncio.run(create_receipt(db, data, user))

    expenses = [o for o in db.added if isinstance(o, Expense)]
    journals  = [o for o in db.added if isinstance(o, Journal)]
    assert expenses == []
    assert journals  == []


def test_create_receipt_no_expense_when_cost_is_zero():
    product = _make_product(stock=5)
    user    = _make_user()

    db = FakeReceiveSession([
        FakeScalarResult(product),
        FakeScalarResult(None),
    ])

    data = ReceiptCreate(product_id=1, qty=2, unit_cost=0, receive_date=date(2026, 4, 13))
    asyncio.run(create_receipt(db, data, user))

    expenses = [o for o in db.added if isinstance(o, Expense)]
    assert expenses == []


def test_create_receipt_updates_product_cost():
    product = _make_product(stock=10, cost=4.00)
    user    = _make_user()

    db = FakeReceiveSession([
        FakeScalarResult(product),
        FakeScalarResult(None),
    ])

    data = ReceiptCreate(product_id=1, qty=5, unit_cost=6.50, receive_date=date(2026, 4, 13),
                         supplier_ref="Acme", notes="Bulk order")
    asyncio.run(create_receipt(db, data, user))

    # cost should be updated to the received unit cost
    from decimal import Decimal
    assert product.cost == Decimal("6.50")


# ── create_receipt: with-cost path ───────────────────────────────────────────

def test_create_receipt_with_cost_creates_expense_and_journal():
    product  = _make_product(stock=0)
    user     = _make_user()

    # Pre-existing stock purchase category (avoids category-creation branch)
    category = ExpenseCategory(id=11, name="Stock Purchase", account_code="5011", is_active="1")
    exp_acc  = Account(id=20, code="5011", name="Stock Purchase", type="expense", balance=0)
    cash_acc = Account(id=21, code="1000", name="Cash", type="asset", balance=100)

    # execute() call sequence (with-cost, category already exists):
    #   1. select(Product)                → product
    #   2. select(max(ProductReceipt.id)) → None
    #   3. select(ExpenseCategory 5011)   → category  (found, skip creation)
    #   4. select(max(Expense.id))        → None      (first expense)
    #   5. select(Account 5011)           → exp_acc
    #   6. select(Account 1000)           → cash_acc
    db = FakeReceiveSession([
        FakeScalarResult(product),
        FakeScalarResult(None),
        FakeScalarResult(category),
        FakeScalarResult(None),
        FakeScalarResult(exp_acc),
        FakeScalarResult(cash_acc),
    ])

    data   = ReceiptCreate(product_id=1, qty=10, unit_cost=3.00, receive_date=date(2026, 4, 13))
    result = asyncio.run(create_receipt(db, data, user))

    assert result["total_cost"]  == 30.0
    assert result["expense_ref"] is not None
    assert result["expense_ref"].startswith("EXP-")

    expenses       = [o for o in db.added if isinstance(o, Expense)]
    journals       = [o for o in db.added if isinstance(o, Journal)]
    journal_entries = [o for o in db.added if isinstance(o, JournalEntry)]

    assert len(expenses)        == 1
    assert len(journals)        == 1
    assert len(journal_entries) == 2

    exp = expenses[0]
    assert exp.amount          == 30.0
    assert exp.payment_method  == "cash"
    assert exp.category_id     == category.id

    # double-entry: one debit (expense), one credit (cash)
    debits  = [je for je in journal_entries if je.debit  > 0]
    credits = [je for je in journal_entries if je.credit > 0]
    assert len(debits)  == 1
    assert len(credits) == 1
    assert debits[0].debit   == 30.0
    assert credits[0].credit == 30.0


def test_create_receipt_links_expense_to_receipt():
    product  = _make_product(stock=5)
    user     = _make_user()
    category = ExpenseCategory(id=11, name="Stock Purchase", account_code="5011", is_active="1")
    exp_acc  = Account(id=20, code="5011", name="Stock Purchase", type="expense", balance=0)
    cash_acc = Account(id=21, code="1000", name="Cash", type="asset", balance=50)

    db = FakeReceiveSession([
        FakeScalarResult(product),
        FakeScalarResult(None),
        FakeScalarResult(category),
        FakeScalarResult(None),
        FakeScalarResult(exp_acc),
        FakeScalarResult(cash_acc),
    ])

    data   = ReceiptCreate(product_id=1, qty=2, unit_cost=5.00, receive_date=date(2026, 4, 13))
    result = asyncio.run(create_receipt(db, data, user))

    receipts = [o for o in db.added if isinstance(o, ProductReceipt)]
    expenses = [o for o in db.added if isinstance(o, Expense)]
    assert len(receipts) == 1
    assert len(expenses) == 1
    assert receipts[0].expense_id == expenses[0].id
    assert result["expense_id"]   == expenses[0].id


# ── create_receipt: error paths ───────────────────────────────────────────────

def test_create_receipt_raises_404_when_product_missing():
    from fastapi import HTTPException

    db = FakeReceiveSession([FakeScalarResult(None)])   # product not found

    data = ReceiptCreate(product_id=999, qty=1, receive_date=date(2026, 4, 13))
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(create_receipt(db, data, _make_user()))

    assert exc_info.value.status_code == 404


# ── BatchReceiptCreate schema ─────────────────────────────────────────────────

def test_batch_schema_rejects_empty_items():
    with pytest.raises(Exception):
        BatchReceiptCreate(
            receive_date=date.today(),
            items=[],
        )


def test_batch_schema_accepts_multiple_items():
    b = BatchReceiptCreate(
        receive_date=date.today(),
        items=[
            BatchReceiptItem(product_id=1, qty=2),
            BatchReceiptItem(product_id=2, qty=5, unit_cost=3.0),
        ],
    )
    assert len(b.items) == 2


# ── create_receipt_batch ──────────────────────────────────────────────────────

def test_batch_receive_two_products():
    """Two products in one batch: both stocks increase, one commit."""
    p1 = _make_product(id=1, sku="A", name="Olive Oil", stock=10, cost=5)
    p2 = _make_product(id=2, sku="B", name="Honey",     stock=20, cost=8)
    user = _make_user()

    # execute() sequence for two products, no cost (no expense path):
    #  product 1: select(Product) → p1 ; select(max(Receipt.id)) → None
    #  product 2: select(Product) → p2 ; select(max(Receipt.id)) → 1 (first flushed)
    db = FakeReceiveSession([
        FakeScalarResult(p1),
        FakeScalarResult(None),  # max receipt id → RCV-00001
        FakeScalarResult(p2),
        FakeScalarResult(1),     # max receipt id → RCV-00002
    ])

    data = BatchReceiptCreate(
        receive_date=date(2026, 4, 13),
        items=[
            BatchReceiptItem(product_id=1, qty=5),
            BatchReceiptItem(product_id=2, qty=3),
        ],
    )
    result = asyncio.run(create_receipt_batch(db, data, user))

    assert result["count"]      == 2
    assert result["total_cost"] == 0.0   # no costs provided
    assert len(result["receipts"]) == 2
    assert float(p1.stock) == 15.0
    assert float(p2.stock) == 23.0
    assert db.committed is True


def test_batch_receive_single_commit_even_with_cost():
    """Batch with one product + cost: still only one db.commit()."""
    product  = _make_product(stock=0)
    user     = _make_user()
    category = ExpenseCategory(id=11, name="Stock Purchase", account_code="5011", is_active="1")
    exp_acc  = Account(id=20, code="5011", name="Stock Purchase", type="expense", balance=0)
    cash_acc = Account(id=21, code="1000", name="Cash",           type="asset",   balance=100)

    db = FakeReceiveSession([
        FakeScalarResult(product),
        FakeScalarResult(None),    # max receipt id
        FakeScalarResult(category),
        FakeScalarResult(None),    # max expense id
        FakeScalarResult(exp_acc),
        FakeScalarResult(cash_acc),
    ])

    data = BatchReceiptCreate(
        receive_date=date(2026, 4, 13),
        items=[BatchReceiptItem(product_id=1, qty=4, unit_cost=5.00)],
    )
    result = asyncio.run(create_receipt_batch(db, data, user))

    assert result["count"]      == 1
    assert result["total_cost"] == 20.0
    assert result["receipts"][0]["expense_ref"].startswith("EXP-")
    assert db.committed is True


# ── list_receipts ─────────────────────────────────────────────────────────────

def test_list_receipts_returns_empty_on_no_rows():
    class FakeListSession:
        async def execute(self, _stmt):
            return FakeScalarResult(0)

    # patch scalars().all() to return []
    class FakeCountResult:
        def scalar(self): return 0

    class FakeRowsResult:
        def scalars(self): return self
        def all(self):     return []

    call_count = 0

    class FakeListSession2:
        async def execute(self, _stmt):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return FakeCountResult()   # COUNT query
            return FakeRowsResult()        # rows query

    result = asyncio.run(list_receipts(FakeListSession2()))
    assert result["total"] == 0
    assert result["items"] == []
