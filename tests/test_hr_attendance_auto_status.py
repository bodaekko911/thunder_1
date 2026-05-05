import asyncio
from datetime import date as real_date
from types import SimpleNamespace

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import app.routers.hr as hr
from app.database import Base
from app.models.hr import Attendance, Employee, Payroll


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


def set_today(monkeypatch, today: real_date) -> None:
    class FixedDate(real_date):
        @classmethod
        def today(cls):
            return today

    monkeypatch.setattr(hr, "date", FixedDate)


def attendance_count(session, employee_id: int) -> int:
    return session.execute(
        select(func.count(Attendance.id)).where(Attendance.employee_id == employee_id)
    ).scalar_one()


def attendance_for(session, employee_id: int, day: real_date) -> Attendance:
    return session.execute(
        select(Attendance).where(
            Attendance.employee_id == employee_id,
            Attendance.date == day,
        )
    ).scalar_one()


def test_absent_auto_status_persists_until_marked_present(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[Employee.__table__, Attendance.__table__, Payroll.__table__],
    )
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    user = SimpleNamespace(id=1, name="Admin")

    with Session() as session:
        db = AsyncSessionAdapter(session)
        employee = Employee(name="Mona Salary", base_salary=1000)
        session.add(employee)
        session.commit()

        day_one = real_date(2026, 5, 5)
        set_today(monkeypatch, day_one)
        result = run(hr.auto_mark_today(db=db, current_user=user))
        assert result["created"] == 1
        assert attendance_for(session, employee.id, day_one).status == "present"

        result = run(
            hr.mark_absent_today(
                hr.AttendanceCreate(
                    employee_id=employee.id,
                    date=str(day_one),
                    status="absent",
                ),
                db=db,
                current_user=user,
            )
        )
        session.refresh(employee)
        assert result["auto_status"] == "absent"
        assert employee.attendance_auto_status == "absent"
        assert attendance_for(session, employee.id, day_one).status == "absent"
        assert attendance_count(session, employee.id) == 1

        day_two = real_date(2026, 5, 6)
        set_today(monkeypatch, day_two)
        result = run(hr.auto_mark_today(db=db, current_user=user))
        assert result["created"] == 1
        assert result["absent"] == 1
        assert attendance_for(session, employee.id, day_two).status == "absent"

        result = run(hr.auto_mark_today(db=db, current_user=user))
        assert result["created"] == 0
        assert attendance_count(session, employee.id) == 2

        result = run(
            hr.mark_present_today(
                hr.AttendanceCreate(
                    employee_id=employee.id,
                    date=str(day_two),
                    status="present",
                ),
                db=db,
                current_user=user,
            )
        )
        session.refresh(employee)
        assert result["auto_status"] == "present"
        assert employee.attendance_auto_status == "present"
        assert attendance_for(session, employee.id, day_two).status == "present"
        assert attendance_count(session, employee.id) == 2

        day_three = real_date(2026, 5, 7)
        set_today(monkeypatch, day_three)
        result = run(hr.auto_mark_today(db=db, current_user=user))
        assert result["created"] == 1
        assert result["present"] == 1
        assert attendance_for(session, employee.id, day_three).status == "present"
        assert attendance_count(session, employee.id) == 3
