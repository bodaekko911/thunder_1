import asyncio
from collections.abc import AsyncGenerator
from types import SimpleNamespace

from fastapi.testclient import TestClient

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

import app.app_factory as app_factory
from app.app_factory import create_app
from app.core import security
from app.database import get_async_session
from app.models.product import Product
from app.services.barcode_service import find_product_by_barcode, normalize_barcode_value


class FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        if isinstance(self._value, list):
            return self._value[0] if len(self._value) == 1 else None
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value


class FakeBarcodeSession:
    def __init__(self, products):
        self.products = list(products)

    async def execute(self, statement):
        entity = statement.column_descriptions[0].get("entity")
        if entity is Product:
            return FakeScalarResult(list(self.products))
        return FakeScalarResult([])


def test_normalize_barcode_value_handles_whitespace_and_unicode() -> None:
    assert normalize_barcode_value("  ABC 123 \t\n") == "abc123"
    assert normalize_barcode_value("ＡＢＣ１２３") == "abc123"


def test_find_product_by_barcode_matches_normalized_sku() -> None:
    product = Product(id=1, sku="Abc 123", name="Olives", price=10, stock=5, is_active=True)
    fake_db = FakeBarcodeSession([product])

    found = asyncio.run(find_product_by_barcode(fake_db, "  abc123\t"))

    assert found is product


def test_barcode_lookup_endpoint_returns_clear_not_found_message() -> None:
    fake_db = FakeBarcodeSession([])

    async def override_session() -> AsyncGenerator[FakeBarcodeSession, None]:
        yield fake_db

    async def override_user():
        return SimpleNamespace(id=1, name="Cashier", role="admin", permissions=["*"], is_active=True)

    async def noop() -> None:
        return None

    app_factory.configure_logging = lambda: None
    app_factory.configure_monitoring = lambda: None
    app_factory.verify_migration_status = noop

    app = create_app()
    app.dependency_overrides[get_async_session] = override_session
    app.dependency_overrides[security.get_current_user] = override_user

    with TestClient(app) as client:
        response = client.get("/barcode-lookup", params={"barcode": "  missing-001\t"})

    assert response.status_code == 200
    assert response.json() == {
        "found": False,
        "barcode": "missing-001",
        "detail": "No product found for barcode 'missing-001'.",
    }
