"""
test_sales_import_response_shape.py

Smoke tests that assert every summary field the frontend depends on is present
in the backend response.  These tests catch the class of bug where a field is
renamed or dropped on the backend and the frontend starts showing "undefined".
"""

import asyncio
import io

import openpyxl
import pytest

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

from app.models.product import Product
from app.models.customer import Customer
from app.models.invoice import Invoice, InvoiceItem
from app.models.inventory import StockMove
from app.models.accounting import Account, Journal
from app.models.refund import RetailRefund
from app.models.b2b import B2BInvoiceItem
from app.models.supplier import PurchaseItem
from app.services.sales_import_service import import_sales


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_xlsx(rows, headers=None):
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


class _FakeResult:
    def __init__(self, data):
        self._list = data if isinstance(data, list) else ([] if data is None else [data])

    def scalar_one_or_none(self):
        return self._list[0] if self._list else None

    def scalars(self):
        return self

    def all(self):
        return self._list


class _MinimalSession:
    """Minimal async session: no products, no customers → triggers auto-creation paths."""

    def __init__(self):
        self.added = []
        self._next_id = 1000
        self.committed = 0

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
        if ent is Product:
            return _FakeResult([])
        if ent in (Customer, Invoice, Account, Journal, StockMove,
                   InvoiceItem, B2BInvoiceItem, PurchaseItem, RetailRefund):
            return _FakeResult([])
        return _FakeResult(None)

    def add(self, obj):
        if not hasattr(obj, "id") or obj.id is None:
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
        pass

    async def refresh(self, _):
        pass

    async def delete(self, _):
        pass


# ── Required summary fields ───────────────────────────────────────────────────

# All fields the frontend reads from data.summary in renderSalesResult()
DRY_RUN_SUMMARY_FIELDS = [
    "rows_read",
    "invoices_created",       # present in both; 0 in dry-run
    "invoices_would_create",  # present only in dry-run
    "line_items",
    "rows_skipped",
    "customers_auto_created",
    "products_auto_created",
    "earliest_date",
    "latest_date",
    "total_value",
]

REAL_RUN_SUMMARY_FIELDS = [
    "rows_read",
    "invoices_created",
    "line_items",
    "rows_skipped",
    "customers_auto_created",
    "products_auto_created",
    "earliest_date",
    "latest_date",
    "total_value",
]

TOP_LEVEL_FIELDS = [
    "dry_run",
    "mode",
    "file",
    "batch_id",
    "summary",
    "errors",
    "auto_created_customers",
    "auto_created_products",
    "warnings",
]


def _minimal_sheet():
    return _make_xlsx([
        ["SKU-001", "Olive Oil", 2, 15.0, "Ahmed", "2026-02-01"],
    ])


# ═════════════════════════════════════════════════════════════════════════════
# Dry-run shape
# ═════════════════════════════════════════════════════════════════════════════

def test_dry_run_response_contains_all_required_top_level_fields():
    """Dry-run response has every top-level key the frontend depends on."""
    db  = _MinimalSession()
    xls = _minimal_sheet()
    result = _run(import_sales(db, xls, "test.xlsx", 1, dry_run=True))

    for field in TOP_LEVEL_FIELDS:
        assert field in result, f"Missing top-level field: '{field}'"


def test_dry_run_response_contains_all_summary_fields():
    """Every summary field the frontend reads is present in dry-run response."""
    db  = _MinimalSession()
    xls = _minimal_sheet()
    result = _run(import_sales(db, xls, "test.xlsx", 1, dry_run=True))

    assert "summary" in result, "Response missing 'summary'"
    s = result["summary"]
    for field in DRY_RUN_SUMMARY_FIELDS:
        assert field in s, f"Missing field: summary.{field}"


def test_dry_run_summary_fields_are_not_none():
    """Numeric summary fields in dry-run are never None (0 is acceptable, None is not)."""
    db  = _MinimalSession()
    xls = _minimal_sheet()
    result = _run(import_sales(db, xls, "test.xlsx", 1, dry_run=True))
    s = result["summary"]

    for field in ["rows_read", "invoices_would_create", "invoices_created",
                  "line_items", "rows_skipped", "customers_auto_created",
                  "products_auto_created", "total_value"]:
        assert s[field] is not None, f"summary.{field} is None (frontend would show 'undefined')"


def test_dry_run_summary_dry_run_flag_is_true():
    """data.dry_run is True in a dry-run response (frontend uses this to pick invoices_would_create)."""
    db  = _MinimalSession()
    xls = _minimal_sheet()
    result = _run(import_sales(db, xls, "test.xlsx", 1, dry_run=True))

    assert result["dry_run"] is True


def test_dry_run_with_unknown_sku_still_has_all_summary_fields():
    """Auto-created product path doesn't break the response shape."""
    db  = _MinimalSession()   # empty products → auto-create path
    xls = _make_xlsx([["MISSING-SKU", "New Item", 1, 50.0, "Ahmed", "2026-03-01"]])
    result = _run(import_sales(db, xls, "test.xlsx", 1, dry_run=True))

    s = result["summary"]
    for field in DRY_RUN_SUMMARY_FIELDS:
        assert field in s, f"Missing field after auto-create path: summary.{field}"
    assert s["products_auto_created"] == 1


# ═════════════════════════════════════════════════════════════════════════════
# Real-import shape
# ═════════════════════════════════════════════════════════════════════════════

def test_real_import_response_contains_all_required_top_level_fields():
    """Real-import response has every top-level key the frontend depends on."""
    db  = _MinimalSession()
    xls = _minimal_sheet()
    result = _run(import_sales(db, xls, "test.xlsx", 1, dry_run=False))

    for field in TOP_LEVEL_FIELDS:
        assert field in result, f"Missing top-level field in real-import: '{field}'"


def test_real_import_response_contains_all_summary_fields():
    """Every summary field the frontend reads is present in real-import response."""
    db  = _MinimalSession()
    xls = _minimal_sheet()
    result = _run(import_sales(db, xls, "test.xlsx", 1, dry_run=False))

    assert "summary" in result
    s = result["summary"]
    for field in REAL_RUN_SUMMARY_FIELDS:
        assert field in s, f"Missing field in real-import summary: summary.{field}"


def test_real_import_dry_run_flag_is_false():
    """data.dry_run is False in a real-import response."""
    db  = _MinimalSession()
    xls = _minimal_sheet()
    result = _run(import_sales(db, xls, "test.xlsx", 1, dry_run=False))

    assert result["dry_run"] is False


def test_error_response_for_missing_columns_has_no_undefined_trap():
    """When columns are missing, the response has 'error' key (handled by frontend guard)."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["SKU", "QTY"])   # Missing Price and Date
    ws.append(["X", 1])
    buf = io.BytesIO(); wb.save(buf); xls = buf.getvalue()

    db  = _MinimalSession()
    result = _run(import_sales(db, xls, "bad.xlsx", 1, dry_run=True))

    # Must have 'error' key so the frontend check fires and renderSalesResult is NOT called
    assert "error" in result
    assert result["error"]          # must be truthy so `if (data.error)` catches it
    assert "summary" not in result  # no summary → renderSalesResult must not be called
