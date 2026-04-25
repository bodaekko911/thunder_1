import asyncio
import io

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from app.models.customer import Customer
from app.models.inventory import StockMove
from app.models.product import Product
from app.routers import import_data


class FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value


class FakeImportSession:
    def __init__(self, *, products=None, customers=None):
        self.products = list(products or [])
        self.customers = list(customers or [])
        self.stock_moves = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        compiled = statement.compile()
        criteria = {}
        for expr in statement._where_criteria:
            column_name = getattr(getattr(expr, "left", None), "name", None)
            if column_name is None:
                continue
            right = getattr(expr, "right", None)
            value = getattr(right, "value", None)
            if value is None and hasattr(right, "key"):
                value = compiled.params.get(right.key)
            if value is None:
                right_text = str(right).lower()
                if right_text == "true":
                    value = True
                elif right_text == "false":
                    value = False
            criteria[column_name] = value

        pool = self.products if entity is Product else self.customers
        if not criteria:
            return FakeScalarResult(list(pool))

        for item in pool:
            if all(getattr(item, field) == expected for field, expected in criteria.items()):
                return FakeScalarResult(item)
        return FakeScalarResult(None)

    def add(self, obj):
        if isinstance(obj, Product):
            if obj.id is None:
                obj.id = len(self.products) + 1
            if obj not in self.products:
                self.products.append(obj)
            return
        if isinstance(obj, Customer):
            if obj.id is None:
                obj.id = len(self.customers) + 1
            if obj not in self.customers:
                self.customers.append(obj)
            return
        if isinstance(obj, StockMove):
            self.stock_moves.append(obj)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


def _upload_workbook(filename: str, rows: list[list[object]]) -> UploadFile:
    workbook = import_data.openpyxl.Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)

    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return UploadFile(filename=filename, file=buffer)


def test_preview_file_returns_headers_rows_and_total() -> None:
    upload = _upload_workbook(
        "products.xlsx",
        [
            ["SKU", "Item", "Sales price"],
            ["1001", "Olives", 15],
            ["1002", "Cheese", 25],
        ],
    )

    payload = asyncio.run(import_data.preview_file(upload))

    assert payload["headers"] == ["SKU", "Item", "Sales price"]
    assert payload["rows"] == [["1001", "Olives", "15"], ["1002", "Cheese", "25"]]
    assert payload["total_rows"] == 2


def test_preview_file_rejects_unsupported_extension() -> None:
    upload = _upload_workbook(
        "products.xls",
        [
            ["SKU", "Item"],
            ["1001", "Olives"],
        ],
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(import_data.preview_file(upload))

    assert exc_info.value.status_code == 400
    assert "Unsupported file type" in exc_info.value.detail


def test_preview_file_rejects_oversized_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(import_data, "MAX_IMPORT_UPLOAD_BYTES", 8)
    upload = UploadFile(filename="huge.xlsx", file=io.BytesIO(b"123456789"))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(import_data.preview_file(upload))

    assert exc_info.value.status_code == 413
    assert "File too large" in exc_info.value.detail


def test_import_products_creates_and_updates_records() -> None:
    existing = Product(
        id=1,
        sku="1001",
        name="Old Olives",
        unit="pcs",
        cost=5,
        price=10,
        stock=3,
        min_stock=1,
        is_active=True,
    )
    fake_db = FakeImportSession(products=[existing])
    upload = _upload_workbook(
        "products.xlsx",
        [
            ["SKU", "Item", "UOM", "Unit Cost", "Sales price", "Group", "Item Type"],
            ["1001", "Olives", "jar", 7.5, 14, "Pantry", "finished"],
            ["1002", "Feta", "kg", 20, 32, "Dairy", "raw material"],
        ],
    )

    payload = asyncio.run(import_data.import_products(upload, fake_db))

    assert payload["ok"] is True
    assert payload["created"] == 1
    assert payload["updated"] == 1
    assert existing.name == "Olives"
    assert existing.unit == "jar"
    assert float(existing.cost) == 7.5
    assert float(existing.price) == 14
    assert existing.category == "Pantry"
    assert existing.item_type == "finished"
    new_product = next(product for product in fake_db.products if product.sku == "1002")
    assert new_product.name == "Feta"
    assert new_product.item_type == "raw"
    assert fake_db.committed is True


def test_import_products_keeps_extended_item_types() -> None:
    fake_db = FakeImportSession()
    upload = _upload_workbook(
        "products.xlsx",
        [
            ["SKU", "Item", "Item Type"],
            ["2001", "Fresh Basil", "fresh"],
            ["2002", "Paper Tray", "packing"],
            ["2003", "Citric Acid", "ingredient"],
        ],
    )

    payload = asyncio.run(import_data.import_products(upload, fake_db))

    assert payload["ok"] is True
    assert payload["created"] == 3
    assert next(product for product in fake_db.products if product.sku == "2001").item_type == "fresh"
    assert next(product for product in fake_db.products if product.sku == "2002").item_type == "packing"
    assert next(product for product in fake_db.products if product.sku == "2003").item_type == "ingredient"


def test_import_stock_updates_product_and_records_adjustment() -> None:
    existing = Product(
        id=1,
        sku="1001",
        name="Olives",
        unit="jar",
        cost=7,
        price=14,
        stock=3,
        min_stock=1,
        is_active=True,
    )
    fake_db = FakeImportSession(products=[existing])
    upload = _upload_workbook(
        "stock.xlsx",
        [
            ["SKU", "Stock"],
            ["1001", 11],
        ],
    )

    payload = asyncio.run(import_data.import_stock(upload, fake_db))

    assert payload["ok"] is True
    assert payload["updated"] == 1
    assert float(existing.stock) == 11
    assert len(fake_db.stock_moves) == 1
    move = fake_db.stock_moves[0]
    assert move.product_id == 1
    assert float(move.qty_before) == 3
    assert float(move.qty_after) == 11
    assert float(move.qty) == 8


def test_import_customers_skips_duplicate_phone_and_adds_new_customer() -> None:
    existing = Customer(id=1, name="Existing Customer", phone="0100", email=None, address=None)
    fake_db = FakeImportSession(customers=[existing])
    upload = _upload_workbook(
        "customers.xlsx",
        [
            ["Name", "Phone", "Email", "Address"],
            ["Existing Customer", "0100", "dup@example.com", "Cairo"],
            ["New Customer", "0101", "new@example.com", "Giza"],
        ],
    )

    payload = asyncio.run(import_data.import_customers(upload, fake_db))

    assert payload["ok"] is True
    assert payload["created"] == 1
    assert payload["skipped"] == 1
    assert any(customer.name == "New Customer" for customer in fake_db.customers)
