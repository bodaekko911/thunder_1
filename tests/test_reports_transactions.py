import asyncio
import io
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import openpyxl

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


async def read_streaming_response(response):
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    return b"".join(chunks)


def make_linked_product_expense(
    *,
    business_date=date(2026, 5, 6),
    created_at=datetime(2026, 5, 6, 10, 30, tzinfo=timezone.utc),
):
    amount = Decimal("123.45")
    category = SimpleNamespace(id=7, name="Products")
    user = SimpleNamespace(id=1, name="Admin")
    expense = SimpleNamespace(
        id=42,
        ref_number="EXP-00042",
        category=category,
        user=user,
        expense_date=business_date,
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
        receive_date=business_date,
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
    assert "_sort_date" not in data["rows"][0]


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
    assert "_sort_date" not in data["rows"][0]


def test_transactions_expense_source_displays_expense_date_not_import_time():
    expense, _receipt = make_linked_product_expense(
        business_date=date(2024, 1, 15),
        created_at=datetime(2026, 5, 7, 14, 45, tzinfo=timezone.utc),
    )
    db = FakeSession(
        [
            [],  # B2B payment lookup
            [expense],
        ]
    )

    data = run(
        reports._build_transactions_report(
            db,
            d_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
            d_to=datetime(2024, 1, 31, 23, 59, 59, tzinfo=timezone.utc),
            source="expense",
        )
    )

    assert data["total_rows"] == 1
    assert data["rows"][0]["date"] == "2024-01-15"
    assert not data["rows"][0]["date"].startswith("2026-05-07")


def test_transactions_receive_source_displays_receive_date_not_import_time():
    _expense, receipt = make_linked_product_expense(
        business_date=date(2024, 2, 10),
        created_at=datetime(2026, 5, 7, 14, 45, tzinfo=timezone.utc),
    )
    db = FakeSession(
        [
            [],  # B2B payment lookup
            [receipt],
        ]
    )

    data = run(
        reports._build_transactions_report(
            db,
            d_from=datetime(2024, 2, 1, tzinfo=timezone.utc),
            d_to=datetime(2024, 2, 29, 23, 59, 59, tzinfo=timezone.utc),
            source="receive",
        )
    )

    assert receipt.receive_date == date(2024, 2, 10)
    assert receipt.expense.expense_date == date(2024, 2, 10)
    assert data["total_rows"] == 1
    assert data["rows"][0]["date"] == "2024-02-10"
    assert not data["rows"][0]["date"].startswith("2026-05-07")


def test_transactions_sort_uses_internal_business_date_and_strips_it():
    newer_expense, _newer_receipt = make_linked_product_expense(
        business_date=date(2024, 2, 10),
        created_at=datetime(2026, 5, 7, 9, 0, tzinfo=timezone.utc),
    )
    older_expense, _older_receipt = make_linked_product_expense(
        business_date=date(2024, 1, 15),
        created_at=datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc),
    )
    older_expense.id = 43
    older_expense.ref_number = "EXP-00043"
    db = FakeSession(
        [
            [],  # B2B payment lookup
            [older_expense, newer_expense],
        ]
    )

    data = run(
        reports._build_transactions_report(
            db,
            d_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
            d_to=datetime(2024, 2, 29, 23, 59, 59, tzinfo=timezone.utc),
            source="expense",
        )
    )

    assert [row["date"] for row in data["rows"]] == ["2024-02-10", "2024-01-15"]
    assert all("_sort_date" not in row for row in data["rows"])


def test_transactions_export_uses_fixed_receive_date():
    _expense, receipt = make_linked_product_expense(
        business_date=date(2024, 2, 10),
        created_at=datetime(2026, 5, 7, 14, 45, tzinfo=timezone.utc),
    )
    db = FakeSession(
        [
            [],  # B2B payment lookup
            [receipt],
        ]
    )

    response = run(
        reports.export_transactions(
            date_from="2024-02-01",
            date_to="2024-02-29",
            source="receive",
            db=db,
        )
    )
    workbook = openpyxl.load_workbook(io.BytesIO(run(read_streaming_response(response))), data_only=True)
    sheet = workbook["Transactions"]
    exported_rows = list(sheet.iter_rows(values_only=True))
    receipt_row = next(row for row in exported_rows if row[1] == "REC-00009")

    assert receipt_row[0] == "2024-02-10"
