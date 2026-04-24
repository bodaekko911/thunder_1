"""Tests for POS inline unit-price editing (permission gate + audit log)."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

from app.schemas.invoice import InvoiceCreate, InvoiceItemCreate
from app.services import pos_service


# ---------------------------------------------------------------------------
# Fake DB helpers
# ---------------------------------------------------------------------------

class FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value if isinstance(self._value, list) else [self._value]


class FakeSession:
    """Pops pre-loaded results for each execute() call."""

    def __init__(self, results):
        self._results = list(results)
        self.added = []

    async def execute(self, _stmt):
        return FakeScalarResult(self._results.pop(0))

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if not getattr(obj, "id", None):
                obj.id = 1

    async def commit(self):
        pass

    async def refresh(self, obj):
        pass

    async def rollback(self):
        pass


def _make_product(sku="OLV-500", price=12.00, stock=100):
    return SimpleNamespace(
        id=1, sku=sku, name="Olive Oil 500ml",
        price=price, stock=stock, is_active=True,
    )


def _make_user(role="manager"):
    """Return a user whose effective permissions match the given role."""
    return SimpleNamespace(
        id=1, name="Test User", role=role,
        permissions="",   # rely on role-defined permissions
        is_active=True,
    )


def _walk_in():
    return SimpleNamespace(id=99, name="Walk-in Customer")


async def _noop_journal(*args, **kwargs):
    pass


async def _fake_sync(db, product):
    return SimpleNamespace(), SimpleNamespace(qty=product.stock)


def _run(coro):
    return asyncio.run(coro)


def _invoke(data, user, *, extra_results=None):
    """Call create_invoice with mocked journal and location-sync."""
    walk_in = _walk_in()
    user_db_obj = SimpleNamespace(id=user.id, name=user.name, role=user.role)
    results = (extra_results or []) + [None, user_db_obj, walk_in]
    fake_db = FakeSession(results)

    orig_journal = pos_service.post_journal
    orig_sync = pos_service.sync_product_stock_to_default_location
    pos_service.post_journal = _noop_journal
    pos_service.sync_product_stock_to_default_location = _fake_sync
    try:
        return _run(pos_service.create_invoice(db=fake_db, data=data, user_id=user.id, user=user)), fake_db
    finally:
        pos_service.post_journal = orig_journal
        pos_service.sync_product_stock_to_default_location = orig_sync


# ---------------------------------------------------------------------------
# Test 1: pos_edit_price permission + general customer → sale succeeds,
#         correct total, audit log entry written.
# ---------------------------------------------------------------------------

def test_price_edit_with_permission_records_audit_log() -> None:
    product = _make_product(price=12.00, stock=10)
    data = InvoiceCreate(
        customer_id=None,
        items=[InvoiceItemCreate(sku="OLV-500", qty=2, unit_price=8.50)],
        discount_percent=0,
        payment_method="cash",
    )
    result, fake_db = _invoke(data, _make_user("manager"), extra_results=[[product]])

    assert result["total"] == pytest.approx(17.00)   # 8.50 * 2

    audit = [obj for obj in fake_db.added if getattr(obj, "action", None) == "pos_sale_with_price_edits"]
    assert len(audit) == 1
    payload = json.loads(audit[0].description)
    assert payload["edits"][0]["sku"] == "OLV-500"
    assert payload["edits"][0]["catalog_price"] == pytest.approx(12.00)
    assert payload["edits"][0]["sold_at"] == pytest.approx(8.50)
    assert payload["total_discount_vs_catalog"] == pytest.approx(7.00)   # (12-8.5)*2


# ---------------------------------------------------------------------------
# Test 2: No pos_edit_price + unit_price < 50 % of catalog → 403.
# ---------------------------------------------------------------------------

def test_no_permission_big_discount_raises_403() -> None:
    product = _make_product(price=10.00, stock=5)
    data = InvoiceCreate(
        customer_id=None,
        items=[InvoiceItemCreate(sku="OLV-500", qty=1, unit_price=4.00)],   # 40 % of catalog
        discount_percent=0,
        payment_method="cash",
    )
    user = _make_user("cashier")  # cashier role has no pos_edit_price

    fake_db = FakeSession([[product]])
    orig_sync = pos_service.sync_product_stock_to_default_location
    pos_service.sync_product_stock_to_default_location = _fake_sync
    try:
        with pytest.raises(HTTPException) as exc_info:
            _run(pos_service.create_invoice(db=fake_db, data=data, user_id=5, user=user))
    finally:
        pos_service.sync_product_stock_to_default_location = orig_sync

    assert exc_info.value.status_code == 403
    assert "action_pos_edit_price" in exc_info.value.detail


# ---------------------------------------------------------------------------
# Test 3: No pos_edit_price + catalog price (no edits) → succeeds.
# ---------------------------------------------------------------------------

def test_no_permission_catalog_price_passes() -> None:
    product = _make_product(price=10.00, stock=5)
    data = InvoiceCreate(
        customer_id=None,
        items=[InvoiceItemCreate(sku="OLV-500", qty=1)],   # no unit_price → uses catalog
        discount_percent=0,
        payment_method="cash",
    )
    result, _ = _invoke(data, _make_user("cashier"), extra_results=[[product]])
    assert result["total"] == pytest.approx(10.00)


# ---------------------------------------------------------------------------
# Test 4: InvoiceItem stored with edited unit_price and correct line total.
# ---------------------------------------------------------------------------

def test_invoice_item_stored_with_edited_unit_price() -> None:
    product = _make_product(price=12.00, stock=5)
    data = InvoiceCreate(
        customer_id=None,
        items=[InvoiceItemCreate(sku="OLV-500", qty=3, unit_price=9.00)],
        discount_percent=0,
        payment_method="cash",
    )
    result, fake_db = _invoke(data, _make_user("manager"), extra_results=[[product]])

    assert result["total"] == pytest.approx(27.00)   # 9.00 * 3

    from app.models.invoice import InvoiceItem
    items = [obj for obj in fake_db.added if isinstance(obj, InvoiceItem)]
    assert len(items) == 1
    assert items[0].unit_price == pytest.approx(9.00)
    assert items[0].total == pytest.approx(27.00)


# ---------------------------------------------------------------------------
# Test 5: 3-line cart with 2 edits → audit entry lists both edited SKUs.
# ---------------------------------------------------------------------------

def test_audit_log_contains_all_edited_lines() -> None:
    prod_a = SimpleNamespace(id=1, sku="SKU-A", name="A", price=10.00, stock=10, is_active=True)
    prod_b = SimpleNamespace(id=2, sku="SKU-B", name="B", price=20.00, stock=10, is_active=True)
    prod_c = SimpleNamespace(id=3, sku="SKU-C", name="C", price=30.00, stock=10, is_active=True)
    data = InvoiceCreate(
        customer_id=None,
        items=[
            InvoiceItemCreate(sku="SKU-A", qty=1, unit_price=8.00),    # edited
            InvoiceItemCreate(sku="SKU-B", qty=1),                      # not edited
            InvoiceItemCreate(sku="SKU-C", qty=1, unit_price=25.00),   # edited
        ],
        discount_percent=0,
        payment_method="cash",
    )
    _, fake_db = _invoke(
        data, _make_user("manager"),
        extra_results=[[prod_a], [prod_b], [prod_c]],
    )

    audit = [obj for obj in fake_db.added if getattr(obj, "action", None) == "pos_sale_with_price_edits"]
    assert len(audit) == 1
    payload = json.loads(audit[0].description)
    edited_skus = {e["sku"] for e in payload["edits"]}
    assert edited_skus == {"SKU-A", "SKU-C"}
    # total discount: (10-8)*1 + (30-25)*1 = 7
    assert payload["total_discount_vs_catalog"] == pytest.approx(7.00)


# ---------------------------------------------------------------------------
# Test 6: Invoice subtotal and total reconcile with edited prices.
# ---------------------------------------------------------------------------

def test_invoice_totals_reconcile_with_edited_prices() -> None:
    product = _make_product(price=50.00, stock=10)
    # 4 × 35.00 = subtotal 140.00, 10 % discount → total 126.00
    data = InvoiceCreate(
        customer_id=None,
        items=[InvoiceItemCreate(sku="OLV-500", qty=4, unit_price=35.00)],
        discount_percent=10,
        payment_method="cash",
    )
    result, fake_db = _invoke(data, _make_user("manager"), extra_results=[[product]])

    from app.models.invoice import Invoice
    inv_objs = [obj for obj in fake_db.added if isinstance(obj, Invoice)]
    assert len(inv_objs) == 1
    assert float(inv_objs[0].subtotal) == pytest.approx(140.00)
    assert float(inv_objs[0].total) == pytest.approx(126.00)
    assert result["total"] == pytest.approx(126.00)


# ---------------------------------------------------------------------------
# Test 7: Named customer + any price edit → 400.
# ---------------------------------------------------------------------------

def test_named_customer_with_price_edit_rejected() -> None:
    product = _make_product(price=12.00, stock=10)
    data = InvoiceCreate(
        customer_id=42,   # named customer
        items=[InvoiceItemCreate(sku="OLV-500", qty=1, unit_price=10.00)],
        discount_percent=0,
        payment_method="cash",
    )
    user = _make_user("admin")   # even admin is rejected for named-customer price edits

    fake_db = FakeSession([[product]])
    orig_sync = pos_service.sync_product_stock_to_default_location
    pos_service.sync_product_stock_to_default_location = _fake_sync
    try:
        with pytest.raises(HTTPException) as exc_info:
            _run(pos_service.create_invoice(db=fake_db, data=data, user_id=1, user=user))
    finally:
        pos_service.sync_product_stock_to_default_location = orig_sync

    assert exc_info.value.status_code == 400
    assert "general customer" in exc_info.value.detail.lower()
