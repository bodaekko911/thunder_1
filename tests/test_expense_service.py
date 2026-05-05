import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.schemas.expense import ExpenseCreate
from app.services import expense_service


class FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value


class FakeSession:
    def __init__(self, results):
        self._results = list(results)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return FakeScalarResult(self._results.pop(0))


def test_payment_account_code_mapping() -> None:
    assert expense_service._payment_account_code("cash") == "1000"
    assert expense_service._payment_account_code("card") == "1000"
    assert expense_service._payment_account_code("bank_transfer") == "1200"


def test_create_expense_entry_rejects_invalid_date() -> None:
    fake_db = FakeSession(
        [
            SimpleNamespace(id=7, name="Utilities", account_code="5001"),
        ]
    )
    payload = ExpenseCreate(
        category_id=7,
        expense_date="2026-13-99",
        amount=120.0,
        payment_method="cash",
    )
    user = SimpleNamespace(id=1, name="Admin")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(expense_service.create_expense_entry(fake_db, payload, user))

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Invalid date format - use YYYY-MM-DD"


def test_archive_category_rejects_category_with_existing_expenses() -> None:
    fake_db = FakeSession(
        [
            SimpleNamespace(id=1, expenses=[SimpleNamespace(id=9)]),
        ]
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(expense_service.archive_category(fake_db, 1))

    assert exc_info.value.status_code == 400
    assert "Archive it instead" in exc_info.value.detail


def test_list_expenses_returns_expenses_with_no_filters() -> None:
    fake_db = FakeSession(
        [
            [
                SimpleNamespace(
                    id=1,
                    ref_number="EXP-00001",
                    category_id=7,
                    category=SimpleNamespace(name="Utilities", account_code="5001"),
                    user=SimpleNamespace(name="Admin"),
                    farm=SimpleNamespace(name="North Farm"),
                    farm_id=2,
                    expense_date=date(2026, 4, 10),
                    amount=Decimal("125.50"),
                    payment_method="cash",
                    vendor="Power Co",
                    description="Monthly bill",
                )
            ]
        ]
    )

    result = asyncio.run(expense_service.list_expenses(fake_db))

    assert result == [
        {
            "id": 1,
            "ref_number": "EXP-00001",
            "category": "Utilities",
            "category_id": 7,
            "account_code": "5001",
            "expense_date": "2026-04-10",
            "amount": 125.5,
            "payment_method": "cash",
            "vendor": "Power Co",
            "description": "Monthly bill",
            "created_by": "Admin",
            "farm_id": 2,
            "farm_name": "North Farm",
        }
    ]


def test_list_expenses_applies_category_filter() -> None:
    fake_db = FakeSession([[]])

    asyncio.run(expense_service.list_expenses(fake_db, category_id=7))

    sql = str(fake_db.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "expenses.category_id = 7" in sql


def test_list_expenses_applies_date_range_filters() -> None:
    fake_db = FakeSession([[]])

    asyncio.run(
        expense_service.list_expenses(
            fake_db,
            date_from="2026-04-01",
            date_to="2026-04-30",
        )
    )

    sql = str(fake_db.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "expenses.expense_date >= '2026-04-01'" in sql
    assert "expenses.expense_date <= '2026-04-30'" in sql


def test_list_expenses_rejects_invalid_date_filters_without_500() -> None:
    fake_db = FakeSession([[]])

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            expense_service.list_expenses(
                fake_db,
                date_from="2026-99-99",
                date_to="not-a-date",
            )
        )

    assert exc_info.value.status_code == 400
    assert "YYYY-MM-DD" in exc_info.value.detail
    assert fake_db.statements == []


def test_list_expenses_uses_month_only_without_date_range() -> None:
    fake_db = FakeSession([[]])

    asyncio.run(
        expense_service.list_expenses(
            fake_db,
            month="2026-04",
            date_from="2026-04-01",
            date_to="2026-04-30",
        )
    )

    sql = str(fake_db.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "EXTRACT(year FROM expenses.expense_date)" not in sql
    assert "expenses.expense_date >= '2026-04-01'" in sql
    assert "expenses.expense_date <= '2026-04-30'" in sql
