from collections.abc import AsyncGenerator
from types import SimpleNamespace

from fastapi.testclient import TestClient

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

import app.app_factory as app_factory
from app.app_factory import create_app
from app.core import security
from app.database import get_async_session


class FakePermissionSession:
    def __init__(self) -> None:
        self.logged = []

    def add(self, obj) -> None:
        self.logged.append(obj)

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


def _make_client(user) -> tuple[TestClient, FakePermissionSession]:
    fake_db = FakePermissionSession()

    async def override_session() -> AsyncGenerator[FakePermissionSession, None]:
        yield fake_db

    async def override_user():
        return user

    async def noop() -> None:
        return None

    app_factory.configure_logging = lambda: None
    app_factory.configure_monitoring = lambda: None
    app_factory.verify_migration_status = noop

    app = create_app()
    app.dependency_overrides[get_async_session] = override_session
    app.dependency_overrides[security.get_current_user] = override_user
    return TestClient(app), fake_db


def test_retail_refund_requires_explicit_refund_permission() -> None:
    user = SimpleNamespace(
        id=8,
        name="Limited POS User",
        role="viewer",
        permissions="page_pos,action_pos_create_sale",
        is_active=True,
    )
    client, fake_db = _make_client(user)

    response = client.post(
        "/refunds/api/create",
        json={
            "invoice_id": 1,
            "reason": "Damaged",
            "refund_method": "cash",
            "items": [{"product_id": 1, "qty": 1}],
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission denied: action_pos_refund"
    assert any(log.action == "PERMISSION_DENIED" and log.ref_id == "action_pos_refund" for log in fake_db.logged)


def test_b2b_delete_requires_explicit_delete_permission() -> None:
    user = SimpleNamespace(
        id=9,
        name="B2B Viewer",
        role="viewer",
        permissions="page_b2b,tab_b2b_invoices",
        is_active=True,
    )
    client, fake_db = _make_client(user)

    response = client.delete("/b2b/api/invoices/12")

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission denied: action_b2b_delete"
    assert any(log.action == "PERMISSION_DENIED" and log.ref_id == "action_b2b_delete" for log in fake_db.logged)


def test_expense_edit_requires_explicit_update_permission() -> None:
    user = SimpleNamespace(
        id=10,
        name="Expense Viewer",
        role="viewer",
        permissions="page_expenses",
        is_active=True,
    )
    client, fake_db = _make_client(user)

    response = client.put(
        "/expenses/api/edit/12",
        json={
            "amount": 50,
            "expense_date": "2026-04-14",
            "payment_method": "cash",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission denied: action_expenses_update"
    assert any(
        log.action == "PERMISSION_DENIED" and log.ref_id == "action_expenses_update"
        for log in fake_db.logged
    )


def test_receive_products_create_requires_explicit_create_permission() -> None:
    user = SimpleNamespace(
        id=11,
        name="Receive Viewer",
        role="viewer",
        permissions="page_receive_products",
        is_active=True,
    )
    client, fake_db = _make_client(user)

    response = client.post(
        "/receive/api/receive",
        json={
            "product_id": 1,
            "qty": 2,
            "unit_cost": 5,
            "receive_date": "2026-04-14",
            "supplier_ref": "INV-1",
            "notes": "test",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission denied: action_receive_products_create"
    assert any(
        log.action == "PERMISSION_DENIED" and log.ref_id == "action_receive_products_create"
        for log in fake_db.logged
    )


def test_receive_products_update_requires_explicit_update_permission() -> None:
    user = SimpleNamespace(
        id=12,
        name="Receive Viewer",
        role="viewer",
        permissions="page_receive_products",
        is_active=True,
    )
    client, fake_db = _make_client(user)

    response = client.put(
        "/receive/api/receipt/4",
        json={
            "qty": 2,
            "unit_cost": 5,
            "receive_date": "2026-04-14",
            "supplier_ref": "INV-1",
            "notes": "test",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission denied: action_receive_products_update"
    assert any(
        log.action == "PERMISSION_DENIED" and log.ref_id == "action_receive_products_update"
        for log in fake_db.logged
    )


def test_receive_products_delete_requires_explicit_delete_permission() -> None:
    user = SimpleNamespace(
        id=13,
        name="Receive Viewer",
        role="viewer",
        permissions="page_receive_products",
        is_active=True,
    )
    client, fake_db = _make_client(user)

    response = client.delete("/receive/api/receipt/4")

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission denied: action_receive_products_delete"
    assert any(
        log.action == "PERMISSION_DENIED" and log.ref_id == "action_receive_products_delete"
        for log in fake_db.logged
    )


def test_receive_products_export_requires_explicit_export_permission() -> None:
    user = SimpleNamespace(
        id=14,
        name="Receive Viewer",
        role="viewer",
        permissions="page_receive_products",
        is_active=True,
    )
    client, fake_db = _make_client(user)

    response = client.get("/receive/api/export.xlsx")

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission denied: action_receive_products_export"
    assert any(
        log.action == "PERMISSION_DENIED" and log.ref_id == "action_receive_products_export"
        for log in fake_db.logged
    )
