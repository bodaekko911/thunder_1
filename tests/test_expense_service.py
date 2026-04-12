import asyncio
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

    async def execute(self, _statement):
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
