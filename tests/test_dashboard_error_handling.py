"""
Tests for /dashboard/data error-handling hardening.

Verifies: section-level try/except, NULL-safe arithmetic, missing accounts,
forced section failures returning _errors, partial responses on total DB
failure, and unauthenticated 401/403.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()


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
        return iter(self.all())


class _ZeroScalarDB:
    async def execute(self, _stmt):
        return _FakeResult(0)


class _ThrowAllDB:
    async def execute(self, _stmt):
        raise RuntimeError("injected total DB failure")


class _ThrowTopProductsDB:
    async def execute(self, stmt):
        stmt_str = str(stmt).lower()
        if "invoice_items" in stmt_str and "group by" in stmt_str:
            raise RuntimeError("injected top_products error")
        return _FakeResult(0)


class _NullStockProductDB:
    async def execute(self, stmt):
        stmt_str = str(stmt).lower()
        if "products" in stmt_str and "is_active" in stmt_str and "invoice" not in stmt_str:
            bad_prod = SimpleNamespace(
                id=1,
                name="BadProd",
                sku="BP001",
                stock=None,
                price=None,
                min_stock=None,
                is_active=True,
            )
            return _FakeResult([bad_prod])
        return _FakeResult(0)


def _build_client(db_class, include_auth: bool = True):
    import app.app_factory as app_factory_mod
    from app.app_factory import create_app
    from app.core import security
    from app.database import get_async_session
    from fastapi.testclient import TestClient

    async def override_session():
        yield db_class()

    async def override_user():
        return SimpleNamespace(id=1, name="Admin", role="admin", permissions="", is_active=True)

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


def test_dashboard_data_empty_db_returns_200_with_all_keys():
    client = _build_client(_ZeroScalarDB)
    response = client.get("/dashboard/data")
    assert response.status_code == 200, response.text
    data = response.json()

    expected_keys = [
        "pos_today", "pos_month", "pos_year", "b2b_today", "b2b_month", "b2b_year",
        "total_today", "total_month", "total_year", "expenses_month", "expenses_last_month",
        "b2b_outstanding", "ref_today", "ref_month", "ref_count_today", "ref_count_month",
        "invoices_today", "invoices_month", "total_customers", "b2b_clients",
        "total_products", "out_of_stock_count", "low_stock_count", "stock_value",
        "out_of_stock", "low_stock", "farm_month", "spoilage_month", "batches_month",
        "last7", "top_products", "pay_methods", "recent_sales", "_errors",
    ]
    for key in expected_keys:
        assert key in data, f"Missing key: {key}"

    assert data["_errors"] == []
    assert data["total_today"] == 0.0
    assert isinstance(data["last7"], list)


def test_null_price_stock_product_does_not_crash():
    client = _build_client(_NullStockProductDB)
    response = client.get("/dashboard/data")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["stock_value"] == 0.0
    assert data["_errors"] == []


def test_null_price_stock_arithmetic_directly():
    prods = [
        SimpleNamespace(stock=None, price=5.0, min_stock=2.0),
        SimpleNamespace(stock=3.0, price=None, min_stock=2.0),
        SimpleNamespace(stock=2.0, price=7.0, min_stock=2.0),
    ]
    stock_value = sum(float(p.stock or 0) * float(p.price or 0) for p in prods)
    assert stock_value == pytest.approx(14.0)
    assert len([p for p in prods if float(p.stock or 0) <= 0]) == 1


def test_missing_revenue_account_gives_zero_b2b():
    client = _build_client(_ZeroScalarDB)
    response = client.get("/dashboard/data")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["b2b_today"] == 0.0
    assert data["b2b_month"] == 0.0
    assert data["b2b_year"] == 0.0


def test_top_products_exception_gives_partial_response():
    client = _build_client(_ThrowTopProductsDB)
    response = client.get("/dashboard/data")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["top_products"] == []
    assert any(error["section"] == "top_products" for error in data["_errors"])
    assert "total_today" in data


def test_all_sections_fail_returns_partial_payload():
    client = _build_client(_ThrowAllDB)
    response = client.get("/dashboard/data")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["last7"] == []
    assert data["top_products"] == []
    assert len(data["_errors"]) >= 5


def test_unauthenticated_request_returns_401():
    client = _build_client(_ZeroScalarDB, include_auth=False)
    response = client.get("/dashboard/data")
    assert response.status_code in (401, 403)
