import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.accounting import Account, Journal
from app.models.expense import Expense, ExpenseCategory
from app.models.hr import Employee, Payroll
from app.services.expense_service import create_payroll_expense


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


class FakePayrollExpenseSession:
    def __init__(self, *, category=None):
        self.accounts = {
            "1000": Account(id=1, code="1000", name="Cash", type="asset", balance=Decimal("0")),
        }
        self.categories = []
        if category is not None:
            self.categories.append(category)
        self.expenses = []
        self.journals = []
        self.entries = []
        self._next_id = 10

    async def execute(self, statement):
        sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
        if "max(expenses.id)" in sql:
            max_id = max([expense.id for expense in self.expenses], default=0)
            return FakeScalarResult(max_id)
        if "FROM expenses" in sql and "expenses.payroll_id" in sql:
            payroll_id = int(sql.rsplit("= ", 1)[1].split()[0])
            existing = next((expense for expense in self.expenses if expense.payroll_id == payroll_id), None)
            return FakeScalarResult(existing)
        if "FROM expense_categories" in sql:
            category = next(
                (cat for cat in self.categories if cat.name == "Salaries & Wages"),
                None,
            )
            return FakeScalarResult(category)
        if "FROM accounts" in sql:
            code = sql.rsplit("= '", 1)[1].split("'", 1)[0]
            return FakeScalarResult(self.accounts.get(code))
        raise AssertionError(f"Unexpected query: {sql}")

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = self._next_id
            self._next_id += 1
        if isinstance(obj, Account):
            self.accounts[obj.code] = obj
        elif isinstance(obj, ExpenseCategory):
            self.categories.append(obj)
        elif isinstance(obj, Expense):
            self.expenses.append(obj)
        elif isinstance(obj, Journal):
            self.journals.append(obj)
        elif obj.__class__.__name__ == "JournalEntry":
            self.entries.append(obj)

    async def flush(self):
        return None


def _payroll(amount=Decimal("1250.75")):
    payroll = Payroll(
        id=7,
        employee_id=3,
        period="2026-04",
        net_salary=amount,
        paid=False,
    )
    payroll.employee = Employee(id=3, name="Mona Salary")
    return payroll


def _user():
    return SimpleNamespace(id=1, name="Admin", role="admin")


def test_marking_payroll_paid_creates_exactly_one_salary_expense():
    category = ExpenseCategory(id=5, name="Salaries & Wages", account_code="5006", is_active="1")
    db = FakePayrollExpenseSession(category=category)

    first = asyncio.run(create_payroll_expense(db, _payroll(), _user(), payment_method="cash"))
    second = asyncio.run(create_payroll_expense(db, _payroll(), _user(), payment_method="cash"))

    assert first is second
    assert len(db.expenses) == 1
    assert first.category_id == 5
    assert first.amount == 1250.75
    assert first.payroll_id == 7
    assert first.vendor == "Mona Salary"
    assert "payroll #7" in first.description
    assert len(db.journals) == 1
    assert len(db.entries) == 2


def test_missing_salary_category_is_created_with_account_code_5006():
    db = FakePayrollExpenseSession()

    expense = asyncio.run(create_payroll_expense(db, _payroll(), _user()))

    category = next(cat for cat in db.categories if cat.name == "Salaries & Wages")
    assert category.account_code == "5006"
    assert category.is_active == "1"
    assert expense.category_id == category.id


@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("-1")])
def test_payroll_expense_rejects_non_positive_net_salary(amount):
    db = FakePayrollExpenseSession()

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(create_payroll_expense(db, _payroll(amount), _user()))

    assert exc_info.value.status_code == 400
    assert "greater than 0" in exc_info.value.detail
    assert db.expenses == []


def test_payroll_expense_uses_paid_date_and_payment_method():
    category = ExpenseCategory(id=5, name="Salaries & Wages", account_code="5006", is_active="1")
    db = FakePayrollExpenseSession(category=category)

    expense = asyncio.run(
        create_payroll_expense(
            db,
            _payroll(),
            _user(),
            payment_method="bank_transfer",
            paid_date=date(2026, 4, 30),
        )
    )

    assert expense.payment_method == "bank_transfer"
    assert expense.expense_date == date(2026, 4, 30)
