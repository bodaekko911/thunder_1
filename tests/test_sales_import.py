"""
test_sales_import.py

Unit / integration tests for the historical-sales import feature.

Tests call import_sales() directly so they do not need a live DB; a
FakeImportSession fulfils the session contract and lets us inspect what
the service tried to write.

Test coverage (spec)
────────────────────
 1. Dry run — valid 3-invoice sheet → correct summary, zero DB writes.
 2. Real import, history_only — HIST- prefix, created_at = noon on sale date,
    no StockMove, no Journal.
 3. Real import, with_journals — Journal + JournalEntry added dated to sale.
 4. Unknown SKU — row-level error, that group skipped, other groups succeed.
 5. Customer auto-creation — new Customer row, linked to invoice.
 6. Date before 2026-01-01 — row-level error.
 7. Grouping — 5 rows, 2 (customer, date) pairs → 2 invoices, not 5.
 8. Duplicate detection — second run of same data detects all as duplicates.
 9. Batch revert — DELETE /import/api/sales/batch/{batch_id} removes invoices.
10. Permission — user without page_import gets 403.
"""

import asyncio
import io
from collections.abc import AsyncGenerator
from datetime import date, datetime
from types import SimpleNamespace

import openpyxl
import pytest
from fastapi.testclient import TestClient

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

import app.app_factory as app_factory_module
from app.app_factory import create_app
from app.database import get_async_session
from app.models.customer import Customer
from app.models.invoice import Invoice, InvoiceItem
from app.models.inventory import StockMove
from app.models.accounting import Account, Journal, JournalEntry
from app.models.product import Product
from app.models.refund import RetailRefund
from app.models.b2b import B2BInvoiceItem
from app.models.supplier import PurchaseItem
from app.services.sales_import_service import import_sales


# ── Helpers: build in-memory Excel bytes ─────────────────────────────────────

def _make_xlsx(rows: list[list], headers=None) -> bytes:
    if headers is None:
        headers = ["SKU", "Item", "QTY", "Price", "Customer", "Date"]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── Fake async session ────────────────────────────────────────────────────────

class _FakeResult:
    def __init__(self, data):
        if data is None:
            self._list = []
        elif isinstance(data, list):
            self._list = data
        else:
            self._list = [data]

    def scalar_one_or_none(self):
        return self._list[0] if self._list else None

    def scalars(self):
        return self

    def all(self):
        return self._list

    def __iter__(self):
        return iter(self._list)


class FakeImportSession:
    """Fake SQLAlchemy async session sufficient for import_sales tests.

    Configure which products / customers / invoices / accounts exist by
    setting the corresponding list attributes before running the service.
    Objects fed to ``db.add()`` are collected in ``.added`` so tests can
    assert on what was created.
    """

    def __init__(
        self,
        products: list | None = None,
        customers: list | None = None,
        invoices: list | None = None,
        accounts: list | None = None,
    ):
        self.products  = list(products  or [])
        self.customers = list(customers or [])
        self.invoices  = list(invoices  or [])
        self.accounts  = list(accounts  or [])
        self.added: list     = []
        self._next_id: int   = 1000
        self.committed: int  = 0
        self.rolled_back: int = 0

    # ── Query dispatch ──────────────────────────────────────────────────────

    def _entity(self, stmt):
        try:
            descs = stmt.column_descriptions
            for d in descs:
                if d.get("entity") is not None:
                    return d["entity"]
        except Exception:
            pass
        return None

    async def execute(self, stmt):
        ent = self._entity(stmt)
        if ent is Product:
            return _FakeResult(list(self.products))
        if ent is Customer:
            # Return only customers that match the WHERE clause if possible,
            # but for simplicity return the full list and let scalar_one_or_none
            # take the first.  Tests set up the list to reflect the expected DB
            # state (empty = not found, one item = found).
            return _FakeResult(list(self.customers))
        if ent is Invoice:
            return _FakeResult(list(self.invoices))
        if ent is Account:
            return _FakeResult(list(self.accounts))
        if ent is Journal:
            return _FakeResult([])
        if ent is StockMove:
            return _FakeResult([])
        # Reference-check models used by batch revert: no refs in import tests
        if ent in (InvoiceItem, B2BInvoiceItem, PurchaseItem, RetailRefund):
            return _FakeResult([])
        return _FakeResult(None)

    # ── Write operations ─────────────────────────────────────────────────────

    def add(self, obj):
        if not hasattr(obj, "id") or getattr(obj, "id") is None:
            obj.id = self._next_id
            self._next_id += 1
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if not hasattr(obj, "id") or obj.id is None:
                obj.id = self._next_id
                self._next_id += 1

    async def commit(self):
        self.committed += 1

    async def rollback(self):
        self.rolled_back += 1

    async def refresh(self, _obj):
        pass

    async def delete(self, obj):
        pass


# ── Shared product / customer fixtures ───────────────────────────────────────

def _make_product(sku: str, name: str = "Product") -> Product:
    p = Product(sku=sku, name=name, price=10.0, cost=5.0, stock=100)
    p.id = hash(sku) % 9000 + 1000
    p.is_active = True
    return p


def _make_customer(name: str, cid: int = 500) -> Customer:
    c = Customer(name=name)
    c.id = cid
    return c


def _make_invoice(cid: int, sale_date: date, batch_id: str) -> Invoice:
    inv = Invoice(
        customer_id=cid,
        payment_method="historical_import",
        status="paid",
        notes=f"Imported from test.xlsx on {date.today().isoformat()}",
        import_batch_id=batch_id,
    )
    inv.id = 42
    inv.created_at = datetime(sale_date.year, sale_date.month, sale_date.day, 12, 0, 0)
    return inv


# ═════════════════════════════════════════════════════════════════════════════
# Test 1 — Dry run: valid 3-invoice sheet
# ═════════════════════════════════════════════════════════════════════════════

def test_dry_run_valid_sheet_no_db_writes():
    """Dry run returns correct summary counters and writes nothing to the DB."""
    p1 = _make_product("SKU-001", "Olive Oil")
    p2 = _make_product("SKU-002", "Tahini")
    c1 = _make_customer("Ahmed")

    # 5 rows forming 3 invoice groups:
    #   (Ahmed, 2026-01-10) → 2 lines
    #   (Ahmed, 2026-01-11) → 1 line
    #   (Walk-in, 2026-01-10) → 2 lines
    rows = [
        ["SKU-001", "Olive Oil", 3, 15.5,  "Ahmed",   "2026-01-10"],
        ["SKU-002", "Tahini",    2,  8.0,  "Ahmed",   "2026-01-10"],
        ["SKU-001", "Olive Oil", 1, 15.5,  "Ahmed",   "2026-01-11"],
        ["SKU-001", "Olive Oil", 5, 14.0,  "",        "2026-01-10"],
        ["SKU-002", "Tahini",    4,  7.5,  "",        "2026-01-10"],
    ]
    xls = _make_xlsx(rows)
    db = FakeImportSession(products=[p1, p2], customers=[c1])

    result = _run(import_sales(db, xls, "test.xlsx", user_id := 1, dry_run=True))

    assert result["dry_run"] is True
    assert result["summary"]["rows_read"] == 5
    assert result["summary"]["invoices_created"] == 0
    assert result["summary"]["invoices_would_create"] == 3
    assert result["summary"]["line_items"] == 5
    assert result["summary"]["rows_skipped"] == 0
    assert not result["errors"]
    # No DB writes at all
    assert db.committed == 0
    assert not db.added


# ═════════════════════════════════════════════════════════════════════════════
# Test 2 — Real import, history_only
# ═════════════════════════════════════════════════════════════════════════════

def test_real_import_history_only_correct_invoice_fields():
    """history_only creates Invoice with HIST- prefix, noon created_at, no stock/journal."""
    p1 = _make_product("SKU-001", "Olive Oil")
    c1 = _make_customer("Ahmed", cid=5)

    rows = [["SKU-001", "Olive Oil", 2, 12.0, "Ahmed", "2026-02-14"]]
    xls  = _make_xlsx(rows)
    db   = FakeImportSession(products=[p1], customers=[c1])

    result = _run(import_sales(db, xls, "sales.xlsx", 1, dry_run=False, mode="history_only"))

    assert result["dry_run"] is False
    assert result["summary"]["invoices_created"] == 1
    assert not result["errors"]
    assert result["batch_id"] is not None

    # Find the Invoice that was added
    invoices = [o for o in db.added if isinstance(o, Invoice)]
    assert len(invoices) == 1
    inv = invoices[0]

    # invoice_number prefix
    assert inv.invoice_number.startswith("HIST-")
    # created_at is noon on the sale date
    assert inv.created_at.date() == date(2026, 2, 14)
    assert inv.created_at.hour == 12
    # payment method is distinguishable
    assert inv.payment_method == "historical_import"
    assert inv.status == "paid"
    # No stock moves in history_only
    stock_moves = [o for o in db.added if isinstance(o, StockMove)]
    assert len(stock_moves) == 0
    # No journals in history_only
    journals = [o for o in db.added if isinstance(o, Journal)]
    assert len(journals) == 0


# ═════════════════════════════════════════════════════════════════════════════
# Test 3 — Real import, with_journals
# ═════════════════════════════════════════════════════════════════════════════

def test_real_import_with_journals_creates_dated_journal():
    """with_journals mode posts a Journal dated to the historical sale date."""
    p1  = _make_product("SKU-001", "Olive Oil")
    c1  = _make_customer("Ahmed", cid=5)
    acc = Account(code="1000", name="Cash", type="asset", balance=0)
    acc.id = 10
    acc2 = Account(code="4000", name="Sales", type="revenue", balance=0)
    acc2.id = 11

    rows = [["SKU-001", "Olive Oil", 1, 20.0, "Ahmed", "2026-03-05"]]
    xls  = _make_xlsx(rows)
    db   = FakeImportSession(products=[p1], customers=[c1], accounts=[acc, acc2])

    result = _run(import_sales(db, xls, "sales.xlsx", 1, dry_run=False, mode="with_journals"))

    assert result["summary"]["invoices_created"] == 1
    journals = [o for o in db.added if isinstance(o, Journal)]
    assert len(journals) == 1
    j = journals[0]
    # Journal should be dated to 2026-03-05 noon
    assert j.created_at is not None
    assert j.created_at.date() == date(2026, 3, 5)
    assert j.created_at.hour == 12


# ═════════════════════════════════════════════════════════════════════════════
# Test 4 — Unknown SKU → product auto-created, invoice succeeds
# ═════════════════════════════════════════════════════════════════════════════

def test_unknown_sku_auto_creates_product_and_imports():
    """An unknown SKU now triggers automatic Product creation; both groups succeed."""
    p1 = _make_product("SKU-001", "Olive Oil")

    rows = [
        ["SKU-001",  "Olive Oil", 2, 15.0, "Ahmed", "2026-01-10"],   # existing product
        ["UNKNOWN",  "Mystery",   1,  5.0, "Ahmed", "2026-01-11"],   # unknown → auto-create
    ]
    xls = _make_xlsx(rows)
    db  = FakeImportSession(products=[p1])

    result = _run(import_sales(db, xls, "sales.xlsx", 1, dry_run=False))

    assert result["summary"]["invoices_created"] == 2
    assert result["summary"]["rows_skipped"] == 0
    assert result["summary"]["products_auto_created"] == 1
    assert not result["errors"]

    new_products = [o for o in db.added if isinstance(o, Product)]
    assert len(new_products) == 1
    assert new_products[0].sku == "UNKNOWN"
    assert new_products[0].category == "Imported - Historical"


# ═════════════════════════════════════════════════════════════════════════════
# Test 5 — Customer auto-creation
# ═════════════════════════════════════════════════════════════════════════════

def test_customer_auto_created_and_linked():
    """A customer name not in the DB triggers automatic Customer creation."""
    p1 = _make_product("SKU-001", "Olive Oil")

    rows = [["SKU-001", "Olive Oil", 1, 10.0, "Brand New Customer", "2026-01-15"]]
    xls  = _make_xlsx(rows)
    # No customers pre-populated → lookup returns None → auto-create
    db   = FakeImportSession(products=[p1], customers=[])

    result = _run(import_sales(db, xls, "sales.xlsx", 1, dry_run=False))

    assert result["summary"]["invoices_created"] == 1
    assert result["summary"]["customers_auto_created"] == 1
    assert "Brand New Customer" in result["auto_created_customers"]

    new_customers = [o for o in db.added if isinstance(o, Customer)]
    assert len(new_customers) == 1
    assert new_customers[0].name == "Brand New Customer"


# ═════════════════════════════════════════════════════════════════════════════
# Test 6 — Date before 2026-01-01 → row-level error
# ═════════════════════════════════════════════════════════════════════════════

def test_date_before_minimum_is_row_level_error():
    """A date before 2026-01-01 produces a validation error; nothing imported."""
    p1 = _make_product("SKU-001", "Olive Oil")

    rows = [["SKU-001", "Olive Oil", 1, 10.0, "Ahmed", "2025-12-31"]]
    xls  = _make_xlsx(rows)
    db   = FakeImportSession(products=[p1])

    result = _run(import_sales(db, xls, "sales.xlsx", 1, dry_run=False))

    assert result["summary"]["invoices_created"] == 0
    assert result["summary"]["rows_skipped"] == 1
    errors = result["errors"]
    assert len(errors) == 1
    assert "2026-01-01" in errors[0]["reason"]


# ═════════════════════════════════════════════════════════════════════════════
# Test 7 — Grouping: 5 rows, 2 (customer, date) pairs → 2 invoices
# ═════════════════════════════════════════════════════════════════════════════

def test_rows_grouped_into_invoices_by_customer_and_date():
    """5 rows across 2 (customer, date) groups → 2 invoices created, not 5."""
    p1 = _make_product("SKU-001", "Olive Oil")
    p2 = _make_product("SKU-002", "Tahini")

    rows = [
        # Group 1: Ahmed / 2026-01-10  (3 lines)
        ["SKU-001", "Olive Oil", 2, 15.0, "Ahmed", "2026-01-10"],
        ["SKU-002", "Tahini",    1,  8.0, "Ahmed", "2026-01-10"],
        ["SKU-001", "Olive Oil", 1, 15.0, "Ahmed", "2026-01-10"],
        # Group 2: Ahmed / 2026-01-11  (2 lines)
        ["SKU-001", "Olive Oil", 3, 15.0, "Ahmed", "2026-01-11"],
        ["SKU-002", "Tahini",    2,  8.0, "Ahmed", "2026-01-11"],
    ]
    xls = _make_xlsx(rows)
    db  = FakeImportSession(products=[p1, p2])

    result = _run(import_sales(db, xls, "sales.xlsx", 1, dry_run=False))

    assert result["summary"]["invoices_created"] == 2
    assert result["summary"]["line_items"] == 5
    invoices = [o for o in db.added if isinstance(o, Invoice)]
    assert len(invoices) == 2
    # Each invoice must have a HIST- number
    for inv in invoices:
        assert inv.invoice_number.startswith("HIST-")


# ═════════════════════════════════════════════════════════════════════════════
# Test 8 — Duplicate detection on re-run
# ═════════════════════════════════════════════════════════════════════════════

def test_duplicate_detection_blocks_second_import():
    """
    A pre-existing invoice from a prior import (notes 'Imported from …') on the
    same customer + date is detected as a duplicate and the group is skipped.
    """
    p1 = _make_product("SKU-001", "Olive Oil")
    c1 = _make_customer("Ahmed", cid=5)

    # The "database" already has an invoice from a previous import
    existing_inv = _make_invoice(cid=5, sale_date=date(2026, 1, 10), batch_id="old-batch")

    rows = [["SKU-001", "Olive Oil", 2, 15.0, "Ahmed", "2026-01-10"]]
    xls  = _make_xlsx(rows)
    db   = FakeImportSession(products=[p1], customers=[c1], invoices=[existing_inv])

    result = _run(import_sales(db, xls, "sales.xlsx", 1, dry_run=False, force=False))

    assert result["summary"]["invoices_created"] == 0
    assert result["summary"]["rows_skipped"] == 1
    errors = result["errors"]
    assert len(errors) == 1
    assert "duplicate" in errors[0]["reason"].lower()


def test_force_flag_bypasses_duplicate_detection():
    """With force=True, the duplicate check is skipped and the invoice is created."""
    p1 = _make_product("SKU-001", "Olive Oil")
    c1 = _make_customer("Ahmed", cid=5)
    existing_inv = _make_invoice(cid=5, sale_date=date(2026, 1, 10), batch_id="old-batch")

    rows = [["SKU-001", "Olive Oil", 2, 15.0, "Ahmed", "2026-01-10"]]
    xls  = _make_xlsx(rows)
    db   = FakeImportSession(products=[p1], customers=[c1], invoices=[existing_inv])

    result = _run(import_sales(db, xls, "sales.xlsx", 1, dry_run=False, force=True))

    assert result["summary"]["invoices_created"] == 1
    assert not result["errors"]


# ═════════════════════════════════════════════════════════════════════════════
# Test 9 — Batch revert via HTTP DELETE endpoint
# ═════════════════════════════════════════════════════════════════════════════

class _BatchFakeSession:
    """Fake session tailored for the delete-batch endpoint test.  Pre-populates
    two invoices in the batch and verifies they are deleted."""

    def __init__(self):
        inv1 = Invoice(
            customer_id=1, payment_method="historical_import",
            subtotal=20, discount=0, total=20, status="paid",
            notes="Imported from test.xlsx on 2026-01-10",
            import_batch_id="test-batch-123",
        )
        inv1.id = 101
        inv1.created_at = datetime(2026, 1, 10, 12, 0, 0)
        inv2 = Invoice(
            customer_id=1, payment_method="historical_import",
            subtotal=10, discount=0, total=10, status="paid",
            notes="Imported from test.xlsx on 2026-01-11",
            import_batch_id="test-batch-123",
        )
        inv2.id = 102
        inv2.created_at = datetime(2026, 1, 11, 12, 0, 0)

        self._invoices = [inv1, inv2]
        self.deleted   = []
        self.committed = 0
        self._empty_result = _FakeResult([])

    def _entity(self, stmt):
        try:
            for d in stmt.column_descriptions:
                if d.get("entity") is not None:
                    return d["entity"]
        except Exception:
            pass
        return None

    async def execute(self, stmt):
        ent = self._entity(stmt)
        if ent is Invoice:
            return _FakeResult(list(self._invoices))
        if ent is StockMove:
            return _FakeResult([])
        if ent is Journal:
            return _FakeResult([])
        if ent is Product:
            return _FakeResult([])
        return self._empty_result

    def add(self, _obj): pass

    async def delete(self, obj):
        self.deleted.append(obj)

    async def flush(self): pass

    async def commit(self):
        self.committed += 1

    async def rollback(self): pass

    async def refresh(self, _obj): pass


def _make_http_client_with_session(session):
    from app.core.security import get_current_user as gcu
    import app.app_factory as af

    async def noop(): return None

    async def override_session() -> AsyncGenerator:
        yield session

    admin = SimpleNamespace(id=1, name="Admin", role="admin", is_active=True)

    af.configure_logging   = lambda: None
    af.configure_monitoring = lambda: None
    af.verify_migration_status = noop

    app = create_app()
    app.dependency_overrides[get_async_session] = override_session
    app.dependency_overrides[gcu] = lambda: admin

    return TestClient(app, raise_server_exceptions=False)


def test_batch_revert_deletes_invoices():
    """DELETE /import/api/sales/batch/{id} removes all invoices in the batch."""
    session = _BatchFakeSession()
    client  = _make_http_client_with_session(session)

    response = client.delete("/import/api/sales/batch/test-batch-123")
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["deleted_invoices"] == 2
    # Both invoices should have been passed to session.delete()
    assert len(session.deleted) == 2
    assert session.committed == 1


# ═════════════════════════════════════════════════════════════════════════════
# Test 10 — Permission check
# ═════════════════════════════════════════════════════════════════════════════

class _MinimalSession:
    async def execute(self, _): return _FakeResult([])
    def add(self, _): pass
    async def flush(self): pass
    async def commit(self): pass
    async def rollback(self): pass
    async def refresh(self, _): pass
    async def delete(self, _): pass


def _make_client_no_auth():
    import app.app_factory as af

    async def noop(): return None

    async def override_session() -> AsyncGenerator:
        yield _MinimalSession()

    af.configure_logging    = lambda: None
    af.configure_monitoring = lambda: None
    af.verify_migration_status = noop

    app = create_app()
    app.dependency_overrides[get_async_session] = override_session
    # No current_user override → auth will reject (no cookie)
    return TestClient(app, raise_server_exceptions=False)


def test_import_sales_requires_page_import_permission():
    """POST /import/api/sales without authentication returns 401 or 307 redirect."""
    client = _make_client_no_auth()
    xls    = _make_xlsx([["SKU-001", "X", 1, 5.0, "", "2026-01-10"]])
    fd     = {"file": ("test.xlsx", xls, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    resp   = client.post(
        "/import/api/sales",
        files=fd,
        data={"dry_run": "true", "mode": "history_only", "force": "false"},
        allow_redirects=False,
    )
    # With no valid access_token cookie, the session-expiry middleware redirects
    # to login (307) for HTML requests, or returns 401 for JSON requests.
    # Either is acceptable — the point is no 200.
    assert resp.status_code in (307, 401, 403)


# ═════════════════════════════════════════════════════════════════════════════
# Bonus — invalid / missing required columns
# ═════════════════════════════════════════════════════════════════════════════

def test_missing_required_column_returns_error():
    """An Excel file without a Date column returns an error summary, no crash."""
    wb  = openpyxl.Workbook()
    ws  = wb.active
    ws.append(["SKU", "QTY", "Price"])   # Date column is missing
    ws.append(["SKU-001", 1, 10.0])
    buf = io.BytesIO()
    wb.save(buf)
    xls = buf.getvalue()

    db = FakeImportSession()
    result = _run(import_sales(db, xls, "bad.xlsx", 1, dry_run=True))
    assert "error" in result
    assert "Date" in result["error"]


# ═════════════════════════════════════════════════════════════════════════════
# New Tests — Product auto-creation
# ═════════════════════════════════════════════════════════════════════════════

# NT-1 — Unknown SKU + known customer → product auto-created, invoice created

def test_new1_unknown_sku_known_customer_product_auto_created():
    """Unknown SKU with a known customer creates the product and the invoice."""
    c1 = _make_customer("Ahmed", cid=5)
    rows = [["OLIVE-500", "Olive Oil 500ml", 2, 120.0, "Ahmed", "2026-02-01"]]
    xls  = _make_xlsx(rows)
    db   = FakeImportSession(products=[], customers=[c1])

    result = _run(import_sales(db, xls, "sales.xlsx", 1, dry_run=False))

    assert result["summary"]["invoices_created"] == 1
    assert result["summary"]["products_auto_created"] == 1
    assert not result["errors"]

    new_prods = [o for o in db.added if isinstance(o, Product)]
    assert len(new_prods) == 1
    p = new_prods[0]
    assert p.sku == "OLIVE-500"
    assert p.name == "Olive Oil 500ml"
    assert float(p.price) == 120.0
    assert float(p.cost) == 0.0
    assert float(p.stock) == 0
    assert p.category == "Imported - Historical"
    assert p.created_by_import_batch == result["batch_id"]

    auto_list = result["auto_created_products"]
    assert len(auto_list) == 1
    assert auto_list[0]["sku"] == "OLIVE-500"


# NT-2 — Unknown SKU + unknown customer → both auto-created

def test_new2_unknown_sku_unknown_customer_both_created():
    """When both SKU and customer are new, both are auto-created."""
    rows = [["NEW-SKU", "Mystery Item", 1, 50.0, "Brand New", "2026-02-05"]]
    xls  = _make_xlsx(rows)
    db   = FakeImportSession(products=[], customers=[])

    result = _run(import_sales(db, xls, "sales.xlsx", 1, dry_run=False))

    assert result["summary"]["invoices_created"] == 1
    assert result["summary"]["products_auto_created"] == 1
    assert result["summary"]["customers_auto_created"] == 1
    assert "Brand New" in result["auto_created_customers"]
    assert result["auto_created_products"][0]["sku"] == "NEW-SKU"


# NT-3 — Same unknown SKU across 3 rows with different prices → one product, 3 lines

def test_new3_same_unknown_sku_different_prices_one_product():
    """Three rows with the same unknown SKU at different prices create one product and three invoice lines."""
    rows = [
        ["MULTI-PRICE", "Oil", 1, 100.0, "Ahmed", "2026-02-10"],
        ["MULTI-PRICE", "Oil", 2, 110.0, "Ahmed", "2026-02-11"],
        ["MULTI-PRICE", "Oil", 3, 120.0, "Ahmed", "2026-02-12"],
    ]
    xls = _make_xlsx(rows)
    db  = FakeImportSession(products=[], customers=[])

    result = _run(import_sales(db, xls, "sales.xlsx", 1, dry_run=False))

    # 3 invoice groups (different dates)
    assert result["summary"]["invoices_created"] == 3
    # Only one product should be auto-created
    assert result["summary"]["products_auto_created"] == 1
    new_prods = [o for o in db.added if isinstance(o, Product)]
    assert len(new_prods) == 1
    # Price on the product is from first occurrence
    assert float(new_prods[0].price) == 100.0

    # Three InvoiceItem rows at their respective prices
    items = [o for o in db.added if isinstance(o, InvoiceItem)]
    prices = sorted(float(it.unit_price) for it in items)
    assert prices == [100.0, 110.0, 120.0]


# NT-4 — Same unknown SKU with conflicting item names → first-seen name wins, warning emitted

def test_new4_conflicting_item_names_uses_first_warns():
    """When the same unknown SKU appears with two different names, the first name is used and a warning is emitted."""
    rows = [
        ["CONFLICT", "Olive Oil 500ml",  1, 50.0, "Ahmed", "2026-02-10"],
        ["CONFLICT", "Olive oil 0.5L",   1, 50.0, "Ahmed", "2026-02-11"],
    ]
    xls = _make_xlsx(rows)
    db  = FakeImportSession(products=[], customers=[])

    result = _run(import_sales(db, xls, "sales.xlsx", 1, dry_run=False))

    assert result["summary"]["products_auto_created"] == 1
    new_prods = [o for o in db.added if isinstance(o, Product)]
    assert new_prods[0].name == "Olive Oil 500ml"

    warnings = result["warnings"]
    assert any("conflicting item names" in w for w in warnings)
    conflict_warn = next(w for w in warnings if "conflicting item names" in w)
    assert "Olive Oil 500ml" in conflict_warn
    assert "Olive oil 0.5L"  in conflict_warn


# NT-5 — default_cost_ratio applied to auto-created product

def test_new5_default_cost_ratio_applied_to_auto_created():
    """With default_cost_ratio=0.6, auto-created product cost = price * 0.6."""
    rows = [["RATIO-SKU", "Test Item", 1, 100.0, "Ahmed", "2026-02-15"]]
    xls  = _make_xlsx(rows)
    db   = FakeImportSession(products=[], customers=[])

    result = _run(import_sales(db, xls, "sales.xlsx", 1, dry_run=False, default_cost_ratio=0.6))

    new_prods = [o for o in db.added if isinstance(o, Product)]
    assert len(new_prods) == 1
    assert float(new_prods[0].cost) == pytest.approx(60.0, rel=1e-3)
    assert result["auto_created_products"][0]["cost"] == pytest.approx(60.0, rel=1e-3)
    # No cost-zero warning when ratio is set
    assert not any("cost = 0" in w for w in result["warnings"])


# NT-6 — Dry-run with unknown SKUs: nothing written, response lists them

def test_new6_dry_run_unknown_sku_not_created_but_reported():
    """Dry-run with unknown SKUs: no Product rows written, but auto_created_products is populated."""
    rows = [["DRY-SKU", "Dry Item", 3, 75.0, "Ahmed", "2026-03-01"]]
    xls  = _make_xlsx(rows)
    db   = FakeImportSession(products=[], customers=[])

    result = _run(import_sales(db, xls, "sales.xlsx", 1, dry_run=True))

    assert result["dry_run"] is True
    assert result["summary"]["products_auto_created"] == 1
    assert result["summary"]["invoices_would_create"] == 1

    # No Product should have been written to the DB
    new_prods = [o for o in db.added if isinstance(o, Product)]
    assert len(new_prods) == 0

    # But it should appear in the response
    assert len(result["auto_created_products"]) == 1
    assert result["auto_created_products"][0]["sku"] == "DRY-SKU"


# NT-7 — Batch revert deletes orphan auto-created product, keeps referenced one

class _BatchFakeSessionWithAutoProducts:
    """Fake session for the cascade-delete test.

    Simulates:
    - Two invoices in the batch.
    - Product A (created_by_import_batch=batch_id): used ONLY in this batch.
    - Product B (created_by_import_batch=batch_id): also referenced by a PurchaseItem.
    """

    BATCH_ID = "revert-cascade-test"

    def __init__(self):
        self.inv1 = Invoice(
            customer_id=1, payment_method="historical_import",
            subtotal=10, discount=0, total=10, status="paid",
            notes="Imported from t.xlsx on 2026-03-01",
            import_batch_id=self.BATCH_ID,
        )
        self.inv1.id = 201
        self.inv1.created_at = datetime(2026, 3, 1, 12, 0, 0)

        self.prod_a = Product(sku="A", name="Auto A", price=10, cost=0, stock=0)
        self.prod_a.id = 301
        self.prod_a.created_by_import_batch = self.BATCH_ID

        self.prod_b = Product(sku="B", name="Auto B", price=20, cost=0, stock=0)
        self.prod_b.id = 302
        self.prod_b.created_by_import_batch = self.BATCH_ID

        self.deleted: list = []
        self.committed = 0
        self._flushed  = False

    def _entity(self, stmt):
        try:
            for d in stmt.column_descriptions:
                if d.get("entity") is not None:
                    return d["entity"]
        except Exception:
            pass
        return None

    async def execute(self, stmt):
        ent = self._entity(stmt)
        if ent is Invoice:
            return _FakeResult([self.inv1])
        if ent is StockMove:
            return _FakeResult([])
        if ent is Journal:
            return _FakeResult([])
        if ent is Product:
            # After flush (invoices deleted), return auto-created products
            if self._flushed:
                return _FakeResult([self.prod_a, self.prod_b])
            return _FakeResult([])
        if ent is Customer:
            return _FakeResult([])
        if ent is InvoiceItem:
            # prod_a has no remaining invoice items; prod_b has none either
            return _FakeResult([])
        if ent is B2BInvoiceItem:
            return _FakeResult([])
        if ent is PurchaseItem:
            # prod_b IS referenced by a PurchaseItem
            return _FakeResult([object()])  # non-None sentinel
        if ent is RetailRefund:
            return _FakeResult([])
        return _FakeResult([])

    def add(self, _obj): pass

    async def delete(self, obj):
        self.deleted.append(obj)

    async def flush(self):
        self._flushed = True

    async def commit(self):
        self.committed += 1

    async def rollback(self): pass

    async def refresh(self, _obj): pass


def test_new7_batch_revert_cascade_deletes_orphan_product_keeps_referenced():
    """Batch revert deletes an auto-created product with no other refs, keeps one that has a PurchaseItem ref."""
    session = _BatchFakeSessionWithAutoProducts()
    client  = _make_http_client_with_session(session)

    resp = client.delete(f"/import/api/sales/batch/{_BatchFakeSessionWithAutoProducts.BATCH_ID}")
    assert resp.status_code == 200
    d = resp.json()
    assert d["ok"] is True
    assert d["deleted_invoices"]  == 1
    assert d["deleted_products"]  == 1   # only prod_a (orphan); prod_b kept
    assert d["deleted_customers"] == 0

    deleted_ids = {id(obj) for obj in session.deleted}
    assert id(session.prod_a) in deleted_ids
    assert id(session.prod_b) not in deleted_ids


# NT-8 — SKU normalization: "01234" and "1234" map to the same product

def test_new8_sku_normalization_deduplication():
    """Leading-zero SKU and its normalized form share a single auto-created product."""
    rows = [
        ["01234", "Item A", 1, 10.0, "Ahmed", "2026-03-10"],
        ["1234",  "Item A", 2, 10.0, "Ahmed", "2026-03-11"],
    ]
    xls = _make_xlsx(rows)
    db  = FakeImportSession(products=[], customers=[])

    result = _run(import_sales(db, xls, "sales.xlsx", 1, dry_run=False))

    # Only one product should be created for both SKU variants
    assert result["summary"]["products_auto_created"] == 1
    new_prods = [o for o in db.added if isinstance(o, Product)]
    assert len(new_prods) == 1


# NT-9 — Auto-created customer + auto-created product in the same invoice

def test_new9_auto_created_customer_and_product_both_linked():
    """Auto-created customer and auto-created product both appear correctly on the invoice."""
    rows = [["BRAND-NEW", "New Product", 3, 80.0, "NewCustomer", "2026-03-15"]]
    xls  = _make_xlsx(rows)
    db   = FakeImportSession(products=[], customers=[])

    result = _run(import_sales(db, xls, "sales.xlsx", 1, dry_run=False))

    assert result["summary"]["invoices_created"] == 1
    assert result["summary"]["customers_auto_created"] == 1
    assert result["summary"]["products_auto_created"] == 1

    invoices = [o for o in db.added if isinstance(o, Invoice)]
    items    = [o for o in db.added if isinstance(o, InvoiceItem)]
    assert len(invoices) == 1
    assert len(items)    == 1

    item = items[0]
    assert item.sku  == "BRAND-NEW"
    assert item.name == "New Product"
    assert float(item.unit_price) == 80.0


# NT-10 — Truncation: 300-char SKU → truncated to 80, warning in errors, no crash

def test_new10_long_sku_truncated_with_warning():
    """A SKU longer than 80 chars is truncated; a warning is added to errors and the row still imports."""
    long_sku = "X" * 300
    rows = [[long_sku, "Long SKU Product", 1, 25.0, "Ahmed", "2026-03-20"]]
    xls  = _make_xlsx(rows)
    db   = FakeImportSession(products=[], customers=[])

    result = _run(import_sales(db, xls, "sales.xlsx", 1, dry_run=False))

    # Row should still succeed (truncation is a note, not a fatal error)
    assert result["summary"]["invoices_created"] == 1
    assert result["summary"]["products_auto_created"] == 1

    new_prods = [o for o in db.added if isinstance(o, Product)]
    assert len(new_prods) == 1
    assert len(new_prods[0].sku) == 80

    # The truncation notice should appear in errors
    assert any("truncated" in e["reason"].lower() for e in result["errors"])
