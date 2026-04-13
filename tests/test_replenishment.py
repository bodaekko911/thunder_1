import asyncio

from app.models.product import Product
from app.models.supplier import Purchase, PurchaseItem, Supplier
from app.services.replenishment_service import create_or_reuse_draft_purchases, serialize_low_stock_product


class FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        if isinstance(self._value, list):
            return self._value[0] if self._value else None
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value


class FakeReplenishmentSession:
    def __init__(self, *, purchases=None):
        self.purchases = list(purchases or [])
        self.purchase_items = []

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

        if entity is Purchase:
            matches = self.purchases
            for field, expected in criteria.items():
                matches = [purchase for purchase in matches if getattr(purchase, field) == expected]
            return FakeScalarResult(matches)

        return FakeScalarResult([])

    def add(self, obj):
        if isinstance(obj, Purchase):
            if obj.id is None:
                obj.id = len(self.purchases) + 1
            if obj.supplier is None and obj.supplier_id is not None:
                obj.supplier = Supplier(id=obj.supplier_id, name=f"Supplier {obj.supplier_id}")
            obj.items = []
            self.purchases.append(obj)
            return
        if isinstance(obj, PurchaseItem):
            if obj.id is None:
                obj.id = len(self.purchase_items) + 1
            self.purchase_items.append(obj)
            purchase = next(purchase for purchase in self.purchases if purchase.id == obj.purchase_id)
            purchase.items.append(obj)

    async def flush(self):
        return None


def test_low_stock_metadata_includes_alerts_supplier_and_suggestion() -> None:
    supplier = Supplier(id=7, name="Acme Supplies")
    product = Product(
        id=1,
        sku="1001",
        name="Olives",
        stock=2,
        min_stock=1,
        reorder_level=5,
        reorder_qty=10,
        unit="jar",
        preferred_supplier_id=7,
    )
    product.preferred_supplier = supplier

    payload = serialize_low_stock_product(product)

    assert payload["alert_state"] == "low_stock"
    assert payload["alert_active"] is True
    assert payload["reorder_level"] == 5.0
    assert payload["reorder_qty"] == 10.0
    assert payload["suggested_reorder_qty"] == 10.0
    assert payload["preferred_supplier"] == {"id": 7, "name": "Acme Supplies"}
    assert payload["draft_purchase_eligible"] is True


def test_low_stock_metadata_uses_shortage_when_reorder_qty_is_smaller() -> None:
    product = Product(
        id=2,
        sku="1002",
        name="Feta",
        stock=1,
        min_stock=1,
        reorder_level=5,
        reorder_qty=2,
        unit="kg",
    )

    payload = serialize_low_stock_product(product)

    assert payload["suggested_reorder_qty"] == 4.0


def test_draft_purchase_generation_groups_by_supplier_and_is_idempotent() -> None:
    supplier = Supplier(id=3, name="Dairy Co")
    product_one = Product(
        id=10,
        sku="2001",
        name="Milk",
        stock=1,
        min_stock=1,
        reorder_level=4,
        reorder_qty=6,
        cost=5.5,
        preferred_supplier_id=3,
        unit="ltr",
    )
    product_two = Product(
        id=11,
        sku="2002",
        name="Yogurt",
        stock=0,
        min_stock=1,
        reorder_level=3,
        reorder_qty=2,
        cost=4,
        preferred_supplier_id=3,
        unit="cup",
    )
    product_one.preferred_supplier = supplier
    product_two.preferred_supplier = supplier

    fake_db = FakeReplenishmentSession()

    first = asyncio.run(
        create_or_reuse_draft_purchases(
            fake_db,
            products=[product_one, product_two],
            user_id=9,
        )
    )
    second = asyncio.run(
        create_or_reuse_draft_purchases(
            fake_db,
            products=[product_one, product_two],
            user_id=9,
        )
    )

    assert len(first) == 1
    assert first[0]["reused"] is False
    assert first[0]["status"] == "draft"
    assert first[0]["items_count"] == 2
    assert first[0]["total"] == 45.0
    assert len(fake_db.purchases) == 1
    assert len(fake_db.purchase_items) == 2
    assert second[0]["reused"] is True
    assert second[0]["purchase_number"] == first[0]["purchase_number"]
