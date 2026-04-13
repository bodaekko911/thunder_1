import asyncio

from app.models.inventory import LocationStock, StockLocation, StockTransfer
from app.models.product import Product
from app.services.location_inventory_service import (
    create_stock_transfer,
    serialize_product_location_stock,
)


class FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        if isinstance(self._value, list):
            if len(self._value) == 1:
                return self._value[0]
            return None
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value


class FakeLocationSession:
    def __init__(self, *, location_stocks=None, transfers=None):
        self.location_stocks = list(location_stocks or [])
        self.transfers = list(transfers or [])

    async def execute(self, statement):
        entity = statement.column_descriptions[0].get("entity")
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
            criteria[column_name] = value

        if entity is LocationStock:
            matches = self.location_stocks
            for field, expected in criteria.items():
                matches = [stock for stock in matches if getattr(stock, field) == expected]
            return FakeScalarResult(matches)

        return FakeScalarResult([])

    def add(self, obj):
        if isinstance(obj, LocationStock):
            if obj.id is None:
                obj.id = len(self.location_stocks) + 1
            if obj not in self.location_stocks:
                self.location_stocks.append(obj)
            return
        if isinstance(obj, StockTransfer):
            if obj.id is None:
                obj.id = len(self.transfers) + 1
            self.transfers.append(obj)

    async def flush(self):
        return None


def test_serialize_product_location_stock_includes_assigned_and_unassigned_qty() -> None:
    product = Product(id=4, sku="1004", name="Tahini", unit="jar", stock=10)
    warehouse = StockLocation(id=1, name="Main Warehouse", code="MAIN", location_type="warehouse", is_active=True)
    store = StockLocation(id=2, name="Front Store", code="SHOP", location_type="store", is_active=True)

    first = LocationStock(id=1, product_id=4, location_id=1, qty=4)
    first.location = warehouse
    second = LocationStock(id=2, product_id=4, location_id=2, qty=3.5)
    second.location = store

    payload = serialize_product_location_stock(product, [second, first])

    assert payload["product_id"] == 4
    assert payload["assigned_stock"] == 7.5
    assert payload["unassigned_stock"] == 2.5
    assert payload["locations"] == [
        {
            "location_id": 2,
            "location_name": "Front Store",
            "location_code": "SHOP",
            "location_type": "store",
            "qty": 3.5,
        },
        {
            "location_id": 1,
            "location_name": "Main Warehouse",
            "location_code": "MAIN",
            "location_type": "warehouse",
            "qty": 4.0,
        },
    ]


def test_create_stock_transfer_moves_qty_and_creates_destination_stock() -> None:
    product = Product(id=10, sku="2010", name="Olive Oil", unit="ltr", stock=12)
    source = StockLocation(id=5, name="Warehouse A", code="A", location_type="warehouse", is_active=True)
    destination = StockLocation(id=6, name="Store 1", code="S1", location_type="store", is_active=True)
    source_stock = LocationStock(id=1, product_id=10, location_id=5, qty=7)
    source_stock.location = source

    fake_db = FakeLocationSession(location_stocks=[source_stock])

    summary = asyncio.run(
        create_stock_transfer(
            fake_db,
            product=product,
            source_location=source,
            destination_location=destination,
            qty=3,
            user_id=8,
            note="Rebalance to store",
        )
    )

    destination_stock = next(stock for stock in fake_db.location_stocks if stock.location_id == 6)

    assert float(source_stock.qty) == 4
    assert float(destination_stock.qty) == 3
    assert len(fake_db.transfers) == 1
    assert summary["transfer"]["source_location"] == "Warehouse A"
    assert summary["transfer"]["destination_location"] == "Store 1"
    assert summary["source_qty_before"] == 7.0
    assert summary["source_qty_after"] == 4.0
    assert summary["destination_qty_before"] == 0.0
    assert summary["destination_qty_after"] == 3.0


def test_create_stock_transfer_rejects_invalid_requests() -> None:
    product = Product(id=11, sku="2011", name="Cheese", unit="kg", stock=4)
    source = StockLocation(id=7, name="Warehouse B", code="B", location_type="warehouse", is_active=True)
    destination = StockLocation(id=8, name="Bin 3", code="BIN3", location_type="bin", is_active=True)
    source_stock = LocationStock(id=1, product_id=11, location_id=7, qty=2)
    source_stock.location = source

    fake_db = FakeLocationSession(location_stocks=[source_stock])

    try:
        asyncio.run(
            create_stock_transfer(
                fake_db,
                product=product,
                source_location=source,
                destination_location=source,
                qty=1,
                user_id=5,
            )
        )
    except ValueError as exc:
        assert str(exc) == "Source and destination locations must be different"
    else:
        raise AssertionError("Expected same-location transfer to fail")

    try:
        asyncio.run(
            create_stock_transfer(
                fake_db,
                product=product,
                source_location=source,
                destination_location=destination,
                qty=3,
                user_id=5,
            )
        )
    except ValueError as exc:
        assert "Insufficient stock" in str(exc)
    else:
        raise AssertionError("Expected insufficient-stock transfer to fail")

    try:
        asyncio.run(
            create_stock_transfer(
                fake_db,
                product=product,
                source_location=source,
                destination_location=destination,
                qty=0,
                user_id=5,
            )
        )
    except ValueError as exc:
        assert str(exc) == "Transfer quantity must be greater than 0"
    else:
        raise AssertionError("Expected zero-qty transfer to fail")
