"""
test_b2b_print_routes.py

Guardrail tests for the three B2B print routes after the Jinja2 template
refactor.  Mocks the DB layer with canned ORM-shaped objects so we don't
need a real Postgres, and overrides the auth dependency with a synthetic
admin user (permissions="*") so the router-level
``dependencies=[Depends(require_permission("page_b2b"))]`` short-circuits.

If a future change to the templates or the route wiring breaks rendering,
these tests catch it: they assert the response is 200 HTML and that
key fields (invoice number, client name, totals, status badges) appear
in the body.
"""
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

from app.core import security
from app.database import get_async_session
from app.routers.b2b import router as b2b_router


# ── Mock ORM objects ────────────────────────────────────────────────────────

_PRODUCT = SimpleNamespace(name="Olive Oil 500ml", sku="OO-500")
_INVOICE_ITEM = SimpleNamespace(
    product=_PRODUCT, qty=10.0, unit_price=45.0, total=450.0
)
_REFUND_ITEM = SimpleNamespace(
    product=_PRODUCT, qty=2.0, unit_price=45.0, total=90.0
)
_CLIENT = SimpleNamespace(id=42, name="ACME Imports")

_INVOICE = SimpleNamespace(
    id=7,
    invoice_number="B2B-2026-0042",
    created_at=datetime(2026, 4, 21, 10, 30),
    status="paid",
    invoice_type="cash_sale",
    payment_method="transfer",
    items=[_INVOICE_ITEM, _INVOICE_ITEM],
    subtotal=900.0,
    discount=50.0,
    total=850.0,
    client=_CLIENT,
)

_REFUND = SimpleNamespace(
    id=3,
    refund_number="REF-2026-0007",
    created_at=datetime(2026, 4, 22),
    items=[_REFUND_ITEM],
    notes="Customer reported damaged packaging on delivery.",
    subtotal=90.0,
    discount=0.0,
    total=90.0,
    client=_CLIENT,
)


# ── Fake auth user with full permissions ────────────────────────────────────
# permissions="*" causes has_permission to short-circuit True for any
# permission check, so the router-level require_permission("page_b2b")
# dependency lets every request through.

def _fake_admin():
    return SimpleNamespace(
        id=1,
        name="Test Admin",
        role="admin",
        permissions="*",
        is_active=True,
    )


async def _override_user():
    return _fake_admin()


# ── Fake DB session ─────────────────────────────────────────────────────────

class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _RouteAwareSession:
    """Returns the right canned object based on the model in the SELECT."""

    async def execute(self, stmt):
        stmt_str = str(stmt)
        if "B2BInvoice" in stmt_str or "b2b_invoices" in stmt_str.lower():
            return _FakeResult(_INVOICE)
        if "B2BRefund" in stmt_str or "b2b_refunds" in stmt_str.lower():
            return _FakeResult(_REFUND)
        return _FakeResult(None)


async def _override_session():
    yield _RouteAwareSession()


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture()
def app():
    """Standalone FastAPI app with only the b2b router mounted, plus
    overrides for both auth and DB so the router-level permission check
    short-circuits and the canned ORM objects are returned."""
    fastapi_app = FastAPI()
    fastapi_app.include_router(b2b_router)
    fastapi_app.dependency_overrides[get_async_session] = _override_session
    fastapi_app.dependency_overrides[security.get_current_user] = _override_user
    return fastapi_app


@pytest.fixture()
def client(app):
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ── Tests ───────────────────────────────────────────────────────────────────

def test_invoice_print_renders_expected_html(client: TestClient) -> None:
    """GET /b2b/invoice/{id}/print → 200 HTML with invoice + client fields."""
    response = client.get("/b2b/invoice/7/print")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")

    body = response.text
    assert "<!DOCTYPE html>" in body
    assert "B2B-2026-0042" in body, "invoice number missing"
    assert "ACME Imports" in body, "client name missing"
    assert "C0042" in body, "client code missing"
    assert "Cash Sale" in body, "humanize_snake filter not applied to invoice_type"
    assert "ج.م. 850.00" in body, "total in Arabic currency missing"
    assert "ج.م. 50.00" in body, "discount amount missing"
    assert "PAID" in body, "paid status stamp missing"
    assert "Olive Oil 500ml" in body, "item name missing"


def test_refund_print_renders_expected_html(client: TestClient) -> None:
    """GET /b2b/refund/{id}/print → 200 HTML with refund + client + notes."""
    response = client.get("/b2b/refund/3/print")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")

    body = response.text
    assert "REF-2026-0007" in body, "refund number missing"
    assert "ACME Imports" in body, "client name missing"
    assert "REFUND" in body, "REFUND stamp missing"
    assert "OO-500" in body, "SKU under item missing"
    assert "ج.م. 90.00" in body, "total in Arabic currency missing"
    assert "damaged packaging" in body, "notes block missing"
    # No discount in the mock refund — the discount <tr> should not be rendered.
    # (The `.discount-row` CSS class is defined in the shared base_print.html
    # stylesheet, so we check for the table row specifically rather than the
    # class name string, which always appears in the embedded CSS.)
    assert '<tr class="discount-row">' not in body, (
        "discount row rendered when discount is 0"
    )


def test_invoice_print_returns_404_when_missing(app, client: TestClient) -> None:
    """A session that returns None for the invoice query → 404 with the
    same JSON detail the original f-string version produced.  Guards the
    `if not inv: raise HTTPException(404)` branch."""

    class _NullSession:
        async def execute(self, _stmt):
            return _FakeResult(None)

    async def _null_override():
        yield _NullSession()

    app.dependency_overrides[get_async_session] = _null_override
    try:
        response = client.get("/b2b/invoice/99999/print")
        assert response.status_code == 404
        assert response.json()["detail"] == "Invoice not found"
    finally:
        app.dependency_overrides[get_async_session] = _override_session


def test_refund_print_returns_404_when_missing(app, client: TestClient) -> None:
    """Same 404 guardrail for the refund route."""

    class _NullSession:
        async def execute(self, _stmt):
            return _FakeResult(None)

    async def _null_override():
        yield _NullSession()

    app.dependency_overrides[get_async_session] = _null_override
    try:
        response = client.get("/b2b/refund/99999/print")
        assert response.status_code == 404
        assert response.json()["detail"] == "Refund not found"
    finally:
        app.dependency_overrides[get_async_session] = _override_session
