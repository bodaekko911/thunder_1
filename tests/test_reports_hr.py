import asyncio
import io
from collections.abc import AsyncGenerator
from datetime import date
from types import SimpleNamespace

import openpyxl
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

import app.app_factory as app_factory
import app.routers.reports as reports
from app.app_factory import create_app
from app.core import security
from app.database import Base, get_async_session
from app.models.farm import Farm
from app.models.hr import Attendance, Employee, Payroll


class AsyncSessionAdapter:
    def __init__(self, session):
        self.session = session

    async def execute(self, statement, params=None):
        return self.session.execute(statement, params or {})


class FakePermissionSession:
    def __init__(self):
        self.logged = []

    def add(self, obj):
        self.logged.append(obj)

    async def commit(self):
        return None

    async def rollback(self):
        return None


def run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


async def read_streaming_response(response) -> bytes:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    return b"".join(chunks)


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            Farm.__table__,
            Employee.__table__,
            Attendance.__table__,
            Payroll.__table__,
        ],
    )
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    return Session()


def seed_hr_data(session):
    north = Farm(id=1, name="North Farm", is_active=1)
    mona = Employee(
        id=1,
        name="Mona Field",
        phone="0100",
        position="Grower",
        department="Field",
        hire_date=date(2024, 1, 5),
        base_salary=3000,
        farm=north,
        is_active=True,
    )
    ali = Employee(
        id=2,
        name="Ali Admin",
        phone="0111",
        position="Coordinator",
        department="Admin",
        hire_date=date(2024, 2, 1),
        base_salary=2000,
        is_active=True,
    )
    omar = Employee(
        id=3,
        name="Omar Seasonal",
        phone="0122",
        position="Picker",
        department="Field",
        hire_date=date(2023, 12, 1),
        base_salary=1000,
        farm=north,
        is_active=False,
    )
    session.add_all([north, mona, ali, omar])
    session.flush()
    session.add_all(
        [
            Attendance(employee_id=1, date=date(2026, 1, 2), status="present"),
            Attendance(employee_id=1, date=date(2026, 1, 3), status="absent"),
            Attendance(employee_id=1, date=date(2026, 1, 4), status="late"),
            Attendance(employee_id=1, date=date(2026, 1, 5), status="leave"),
            Attendance(employee_id=2, date=date(2026, 1, 2), status="present"),
            Attendance(employee_id=3, date=date(2026, 1, 2), status="present"),
            Attendance(employee_id=1, date=date(2026, 2, 2), status="present"),
        ]
    )
    session.add_all(
        [
            Payroll(
                employee_id=1,
                period="2026-01",
                base_salary=3000,
                bonuses=200,
                deductions=50,
                net_salary=3150,
                paid=True,
                days_worked=20,
                working_days=22,
            ),
            Payroll(
                employee_id=2,
                period="2026-01",
                base_salary=2000,
                bonuses=0,
                deductions=100,
                net_salary=1900,
                paid=False,
                days_worked=18,
                working_days=22,
            ),
            Payroll(
                employee_id=1,
                period="2026-02",
                base_salary=3000,
                bonuses=0,
                deductions=0,
                net_salary=3000,
                paid=False,
                days_worked=1,
                working_days=20,
            ),
        ]
    )
    session.commit()


def build_report(session, **overrides):
    params = {
        "db": AsyncSessionAdapter(session),
        "d_from": reports.datetime(2026, 1, 1, tzinfo=reports.timezone.utc),
        "d_to": reports.datetime(2026, 1, 31, 23, 59, 59, tzinfo=reports.timezone.utc),
        "period": "2026-01",
    }
    params.update(overrides)
    return run(reports._build_hr_report(**params))


def test_hr_report_data_totals_groups_and_employee_rows():
    with make_session() as session:
        seed_hr_data(session)

        data = build_report(session)

    assert data["date_from"] == "2026-01-01"
    assert data["date_to"] == "2026-01-31"
    assert data["period"] == "2026-01"
    assert data["summary"] == {
        "active_employees": 2,
        "inactive_employees": 1,
        "total_base_salary": 6000.0,
        "attendance_records": 6,
        "present_days": 3,
        "absent_days": 1,
        "late_days": 1,
        "leave_days": 1,
        "attendance_rate": 50.0,
        "payroll_records": 2,
        "gross_salary": 5000.0,
        "bonuses": 200.0,
        "deductions": 150.0,
        "net_salary": 5050.0,
        "paid_salary": 3150.0,
        "unpaid_salary": 1900.0,
    }

    by_department = {row["department"]: row for row in data["by_department"]}
    assert by_department["Field"]["employees"] == 2
    assert by_department["Field"]["base_salary"] == 4000.0
    assert by_department["Field"]["present_days"] == 2
    assert by_department["Field"]["absent_days"] == 1
    assert by_department["Field"]["late_days"] == 1
    assert by_department["Field"]["leave_days"] == 1
    assert by_department["Field"]["net_salary"] == 3150.0
    assert by_department["Admin"]["employees"] == 1
    assert by_department["Admin"]["net_salary"] == 1900.0

    by_farm = {row["farm_name"]: row for row in data["by_farm"]}
    assert by_farm["North Farm"]["employees"] == 2
    assert by_farm["North Farm"]["base_salary"] == 4000.0
    assert by_farm["North Farm"]["present_days"] == 2
    assert by_farm["North Farm"]["net_salary"] == 3150.0
    assert by_farm["Unassigned"]["employees"] == 1
    assert by_farm["Unassigned"]["net_salary"] == 1900.0

    employees = {row["employee"]: row for row in data["employees"]}
    assert employees["Mona Field"]["attendance_records"] == 4
    assert employees["Mona Field"]["attendance_rate"] == 25.0
    assert employees["Mona Field"]["payroll_period"] == "2026-01"
    assert employees["Mona Field"]["days_worked"] == 20
    assert employees["Mona Field"]["working_days"] == 22
    assert employees["Mona Field"]["bonuses"] == 200.0
    assert employees["Mona Field"]["deductions"] == 50.0
    assert employees["Mona Field"]["net_salary"] == 3150.0
    assert employees["Mona Field"]["paid"] is True
    assert employees["Ali Admin"]["paid"] is False
    assert employees["Omar Seasonal"]["payroll_period"] == "—"
    assert data["total_rows"] == 3


def test_hr_report_filters_payroll_by_selected_range_months_when_period_omitted():
    with make_session() as session:
        seed_hr_data(session)

        data = build_report(
            session,
            d_from=reports.datetime(2026, 2, 1, tzinfo=reports.timezone.utc),
            d_to=reports.datetime(2026, 2, 28, 23, 59, 59, tzinfo=reports.timezone.utc),
            period=None,
        )

    assert data["period"] is None
    assert data["summary"]["payroll_records"] == 1
    assert data["summary"]["net_salary"] == 3000.0
    assert data["summary"]["attendance_records"] == 1


def test_hr_export_returns_xlsx_with_expected_sheets():
    with make_session() as session:
        seed_hr_data(session)
        response = run(
            reports.export_hr(
                date_from="2026-01-01",
                date_to="2026-01-31",
                period="2026-01",
                db=AsyncSessionAdapter(session),
            )
        )
        workbook = openpyxl.load_workbook(io.BytesIO(run(read_streaming_response(response))), data_only=True)

    assert response.media_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert workbook.sheetnames == ["HR Summary", "By Department", "By Farm", "Employees"]


def test_hr_report_api_requires_hr_tab_permission():
    user = SimpleNamespace(
        id=99,
        name="Reports Only",
        role="cashier",
        permissions="page_reports",
        is_active=True,
    )
    fake_db = FakePermissionSession()

    async def override_session() -> AsyncGenerator[FakePermissionSession, None]:
        yield fake_db

    async def override_user():
        return user

    async def noop():
        return None

    app_factory.configure_logging = lambda: None
    app_factory.configure_monitoring = lambda: None
    app_factory.verify_migration_status = noop

    app = create_app()
    app.dependency_overrides[get_async_session] = override_session
    app.dependency_overrides[security.get_current_user] = override_user

    with TestClient(app) as client:
        response = client.get("/reports/api/hr?date_from=2026-01-01&date_to=2026-01-31")

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission denied: tab_reports_hr"
    assert any(log.action == "PERMISSION_DENIED" and log.ref_id == "tab_reports_hr" for log in fake_db.logged)
