"""
Tests for /dashboard/data error-handling hardening.

Verifies: section-level try/except, NULL-safe arithmetic, missing accounts,
forced section failures returning _errors, top-level safety net 500,
and unauthenticated 401.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tests.env_defaults import apply_test_environment_defaults
apply_test_environment_defaults()


# ── shared fake-DB primitives ──────────────────────────────────────────


class _FakeResult:
    def __init__(self, value=None):
        self._v = value

    def scalar(self):
        return self._v

    def scalar_one_or_none(self):
        return self._v if self._v else None

    def scalars(self):
        return self

    def all(self):
        return self._v if isinstance(self._v, list) else []

    def one(self):
        if isinstance(self._v, (list, tuple)):
            return self._v
        return (self._v, self._v)

    def one_or_none(self):
        return None

    def __iter__(self):
        items = self._v if isinstance(self._v, list) else []
        return iter(items)


class _ZeroScalarDB:
    """Returns scalar 0 for every query — simulates empty database."""
    async def execute(self, _stmt):
        return _FakeResult(0)


class _ThrowAllDB:
    """Every execute raises — triggers the top-level safety net."""
    async def execute(self, _stmt):
        raise RuntimeError("injected total DB failure")


class _ThrowTopProductsDB:
    """Throws only on the top-products GROUP BY query, zero for everything else."""
    async def execute(self, stmt):
        stmt_str = str(stmt).lower()
        if "invoice_items" in stmt_str and "group by" in stmt_str:
            raise RuntimeError("injected top_products error")
        return _FakeResult(0)


class _NullStockProductDB:
    """
    Returns a product with stock=None and price=None for the inventory
    scalars() query; zero for everything else.
    The new NULL-safe arithmetic must handle this without crashing.
    """
    async def execute(self, stmt):
        stmt_str = str(stmt).lower()
        # Detect the "SELECT product WHERE is_active" query
        if "products" in stmt_str and "is_active" in stmt_str and "invoice" not in stmt_str:
            bad_prod = SimpleNamespace(
                id=1, name="BadProd", sku="BP001",
                stock=None, price=None, min_stock=None, is_active=True,
            )
            return _FakeResult([bad_prod])
        return _FakeResult(0)


# ── TestClient setup helper ────────────────────────────────────────────


def _build_client(db_class, include_auth: bool = True):
    import app.app_factory as app_factory_mod
    from app.app_factory import create_app
    from app.database import get_async_session
    from app.core import security
    from fastapi.testclient import TestClient

    async def override_session():
        yield db_class()

    async def override_user():
        return SimpleNamespace(
            id=1, name="Admin", role="admin",
            permissions="", is_active=True,
        )

    async def noop():
        return None

    app_factory_mod.configure_logging = lambda: None
    app_factory_mod.configure_monitoring = lambda: None
    app_factory_mod.verify_migration_status = noop

    app = create_app()
    app.dependency_overrides[get_async_session] = override_session
    if include_auth:
        app.dependency_overrides[security.get_current_user] = override_user

    return TestClient(app)


# ── test 1: happy path (empty DB) ─────────────────────────────────────


def test_dashboard_data_empty_db_returns_200_with_all_keys():
    """Empty DB returns 200 with all expected keys and _errors = []."""
    client = _build_client(_ZeroScalarDB)
    r = client.get("/dashboard/data")
    assert r.status_code == 200, r.text
    d = r.json()

    expected_keys = [
        "pos_today", "pos_month", "pos_year",
        "b2b_today", "b2b_month", "b2b_year",
        "total_today", "total_month", "total_year",
        "expenses_month", "expenses_last_month",
        "b2b_outstanding", "ref_today", "ref_month",
        "ref_count_today", "ref_count_month",
        "invoices_today", "invoices_month",
        "total_customers", "b2b_clients",
        "total_products", "out_of_stock_count", "low_stock_count",
        "stock_value", "out_of_stock", "low_stock",
        "farm_month", "spoilage_month", "batches_month",
        "last7", "top_products", "pay_methods", "recent_sales",
        "_errors",
    ]
    for key in expected_keys:
        assert key in d, f"Missing key: {key}"

    assert d["_errors"] == [], f"Expected no errors but got: {d['_errors']}"
    assert d["total_today"] == 0.0
    assert isinstance(d["last7"], list)


# ── test 2: NULL price / stock product doesn't crash ──────────────────


def test_null_price_stock_product_does_not_crash():
    """Product with NULL stock and price returns 200 with stock_value=0."""
    client = _build_client(_NullStockProductDB)
    r = client.get("/dashboard/data")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["stock_value"] == 0.0
    assert d["_errors"] == [], f"Unexpected section errors: {d['_errors']}"


def test_null_price_stock_arithmetic_directly():
    """Unit-level: NULL-safe arithmetic in stock_value formula doesn't raise."""
    prods = [
        SimpleNamespace(stock=None, price=5.0, min_stock=2.0),
        SimpleNamespace(stock=3.0, price=None, min_stock=2.0),
        SimpleNamespace(stock=2.0, price=7.0, min_stock=2.0),
    ]
    stock_value = sum(float(p.stock or 0) * float(p.price or 0) for p in prods)
    assert stock_value == pytest.approx(14.0)

    out_of_stock = [p for p in prods if float(p.stock or 0) <= 0]
    assert len(out_of_stock) == 1   # stock=None → 0 → ≤ 0


# ── test 3: missing "4000" account → b2b totals are 0 ────────────────


def test_missing_revenue_account_gives_zero_b2b():
    """No Account(code='4000') → b2b_today/month/year all 0, no crash."""
    client = _build_client(_ZeroScalarDB)
    r = client.get("/dashboard/data")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["b2b_today"] == 0.0
    assert d["b2b_month"] == 0.0
    assert d["b2b_year"]  == 0.0


# ── test 4: forced top_products exception → 200 with _errors ─────────


def test_top_products_exception_gives_partial_response():
    """Exception only in top_products → 200 with empty top_products + _errors entry."""
    client = _build_client(_ThrowTopProductsDB)
    r = client.get("/dashboard/data")
    assert r.status_code == 200, r.text
    d = r.json()

    assert d["top_products"] == [], "top_products should be empty on error"
    assert any(e["section"] == "top_products" for e in d["_errors"]), (
        f"Expected top_products in _errors, got: {d['_errors']}"
    )
    # Other sections (that don't touch invoice_items with GROUP BY) should be fine
    assert "total_today" in d


# ── test 5: all sections fail → 500 with structured detail ───────────


def test_all_sections_fail_returns_500():
    """Total DB failure triggers top-level safety net → 500 with structured detail."""
    client = _build_client(_ThrowAllDB)
    r = client.get("/dashboard/data")
    assert r.status_code == 500, r.text
    body = r.json()
    assert body.get("detail", {}).get("error") == "dashboard_data_failed"
    assert "hint" in body["detail"]


# ── test 6: unauthenticated → 401 ────────────────────────────────────


def test_unauthenticated_request_returns_401():
    """No auth cookie → require_permission raises 401 or 403 (not 200)."""
    client = _build_client(_ZeroScalarDB, include_auth=False)
    r = client.get("/dashboard/data")
    assert r.status_code in (401, 403), (
        f"Expected 401/403 without auth, got {r.status_code}"
    )
