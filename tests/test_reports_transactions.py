import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import app.routers.reports as reports


class FakeResult:
    def __init__(self, value):
        self._value = value

    def scalars(self):
        return self

    def all(self):
        return self._value


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)

    async def execute(self, _statement):
        if not self._responses:
            raise AssertionError("Unexpected query in transactions report test")
        return FakeResult(self._responses.pop(0))


def run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


def make_linked_product_expense():
    created_at = datetime(2026, 5, 6, 10, 30, tzinfo=timezone.utc)
    amount = Decimal("123.45")
    category = SimpleNamespace(id=7, name="Products")
    user = SimpleNamespace(id=1, name="Admin")
    expense = SimpleNamespace(
        id=42,
        ref_number="EXP-00042",
        category=category,
        user=user,
        expense_date=date(2026, 5, 6),
        amount=amount,
        payment_method="cash",
        vendor="Supplier One",
        description="Auto expense from receive products",
        created_at=created_at,
    )
    receipt = SimpleNamespace(
        id=9,
        ref_number="REC-00009",
        product=SimpleNamespace(id=3, sku="SKU-PROD", name="Olive Oil"),
        user=user,
        receive_date=date(2026, 5, 6),
        qty=Decimal("5"),
        unit_cost=Decimal("24.69"),
        total_cost=amount,
        supplier_ref="Supplier One",
        notes="Receive Products",
        expense=expense,
        expense_id=expense.id,
        created_at=created_at,
    )
    return expense, receipt


def test_transactions_expense_source_includes_receipt_linked_products_expense():
    expense, _receipt = make_linked_product_expense()
    db = FakeSession(
        [
            [],  # B2B payment lookup
            [expense],
        ]
    )

    data = run(
        reports._build_transactions_report(
            db,
            d_from=datetime(2026, 5, 1, tzinfo=timezone.utc),
            d_to=datetime(2026, 5, 31, 23, 59, 59, tzinfo=timezone.utc),
            source="expense",
        )
    )

    assert data["total_rows"] == 1
    assert data["money_out"] == 123.45
    assert data["rows"][0]["source"] == "Expense"
    assert data["rows"][0]["transaction_type"] == "Expense"
    assert data["rows"][0]["reference"] == "EXP-00042"
    assert data["rows"][0]["product"] == "Products"
    assert data["rows"][0]["money_effect"] == -123.45


def test_transactions_all_sources_counts_receipt_linked_expense_once_as_receive():
    expense, receipt = make_linked_product_expense()
    db = FakeSession(
        [
            [],  # B2B payment lookup
            [],  # POS rows
            [],  # B2B invoice rows
            [],  # Refund rows
            [receipt],
            [(expense.id,)],
            [expense],
        ]
    )

    data = run(
        reports._build_transactions_report(
            db,
            d_from=datetime(2026, 5, 1, tzinfo=timezone.utc),
            d_to=datetime(2026, 5, 31, 23, 59, 59, tzinfo=timezone.utc),
            source=None,
        )
    )

    assert data["total_rows"] == 1
    assert data["money_out"] == 123.45
    assert data["rows"][0]["source"] == "Receive"
    assert data["rows"][0]["transaction_type"] == "Stock Receipt"
    assert data["rows"][0]["reference"] == "REC-00009"
    assert data["rows"][0]["money_effect"] == -123.45
    assert [row["source"] for row in data["rows"]] == ["Receive"]
