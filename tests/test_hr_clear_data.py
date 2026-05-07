from collections.abc import AsyncGenerator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

import app.app_factory as app_factory
import app.models  # noqa: F401
import app.routers.hr as hr
from app.app_factory import create_app
from app.core import security
from app.core.log import ActivityLog
from app.database import Base, get_async_session
from app.models.accounting import Account, Journal, JournalEntry
from app.models.b2b import B2BClient, B2BInvoice
from app.models.customer import Customer
from app.models.expense import Expense, ExpenseCategory
from app.models.hr import Attendance, Employee, Payroll
from app.models.invoice import Invoice
from app.models.product import Product
from app.models.user import User
from app.services.expense_service import SALARY_CATEGORY_NAME


class AsyncSessionAdapter:
    def __init__(self, session):
        self.session = session

    async def execute(self, statement, params=None):
        return self.session.execute(statement, params or {})

    def add(self, obj):
        self.session.add(obj)

    async def flush(self):
        self.session.flush()

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()

    async def refresh(self, obj):
        self.session.refresh(obj)


def make_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    return Session()


def make_client(monkeypatch: pytest.MonkeyPatch, session, user) -> TestClient:
    async def override_session() -> AsyncGenerator[AsyncSessionAdapter, None]:
        yield AsyncSessionAdapter(session)

    async def override_user():
        return user

    async def noop() -> None:
        return None

    monkeypatch.setattr(app_factory, "configure_logging", lambda: None)
    monkeypatch.setattr(app_factory, "configure_monitoring", lambda: None)
    monkeypatch.setattr(app_factory, "verify_migration_status", noop)
    monkeypatch.setattr(app_factory, "seed_chart_of_accounts", noop)

    app = create_app()
    app.dependency_overrides[get_async_session] = override_session
    app.dependency_overrides[security.get_current_user] = override_user
    return TestClient(app)


def count(session, model) -> int:
    return session.execute(select(func.count(model.id))).scalar_one()


def make_user(session, *, role="admin", permissions="") -> User:
    user = User(
        name=f"{role.title()} User",
        email=f"{role}-{len(session.identity_map)}@example.com",
        password="x",
        role=role,
        permissions=permissions,
        is_active=True,
    )
    session.add(user)
    session.commit()
    return user


def seed_business_and_hr_data(session, user: User) -> dict:
    product = Product(sku="SKU-HR-CLEAR", name="Tomatoes", price=Decimal("10.00"), stock=5)
    customer = Customer(name="Retail Customer")
    b2b_client = B2BClient(name="B2B Client")
    salary_category = ExpenseCategory(name=SALARY_CATEGORY_NAME, account_code="5006", is_active="1")
    utilities_category = ExpenseCategory(name="Utilities", account_code="5001", is_active="1")
    salary_account = Account(code="5006", name="Salaries & Wages", type="expense", balance=Decimal("1000.00"))
    cash_account = Account(code="1000", name="Cash", type="asset", balance=Decimal("-1000.00"))
    employee = Employee(name="Mona Salary", base_salary=Decimal("1000.00"))
    session.add_all(
        [
            product,
            customer,
            b2b_client,
            salary_category,
            utilities_category,
            salary_account,
            cash_account,
            employee,
        ]
    )
    session.flush()

    invoice = Invoice(
        invoice_number="INV-HR-CLEAR",
        customer_id=customer.id,
        user_id=user.id,
        total=Decimal("10.00"),
    )
    b2b_invoice = B2BInvoice(
        invoice_number="B2B-HR-CLEAR",
        client_id=b2b_client.id,
        user_id=user.id,
        invoice_type="full_payment",
        total=Decimal("20.00"),
    )
    payroll = Payroll(
        employee_id=employee.id,
        period="2026-04",
        base_salary=Decimal("1000.00"),
        net_salary=Decimal("1000.00"),
        paid=True,
    )
    session.add_all([invoice, b2b_invoice, payroll])
    session.flush()

    journal = Journal(ref_type="expense", description="Salary expense", user_id=user.id)
    session.add(journal)
    session.flush()
    session.add_all(
        [
            JournalEntry(journal_id=journal.id, account_id=salary_account.id, debit=Decimal("1000.00"), credit=0),
            JournalEntry(journal_id=journal.id, account_id=cash_account.id, debit=0, credit=Decimal("1000.00")),
            Attendance(employee_id=employee.id, date=date(2026, 4, 1), status="present"),
            Attendance(employee_id=employee.id, date=date(2026, 4, 2), status="absent"),
            Expense(
                ref_number="EXP-HR-001",
                category_id=salary_category.id,
                user_id=user.id,
                expense_date=date(2026, 4, 30),
                amount=Decimal("1000.00"),
                payment_method="cash",
                vendor="Mona Salary",
                description="Salary payment - Mona Salary - 2026-04 - payroll #1",
                journal_id=journal.id,
                payroll_id=payroll.id,
            ),
            Expense(
                ref_number="EXP-UTIL-001",
                category_id=utilities_category.id,
                user_id=user.id,
                expense_date=date(2026, 4, 15),
                amount=Decimal("125.00"),
                payment_method="cash",
                vendor="Power Co",
                description="Unrelated utilities",
            ),
        ]
    )
    session.commit()
    return {
        "salary_account_id": salary_account.id,
        "cash_account_id": cash_account.id,
    }


def test_clear_hr_data_requires_explicit_permission(monkeypatch):
    with make_session() as session:
        user = make_user(session, role="viewer", permissions="page_hr")
        client = make_client(monkeypatch, session, user)

        response = client.post("/hr/clear-data", json={"confirmation": "CLEAR HR DATA"})

        assert response.status_code == 403
        assert response.json()["detail"] == "Permission denied: action_hr_clear_data"
        assert any(
            log.action == "PERMISSION_DENIED" and log.ref_id == "action_hr_clear_data"
            for log in session.execute(select(ActivityLog)).scalars().all()
        )


def test_clear_hr_data_requires_exact_confirmation(monkeypatch):
    with make_session() as session:
        user = make_user(session)
        client = make_client(monkeypatch, session, user)

        missing = client.post("/hr/clear-data")
        wrong = client.post("/hr/clear-data", json={"confirmation": "clear hr data"})

        assert missing.status_code == 400
        assert wrong.status_code == 400
        assert missing.json()["detail"] == 'Type "CLEAR HR DATA" to confirm.'
        assert wrong.json()["detail"] == 'Type "CLEAR HR DATA" to confirm.'


def test_clear_hr_data_deletes_only_hr_scope(monkeypatch):
    with make_session() as session:
        user = make_user(session)
        account_ids = seed_business_and_hr_data(session, user)
        client = make_client(monkeypatch, session, user)

        response = client.post("/hr/clear-data", json={"confirmation": "CLEAR HR DATA"})

        assert response.status_code == 200
        assert response.json() == {
            "ok": True,
            "deleted": {
                "attendance": 2,
                "payroll": 1,
                "employees": 1,
                "hr_expenses": 1,
            },
        }
        assert count(session, Attendance) == 0
        assert count(session, Payroll) == 0
        assert count(session, Employee) == 0
        assert session.execute(select(func.count(Expense.id)).where(Expense.payroll_id.is_not(None))).scalar_one() == 0
        assert session.execute(select(Expense).where(Expense.ref_number == "EXP-UTIL-001")).scalar_one()
        assert count(session, Product) == 1
        assert count(session, Invoice) == 1
        assert count(session, B2BInvoice) == 1
        assert count(session, User) == 1
        assert count(session, JournalEntry) == 0
        assert count(session, Journal) == 0
        salary_account = session.get(Account, account_ids["salary_account_id"])
        cash_account = session.get(Account, account_ids["cash_account_id"])
        assert salary_account.balance == Decimal("0.00")
        assert cash_account.balance == Decimal("0.00")
        assert session.execute(select(ActivityLog).where(ActivityLog.action == "clear_hr_data")).scalar_one()


def test_clear_hr_data_allows_custom_grant(monkeypatch):
    with make_session() as session:
        user = make_user(session, role="viewer", permissions="page_hr,action_hr_clear_data")
        client = make_client(monkeypatch, session, user)

        response = client.post("/hr/clear-data", json={"confirmation": "CLEAR HR DATA"})

        assert response.status_code == 200
        assert response.json()["ok"] is True


def test_clear_hr_data_rolls_back_if_delete_flow_fails(monkeypatch):
    with make_session() as session:
        user = make_user(session)
        seed_business_and_hr_data(session, user)
        client = make_client(monkeypatch, session, user)

        def fail_record(*_args, **_kwargs):
            raise RuntimeError("audit unavailable")

        monkeypatch.setattr(hr, "record", fail_record)

        response = client.post("/hr/clear-data", json={"confirmation": "CLEAR HR DATA"})

        assert response.status_code == 500
        assert response.json()["detail"] == "Could not clear HR data. No records were deleted."
        assert count(session, Attendance) == 2
        assert count(session, Payroll) == 1
        assert count(session, Employee) == 1
        assert session.execute(select(func.count(Expense.id)).where(Expense.payroll_id.is_not(None))).scalar_one() == 1
        assert count(session, Product) == 1
        assert count(session, Invoice) == 1
        assert count(session, User) == 1


def test_hr_page_exposes_clear_data_modal_only_through_permission_gate():
    source = Path("app/routers/hr.py").read_text(encoding="utf-8")

    assert 'id="btn-clear-hr-data"' in source
    assert 'hasPermission("action_hr_clear_data", u)' in source
    assert 'input.value !== "CLEAR HR DATA"' in source
    assert 'fetch("/hr/clear-data"' in source
    assert 'typeof raw === "string"' in source
