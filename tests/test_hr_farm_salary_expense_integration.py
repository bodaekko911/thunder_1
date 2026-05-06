import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.routers.hr as hr
import app.routers.reports as reports
from app.core.log import ActivityLog
from app.database import Base
from app.models.accounting import Account, Journal, JournalEntry
from app.models.expense import Expense, ExpenseCategory
from app.models.farm import Farm, FarmDelivery
from app.models.hr import Employee, Payroll
from app.models.user import User
from app.services.expense_service import SALARY_CATEGORY_NAME, get_cost_allocation


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


def run(coro):
    return asyncio.run(coro)


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            User.__table__,
            Farm.__table__,
            Employee.__table__,
            Payroll.__table__,
            ExpenseCategory.__table__,
            Account.__table__,
            Journal.__table__,
            JournalEntry.__table__,
            Expense.__table__,
            FarmDelivery.__table__,
            ActivityLog.__table__,
        ],
    )
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    return Session()


def make_user(session):
    user = User(
        name="Admin",
        email="admin@example.com",
        password="x",
        role="admin",
        is_active=True,
    )
    session.add(user)
    session.commit()
    return user


def test_employee_create_edit_and_response_include_farm():
    with make_session() as session:
        db = AsyncSessionAdapter(session)
        user = make_user(session)
        north = Farm(name="North Farm", is_active=1)
        south = Farm(name="South Farm", is_active=1)
        session.add_all([north, south])
        session.commit()

        created = run(
            hr.add_employee(
                hr.EmployeeCreate(
                    name="Mona Salary",
                    position="Grower",
                    department="Farm",
                    base_salary=1000,
                    farm_id=north.id,
                ),
                db=db,
                current_user=user,
            )
        )

        employee = session.execute(select(Employee).where(Employee.id == created["id"])).scalar_one()
        assert employee.farm_id == north.id
        assert created["farm_id"] == north.id
        assert created["farm_name"] == "North Farm"

        updated = run(
            hr.edit_employee(
                employee.id,
                hr.EmployeeUpdate(farm_id=south.id),
                db=db,
                current_user=user,
            )
        )
        session.refresh(employee)
        assert employee.farm_id == south.id
        assert updated["farm_id"] == south.id
        assert updated["farm_name"] == "South Farm"

        employees = run(hr.get_employees(db=db))
        assert employees[0]["farm_id"] == south.id
        assert employees[0]["farm_name"] == "South Farm"


def test_mark_payroll_paid_creates_one_salary_expense_linked_to_employee_farm():
    with make_session() as session:
        db = AsyncSessionAdapter(session)
        user = make_user(session)
        farm = Farm(name="Main Farm", is_active=1)
        employee = Employee(name="John Farmhand", base_salary=Decimal("1200.00"), farm=farm)
        payroll = Payroll(
            employee=employee,
            period="2026-04",
            base_salary=Decimal("1200.00"),
            net_salary=Decimal("1000.00"),
            paid=False,
        )
        session.add_all([farm, employee, payroll])
        session.commit()

        first = run(
            hr.mark_paid(
                payroll.id,
                data=hr.PayrollPayRequest(payment_method="cash"),
                db=db,
                current_user=user,
            )
        )
        second = run(
            hr.mark_paid(
                payroll.id,
                data=hr.PayrollPayRequest(payment_method="cash"),
                db=db,
                current_user=user,
            )
        )

        expenses = session.execute(select(Expense).where(Expense.payroll_id == payroll.id)).scalars().all()
        category = session.execute(
            select(ExpenseCategory).where(ExpenseCategory.name == SALARY_CATEGORY_NAME)
        ).scalar_one()
        session.refresh(payroll)

        assert payroll.paid is True
        assert len(expenses) == 1
        assert expenses[0].category_id == category.id
        assert expenses[0].farm_id == farm.id
        assert float(expenses[0].amount) == 1000.0
        assert first["expense_id"] == second["expense_id"] == expenses[0].id
        assert first["category"] == SALARY_CATEGORY_NAME
        assert first["farm_id"] == farm.id
        assert first["farm_name"] == "Main Farm"
        assert first["amount"] == 1000.0


def test_employee_without_farm_still_creates_unassigned_salary_expense():
    with make_session() as session:
        db = AsyncSessionAdapter(session)
        user = make_user(session)
        employee = Employee(name="No Farm Employee", base_salary=Decimal("900.00"))
        payroll = Payroll(
            employee=employee,
            period="2026-04",
            base_salary=Decimal("900.00"),
            net_salary=Decimal("750.00"),
            paid=False,
        )
        session.add_all([employee, payroll])
        session.commit()

        response = run(
            hr.mark_paid(
                payroll.id,
                data=hr.PayrollPayRequest(payment_method="cash"),
                db=db,
                current_user=user,
            )
        )

        expense = session.execute(select(Expense).where(Expense.payroll_id == payroll.id)).scalar_one()
        assert expense.farm_id is None
        assert response["farm_id"] is None
        assert "warning" in response


def test_farm_cost_allocation_uses_salary_expenses_by_farm_without_double_counting():
    with make_session() as session:
        db = AsyncSessionAdapter(session)
        user = make_user(session)
        north = Farm(name="North Farm", is_active=1)
        south = Farm(name="South Farm", is_active=1)
        salary = ExpenseCategory(name=SALARY_CATEGORY_NAME, account_code="5006", is_active="1")
        utilities = ExpenseCategory(name="Utilities", account_code="5001", is_active="1")
        session.add_all([north, south, salary, utilities])
        session.commit()
        session.add_all(
            [
                Expense(
                    ref_number="EXP-00001",
                    category_id=salary.id,
                    user_id=user.id,
                    expense_date=date(2026, 4, 15),
                    amount=Decimal("1000.00"),
                    payment_method="cash",
                    farm_id=north.id,
                ),
                Expense(
                    ref_number="EXP-00002",
                    category_id=salary.id,
                    user_id=user.id,
                    expense_date=date(2026, 4, 15),
                    amount=Decimal("500.00"),
                    payment_method="cash",
                    farm_id=south.id,
                ),
                Expense(
                    ref_number="EXP-00003",
                    category_id=salary.id,
                    user_id=user.id,
                    expense_date=date(2026, 4, 15),
                    amount=Decimal("250.00"),
                    payment_method="cash",
                    farm_id=None,
                ),
                Expense(
                    ref_number="EXP-00004",
                    category_id=utilities.id,
                    user_id=user.id,
                    expense_date=date(2026, 4, 15),
                    amount=Decimal("200.00"),
                    payment_method="cash",
                    farm_id=north.id,
                ),
            ]
        )
        session.commit()

        result = run(
            get_cost_allocation(
                db,
                farm_id=str(north.id),
                date_from="2026-04-01",
                date_to="2026-04-30",
            )
        )

        assert result["salary_cost"] == 1000.0
        assert result["labor_cost"] == 1000.0
        assert result["total_cost"] == 1200.0
        assert result["total_expenses"] == 1200.0
        assert result["expense_count"] == 2
        assert {row["name"]: row["amount"] for row in result["cost_by_category"]} == {
            SALARY_CATEGORY_NAME: 1000.0,
            "Utilities": 200.0,
        }

        all_farms = run(
            get_cost_allocation(
                db,
                farm_id="both",
                date_from="2026-04-01",
                date_to="2026-04-30",
            )
        )
        assert all_farms["salary_cost"] == 1750.0
        assert all_farms["unassigned_salary_cost"] == 250.0


def test_farm_intake_report_exposes_salary_cost_per_farm():
    with make_session() as session:
        db = AsyncSessionAdapter(session)
        user = make_user(session)
        north = Farm(name="North Farm", is_active=1)
        south = Farm(name="South Farm", is_active=1)
        salary = ExpenseCategory(name=SALARY_CATEGORY_NAME, account_code="5006", is_active="1")
        session.add_all([north, south, salary])
        session.commit()
        session.add_all(
            [
                Expense(
                    ref_number="EXP-00010",
                    category_id=salary.id,
                    user_id=user.id,
                    expense_date=date(2026, 4, 20),
                    amount=Decimal("1300.00"),
                    payment_method="cash",
                    farm_id=north.id,
                ),
                Expense(
                    ref_number="EXP-00011",
                    category_id=salary.id,
                    user_id=user.id,
                    expense_date=date(2026, 4, 20),
                    amount=Decimal("700.00"),
                    payment_method="cash",
                    farm_id=south.id,
                ),
            ]
        )
        session.commit()

        data = run(
            reports._build_farm_intake_report(
                db,
                d_from=datetime(2026, 4, 1, tzinfo=timezone.utc),
                d_to=datetime(2026, 4, 30, tzinfo=timezone.utc),
            )
        )

        by_farm = {row["farm"]: row for row in data["summary"]}
        assert by_farm["North Farm"]["salary_cost"] == 1300.0
        assert by_farm["North Farm"]["labor_cost"] == 1300.0
        assert by_farm["South Farm"]["salary_cost"] == 700.0
        assert data["totals"]["salary_cost"] == 2000.0
        assert data["totals"]["salary_expense_count"] == 2
