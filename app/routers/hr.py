from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
import re
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import delete, func, select
from typing import Optional, List
from pydantic import BaseModel, Field
from datetime import date, datetime, timezone

from app.database import get_async_session
from app.core.permissions import get_current_user, has_permission, require_permission
from app.core.log import record
from app.core.navigation import render_app_header
from app.models.accounting import Account, Journal, JournalEntry
from app.models.expense import Expense
from app.models.hr import (
    Employee,
    Attendance,
    EmployeeLoan,
    EmployeeLoanRepayment,
    EmployeePayrollDeduction,
    Payroll,
)
from app.models.farm import Farm
from app.models.user import User
from app.services.expense_service import create_payroll_expense

ATTENDANCE_STATUS_PRESENT = "present"
ATTENDANCE_STATUS_ABSENT = "absent"
ATTENDANCE_AUTO_STATUSES = {ATTENDANCE_STATUS_PRESENT, ATTENDANCE_STATUS_ABSENT}
ATTENDANCE_STATUSES = ATTENDANCE_AUTO_STATUSES | {"late", "leave"}

router = APIRouter(
    prefix="/hr",
    tags=["HR"],
    dependencies=[Depends(require_permission("page_hr"))],
)


# ── Schemas ────────────────────────────────────────────
class EmployeeCreate(BaseModel):
    name:        str
    phone:       Optional[str]  = None
    position:    Optional[str]  = None
    department:  Optional[str]  = None
    hire_date:   Optional[str]  = None
    base_salary: float          = 0
    farm_id:     Optional[int]  = None

class EmployeeUpdate(BaseModel):
    name:        Optional[str]   = None
    phone:       Optional[str]   = None
    position:    Optional[str]   = None
    department:  Optional[str]   = None
    base_salary: Optional[float] = None
    farm_id:     Optional[int]   = None
    is_active:   Optional[bool]  = None

class AttendanceCreate(BaseModel):
    employee_id: int
    date:        str
    status:      str = "present"
    note:        Optional[str] = None

class PayrollRun(BaseModel):
    period:  str  # "2025-01"
    emp_ids: Optional[List[int]] = None  # None = all employees
    bonuses: Optional[dict[int, Decimal]] = None
    loan_repayments: Optional[dict[int, Decimal]] = None

class PayrollUpdate(BaseModel):
    bonuses:    Decimal = Decimal("0")
    deductions: Decimal = Decimal("0")
    notes:      Optional[str] = None

class PayrollPayRequest(BaseModel):
    payment_method: Optional[str] = "cash"


class EmployeeLoanCreate(BaseModel):
    loan_date: str
    amount: Decimal = Field(gt=0)
    description: Optional[str] = None


class LoanRepaymentCreate(BaseModel):
    repayment_date: str
    amount: Decimal = Field(gt=0)
    note: Optional[str] = None


class DayDeductionCreate(BaseModel):
    period: str
    deduction_date: str
    days: Decimal = Field(gt=0)
    working_days: Decimal = Field(gt=0)
    note: Optional[str] = None


class ManualDeductionCreate(BaseModel):
    period: str
    amount: Decimal = Field(gt=0)
    note: Optional[str] = None


class ClearHRDataRequest(BaseModel):
    confirmation: Optional[str] = None


CLEAR_HR_DATA_CONFIRMATION = "CLEAR HR DATA"
LOAN_STATUSES = {"open", "paid", "cancelled"}
DEDUCTION_TYPES = {"loan_repayment", "day_deduction", "manual"}
PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")
MONEY_QUANT = Decimal("0.01")
DAY_QUANT = Decimal("0.01")


def _normalize_attendance_status(status: str) -> str:
    normalized = (status or ATTENDANCE_STATUS_PRESENT).strip().lower()
    if normalized not in ATTENDANCE_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid attendance status")
    return normalized


def _normalize_auto_attendance_status(status: str | None) -> str:
    normalized = (status or ATTENDANCE_STATUS_PRESENT).strip().lower()
    return normalized if normalized in ATTENDANCE_AUTO_STATUSES else ATTENDANCE_STATUS_PRESENT


def _parse_optional_iso_date(value: str | None, field_name: str) -> date | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    try:
        return date.fromisoformat(normalized)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field_name}. Use YYYY-MM-DD.",
        ) from exc


def _parse_required_iso_date(value: str | None, field_name: str) -> date:
    parsed = _parse_optional_iso_date(value, field_name)
    if parsed is None:
        raise HTTPException(status_code=400, detail=f"{field_name} is required")
    return parsed


def _validate_period(period: str | None) -> str:
    normalized = (period or "").strip()
    if not PERIOD_RE.match(normalized):
        raise HTTPException(status_code=400, detail="Invalid period. Use YYYY-MM.")
    month = int(normalized[5:7])
    if month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="Invalid period. Use YYYY-MM.")
    return normalized


def _dec(value, default: str = "0") -> Decimal:
    if value is None:
        return Decimal(default)
    return Decimal(str(value))


def _money(value) -> Decimal:
    return _dec(value).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def _days(value) -> Decimal:
    return _dec(value).quantize(DAY_QUANT, rounding=ROUND_HALF_UP)


def _as_float(value) -> float:
    return float(_money(value))


def _as_day_float(value) -> float:
    return float(_days(value))


async def _get_employee_or_404(db: AsyncSession, employee_id: int) -> Employee:
    result = await db.execute(select(Employee).where(Employee.id == employee_id))
    employee = result.scalar_one_or_none()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    return employee


async def _get_attendance_for_day(
    db: AsyncSession,
    employee_id: int,
    attendance_date: date,
) -> Attendance | None:
    result = await db.execute(
        select(Attendance)
        .where(
            Attendance.employee_id == employee_id,
            Attendance.date == attendance_date,
        )
        .order_by(Attendance.id.desc())
    )
    return result.scalars().first()


async def _upsert_attendance_for_day(
    db: AsyncSession,
    employee_id: int,
    attendance_date: date,
    status: str,
    note: str | None = None,
) -> tuple[Attendance, bool]:
    status = _normalize_attendance_status(status)
    existing = await _get_attendance_for_day(db, employee_id, attendance_date)
    if existing:
        existing.status = status
        existing.note = note
        return existing, True

    attendance = Attendance(
        employee_id=employee_id,
        date=attendance_date,
        status=status,
        note=note,
    )
    db.add(attendance)
    await db.flush()
    return attendance, False


async def _get_active_farm_or_404(db: AsyncSession, farm_id: int | None) -> Farm | None:
    if farm_id is None:
        return None
    if farm_id <= 0:
        raise HTTPException(status_code=400, detail="Invalid farm_id")
    result = await db.execute(
        select(Farm).where(Farm.id == farm_id, Farm.is_active == 1)
    )
    farm = result.scalar_one_or_none()
    if not farm:
        raise HTTPException(status_code=404, detail="Farm not found")
    return farm


def _employee_payload(employee: Employee) -> dict:
    farm = getattr(employee, "farm", None)
    return {
        "id": employee.id,
        "name": employee.name,
        "phone": employee.phone or "—",
        "position": employee.position or "—",
        "department": employee.department or "—",
        "hire_date": str(employee.hire_date) if employee.hire_date else "—",
        "base_salary": float(employee.base_salary),
        "is_active": employee.is_active,
        "farm_id": employee.farm_id,
        "farm_name": farm.name if farm else None,
        "attendance_auto_status": _normalize_auto_attendance_status(
            getattr(employee, "attendance_auto_status", None)
        ),
    }


async def _loan_repaid_amounts(db: AsyncSession, loan_ids: list[int]) -> dict[int, Decimal]:
    if not loan_ids:
        return {}
    result = await db.execute(
        select(
            EmployeeLoanRepayment.loan_id,
            func.coalesce(func.sum(EmployeeLoanRepayment.amount), 0),
        )
        .where(EmployeeLoanRepayment.loan_id.in_(loan_ids))
        .group_by(EmployeeLoanRepayment.loan_id)
    )
    return {loan_id: _money(total) for loan_id, total in result.all()}


async def _employee_loan_balance(db: AsyncSession, employee_id: int) -> Decimal:
    loans_result = await db.execute(
        select(EmployeeLoan).where(
            EmployeeLoan.employee_id == employee_id,
            EmployeeLoan.status != "cancelled",
        )
    )
    loans = loans_result.scalars().all()
    repaid = await _loan_repaid_amounts(db, [loan.id for loan in loans])
    return _money(sum((_money(loan.amount) - repaid.get(loan.id, Decimal("0"))) for loan in loans))


async def _loan_balance(db: AsyncSession, loan: EmployeeLoan) -> Decimal:
    repaid = await _loan_repaid_amounts(db, [loan.id])
    return _money(_money(loan.amount) - repaid.get(loan.id, Decimal("0")))


def _loan_payload(loan: EmployeeLoan, repaid: Decimal) -> dict:
    amount = _money(loan.amount)
    balance = Decimal("0") if loan.status == "cancelled" else _money(amount - repaid)
    return {
        "id": loan.id,
        "employee_id": loan.employee_id,
        "loan_date": loan.loan_date.isoformat(),
        "amount": _as_float(amount),
        "repaid_amount": _as_float(repaid),
        "balance": _as_float(balance),
        "status": loan.status,
        "description": loan.description or "",
        "created_at": str(loan.created_at) if loan.created_at else None,
        "updated_at": str(loan.updated_at) if loan.updated_at else None,
    }


def _deduction_payload(deduction: EmployeePayrollDeduction) -> dict:
    payroll = getattr(deduction, "payroll", None)
    return {
        "id": deduction.id,
        "employee_id": deduction.employee_id,
        "payroll_id": deduction.payroll_id,
        "payroll_period": payroll.period if payroll else deduction.period,
        "period": deduction.period,
        "deduction_date": deduction.deduction_date.isoformat() if deduction.deduction_date else None,
        "type": deduction.type,
        "days": _as_day_float(deduction.days) if deduction.days is not None else None,
        "daily_rate": _as_float(deduction.daily_rate) if deduction.daily_rate is not None else None,
        "amount": _as_float(deduction.amount),
        "note": deduction.note or "",
        "created_at": str(deduction.created_at) if deduction.created_at else None,
    }


async def _update_loan_status(db: AsyncSession, loan: EmployeeLoan) -> Decimal:
    if loan.status == "cancelled":
        return Decimal("0")
    balance = await _loan_balance(db, loan)
    loan.status = "paid" if balance <= 0 else "open"
    return max(balance, Decimal("0"))


async def _apply_loan_repayment_to_oldest_loans(
    db: AsyncSession,
    *,
    employee_id: int,
    amount: Decimal,
    repayment_date: date,
    payroll_id: int | None,
    note: str,
    current_user: User,
) -> Decimal:
    amount = _money(amount)
    if amount <= 0:
        return Decimal("0")

    outstanding = await _employee_loan_balance(db, employee_id)
    if amount > outstanding:
        raise HTTPException(status_code=400, detail="Loan repayment exceeds outstanding balance")

    loans_result = await db.execute(
        select(EmployeeLoan)
        .where(EmployeeLoan.employee_id == employee_id, EmployeeLoan.status == "open")
        .order_by(EmployeeLoan.loan_date, EmployeeLoan.id)
    )
    loans = loans_result.scalars().all()
    remaining = amount
    for loan in loans:
        if remaining <= 0:
            break
        loan_balance = await _loan_balance(db, loan)
        if loan_balance <= 0:
            loan.status = "paid"
            continue
        applied = min(remaining, loan_balance)
        db.add(
            EmployeeLoanRepayment(
                loan_id=loan.id,
                employee_id=employee_id,
                payroll_id=payroll_id,
                repayment_date=repayment_date,
                amount=applied,
                note=note,
                created_by_user_id=current_user.id,
            )
        )
        remaining = _money(remaining - applied)
        if _money(loan_balance - applied) <= 0:
            loan.status = "paid"

    return amount


async def _count_records(db: AsyncSession, model) -> int:
    result = await db.execute(select(func.count(model.id)))
    return int(result.scalar() or 0)


async def _remove_journal_balances(db: AsyncSession, journal_ids: list[int]) -> None:
    if not journal_ids:
        return

    entries_result = await db.execute(
        select(JournalEntry.account_id, JournalEntry.debit, JournalEntry.credit)
        .where(JournalEntry.journal_id.in_(journal_ids))
    )
    deltas_by_account = {}
    for account_id, debit, credit in entries_result.all():
        if account_id is None:
            continue
        deltas_by_account[account_id] = deltas_by_account.get(account_id, 0) + (
            (debit or 0) - (credit or 0)
        )

    if not deltas_by_account:
        return

    accounts_result = await db.execute(
        select(Account).where(Account.id.in_(deltas_by_account.keys()))
    )
    for account in accounts_result.scalars().all():
        account.balance = (account.balance or 0) - deltas_by_account.get(account.id, 0)


async def _clear_hr_data(db: AsyncSession, current_user: User) -> dict:
    deleted = {
        "attendance": await _count_records(db, Attendance),
        "payroll": await _count_records(db, Payroll),
        "employees": await _count_records(db, Employee),
        "loans": await _count_records(db, EmployeeLoan),
        "loan_repayments": await _count_records(db, EmployeeLoanRepayment),
        "payroll_deductions": await _count_records(db, EmployeePayrollDeduction),
        "hr_expenses": 0,
    }

    expense_result = await db.execute(
        select(Expense.id, Expense.journal_id).where(Expense.payroll_id.is_not(None))
    )
    hr_expense_rows = expense_result.all()
    hr_expense_ids = [row[0] for row in hr_expense_rows]
    hr_journal_ids = sorted({row[1] for row in hr_expense_rows if row[1] is not None})
    if hr_journal_ids:
        shared_journal_result = await db.execute(
            select(Expense.journal_id)
            .where(
                Expense.journal_id.in_(hr_journal_ids),
                Expense.payroll_id.is_(None),
            )
        )
        shared_journal_ids = {
            journal_id
            for journal_id in shared_journal_result.scalars().all()
            if journal_id is not None
        }
        hr_journal_ids = [
            journal_id for journal_id in hr_journal_ids if journal_id not in shared_journal_ids
        ]
    deleted["hr_expenses"] = len(hr_expense_ids)

    await db.execute(delete(Attendance).execution_options(synchronize_session=False))
    await db.execute(delete(EmployeePayrollDeduction).execution_options(synchronize_session=False))
    await db.execute(delete(EmployeeLoanRepayment).execution_options(synchronize_session=False))
    await db.execute(delete(EmployeeLoan).execution_options(synchronize_session=False))

    await _remove_journal_balances(db, hr_journal_ids)
    if hr_journal_ids:
        await db.execute(
            delete(JournalEntry)
            .where(JournalEntry.journal_id.in_(hr_journal_ids))
            .execution_options(synchronize_session=False)
        )
    if hr_expense_ids:
        await db.execute(
            delete(Expense)
            .where(Expense.id.in_(hr_expense_ids))
            .execution_options(synchronize_session=False)
        )
    if hr_journal_ids:
        await db.execute(
            delete(Journal)
            .where(Journal.id.in_(hr_journal_ids))
            .execution_options(synchronize_session=False)
        )

    await db.execute(delete(Payroll).execution_options(synchronize_session=False))
    await db.execute(delete(Employee).execution_options(synchronize_session=False))
    record(
        db,
        "HR",
        "clear_hr_data",
        (
            "Cleared HR data: "
            f"{deleted['employees']} employees, "
            f"{deleted['attendance']} attendance records, "
            f"{deleted['payroll']} payroll records, "
            f"{deleted['loans']} loans, "
            f"{deleted['loan_repayments']} loan repayments, "
            f"{deleted['payroll_deductions']} payroll deductions, "
            f"{deleted['hr_expenses']} payroll expenses"
        ),
        user=current_user,
        ref_type="hr_clear_data",
        ref_id="all",
    )
    return {"ok": True, "deleted": deleted}


# ── EMPLOYEE API ───────────────────────────────────────
@router.get("/api/employees")
async def get_employees(q: str = "", db: AsyncSession = Depends(get_async_session)):
    stmt = select(Employee).options(selectinload(Employee.farm)).where(Employee.is_active == True)
    if q:
        stmt = stmt.where(
            Employee.name.ilike(f"%{q}%") |
            Employee.position.ilike(f"%{q}%") |
            Employee.department.ilike(f"%{q}%")
        )
    stmt = stmt.order_by(Employee.name)
    _r = await db.execute(stmt)
    emps = _r.scalars().all()
    return [_employee_payload(e) for e in emps]

@router.post("/api/employees")
async def add_employee(data: EmployeeCreate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    hire = _parse_optional_iso_date(data.hire_date, "hire_date")
    farm = await _get_active_farm_or_404(db, data.farm_id)
    e = Employee(
        name=data.name, phone=data.phone,
        position=data.position, department=data.department,
        hire_date=hire, base_salary=data.base_salary,
        farm_id=farm.id if farm else None,
    )
    db.add(e); await db.flush()
    record(db, "HR", "add_employee",
           f"Added employee: {e.name} — {e.position or ''} / {e.department or ''} — salary: {float(e.base_salary):.2f}",
           ref_type="employee", ref_id=e.id)
    await db.commit(); await db.refresh(e)
    if farm:
        e.farm = farm
    return _employee_payload(e)

@router.put("/api/employees/{emp_id}")
async def edit_employee(emp_id: int, data: EmployeeUpdate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    _r = await db.execute(select(Employee).options(selectinload(Employee.farm)).where(Employee.id == emp_id))
    e = _r.scalar_one_or_none()
    if not e:
        raise HTTPException(status_code=404, detail="Employee not found")
    payload = data.model_dump(exclude_unset=True)
    if "farm_id" in payload:
        farm = await _get_active_farm_or_404(db, payload["farm_id"])
        payload["farm_id"] = farm.id if farm else None
        e.farm = farm
    for k, v in payload.items():
        setattr(e, k, v)
    record(db, "HR", "edit_employee",
           f"Edited employee: {e.name}",
           ref_type="employee", ref_id=emp_id)
    await db.commit()
    return {"ok": True, **_employee_payload(e)}

@router.delete("/api/employees/{emp_id}")
async def deactivate_employee(emp_id: int, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    _r = await db.execute(select(Employee).where(Employee.id == emp_id))
    e = _r.scalar_one_or_none()
    if not e:
        raise HTTPException(status_code=404, detail="Employee not found")
    e.is_active = False
    record(db, "HR", "deactivate_employee",
           f"Deactivated employee: {e.name}",
           ref_type="employee", ref_id=emp_id)
    await db.commit()
    return {"ok": True}


# ── LOANS & DEDUCTIONS API ─────────────────────────────
@router.get("/api/employees/{employee_id}/loans", dependencies=[Depends(require_permission("action_hr_view_loans"))])
async def get_employee_loans(employee_id: int, db: AsyncSession = Depends(get_async_session)):
    await _get_employee_or_404(db, employee_id)
    result = await db.execute(
        select(EmployeeLoan)
        .where(EmployeeLoan.employee_id == employee_id)
        .order_by(EmployeeLoan.loan_date.desc(), EmployeeLoan.id.desc())
    )
    loans = result.scalars().all()
    repaid = await _loan_repaid_amounts(db, [loan.id for loan in loans])
    return [_loan_payload(loan, repaid.get(loan.id, Decimal("0"))) for loan in loans]


@router.post("/api/employees/{employee_id}/loans", dependencies=[Depends(require_permission("action_hr_manage_loans"))])
async def create_employee_loan(
    employee_id: int,
    data: EmployeeLoanCreate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    employee = await _get_employee_or_404(db, employee_id)
    loan_date = _parse_required_iso_date(data.loan_date, "loan_date")
    amount = _money(data.amount)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Loan amount must be greater than 0")
    loan = EmployeeLoan(
        employee_id=employee.id,
        loan_date=loan_date,
        amount=amount,
        description=(data.description or "").strip() or None,
        status="open",
        created_by_user_id=current_user.id,
    )
    db.add(loan)
    await db.flush()
    record(
        db,
        "HR",
        "create_employee_loan",
        f"Created loan for {employee.name}: {amount:.2f}",
        user=current_user,
        ref_type="employee_loan",
        ref_id=loan.id,
    )
    await db.commit()
    await db.refresh(loan)
    return {"ok": True, **_loan_payload(loan, Decimal("0"))}


@router.post("/api/loans/{loan_id}/repayments", dependencies=[Depends(require_permission("action_hr_manage_loans"))])
async def create_loan_repayment(
    loan_id: int,
    data: LoanRepaymentCreate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(EmployeeLoan).where(EmployeeLoan.id == loan_id))
    loan = result.scalar_one_or_none()
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")
    if loan.status == "cancelled":
        raise HTTPException(status_code=400, detail="Cannot repay a cancelled loan")
    repayment_date = _parse_required_iso_date(data.repayment_date, "repayment_date")
    amount = _money(data.amount)
    balance = await _loan_balance(db, loan)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Repayment amount must be greater than 0")
    if amount > balance:
        raise HTTPException(status_code=400, detail="Repayment amount exceeds outstanding balance")
    repayment = EmployeeLoanRepayment(
        loan_id=loan.id,
        employee_id=loan.employee_id,
        repayment_date=repayment_date,
        amount=amount,
        note=(data.note or "").strip() or None,
        created_by_user_id=current_user.id,
    )
    db.add(repayment)
    await db.flush()
    balance = await _update_loan_status(db, loan)
    record(
        db,
        "HR",
        "create_loan_repayment",
        f"Recorded repayment for loan #{loan.id}: {amount:.2f}",
        user=current_user,
        ref_type="employee_loan",
        ref_id=loan.id,
    )
    await db.commit()
    return {
        "ok": True,
        "id": repayment.id,
        "loan_id": loan.id,
        "employee_id": loan.employee_id,
        "amount": _as_float(amount),
        "balance": _as_float(balance),
        "status": loan.status,
    }


@router.post("/api/loans/{loan_id}/cancel", dependencies=[Depends(require_permission("action_hr_manage_loans"))])
async def cancel_employee_loan(
    loan_id: int,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(EmployeeLoan).where(EmployeeLoan.id == loan_id))
    loan = result.scalar_one_or_none()
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")
    repayments_result = await db.execute(
        select(func.count(EmployeeLoanRepayment.id)).where(EmployeeLoanRepayment.loan_id == loan.id)
    )
    repayment_count = repayments_result.scalar() or 0
    if repayment_count and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can cancel loans with repayments")
    loan.status = "cancelled"
    record(
        db,
        "HR",
        "cancel_employee_loan",
        f"Cancelled loan #{loan.id}",
        user=current_user,
        ref_type="employee_loan",
        ref_id=loan.id,
    )
    await db.commit()
    return {"ok": True, "loan_id": loan.id, "status": loan.status}


@router.get("/api/employees/{employee_id}/deductions", dependencies=[Depends(require_permission("action_hr_view_deductions"))])
async def get_employee_deductions(employee_id: int, db: AsyncSession = Depends(get_async_session)):
    await _get_employee_or_404(db, employee_id)
    result = await db.execute(
        select(EmployeePayrollDeduction)
        .options(selectinload(EmployeePayrollDeduction.payroll))
        .where(EmployeePayrollDeduction.employee_id == employee_id)
        .order_by(EmployeePayrollDeduction.created_at.desc(), EmployeePayrollDeduction.id.desc())
    )
    return [_deduction_payload(deduction) for deduction in result.scalars().all()]


@router.post("/api/employees/{employee_id}/deductions/day", dependencies=[Depends(require_permission("action_hr_manage_deductions"))])
async def create_day_deduction(
    employee_id: int,
    data: DayDeductionCreate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    employee = await _get_employee_or_404(db, employee_id)
    period = _validate_period(data.period)
    deduction_date = _parse_required_iso_date(data.deduction_date, "deduction_date")
    days = _days(data.days)
    working_days = _days(data.working_days)
    base_salary = _money(employee.base_salary)
    if base_salary <= 0:
        raise HTTPException(status_code=400, detail="Employee base salary must be greater than 0")
    if days <= 0:
        raise HTTPException(status_code=400, detail="Deduction days must be greater than 0")
    if working_days <= 0:
        raise HTTPException(status_code=400, detail="Working days must be greater than 0")
    daily_rate = _money(base_salary / working_days)
    amount = _money(daily_rate * days)
    deduction = EmployeePayrollDeduction(
        employee_id=employee.id,
        period=period,
        deduction_date=deduction_date,
        type="day_deduction",
        days=days,
        daily_rate=daily_rate,
        amount=amount,
        note=(data.note or "").strip() or None,
        created_by_user_id=current_user.id,
    )
    db.add(deduction)
    await db.flush()
    record(
        db,
        "HR",
        "create_day_deduction",
        f"Created {days} day deduction for {employee.name}: {amount:.2f}",
        user=current_user,
        ref_type="employee_payroll_deduction",
        ref_id=deduction.id,
    )
    await db.commit()
    await db.refresh(deduction)
    return {"ok": True, **_deduction_payload(deduction)}


@router.post("/api/employees/{employee_id}/deductions/manual", dependencies=[Depends(require_permission("action_hr_manage_deductions"))])
async def create_manual_deduction(
    employee_id: int,
    data: ManualDeductionCreate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    employee = await _get_employee_or_404(db, employee_id)
    period = _validate_period(data.period)
    amount = _money(data.amount)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Deduction amount must be greater than 0")
    deduction = EmployeePayrollDeduction(
        employee_id=employee.id,
        period=period,
        type="manual",
        amount=amount,
        note=(data.note or "").strip() or None,
        created_by_user_id=current_user.id,
    )
    db.add(deduction)
    await db.flush()
    record(
        db,
        "HR",
        "create_manual_deduction",
        f"Created manual deduction for {employee.name}: {amount:.2f}",
        user=current_user,
        ref_type="employee_payroll_deduction",
        ref_id=deduction.id,
    )
    await db.commit()
    await db.refresh(deduction)
    return {"ok": True, **_deduction_payload(deduction)}


async def _pending_deductions_for_period(
    db: AsyncSession,
    employee_id: int,
    period: str,
) -> tuple[list[EmployeePayrollDeduction], Decimal, Decimal, Decimal]:
    result = await db.execute(
        select(EmployeePayrollDeduction)
        .where(
            EmployeePayrollDeduction.employee_id == employee_id,
            EmployeePayrollDeduction.period == period,
            EmployeePayrollDeduction.payroll_id.is_(None),
            EmployeePayrollDeduction.type.in_(["day_deduction", "manual"]),
        )
        .order_by(EmployeePayrollDeduction.deduction_date, EmployeePayrollDeduction.id)
    )
    deductions = result.scalars().all()
    day_deduction_days = _days(
        sum((_dec(item.days) for item in deductions if item.type == "day_deduction"), Decimal("0"))
    )
    day_deductions = _money(
        sum((_dec(item.amount) for item in deductions if item.type == "day_deduction"), Decimal("0"))
    )
    manual_deductions = _money(
        sum((_dec(item.amount) for item in deductions if item.type == "manual"), Decimal("0"))
    )
    return deductions, day_deduction_days, day_deductions, manual_deductions


async def _payroll_preview_for_employee(
    db: AsyncSession,
    employee: Employee,
    *,
    period: str,
    working_days: int,
    days_elapsed: int,
    year: int,
    month: int,
    include_loans: bool,
    include_deductions: bool,
) -> dict:
    _dp = await db.execute(select(func.count(Attendance.id)).where(
        Attendance.employee_id == employee.id,
        Attendance.status == "present",
        func.extract("year",  Attendance.date) == year,
        func.extract("month", Attendance.date) == month,
    ))
    days_present = _dp.scalar() or 0

    _ar = await db.execute(select(Payroll).where(
        Payroll.employee_id == employee.id,
        Payroll.period == period,
    ))
    already_run = _ar.scalar_one_or_none() is not None

    base_salary = _money(employee.base_salary)
    daily_rate = _money(base_salary / Decimal(str(working_days))) if working_days > 0 else Decimal("0")
    pending_day_days = Decimal("0")
    pending_day_amount = Decimal("0")
    pending_manual_amount = Decimal("0")
    if include_deductions:
        _, pending_day_days, pending_day_amount, pending_manual_amount = await _pending_deductions_for_period(
            db,
            employee.id,
            period,
        )
    outstanding_loan_balance = await _employee_loan_balance(db, employee.id) if include_loans else None
    total_pending = _money(pending_day_amount + pending_manual_amount)
    net_before_loan = _money(base_salary - total_pending)
    return {
        "employee_id": employee.id,
        "employee": employee.name,
        "position": employee.position or "—",
        "base_salary": _as_float(base_salary),
        "working_days": working_days,
        "days_elapsed": days_elapsed,
        "days_present": days_present,
        "days_absent": days_elapsed - days_present,
        "daily_rate": _as_float(daily_rate),
        "earned": _as_float(base_salary),
        "already_run": already_run,
        "outstanding_loan_balance": _as_float(outstanding_loan_balance) if outstanding_loan_balance is not None else None,
        "pending_day_deduction_days": _as_day_float(pending_day_days),
        "pending_day_deductions": _as_float(pending_day_amount),
        "pending_manual_deductions": _as_float(pending_manual_amount),
        "pending_total_deductions": _as_float(total_pending),
        "net_before_loan": _as_float(net_before_loan),
    }


# ── ATTENDANCE API ─────────────────────────────────────
@router.get("/api/attendance")
async def get_attendance(emp_id: int = None, period: str = None, db: AsyncSession = Depends(get_async_session)):
    stmt = select(Attendance).options(selectinload(Attendance.employee))
    if emp_id:
        stmt = stmt.where(Attendance.employee_id == emp_id)
    if period:
        # period like "2025-01"
        year, month = period.split("-")
        stmt = stmt.where(
            func.extract("year",  Attendance.date) == int(year),
            func.extract("month", Attendance.date) == int(month),
        )
    stmt = stmt.order_by(Attendance.date.desc()).limit(200)
    _r = await db.execute(stmt)
    records = _r.scalars().all()
    return [
        {
            "id":          r.id,
            "employee_id": r.employee_id,
            "employee":    r.employee.name if r.employee else "—",
            "date":        str(r.date),
            "status":      r.status,
            "note":        r.note or "",
        }
        for r in records
    ]

@router.post("/api/attendance")
async def log_attendance(data: AttendanceCreate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    attendance_date = date.fromisoformat(data.date)
    status = _normalize_attendance_status(data.status)
    employee = await _get_employee_or_404(db, data.employee_id)
    attendance, updated = await _upsert_attendance_for_day(
        db,
        data.employee_id,
        attendance_date,
        status,
        data.note,
    )
    if attendance_date == date.today() and status in ATTENDANCE_AUTO_STATUSES:
        employee.attendance_auto_status = status
    await db.commit()
    return {"id": attendance.id, "updated": updated}


@router.post("/api/attendance/auto-today")
async def auto_mark_today(db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    """Auto-log all active employees today using their persistent attendance mode."""
    today = date.today()
    _r = await db.execute(select(Employee).where(Employee.is_active == True))
    employees = _r.scalars().all()
    created = 0
    present = 0
    absent = 0
    for emp in employees:
        status = _normalize_auto_attendance_status(getattr(emp, "attendance_auto_status", None))
        exists = await _get_attendance_for_day(db, emp.id, today)
        if not exists:
            db.add(Attendance(employee_id=emp.id, date=today, status=status))
            created += 1
            if status == ATTENDANCE_STATUS_ABSENT:
                absent += 1
            else:
                present += 1
    await db.commit()
    return {
        "ok": True,
        "created": created,
        "present": present,
        "absent": absent,
        "date": str(today),
    }

@router.post("/api/attendance/mark-absent")
async def mark_absent_today(data: AttendanceCreate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    """Keep an employee absent every day until they are manually marked present."""
    today = date.today()
    employee = await _get_employee_or_404(db, data.employee_id)
    employee.attendance_auto_status = ATTENDANCE_STATUS_ABSENT
    attendance, updated = await _upsert_attendance_for_day(
        db,
        data.employee_id,
        today,
        ATTENDANCE_STATUS_ABSENT,
        data.note,
    )
    await db.commit()
    return {
        "id": attendance.id,
        "updated": updated,
        "auto_status": employee.attendance_auto_status,
    }

@router.post("/api/attendance/mark-present")
async def mark_present_today(data: AttendanceCreate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    """Return an employee to the default auto-present mode."""
    today = date.today()
    employee = await _get_employee_or_404(db, data.employee_id)
    employee.attendance_auto_status = ATTENDANCE_STATUS_PRESENT
    attendance, updated = await _upsert_attendance_for_day(
        db,
        data.employee_id,
        today,
        ATTENDANCE_STATUS_PRESENT,
        data.note,
    )
    await db.commit()
    return {
        "id": attendance.id,
        "updated": updated,
        "auto_status": employee.attendance_auto_status,
    }


# ── PAYROLL API ────────────────────────────────────────
@router.get("/api/payroll")
async def get_payroll(period: str = None, db: AsyncSession = Depends(get_async_session)):
    stmt = select(Payroll).options(selectinload(Payroll.employee).selectinload(Employee.farm))
    if period:
        stmt = stmt.where(Payroll.period == period)
    stmt = stmt.order_by(Payroll.period.desc(), Payroll.id)
    _r = await db.execute(stmt)
    records = _r.scalars().all()
    return [
        {
            "id":          r.id,
            "employee_id": r.employee_id,
            "employee":    r.employee.name if r.employee else "—",
            "farm_id":     r.employee.farm_id if r.employee else None,
            "farm_name":   r.employee.farm.name if r.employee and r.employee.farm else None,
            "period":      r.period,
            "base_salary": float(r.base_salary) if r.base_salary else 0,
            "days_worked": r.days_worked or 0,
            "working_days":r.working_days or 0,
            "bonuses":     float(r.bonuses)     if r.bonuses     else 0,
            "deductions":  float(r.deductions)  if r.deductions  else 0,
            "loan_deductions": float(r.loan_deductions) if getattr(r, "loan_deductions", None) else 0,
            "day_deduction_days": float(r.day_deduction_days) if getattr(r, "day_deduction_days", None) else 0,
            "day_deductions": float(r.day_deductions) if getattr(r, "day_deductions", None) else 0,
            "manual_deductions": float(r.manual_deductions) if getattr(r, "manual_deductions", None) else 0,
            "net_salary":  float(r.net_salary)  if r.net_salary  else 0,
            "paid":        r.paid,
            "paid_at":     str(r.paid_at) if r.paid_at else None,
        }
        for r in records
    ]

@router.get("/api/payroll/preview")
async def preview_payroll(
    period: str,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """
    Preview payroll for a period without saving.
    Calculates each employee's salary based on days worked.
    period format: "2026-04"
    """
    from calendar import monthrange
    period = _validate_period(period)
    year, month = int(period.split("-")[0]), int(period.split("-")[1])
    # Total working days in month (Mon-Fri)
    total_days   = monthrange(year, month)[1]
    working_days = sum(1 for d in range(1, total_days+1)
                       if date(year, month, d).weekday() < 5)
    # Days so far this month (up to today)
    today = date.today()
    if today.year == year and today.month == month:
        days_elapsed = sum(1 for d in range(1, today.day+1)
                           if date(year, month, d).weekday() < 5)
    else:
        days_elapsed = working_days

    _r = await db.execute(select(Employee).where(Employee.is_active == True))
    employees = _r.scalars().all()
    result = []
    total_to_pay = 0
    include_loans = has_permission(current_user, "action_hr_view_loans")
    include_deductions = has_permission(current_user, "action_hr_view_deductions")
    for emp in employees:
        row = await _payroll_preview_for_employee(
            db,
            emp,
            period=period,
            working_days=working_days,
            days_elapsed=days_elapsed,
            year=year,
            month=month,
            include_loans=include_loans,
            include_deductions=include_deductions,
        )
        total_to_pay += row["net_before_loan"]
        result.append(row)
    return {
        "period":       period,
        "working_days": working_days,
        "days_elapsed": days_elapsed,
        "employees":    result,
        "total_to_pay": round(total_to_pay, 2),
        "can_view_loans": include_loans,
        "can_view_deductions": include_deductions,
    }

@router.post("/api/payroll/run", dependencies=[Depends(require_permission("action_hr_run_payroll"))])
async def run_payroll(data: PayrollRun, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    from calendar import monthrange
    period = _validate_period(data.period)
    year, month = int(period.split("-")[0]), int(period.split("-")[1])
    total_days   = monthrange(year, month)[1]
    working_days = sum(1 for d in range(1, total_days+1)
                       if date(year, month, d).weekday() < 5)

    emp_stmt = select(Employee).where(Employee.is_active == True)
    if data.emp_ids:
        emp_stmt = emp_stmt.where(Employee.id.in_(data.emp_ids))
    _re = await db.execute(emp_stmt)
    employees = _re.scalars().all()

    bonus_by_employee = data.bonuses or {}
    loan_repayment_by_employee = data.loan_repayments or {}
    created = 0
    skipped = 0
    payroll_ids = []
    try:
        for emp in employees:
            _ex = await db.execute(select(Payroll).where(
                Payroll.employee_id == emp.id,
                Payroll.period == period,
            ))
            payroll = _ex.scalar_one_or_none()

            _dp = await db.execute(select(func.count(Attendance.id)).where(
                Attendance.employee_id == emp.id,
                Attendance.status == "present",
                func.extract("year",  Attendance.date) == year,
                func.extract("month", Attendance.date) == month,
            ))
            days_present = _dp.scalar() or 0

            pending_deductions, pending_day_days, pending_day_amount, pending_manual_amount = await _pending_deductions_for_period(
                db,
                emp.id,
                period,
            )
            requested_loan_repayment = _money(loan_repayment_by_employee.get(emp.id, Decimal("0")))
            bonus_amount = _money(bonus_by_employee.get(emp.id, Decimal("0")))

            if payroll:
                skipped += 1
                existing_loan_deductions = _money(getattr(payroll, "loan_deductions", 0))
                existing_day_days = _days(getattr(payroll, "day_deduction_days", 0))
                existing_day_deductions = _money(getattr(payroll, "day_deductions", 0))
                existing_manual_deductions = _money(getattr(payroll, "manual_deductions", 0))
                if emp.id not in bonus_by_employee:
                    bonus_amount = _money(payroll.bonuses)
            else:
                payroll = Payroll(
                    employee_id=emp.id,
                    period=period,
                    paid=False,
                )
                db.add(payroll)
                created += 1
                existing_loan_deductions = Decimal("0")
                existing_day_days = Decimal("0")
                existing_day_deductions = Decimal("0")
                existing_manual_deductions = Decimal("0")

            payroll.base_salary = _money(emp.base_salary)
            payroll.bonuses = bonus_amount
            payroll.days_worked = days_present
            payroll.working_days = working_days
            payroll.day_deduction_days = _days(existing_day_days + pending_day_days)
            payroll.day_deductions = _money(existing_day_deductions + pending_day_amount)
            payroll.manual_deductions = _money(existing_manual_deductions + pending_manual_amount)
            payroll.loan_deductions = _money(existing_loan_deductions + requested_loan_repayment)
            payroll.deductions = _money(
                payroll.loan_deductions + payroll.day_deductions + payroll.manual_deductions
            )
            payroll.net_salary = _money(payroll.base_salary + payroll.bonuses - payroll.deductions)
            await db.flush()
            payroll_ids.append(payroll.id)

            for deduction in pending_deductions:
                deduction.payroll_id = payroll.id

            if requested_loan_repayment > 0:
                applied = await _apply_loan_repayment_to_oldest_loans(
                    db,
                    employee_id=emp.id,
                    amount=requested_loan_repayment,
                    repayment_date=date.today(),
                    payroll_id=payroll.id,
                    note=f"Payroll loan repayment - {period}",
                    current_user=current_user,
                )
                db.add(
                    EmployeePayrollDeduction(
                        employee_id=emp.id,
                        payroll_id=payroll.id,
                        period=period,
                        deduction_date=date.today(),
                        type="loan_repayment",
                        amount=applied,
                        note=f"Payroll loan repayment - {period}",
                        created_by_user_id=current_user.id,
                    )
                )

        record(db, "HR", "run_payroll",
               f"Payroll run for {period} — {created} created, {skipped} updated",
               user=current_user, ref_type="payroll", ref_id=period)
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=500, detail="Could not run payroll. No payroll changes were saved.") from exc
    return {"created": created, "skipped": skipped, "period": period, "payroll_ids": payroll_ids}


@router.put("/api/payroll/{payroll_id}", dependencies=[Depends(require_permission("action_hr_run_payroll"))])
async def update_payroll(payroll_id: int, data: PayrollUpdate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    _r = await db.execute(select(Payroll).where(Payroll.id == payroll_id))
    p = _r.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Payroll record not found")
    p.bonuses = _money(data.bonuses)
    p.manual_deductions = _money(data.deductions)
    p.deductions = _money(
        _dec(getattr(p, "loan_deductions", 0))
        + _dec(getattr(p, "day_deductions", 0))
        + p.manual_deductions
    )
    p.net_salary = _money(_dec(p.base_salary) + p.bonuses - p.deductions)
    if data.notes and p.manual_deductions > 0:
        db.add(
            EmployeePayrollDeduction(
                employee_id=p.employee_id,
                payroll_id=p.id,
                period=p.period,
                type="manual",
                amount=p.manual_deductions,
                note=data.notes,
                created_by_user_id=current_user.id,
            )
        )
    record(db, "HR", "update_payroll",
           f"Updated payroll #{payroll_id} — bonuses: {p.bonuses}, deductions: {p.deductions}, net: {p.net_salary:.2f}",
           user=current_user, ref_type="payroll", ref_id=payroll_id)
    await db.commit()
    return {"ok": True, "net_salary": float(p.net_salary)}

@router.patch("/api/payroll/{payroll_id}/pay", dependencies=[Depends(require_permission("action_hr_mark_paid"))])
async def mark_paid(payroll_id: int, data: Optional[PayrollPayRequest] = None, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    _r = await db.execute(
        select(Payroll)
        .options(selectinload(Payroll.employee).selectinload(Employee.farm))
        .where(Payroll.id == payroll_id)
    )
    p = _r.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Payroll record not found")
    now = datetime.now(timezone.utc)
    payment_method = (data.payment_method if data else "cash") or "cash"
    expense = await create_payroll_expense(
        db,
        p,
        current_user,
        payment_method=payment_method,
        paid_date=now.date(),
    )
    p.paid    = True
    p.paid_at = now
    record(db, "HR", "mark_payroll_paid",
           f"Marked payroll #{payroll_id} as paid — net: {float(p.net_salary):.2f}",
           user=current_user, ref_type="payroll", ref_id=payroll_id)
    await db.commit()
    employee = p.employee
    employee_farm = employee.farm if employee else None
    expense_farm = getattr(expense, "farm", None)
    response = {
        "ok": True,
        "payroll_id": p.id,
        "expense_id": expense.id,
        "expense_ref_number": expense.ref_number,
        "category": expense.category.name if expense.category else "Salaries & Wages",
        "farm_id": expense.farm_id,
        "farm_name": expense_farm.name if expense_farm else (employee_farm.name if employee_farm and expense.farm_id == employee_farm.id else None),
        "amount": float(expense.amount),
    }
    if expense.farm_id is None:
        response["warning"] = "Employee has no farm assigned, so salary expense was not linked to a farm."
    return response


@router.post("/clear-data", dependencies=[Depends(require_permission("action_hr_clear_data"))])
async def clear_hr_data(
    data: Optional[ClearHRDataRequest] = None,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    if data is None or data.confirmation != CLEAR_HR_DATA_CONFIRMATION:
        raise HTTPException(status_code=400, detail='Type "CLEAR HR DATA" to confirm.')

    try:
        result = await _clear_hr_data(db, current_user)
        await db.commit()
        return result
    except HTTPException:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status_code=500,
            detail="Could not clear HR data. No records were deleted.",
        ) from exc


@router.get("/api/summary")
async def hr_summary(db: AsyncSession = Depends(get_async_session)):
    _te = await db.execute(select(func.count(Employee.id)).where(Employee.is_active == True))
    total_employees = _te.scalar() or 0
    today = date.today()
    _pt = await db.execute(select(func.count(Attendance.id)).where(
        Attendance.date == today,
        Attendance.status == "present",
    ))
    present_today = _pt.scalar() or 0
    _at = await db.execute(select(func.count(Attendance.id)).where(
        Attendance.date == today,
        Attendance.status == "absent",
    ))
    absent_today = _at.scalar() or 0
    _ts = await db.execute(select(func.sum(Employee.base_salary)).where(Employee.is_active == True))
    total_salary = _ts.scalar() or 0
    return {
        "total_employees": total_employees,
        "present_today":   present_today,
        "absent_today":    absent_today,
        "total_salary":    float(total_salary),
    }


# ── UI ─────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
def hr_ui(current_user: User = Depends(require_permission("page_hr"))):
    html_content = """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<script src="/static/theme-init.js"></script>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>HR & Payroll</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root {
    --bg:      #060810;
    --surface: #0a0d18;
    --card:    #0f1424;
    --card2:   #151c30;
    --border:  rgba(255,255,255,0.06);
    --border2: rgba(255,255,255,0.11);
    --green:   #00ff9d;
    --blue:    #4d9fff;
    --purple:  #a855f7;
    --danger:  #ff4d6d;
    --warn:    #ffb547;
    --text:    #f0f4ff;
    --sub:     #8899bb;
    --muted:   #445066;
    --sans:    'Outfit', sans-serif;
    --mono:    'JetBrains Mono', monospace;
    --r:       12px;
}
body.light{
    --bg:#f4f5ef;--surface:#f1f3eb;--card:#eceee6;--card2:#e4e6de;
    --border:rgba(0,0,0,0.08);--border2:rgba(0,0,0,0.14);
    --green:#0f8a43;
    --text:#1a1e14;--sub:#4a5040;--muted:#7b816f;
}
body.light nav{background:rgba(244,245,239,.92);}
body.light .nav-link:hover{background:rgba(0,0,0,.05);}
body.light tr:hover td{background:rgba(0,0,0,.03);}
.mode-btn{display:flex;align-items:center;justify-content:center;width:36px;height:36px;border-radius:10px;border:1px solid var(--border);background:var(--card);color:var(--sub);font-size:16px;cursor:pointer;transition:all .2s;font-family:var(--sans);}
.mode-btn:hover{border-color:var(--border2);transform:scale(1.06);}
.topbar-right{display:flex;align-items:center;gap:12px;}
.account-menu{position:relative;}
.user-pill{display:flex;align-items:center;gap:10px;background:var(--card);border:1px solid var(--border);border-radius:40px;padding:7px 16px 7px 10px;cursor:pointer;transition:all .2s;}
.user-pill:hover,.user-pill.open{border-color:var(--border2);}
.user-avatar{width:28px;height:28px;background:linear-gradient(135deg,#7ecb6f,#d4a256);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;color:#0a0c08;}
.user-name{font-size:13px;font-weight:500;color:var(--sub);}
.menu-caret{font-size:11px;color:var(--muted);}
.account-dropdown{position:absolute;right:0;top:calc(100% + 10px);min-width:220px;background:var(--card);border:1px solid var(--border2);border-radius:14px;padding:8px;box-shadow:0 24px 50px rgba(0,0,0,.35);display:none;z-index:500;}
.account-dropdown.open{display:block;}
.account-head{padding:10px 12px 8px;border-bottom:1px solid var(--border);margin-bottom:6px;}
.account-label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;}
.account-email{font-size:12px;color:var(--sub);margin-top:4px;word-break:break-word;}
.account-item{width:100%;display:flex;align-items:center;gap:10px;padding:10px 12px;border:none;background:transparent;border-radius:10px;color:var(--sub);font-family:var(--sans);font-size:13px;text-decoration:none;cursor:pointer;text-align:left;}
.account-item:hover{background:var(--card2);color:var(--text);}
.account-item.danger:hover{color:#c97a7a;}
.logout-btn{background:transparent;border:1px solid var(--border);color:var(--muted);font-family:var(--sans);font-size:12px;font-weight:500;padding:8px 16px;border-radius:8px;cursor:pointer;transition:all .2s;letter-spacing:.3px;}
.logout-btn:hover{border-color:#c97a7a;color:#c97a7a;}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: var(--sans); background: var(--bg); color: var(--text); min-height: 100vh; font-size: 14px; }
nav {
    position: sticky; top: 0; z-index: 100;
    display: flex; align-items: center; gap: 10px;
    padding: 0 24px; height: 58px;
    background: rgba(10,13,24,.92); backdrop-filter: blur(20px);
    border-bottom: 1px solid var(--border);
}
.logo {
    font-size: 18px; font-weight: 900;
    background: linear-gradient(135deg, var(--green), var(--blue));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    background-clip: text; margin-right: 12px;
}
.nav-link { padding: 7px 14px; border-radius: 8px; color: var(--sub); font-size: 13px; font-weight: 600; text-decoration: none; transition: all .2s; }
.nav-link:hover { background: rgba(255,255,255,.05); color: var(--text); }
.nav-link.active { background: rgba(0,255,157,.1); color: var(--green); }
.nav-spacer { flex: 1; }
.content { max-width: 1300px; margin: 0 auto; padding: 28px 24px; display: flex; flex-direction: column; gap: 20px; }
.page-title { font-size: 24px; font-weight: 800; letter-spacing: -.5px; }
.page-sub   { color: var(--muted); font-size: 13px; margin-top: 3px; }
.stats-grid { display: grid; grid-template-columns: repeat(auto-fit,minmax(180px,1fr)); gap: 14px; }
.stat-card { background: var(--card); border: 1px solid var(--border); border-radius: var(--r); padding: 18px 20px; display: flex; flex-direction: column; gap: 8px; position: relative; overflow: hidden; }
.stat-card::before { content:''; position:absolute; top:0; left:0; right:0; height:2px; }
.stat-card.green::before  { background: linear-gradient(90deg,var(--green),transparent); }
.stat-card.blue::before   { background: linear-gradient(90deg,var(--blue),transparent); }
.stat-card.warn::before   { background: linear-gradient(90deg,var(--warn),transparent); }
.stat-card.purple::before { background: linear-gradient(90deg,var(--purple),transparent); }
.stat-label { font-size: 10px; font-weight: 700; letter-spacing: 1.5px; text-transform: uppercase; color: var(--muted); }
.stat-value { font-family: var(--mono); font-size: 28px; font-weight: 700; }
.stat-value.green  { color: var(--green); }
.stat-value.blue   { color: var(--blue); }
.stat-value.warn   { color: var(--warn); }
.stat-value.purple { color: var(--purple); }
.tabs { display: flex; gap: 4px; background: var(--card); border: 1px solid var(--border); border-radius: var(--r); padding: 4px; width: fit-content; }
.tab { padding: 8px 20px; border-radius: 9px; font-size: 13px; font-weight: 700; cursor: pointer; border: none; background: transparent; color: var(--muted); transition: all .2s; font-family: var(--sans); }
.tab.active { background: var(--card2); color: var(--text); }
.toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.search-box { display: flex; align-items: center; gap: 9px; background: var(--card); border: 1px solid var(--border); border-radius: var(--r); padding: 0 14px; flex: 1; min-width: 200px; transition: border-color .2s; }
.search-box:focus-within { border-color: rgba(0,255,157,.3); }
.search-box svg { color: var(--muted); flex-shrink: 0; }
.search-box input { background: transparent; border: none; outline: none; color: var(--text); font-family: var(--sans); font-size: 14px; padding: 11px 0; width: 100%; }
.search-box input::placeholder { color: var(--muted); }
.btn { display: flex; align-items: center; gap: 7px; padding: 10px 16px; border-radius: var(--r); font-family: var(--sans); font-size: 13px; font-weight: 700; cursor: pointer; border: none; transition: all .2s; white-space: nowrap; }
.btn-green  { background: linear-gradient(135deg,var(--green),#00d4ff); color: #021a10; }
.btn-green:hover { filter: brightness(1.1); transform: translateY(-1px); }
.btn-blue   { background: linear-gradient(135deg,var(--blue),var(--purple)); color: white; }
.btn-blue:hover { filter: brightness(1.1); transform: translateY(-1px); }
.btn-purple { background: linear-gradient(135deg,var(--purple),#e879f9); color: white; }
.btn-purple:hover { filter: brightness(1.1); transform: translateY(-1px); }
.btn-danger { background: linear-gradient(135deg,var(--danger),#ef4444); color: white; }
.btn-danger:hover { filter: brightness(1.08); transform: translateY(-1px); }
.btn-danger:disabled { opacity: .45; cursor: not-allowed; filter: none; transform: none; }
.table-wrap { background: var(--card); border: 1px solid var(--border); border-radius: var(--r); overflow: hidden; }
table { width: 100%; border-collapse: collapse; }
thead { background: var(--card2); }
th { text-align: left; font-size: 10px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; color: var(--muted); padding: 12px 16px; }
td { padding: 12px 16px; border-top: 1px solid var(--border); color: var(--sub); font-size: 13px; }
tr:hover td { background: rgba(255,255,255,.02); }
td.name { color: var(--text); font-weight: 600; }
td.mono { font-family: var(--mono); color: var(--green); }
.action-btn { background: transparent; border: 1px solid var(--border2); color: var(--sub); font-size: 12px; font-weight: 600; padding: 5px 10px; border-radius: 7px; cursor: pointer; transition: all .15s; font-family: var(--sans); }
.action-btn:hover { border-color: var(--blue); color: var(--blue); }
.action-btn.danger:hover { border-color: var(--danger); color: var(--danger); }
.action-btn.green:hover  { border-color: var(--green); color: var(--green); }
.action-btn.purple:hover { border-color: var(--purple); color: var(--purple); }
.status-present { color: var(--green); font-size: 12px; font-weight: 700; }
.status-absent  { color: var(--danger); font-size: 12px; font-weight: 700; }
.status-late    { color: var(--warn); font-size: 12px; font-weight: 700; }
.status-leave   { color: var(--blue); font-size: 12px; font-weight: 700; }
.paid-badge   { display:inline-flex;align-items:center;gap:4px;background:rgba(0,255,157,.1);border:1px solid rgba(0,255,157,.2);color:var(--green);font-size:11px;font-weight:700;padding:2px 8px;border-radius:20px; }
.unpaid-badge { display:inline-flex;align-items:center;gap:4px;background:rgba(255,181,71,.1);border:1px solid rgba(255,181,71,.2);color:var(--warn);font-size:11px;font-weight:700;padding:2px 8px;border-radius:20px; }
.modal-bg { position: fixed; inset: 0; z-index: 500; background: rgba(0,0,0,.7); backdrop-filter: blur(4px); display: none; align-items: center; justify-content: center; }
.modal-bg.open { display: flex; }
.modal { background: var(--card); border: 1px solid var(--border2); border-radius: 16px; padding: 28px; width: 500px; max-width: 95vw; max-height: 90vh; overflow-y: auto; animation: modalIn .2s ease; }
.modal.wide { width: 980px; }
@keyframes modalIn { from{opacity:0;transform:scale(.95)} to{opacity:1;transform:scale(1)} }
.modal-title { font-size: 18px; font-weight: 800; margin-bottom: 20px; }
.modal-title.danger { color: var(--danger); }
.danger-note { border:1px solid rgba(255,77,109,.28); background:rgba(255,77,109,.08); border-radius:12px; padding:12px 14px; color:var(--sub); font-size:13px; line-height:1.45; margin-bottom:16px; }
.confirm-token { font-family:var(--mono); color:var(--danger); font-weight:800; }
.hr-ledger-grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
.hr-ledger-panel { border:1px solid var(--border); border-radius:12px; padding:14px; }
.hr-ledger-title { font-size:11px; font-weight:800; letter-spacing:1.3px; text-transform:uppercase; color:var(--muted); margin-bottom:12px; }
.hr-ledger-table { max-height:220px; overflow:auto; border:1px solid var(--border); border-radius:10px; }
.hr-ledger-table th,.hr-ledger-table td { padding:8px 10px; font-size:12px; }
.money-preview { font-family:var(--mono); color:var(--green); font-weight:800; }
.form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.fld { display: flex; flex-direction: column; gap: 6px; margin-bottom: 14px; }
.fld.span2 { grid-column: span 2; }
.fld label { font-size: 11px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; color: var(--muted); }
.fld input, .fld select { background: var(--card2); border: 1px solid var(--border2); border-radius: 10px; padding: 10px 12px; color: var(--text); font-family: var(--sans); font-size: 14px; outline: none; transition: border-color .2s; width: 100%; }
.fld input:focus, .fld select:focus { border-color: rgba(0,255,157,.4); }
.modal-actions { display: flex; gap: 10px; margin-top: 6px; justify-content: flex-end; }
.btn-cancel { background: transparent; border: 1px solid var(--border2); color: var(--sub); padding: 10px 18px; border-radius: var(--r); font-family: var(--sans); font-size: 13px; font-weight: 700; cursor: pointer; }
.btn-cancel:hover { border-color: var(--danger); color: var(--danger); }
.toast { position: fixed; bottom: 22px; left: 50%; transform: translateX(-50%) translateY(16px); background: var(--card2); border: 1px solid var(--border2); border-radius: var(--r); padding: 12px 20px; font-size: 13px; font-weight: 600; color: var(--text); box-shadow: 0 20px 50px rgba(0,0,0,.5); opacity: 0; pointer-events: none; transition: opacity .25s, transform .25s; z-index: 999; }
.toast.show { opacity:1; transform: translateX(-50%) translateY(0); }
::-webkit-scrollbar { width: 4px; }
::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 4px; }
</style>
    <script src="/static/auth-guard.js"></script>
</head>
<body>
""" + render_app_header(current_user, "page_hr") + """

<div class="content">
    <div>
        <div class="page-title">HR & Payroll</div>
        <div class="page-sub">Manage employees, attendance and salaries</div>
    </div>

    <!-- STATS -->
    <div class="stats-grid">
        <div class="stat-card green">
            <div class="stat-label">Total Employees</div>
            <div class="stat-value green" id="stat-total">-</div>
        </div>
        <div class="stat-card blue">
            <div class="stat-label">Present Today</div>
            <div class="stat-value blue" id="stat-present">-</div>
        </div>
        <div class="stat-card warn">
            <div class="stat-label">Absent Today</div>
            <div class="stat-value warn" id="stat-absent">-</div>
        </div>
        <div class="stat-card purple">
            <div class="stat-label">Monthly Payroll</div>
            <div class="stat-value purple" id="stat-salary">-</div>
        </div>
    </div>

    <!-- TABS -->
    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;">
        <div class="tabs">
            <button class="tab active" id="tab-emp"        onclick="switchTab('employees')">Employees</button>
            <button class="tab"        id="tab-att"        onclick="switchTab('attendance')">Attendance</button>
            <button class="tab"        id="tab-pay"        onclick="switchTab('payroll')">Payroll</button>
        </div>
        <div style="display:flex;gap:10px;" id="tab-actions">
            <button class="btn btn-green"  id="btn-add-emp"  onclick="openAddEmpModal()">+ Add Employee</button>
            <button class="btn btn-blue"   id="btn-log-att"  onclick="openLogAttModal()" style="display:none">+ Log Attendance</button>
            <button class="btn btn-danger" id="btn-clear-hr-data" onclick="openClearHRDataModal()" style="display:none">Clear HR Data</button>
        </div>
    </div>

    <!-- EMPLOYEES -->
    <div id="section-employees">
        <div class="toolbar">
            <div class="search-box">
                <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
                <input id="emp-search" placeholder="Search by name, position or department..." oninput="onEmpSearch()">
            </div>
        </div>
        <div id="farm-load-error" style="display:none;margin-bottom:12px;padding:10px 12px;border:1px solid rgba(255,181,71,.25);border-radius:10px;background:rgba(255,181,71,.08);color:var(--warn);font-size:12px;font-weight:600"></div>
        <div class="table-wrap">
            <table>
                <thead><tr><th>Name</th><th>Position</th><th>Department</th><th>Farm</th><th>Phone</th><th>Hire Date</th><th>Base Salary</th><th>Actions</th></tr></thead>
                <tbody id="emp-body"><tr><td colspan="8" style="text-align:center;color:var(--muted);padding:40px">Loading...</td></tr></tbody>
            </table>
        </div>
    </div>

    <!-- ATTENDANCE -->
    <div id="section-attendance" style="display:none">
        <div class="toolbar">
            <div class="fld" style="margin:0;flex:0 0 180px">
                <input id="att-period" type="month" style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:10px 14px;background:linear-gradient(135deg,var(--green),var(--blue));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;font-family:var(--sans);font-size:14px;outline:none;" onchange="loadAttendance()">
            </div>
            <div class="fld" style="margin:0;flex:0 0 200px">
                <select id="att-emp-filter" style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:10px 14px;color:var(--text);font-family:var(--sans);font-size:14px;outline:none;" onchange="loadAttendance()">
                    <option value="">All Employees</option>
                </select>
            </div>
        </div>

        <!-- TODAY CARD -->
        <div style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:16px 18px;margin-bottom:14px;">
            <div style="font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-bottom:12px">Today's Attendance</div>
            <div id="today-attendance-grid" style="display:flex;flex-direction:column;gap:8px;"></div>
        </div>

        <div class="table-wrap">
            <table>
                <thead><tr><th>Employee</th><th>Date</th><th>Status</th><th>Note</th></tr></thead>
                <tbody id="att-body"><tr><td colspan="4" style="text-align:center;color:var(--muted);padding:40px">Loading...</td></tr></tbody>
            </table>
        </div>
    </div>

    <!-- PAYROLL -->
    <div id="section-payroll" style="display:none">
        <div class="toolbar">
            <div class="fld" style="margin:0;flex:0 0 180px">
                <input id="pay-period" type="month" style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:10px 14px;color:var(--text);font-family:var(--sans);font-size:14px;outline:none;" onchange="loadPayrollPreview()">
            </div>
        </div>

        <!-- PAYROLL PREVIEW -->
        <div id="payroll-preview-wrap" style="display:none;margin-bottom:14px;">
            <div style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:16px 18px;margin-bottom:12px;">
                <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;">
                    <div>
                        <div style="font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-bottom:4px">Payroll Preview</div>
                        <div style="font-size:13px;color:var(--sub)" id="preview-meta"></div>
                    </div>
                    <div style="text-align:right">
                        <div style="font-size:11px;color:var(--muted);margin-bottom:2px">Total to Pay</div>
                        <div style="font-family:var(--mono);font-size:24px;font-weight:700;color:var(--green)" id="preview-total">-</div>
                    </div>
                </div>
            </div>
            <div class="table-wrap">
                <table>
                    <thead><tr><th>Employee</th><th>Base Salary</th><th>Bonus</th><th>Day Ded.</th><th>Manual Ded.</th><th>Loan Balance</th><th>Loan Repay</th><th>Net Preview</th><th>Status</th></tr></thead>
                    <tbody id="preview-body"></tbody>
                </table>
            </div>
            <div style="display:flex;justify-content:flex-end;margin-top:12px;">
                <button class="btn btn-purple" onclick="confirmRunPayroll()">Confirm & Run Payroll</button>
            </div>
        </div>

        <!-- PAYROLL RECORDS -->
        <div class="table-wrap" id="payroll-records-wrap" style="display:none">
            <table>
                <thead><tr><th>Employee</th><th>Period</th><th>Base Salary</th><th>Days</th><th>Bonuses</th><th>Loan</th><th>Day Ded.</th><th>Manual</th><th>Total Ded.</th><th>Net Salary</th><th>Status</th><th>Actions</th></tr></thead>
                <tbody id="pay-body"></tbody>
            </table>
        </div>
    </div>
</div>

<!-- ADD EMPLOYEE MODAL -->
<div class="modal-bg" id="emp-modal">
    <div class="modal">
        <div class="modal-title" id="emp-modal-title">Add Employee</div>
        <div class="form-row">
            <div class="fld span2"><label>Full Name *</label><input id="e-name" placeholder="Employee name"></div>
            <div class="fld"><label>Position</label><input id="e-position" placeholder="e.g. Cashier"></div>
            <div class="fld"><label>Department</label><input id="e-department" placeholder="e.g. Sales"></div>
            <div class="fld"><label>Farm</label><select id="e-farm"><option value="">No farm selected</option></select></div>
            <div class="fld"><label>Phone</label><input id="e-phone" placeholder="+20 100 000 0000"></div>
            <div class="fld"><label>Hire Date</label><input id="e-hire" type="date"></div>
            <div class="fld span2"><label>Base Salary</label><input id="e-salary" type="number" placeholder="0.00" min="0"></div>
        </div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeEmpModal()">Cancel</button>
            <button class="btn btn-green" onclick="saveEmployee()">Save Employee</button>
        </div>
    </div>
</div>

<!-- LOG ATTENDANCE MODAL -->
<div class="modal-bg" id="att-modal">
    <div class="modal">
        <div class="modal-title">Log Attendance</div>
        <div class="fld"><label>Employee *</label>
            <select id="a-emp"></select>
        </div>
        <div class="fld"><label>Date *</label><input id="a-date" type="date"></div>
        <div class="fld"><label>Status</label>
            <select id="a-status">
                <option value="present">Present</option>
                <option value="absent">Absent</option>
                <option value="late">Late</option>
                <option value="leave">Leave</option>
            </select>
        </div>
        <div class="fld"><label>Note</label><input id="a-note" placeholder="Optional note"></div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeAttModal()">Cancel</button>
            <button class="btn btn-blue" onclick="saveAttendance()">Log Attendance</button>
        </div>
    </div>
</div>

<!-- RUN PAYROLL MODAL -->
<div class="modal-bg" id="pay-run-modal">
    <div class="modal">
        <div class="modal-title">Run Payroll</div>
        <div class="fld"><label>Period *</label><input id="pr-period" type="month"></div>
        <p style="color:var(--muted);font-size:13px;margin-bottom:16px">This will generate payroll records for all active employees for the selected period. Already existing records will be skipped.</p>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeRunPayModal()">Cancel</button>
            <button class="btn btn-purple" onclick="runPayroll()">Run Payroll</button>
        </div>
    </div>
</div>

<!-- EDIT PAYROLL MODAL -->
<div class="modal-bg" id="edit-pay-modal">
    <div class="modal">
        <div class="modal-title">Edit Payroll</div>
        <div class="modal-sub" id="edit-pay-emp" style="color:var(--muted);font-size:13px;margin-bottom:16px"></div>
        <div class="fld"><label>Bonuses</label><input id="ep-bonuses" type="number" placeholder="0" min="0"></div>
        <div class="fld"><label>Deductions</label><input id="ep-deductions" type="number" placeholder="0" min="0"></div>
        <div class="fld"><label>Notes</label><input id="ep-notes" placeholder="Optional notes"></div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeEditPayModal()">Cancel</button>
            <button class="btn btn-green" onclick="savePayrollEdit()">Save</button>
        </div>
    </div>
</div>

<!-- LOANS & DEDUCTIONS MODAL -->
<div class="modal-bg" id="loan-deduction-modal">
    <div class="modal wide">
        <div class="modal-title">Loans & Deductions</div>
        <div class="modal-sub" id="loan-deduction-emp" style="color:var(--muted);font-size:13px;margin-bottom:16px"></div>
        <div class="hr-ledger-grid">
            <div class="hr-ledger-panel" id="loan-section">
                <div class="hr-ledger-title">Employee Loans</div>
                <div class="form-row" id="loan-create-form">
                    <div class="fld"><label>Loan Date</label><input id="loan-date" type="date"></div>
                    <div class="fld"><label>Amount</label><input id="loan-amount" type="number" min="0" step="0.01"></div>
                    <div class="fld span2"><label>Description</label><input id="loan-description" placeholder="Salary advance"></div>
                    <div class="fld span2"><button class="btn btn-green" onclick="saveEmployeeLoan()">Save Loan</button></div>
                </div>
                <div class="form-row" id="loan-repayment-form" style="margin-top:10px">
                    <div class="fld"><label>Loan</label><select id="repay-loan-id"></select></div>
                    <div class="fld"><label>Repayment Date</label><input id="repay-date" type="date"></div>
                    <div class="fld"><label>Amount</label><input id="repay-amount" type="number" min="0" step="0.01"></div>
                    <div class="fld"><label>Note</label><input id="repay-note" placeholder="Cash repayment"></div>
                    <div class="fld span2"><button class="btn btn-blue" onclick="saveLoanRepayment()">Save Repayment</button></div>
                </div>
                <div class="hr-ledger-table"><table><thead><tr><th>Date</th><th>Amount</th><th>Repaid</th><th>Balance</th><th>Status</th><th>Actions</th></tr></thead><tbody id="loan-history-body"></tbody></table></div>
            </div>
            <div class="hr-ledger-panel" id="deduction-section">
                <div class="hr-ledger-title">Payroll Deductions</div>
                <div class="form-row" id="day-deduction-form">
                    <div class="fld"><label>Period</label><input id="deduct-period" type="month"></div>
                    <div class="fld"><label>Date</label><input id="deduct-date" type="date"></div>
                    <div class="fld"><label>Days</label><input id="deduct-days" type="number" min="0" step="0.25" oninput="updateDayDeductionPreview()"></div>
                    <div class="fld"><label>Working Days</label><input id="deduct-working-days" type="number" min="1" step="1" oninput="updateDayDeductionPreview()"></div>
                    <div class="fld"><label>Amount Preview</label><div class="money-preview" id="deduct-preview">0.00</div></div>
                    <div class="fld"><label>Note</label><input id="deduct-note" placeholder="Left early"></div>
                    <div class="fld span2"><button class="btn btn-blue" onclick="saveDayDeduction()">Save Day Deduction</button></div>
                </div>
                <div class="form-row" id="manual-deduction-form" style="margin-top:10px">
                    <div class="fld"><label>Period</label><input id="manual-deduct-period" type="month"></div>
                    <div class="fld"><label>Amount</label><input id="manual-deduct-amount" type="number" min="0" step="0.01"></div>
                    <div class="fld span2"><label>Note</label><input id="manual-deduct-note" placeholder="Manual deduction"></div>
                    <div class="fld span2"><button class="btn btn-purple" onclick="saveManualDeduction()">Save Manual Deduction</button></div>
                </div>
                <div class="hr-ledger-table"><table><thead><tr><th>Period</th><th>Type</th><th>Days</th><th>Rate</th><th>Amount</th><th>Payroll</th><th>Note</th></tr></thead><tbody id="deduction-history-body"></tbody></table></div>
            </div>
        </div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeLoanDeductionModal()">Close</button>
        </div>
    </div>
</div>

<!-- CLEAR HR DATA MODAL -->
<div class="modal-bg" id="clear-hr-modal">
    <div class="modal">
        <div class="modal-title danger">Clear HR Data</div>
        <div class="danger-note">
            This permanently deletes employees, attendance, payroll, and payroll-linked salary expenses. Other business data is not cleared.
        </div>
        <div class="fld">
            <label>Type <span class="confirm-token">CLEAR HR DATA</span> to confirm</label>
            <input id="clear-hr-confirmation" autocomplete="off" oninput="updateClearHRDataConfirmState()">
        </div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeClearHRDataModal()">Cancel</button>
            <button class="btn btn-danger" id="btn-confirm-clear-hr" onclick="confirmClearHRData()" disabled>Clear HR Data</button>
        </div>
    </div>
</div>

<div class="toast" id="toast"></div>

<script>
  function setModeButton(isLight){
    const btn = document.getElementById("mode-btn");
    if(btn) btn.innerText = isLight ? "☀️" : "🌙";
}
function toggleMode(){
    const isLight = document.body.classList.toggle("light");
    localStorage.setItem("colorMode", isLight ? "light" : "dark");
    setModeButton(isLight);
}
function initializeColorMode(){
    const isLight = localStorage.getItem("colorMode") === "light";
    document.body.classList.toggle("light", isLight);
    setModeButton(isLight);
}
async function initUser() {
    try {
        const r = await fetch("/auth/me");
        if (!r.ok) { _redirectToLogin(); return; }
        const u = await r.json();
        const nameEl = document.getElementById("user-name");
        const avatarEl = document.getElementById("user-avatar");
        const emailEl = document.getElementById("user-email");
        if (nameEl) nameEl.innerText = u.name;
        if (avatarEl) avatarEl.innerText = u.name.charAt(0).toUpperCase();
        if (emailEl) emailEl.innerText = u.email;
        return u;
    } catch(e) { _redirectToLogin(); }
}
function toggleAccountMenu(event){
    event.stopPropagation();
    const trigger = document.getElementById("account-trigger");
    const dropdown = document.getElementById("account-dropdown");
    const open = dropdown.classList.toggle("open");
    trigger.classList.toggle("open", open);
    trigger.setAttribute("aria-expanded", open ? "true" : "false");
}
document.addEventListener("click", e => {
    const menu = document.getElementById("account-dropdown");
    const trigger = document.getElementById("account-trigger");
    if(!menu || !trigger) return;
    if(menu.contains(e.target) || trigger.contains(e.target)) return;
    menu.classList.remove("open");
    trigger.classList.remove("open");
    trigger.setAttribute("aria-expanded", "false");
});
async function logout(){
    await fetch("/auth/logout", { method: "POST" });
    window.location.href = "/";
}
  let currentUser = null;
  function permissionSet(u = currentUser){
      const raw = u ? (u.permissions || []) : [];
      if(Array.isArray(raw)) return new Set(raw);
      if(typeof raw === "string"){
          return new Set(raw.split(",").map(v => v.trim()).filter(Boolean));
      }
      return new Set();
  }
  function hasPermission(permission, u = currentUser){
      const role = u ? (u.role || "") : "";
      const perms = permissionSet(u);
      return role === "admin" || perms.has(permission);
  }
  function configureHRPermissions(u){
      currentUser = u;
      const tabMap = [
          {id:"tab-emp", permission:"tab_hr_employees", tab:"employees"},
          {id:"tab-att", permission:"tab_hr_attendance", tab:"attendance"},
          {id:"tab-pay", permission:"tab_hr_payroll", tab:"payroll"},
      ];
      let firstAllowed = null;
      tabMap.forEach(conf => {
          let el = document.getElementById(conf.id);
          if(!el) return;
          if(!hasPermission(conf.permission, u)){
              el.style.display = "none";
          } else if(!firstAllowed) {
              firstAllowed = conf.tab;
          }
      });
      if(firstAllowed) setTimeout(() => switchTab(firstAllowed), 0);
      const clearBtn = document.getElementById("btn-clear-hr-data");
      if(clearBtn) clearBtn.style.display = hasPermission("action_hr_clear_data", u) ? "" : "none";
  }
  initializeColorMode();
  initUser().then(u => { if(u) configureHRPermissions(u); });
  let employees    = [];
let farms        = [];
let editingEmpId = null;
let editingPayId = null;
let empSearchTimer = null;
let loanDeductionEmployeeId = null;
let loanDeductionEmployeeSalary = 0;
let currentEmployeeLoans = [];

function escapeHtml(value) {
    return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}
function normalizeDashFallback(value){
    const text = String(value ?? "");
    return text === String.fromCharCode(8212) ? "" : text;
}
function displayText(value){
    const text = normalizeDashFallback(value).trim();
    return text ? escapeHtml(text) : "-";
}
function numberValue(value){
    const n = Number(value || 0);
    return Number.isFinite(n) ? n : 0;
}
function money(value){
    return numberValue(value).toFixed(2);
}
function safeStatusClass(value){
    return String(value || "").replace(/[^a-z0-9_-]/gi, "") || "unknown";
}

/* ── INIT ── */
async function init(){
    // Auto-mark all present today on page load
    await fetch("/hr/api/attendance/auto-today", {method:"POST"});
    await loadSummary();
    await loadEmployeeFarms();
    await loadEmployees();
}

async function loadSummary(){
    let d = await (await fetch("/hr/api/summary")).json();
    document.getElementById("stat-total").innerText   = d.total_employees;
    document.getElementById("stat-present").innerText = d.present_today;
    document.getElementById("stat-absent").innerText  = d.absent_today;
    document.getElementById("stat-salary").innerText  = d.total_salary.toFixed(2);
}

/* ── TABS ── */
function switchTab(tab){
    const required = {
        employees: "tab_hr_employees",
        attendance: "tab_hr_attendance",
        payroll: "tab_hr_payroll",
    };
    if(required[tab] && !hasPermission(required[tab])) return;
    ["employees","attendance","payroll"].forEach(t => {
        document.getElementById("section-"+t).style.display = t===tab?"":"none";
        document.getElementById("tab-"+t.slice(0,3)).classList.toggle("active", t===tab);
    });
    document.getElementById("btn-add-emp").style.display  = tab==="employees" ?"":"none";
    document.getElementById("btn-log-att").style.display  = tab==="attendance"?"":"none";
    if(tab==="attendance") initAttendanceTab();
    if(tab==="payroll")    initPayrollTab();
}

/* ── EMPLOYEES ── */
function onEmpSearch(){
    clearTimeout(empSearchTimer);
    empSearchTimer = setTimeout(loadEmployees, 300);
}

async function loadEmployees(){
    let q   = document.getElementById("emp-search").value.trim();
    let url = `/hr/api/employees${q?"?q="+encodeURIComponent(q):""}`;
    employees = await (await fetch(url)).json();

    if(!employees.length){
        document.getElementById("emp-body").innerHTML =
            `<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:40px">No employees found</td></tr>`;
        return;
    }

    document.getElementById("emp-body").innerHTML = employees.map(e => {
        const id = numberValue(e.id);
        const salary = numberValue(e.base_salary);
        return `
        <tr>
            <td class="name">${displayText(e.name)}</td>
            <td>${displayText(e.position)}</td>
            <td>${displayText(e.department)}</td>
            <td>${displayText(e.farm_name)}</td>
            <td style="font-family:var(--mono);font-size:12px">${displayText(e.phone)}</td>
            <td style="font-size:12px;color:var(--muted)">${displayText(e.hire_date)}</td>
            <td class="mono">${money(salary)}</td>
            <td style="display:flex;gap:6px">
                <button class="action-btn" onclick="openEditEmpFromButton(this)" data-id="${id}" data-name="${escapeHtml(normalizeDashFallback(e.name))}" data-position="${escapeHtml(normalizeDashFallback(e.position))}" data-department="${escapeHtml(normalizeDashFallback(e.department))}" data-phone="${escapeHtml(normalizeDashFallback(e.phone))}" data-salary="${salary}" data-farm-id="${e.farm_id || ""}">Edit</button>
                ${(hasPermission("action_hr_view_loans") || hasPermission("action_hr_view_deductions"))?`<button class="action-btn purple" onclick="openLoanDeductionModalFromButton(this)" data-id="${id}" data-name="${escapeHtml(normalizeDashFallback(e.name))}" data-salary="${salary}">Loans & Deductions</button>`:""}
                ${hasPermission("action_hr_run_payroll")?`<button class="action-btn danger" onclick="deactivateEmployeeFromButton(this)" data-id="${id}" data-name="${escapeHtml(normalizeDashFallback(e.name))}">Remove</button>`:""}
            </td>
        </tr>`;
    }).join("");
}

function openEditEmpFromButton(btn){
    openEditEmpModal(
        numberValue(btn.dataset.id),
        btn.dataset.name || "",
        btn.dataset.position || "",
        btn.dataset.department || "",
        btn.dataset.phone || "",
        numberValue(btn.dataset.salary),
        btn.dataset.farmId || ""
    );
}

function deactivateEmployeeFromButton(btn){
    deactivateEmployee(numberValue(btn.dataset.id), btn.dataset.name || "");
}

function openAddEmpModal(){
    editingEmpId = null;
    document.getElementById("emp-modal-title").innerText = "Add Employee";
    ["e-name","e-position","e-department","e-phone","e-salary"].forEach(id=>document.getElementById(id).value="");
    document.getElementById("e-hire").value = "";
    fillEmployeeFarmSelect("");
    document.getElementById("emp-modal").classList.add("open");
}

function openEditEmpModal(id,name,position,department,phone,salary,farmId){
    editingEmpId = id;
    document.getElementById("emp-modal-title").innerText = "Edit Employee";
    document.getElementById("e-name").value       = name;
    document.getElementById("e-position").value   = normalizeDashFallback(position);
    document.getElementById("e-department").value = normalizeDashFallback(department);
    document.getElementById("e-phone").value      = normalizeDashFallback(phone);
    document.getElementById("e-salary").value     = salary;
    fillEmployeeFarmSelect(farmId || "");
    document.getElementById("emp-modal").classList.add("open");
}

function closeEmpModal(){ document.getElementById("emp-modal").classList.remove("open"); }

function formatApiDetail(detail){
    if(Array.isArray(detail)){
        return detail.map(item => item && item.msg ? item.msg : String(item)).join("; ");
    }
    if(detail && typeof detail === "object") return JSON.stringify(detail);
    return detail ? String(detail) : "";
}

function compactApiText(text){
    const trimmed = (text || "").trim();
    if(!trimmed || trimmed.startsWith("<")) return "";
    return trimmed.length > 180 ? trimmed.slice(0, 180) + "..." : trimmed;
}

async function readApiResponse(res){
    const contentType = res.headers.get("content-type") || "";
    let data = null;
    let raw = "";
    if(contentType.includes("application/json")){
        try{ data = await res.json(); }
        catch(err){ raw = "Response was not valid JSON"; }
    }else{
        raw = await res.text();
    }
    if(!res.ok){
        const detail = formatApiDetail(data && data.detail) || compactApiText(raw) || res.statusText || "Request failed";
        throw new Error(`${detail} (${res.status})`);
    }
    if(data && data.detail) throw new Error(formatApiDetail(data.detail));
    return data || {};
}

async function saveEmployee(){
    let name = document.getElementById("e-name").value.trim();
    if(!name){ showToast("Name is required"); return; }
    let body = {
        name,
        position:    document.getElementById("e-position").value.trim()||null,
        department:  document.getElementById("e-department").value.trim()||null,
        phone:       document.getElementById("e-phone").value.trim()||null,
        hire_date:   document.getElementById("e-hire").value||null,
        base_salary: parseFloat(document.getElementById("e-salary").value)||0,
        farm_id:     parseInt(document.getElementById("e-farm").value)||null,
    };
    let url    = editingEmpId ? `/hr/api/employees/${editingEmpId}` : "/hr/api/employees";
    let method = editingEmpId ? "PUT" : "POST";
    try{
        let res = await fetch(url,{method,headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
        await readApiResponse(res);
        closeEmpModal();
        showToast(editingEmpId?"Employee updated":"Employee added");
        loadEmployees(); loadSummary();
    }catch(err){
        showToast("Error: " + (err.message || "Could not save employee"));
    }
}

async function loadEmployeeFarms(){
    const errorBox = document.getElementById("farm-load-error");
    try{
        let res = await fetch("/farm/api/farms");
        if(!res.ok) throw new Error(`Farm API returned ${res.status}`);
        let data = await res.json();
        if(!Array.isArray(data)) throw new Error("Farm API returned an unexpected response");
        farms = data;
        if(errorBox){ errorBox.style.display = "none"; errorBox.innerText = ""; }
        fillEmployeeFarmSelect("");
    }catch(err){
        farms = [];
        if(errorBox){
            errorBox.innerText = "Farm list could not be loaded. Employees can still be viewed, but farm selection is unavailable.";
            errorBox.style.display = "";
        }
        fillEmployeeFarmSelect("");
    }
}

function fillEmployeeFarmSelect(selectedFarmId){
    const sel = document.getElementById("e-farm");
    if(!sel) return;
    const selected = String(selectedFarmId || "");
    sel.innerHTML = `<option value="">No farm selected</option>` +
        farms.map(f=>`<option value="${numberValue(f.id)}">${displayText(f.name || ("Farm #" + f.id))}</option>`).join("");
    sel.value = selected;
}

async function deactivateEmployee(id,name){
    if(!confirm(`Remove "${name}" from active employees?`)) return;
    try{
        const res = await fetch(`/hr/api/employees/${id}`,{method:"DELETE"});
        await readApiResponse(res);
        showToast("Employee removed from active employees");
        loadEmployees(); loadSummary();
    }catch(err){
        showToast("Error: " + (err.message || "Could not remove employee"));
    }
}

/* ── LOANS & DEDUCTIONS ── */
function openLoanDeductionModalFromButton(btn){
    openLoanDeductionModal(
        numberValue(btn.dataset.id),
        btn.dataset.name || "",
        numberValue(btn.dataset.salary)
    );
}

function setDefaultLoanDeductionDates(){
    const today = new Date().toISOString().split("T")[0];
    const period = today.slice(0, 7);
    ["loan-date","repay-date","deduct-date"].forEach(id => {
        const el = document.getElementById(id);
        if(el && !el.value) el.value = today;
    });
    ["deduct-period","manual-deduct-period"].forEach(id => {
        const el = document.getElementById(id);
        if(el && !el.value) el.value = period;
    });
    const working = document.getElementById("deduct-working-days");
    if(working && !working.value) working.value = "30";
}

async function openLoanDeductionModal(employeeId, name, salary){
    loanDeductionEmployeeId = employeeId;
    loanDeductionEmployeeSalary = salary;
    document.getElementById("loan-deduction-emp").innerText = `${name} - Base salary ${money(salary)} EGP`;
    setDefaultLoanDeductionDates();
    updateDayDeductionPreview();
    document.getElementById("loan-section").style.display = hasPermission("action_hr_view_loans") ? "" : "none";
    document.getElementById("loan-create-form").style.display = hasPermission("action_hr_manage_loans") ? "" : "none";
    document.getElementById("loan-repayment-form").style.display = hasPermission("action_hr_manage_loans") ? "" : "none";
    document.getElementById("deduction-section").style.display = hasPermission("action_hr_view_deductions") ? "" : "none";
    document.getElementById("day-deduction-form").style.display = hasPermission("action_hr_manage_deductions") ? "" : "none";
    document.getElementById("manual-deduction-form").style.display = hasPermission("action_hr_manage_deductions") ? "" : "none";
    document.getElementById("loan-deduction-modal").classList.add("open");
    await refreshLoanDeductionModal();
}

function closeLoanDeductionModal(){
    document.getElementById("loan-deduction-modal").classList.remove("open");
}

async function refreshLoanDeductionModal(){
    if(!loanDeductionEmployeeId) return;
    if(hasPermission("action_hr_view_loans")) await loadEmployeeLoans();
    if(hasPermission("action_hr_view_deductions")) await loadEmployeeDeductions();
}

async function loadEmployeeLoans(){
    const body = document.getElementById("loan-history-body");
    const select = document.getElementById("repay-loan-id");
    try{
        const res = await fetch(`/hr/api/employees/${loanDeductionEmployeeId}/loans`);
        currentEmployeeLoans = await readApiResponse(res);
        const openLoans = currentEmployeeLoans.filter(loan => loan.status === "open" && numberValue(loan.balance) > 0);
        select.innerHTML = openLoans.map(loan =>
            `<option value="${numberValue(loan.id)}">#${numberValue(loan.id)} - ${money(loan.balance)} EGP</option>`
        ).join("") || `<option value="">No open loans</option>`;
        body.innerHTML = currentEmployeeLoans.map(loan => `
            <tr>
                <td>${displayText(loan.loan_date)}</td>
                <td class="mono">${money(loan.amount)}</td>
                <td class="mono">${money(loan.repaid_amount)}</td>
                <td class="mono">${money(loan.balance)}</td>
                <td>${displayText(loan.status)}</td>
                <td>
                    ${loan.status === "open" && hasPermission("action_hr_manage_loans") ? `<button class="action-btn" onclick="selectLoanForRepayment(${numberValue(loan.id)})">Repay</button> <button class="action-btn danger" onclick="cancelLoan(${numberValue(loan.id)})">Cancel</button>` : ""}
                </td>
            </tr>`).join("") || `<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:18px">No loans recorded</td></tr>`;
    }catch(err){
        body.innerHTML = `<tr><td colspan="6" style="color:var(--danger);padding:18px">Could not load loans</td></tr>`;
    }
}

async function loadEmployeeDeductions(){
    const body = document.getElementById("deduction-history-body");
    try{
        const res = await fetch(`/hr/api/employees/${loanDeductionEmployeeId}/deductions`);
        const deductions = await readApiResponse(res);
        body.innerHTML = deductions.map(d => `
            <tr>
                <td>${displayText(d.period || d.payroll_period)}</td>
                <td>${displayText(String(d.type || "").replace(/_/g, " "))}</td>
                <td>${d.days === null || d.days === undefined ? "-" : numberValue(d.days)}</td>
                <td class="mono">${d.daily_rate === null || d.daily_rate === undefined ? "-" : money(d.daily_rate)}</td>
                <td class="mono">${money(d.amount)}</td>
                <td>${d.payroll_id ? "#" + numberValue(d.payroll_id) : "Pending"}</td>
                <td>${displayText(d.note)}</td>
            </tr>`).join("") || `<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:18px">No deductions recorded</td></tr>`;
    }catch(err){
        body.innerHTML = `<tr><td colspan="7" style="color:var(--danger);padding:18px">Could not load deductions</td></tr>`;
    }
}

async function saveEmployeeLoan(){
    if(!hasPermission("action_hr_manage_loans")) return;
    const body = {
        loan_date: document.getElementById("loan-date").value,
        amount: parseFloat(document.getElementById("loan-amount").value || "0"),
        description: document.getElementById("loan-description").value.trim() || null,
    };
    try{
        const res = await fetch(`/hr/api/employees/${loanDeductionEmployeeId}/loans`, {
            method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body),
        });
        await readApiResponse(res);
        document.getElementById("loan-amount").value = "";
        document.getElementById("loan-description").value = "";
        showToast("Loan saved");
        await loadEmployeeLoans();
    }catch(err){ showToast("Error: " + (err.message || "Could not save loan")); }
}

function selectLoanForRepayment(loanId){
    document.getElementById("repay-loan-id").value = String(loanId);
    document.getElementById("repay-amount").focus();
}

async function saveLoanRepayment(){
    if(!hasPermission("action_hr_manage_loans")) return;
    const loanId = document.getElementById("repay-loan-id").value;
    if(!loanId){ showToast("Select an open loan"); return; }
    const body = {
        repayment_date: document.getElementById("repay-date").value,
        amount: parseFloat(document.getElementById("repay-amount").value || "0"),
        note: document.getElementById("repay-note").value.trim() || null,
    };
    try{
        const res = await fetch(`/hr/api/loans/${loanId}/repayments`, {
            method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body),
        });
        await readApiResponse(res);
        document.getElementById("repay-amount").value = "";
        document.getElementById("repay-note").value = "";
        showToast("Repayment saved");
        await loadEmployeeLoans();
    }catch(err){ showToast("Error: " + (err.message || "Could not save repayment")); }
}

async function cancelLoan(loanId){
    if(!hasPermission("action_hr_manage_loans")) return;
    if(!confirm("Cancel this loan? It will stay in history.")) return;
    try{
        const res = await fetch(`/hr/api/loans/${loanId}/cancel`, {method:"POST"});
        await readApiResponse(res);
        showToast("Loan cancelled");
        await loadEmployeeLoans();
    }catch(err){ showToast("Error: " + (err.message || "Could not cancel loan")); }
}

function updateDayDeductionPreview(){
    const days = numberValue(document.getElementById("deduct-days")?.value);
    const workingDays = numberValue(document.getElementById("deduct-working-days")?.value);
    const amount = workingDays > 0 ? (loanDeductionEmployeeSalary / workingDays) * days : 0;
    const preview = document.getElementById("deduct-preview");
    if(preview) preview.innerText = money(amount);
}

async function saveDayDeduction(){
    if(!hasPermission("action_hr_manage_deductions")) return;
    const body = {
        period: document.getElementById("deduct-period").value,
        deduction_date: document.getElementById("deduct-date").value,
        days: parseFloat(document.getElementById("deduct-days").value || "0"),
        working_days: parseFloat(document.getElementById("deduct-working-days").value || "0"),
        note: document.getElementById("deduct-note").value.trim() || null,
    };
    try{
        const res = await fetch(`/hr/api/employees/${loanDeductionEmployeeId}/deductions/day`, {
            method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body),
        });
        await readApiResponse(res);
        document.getElementById("deduct-days").value = "";
        document.getElementById("deduct-note").value = "";
        updateDayDeductionPreview();
        showToast("Day deduction saved");
        await loadEmployeeDeductions();
    }catch(err){ showToast("Error: " + (err.message || "Could not save deduction")); }
}

async function saveManualDeduction(){
    if(!hasPermission("action_hr_manage_deductions")) return;
    const body = {
        period: document.getElementById("manual-deduct-period").value,
        amount: parseFloat(document.getElementById("manual-deduct-amount").value || "0"),
        note: document.getElementById("manual-deduct-note").value.trim() || null,
    };
    try{
        const res = await fetch(`/hr/api/employees/${loanDeductionEmployeeId}/deductions/manual`, {
            method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body),
        });
        await readApiResponse(res);
        document.getElementById("manual-deduct-amount").value = "";
        document.getElementById("manual-deduct-note").value = "";
        showToast("Manual deduction saved");
        await loadEmployeeDeductions();
    }catch(err){ showToast("Error: " + (err.message || "Could not save deduction")); }
}

/* ── ATTENDANCE ── */
function initAttendanceTab(){
    // Set default period to current month
    let now = new Date();
    let m   = String(now.getMonth()+1).padStart(2,"0");
    document.getElementById("att-period").value = `${now.getFullYear()}-${m}`;

    // Fill employee filter
    let sel = document.getElementById("att-emp-filter");
    sel.innerHTML = `<option value="">All Employees</option>` +
        employees.map(e=>`<option value="${numberValue(e.id)}">${displayText(e.name)}</option>`).join("");

    // Fill attendance log employee select
    let aEmp = document.getElementById("a-emp");
    aEmp.innerHTML = employees.map(e=>`<option value="${numberValue(e.id)}">${displayText(e.name)}</option>`).join("");

    // Set today as default date
    document.getElementById("a-date").value = new Date().toISOString().split("T")[0];

    loadTodayAttendance();
    loadAttendance();
}

async function loadAttendance(){
    let period = document.getElementById("att-period").value;
    let empId  = document.getElementById("att-emp-filter").value;
    let url    = `/hr/api/attendance?period=${period}`;
    if(empId) url += `&emp_id=${empId}`;
    let records = await (await fetch(url)).json();

    if(!records.length){
        document.getElementById("att-body").innerHTML =
            `<tr><td colspan="4" style="text-align:center;color:var(--muted);padding:40px">No attendance records for this period</td></tr>`;
        return;
    }

    document.getElementById("att-body").innerHTML = records.map(r => {
        let status = String(r.status || "");
        let cls = `status-${safeStatusClass(status)}`;
        let labels = {present:"Present",absent:"Absent",late:"Late",leave:"Leave"};
        return `
        <tr>
            <td class="name">${displayText(r.employee)}</td>
            <td style="font-family:var(--mono);font-size:12px">${displayText(r.date)}</td>
            <td><span class="${cls}">${displayText(labels[status] || status)}</span></td>
            <td style="color:var(--muted);font-size:12px">${escapeHtml(r.note || "-")}</td>
        </tr>`;
    }).join("");
}

function openLogAttModal(){
    document.getElementById("att-modal").classList.add("open");
}
function closeAttModal(){ document.getElementById("att-modal").classList.remove("open"); }

async function saveAttendance(){
    let emp_id = document.getElementById("a-emp").value;
    let dt     = document.getElementById("a-date").value;
    if(!emp_id||!dt){ showToast("Select employee and date"); return; }
    let body = {
        employee_id: parseInt(emp_id),
        date:        dt,
        status:      document.getElementById("a-status").value,
        note:        document.getElementById("a-note").value.trim()||null,
    };
    let res  = await fetch("/hr/api/attendance",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    closeAttModal();
    showToast(data.updated?"Attendance updated":"Attendance logged");
    if(dt === new Date().toISOString().split("T")[0] && ["present","absent"].includes(body.status)){
        await loadEmployees();
    }
    loadTodayAttendance(); loadAttendance(); loadSummary();
}

/* ── ATTENDANCE TODAY CARD ── */
async function loadTodayAttendance(){
    let today = new Date().toISOString().split("T")[0];
    let records = await (await fetch(`/hr/api/attendance?period=${today.slice(0,7)}`)).json();
    let todayRecs = records.filter(r => r.date === today);
    let grid = document.getElementById("today-attendance-grid");
    if(!grid) return;
    if(!employees.length){ grid.innerHTML = `<div style="color:var(--muted);font-size:13px">No employees found</div>`; return; }
    const todayLabels = {present:"Present",absent:"Absent",late:"Late",leave:"Leave"};
    grid.innerHTML = employees.map(emp => {
        let rec    = todayRecs.find(r => r.employee_id === emp.id);
        let autoStatus = emp.attendance_auto_status || "present";
        let status = rec ? rec.status : autoStatus;
        let isAbs  = status === "absent";
        let isAutoAbsent = autoStatus === "absent";
        let statusText = isAutoAbsent ? "Absent until marked present" : (todayLabels[status] || status || "Present");
        let statusColor = isAbs || isAutoAbsent ? "var(--danger)" : "var(--green)";
        return `<div style="display:flex;align-items:center;justify-content:space-between;padding:8px 12px;background:var(--card2);border:1px solid ${isAbs?"rgba(255,77,109,.2)":"rgba(0,255,157,.1)"};border-radius:9px;">
            <div>
                <span style="font-weight:600;font-size:13px;color:var(--text)">${displayText(emp.name)}</span>
                <span style="font-size:11px;color:var(--muted);margin-left:8px">${escapeHtml(normalizeDashFallback(emp.position))}</span>
            </div>
            <div style="display:flex;align-items:center;gap:10px">
                <span style="font-size:12px;font-weight:700;color:${statusColor}">
                    ${escapeHtml(statusText)}
                </span>
                ${hasPermission("action_hr_run_payroll") ? ((isAbs || isAutoAbsent)
                    ? `<button class="action-btn green" onclick="markPresentToday(${emp.id})">Mark Present</button>`
                    : `<button class="action-btn danger" onclick="markAbsentToday(${emp.id})">Mark Absent</button>`
                ) : ""}
            </div>
        </div>`;
    }).join("");
}

async function markAbsentToday(empId){
    let res = await fetch("/hr/api/attendance/mark-absent",{
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({employee_id: empId, date: new Date().toISOString().split("T")[0], status:"absent"}),
    });
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    showToast("Marked absent until marked present");
    await loadEmployees();
    loadTodayAttendance(); loadAttendance(); loadSummary();
}

async function markPresentToday(empId){
    let res = await fetch("/hr/api/attendance/mark-present",{
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({employee_id: empId, date: new Date().toISOString().split("T")[0], status:"present"}),
    });
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    showToast("Marked present");
    await loadEmployees();
    loadTodayAttendance(); loadAttendance(); loadSummary();
}

/* ── PAYROLL ── */
function initPayrollTab(){
    let now = new Date();
    let m   = String(now.getMonth()+1).padStart(2,"0");
    document.getElementById("pay-period").value = `${now.getFullYear()}-${m}`;
    loadPayrollPreview();
}

async function loadPayrollPreview(){
    let period = document.getElementById("pay-period").value;
    if(!period) return;

    document.getElementById("payroll-preview-wrap").style.display  = "";
    document.getElementById("payroll-records-wrap").style.display  = "none";
    document.getElementById("preview-body").innerHTML =
        `<tr><td colspan="9" style="text-align:center;color:var(--muted);padding:20px">Loading preview...</td></tr>`;

    let d    = await (await fetch(`/hr/api/payroll/preview?period=${period}`)).json();
    let [yr, mo] = period.split("-");
    let monthName = new Date(parseInt(yr), parseInt(mo)-1, 1).toLocaleDateString("en-GB",{month:"long",year:"numeric"});

    document.getElementById("preview-meta").innerHTML =
        `<b>${escapeHtml(monthName)}</b> &nbsp;-&nbsp; ${numberValue(d.days_elapsed)} of ${numberValue(d.working_days)} working days elapsed`;
    document.getElementById("preview-total").innerText = money(d.total_to_pay) + " EGP";

    document.getElementById("preview-body").innerHTML = d.employees.map(e => `
        <tr>
            <td class="name">${displayText(e.employee)}<br><span style="font-size:11px;color:var(--muted)">${displayText(e.position)} - ${numberValue(e.days_present)} present / ${numberValue(e.working_days)} working</span></td>
            <td style="font-family:var(--mono)">${money(e.base_salary)}</td>
            <td><input class="pay-bonus-input" data-emp-id="${numberValue(e.employee_id)}" type="number" min="0" step="0.01" value="0" style="width:86px;background:var(--card2);border:1px solid var(--border2);border-radius:8px;color:var(--text);padding:6px" oninput="updatePayrollPreviewNet(this)"></td>
            <td style="font-family:var(--mono);color:var(--danger)" data-day-ded="${numberValue(e.pending_day_deductions)}">${numberValue(e.pending_day_deduction_days)}d / ${money(e.pending_day_deductions)}</td>
            <td style="font-family:var(--mono);color:var(--danger)" data-manual-ded="${numberValue(e.pending_manual_deductions)}">${money(e.pending_manual_deductions)}</td>
            <td style="font-family:var(--mono);color:var(--warn)">${e.outstanding_loan_balance === null || e.outstanding_loan_balance === undefined ? "-" : money(e.outstanding_loan_balance)}</td>
            <td><input class="pay-loan-input" data-emp-id="${numberValue(e.employee_id)}" data-max="${numberValue(e.outstanding_loan_balance)}" type="number" min="0" step="0.01" value="0" style="width:96px;background:var(--card2);border:1px solid var(--border2);border-radius:8px;color:var(--text);padding:6px" oninput="updatePayrollPreviewNet(this)"></td>
            <td class="mono pay-net-preview" data-base="${numberValue(e.base_salary)}">${money(e.net_before_loan)}</td>
            <td><span style="font-size:11px;color:${e.already_run?"var(--warn)":"var(--muted)"}">${e.already_run?"Will update":"New"}</span></td>
        </tr>`).join("") +
        `<tr style="background:var(--card2)">
            <td colspan="7" style="font-weight:700;color:var(--sub)">Total to Pay</td>
            <td style="font-family:var(--mono);font-size:16px;font-weight:700;color:var(--green)">${money(d.total_to_pay)}</td>
            <td></td>
        </tr>`;
}

function updatePayrollPreviewNet(input){
    const row = input.closest("tr");
    if(!row) return;
    const base = numberValue(row.querySelector(".pay-net-preview")?.dataset.base);
    const bonus = numberValue(row.querySelector(".pay-bonus-input")?.value);
    const loan = numberValue(row.querySelector(".pay-loan-input")?.value);
    const maxLoan = numberValue(row.querySelector(".pay-loan-input")?.dataset.max);
    if(maxLoan > 0 && loan > maxLoan){
        row.querySelector(".pay-loan-input").value = maxLoan.toFixed(2);
    }
    const day = numberValue(row.querySelector("[data-day-ded]")?.dataset.dayDed);
    const manual = numberValue(row.querySelector("[data-manual-ded]")?.dataset.manualDed);
    const safeLoan = Math.min(numberValue(row.querySelector(".pay-loan-input")?.value), maxLoan || 0);
    row.querySelector(".pay-net-preview").innerText = money(base + bonus - day - manual - safeLoan);
}

async function confirmRunPayroll(){
    let period = document.getElementById("pay-period").value;
    if(!period){ showToast("Select a period first"); return; }
    const bonuses = {};
    document.querySelectorAll(".pay-bonus-input").forEach(input => {
        const value = numberValue(input.value);
        if(value > 0) bonuses[numberValue(input.dataset.empId)] = value;
    });
    const loan_repayments = {};
    document.querySelectorAll(".pay-loan-input").forEach(input => {
        const value = numberValue(input.value);
        if(value > 0) loan_repayments[numberValue(input.dataset.empId)] = value;
    });
    let res  = await fetch("/hr/api/payroll/run",{
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({period, bonuses, loan_repayments}),
    });
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    showToast(`Payroll saved - ${data.created} created, ${data.skipped} updated`);
    loadPayrollRecords();
}

function closeRunPayModal(){
    document.getElementById("pay-run-modal").classList.remove("open");
}

async function runPayroll(){
    const modalPeriod = document.getElementById("pr-period").value;
    if(modalPeriod) document.getElementById("pay-period").value = modalPeriod;
    closeRunPayModal();
    await confirmRunPayroll();
}

async function loadPayrollRecords(){
    let period = document.getElementById("pay-period").value;
    let records = await (await fetch(`/hr/api/payroll${period?"?period="+period:""}`)).json();
    document.getElementById("payroll-preview-wrap").style.display = "none";
    document.getElementById("payroll-records-wrap").style.display = "";

    if(!records.length){
        document.getElementById("pay-body").innerHTML =
            `<tr><td colspan="12" style="text-align:center;color:var(--muted);padding:40px">No payroll records. Use preview above to generate.</td></tr>`;
        return;
    }
    let totalNet = records.reduce((s,r)=>s+numberValue(r.net_salary),0);
    document.getElementById("pay-body").innerHTML = records.map(r=>`
        <tr>
            <td class="name">${displayText(r.employee)}</td>
            <td style="font-family:var(--mono);font-size:12px;color:var(--muted)">${displayText(r.period)}</td>
            <td style="font-family:var(--mono)">${money(r.base_salary)}</td>
            <td style="font-family:var(--mono);color:var(--sub)">${r.days_worked ? numberValue(r.days_worked) : "-"} / ${r.working_days ? numberValue(r.working_days) : "-"}</td>
            <td style="font-family:var(--mono);color:var(--green)">+${money(r.bonuses)}</td>
            <td style="font-family:var(--mono);color:var(--danger)">-${money(r.loan_deductions)}</td>
            <td style="font-family:var(--mono);color:var(--danger)">${numberValue(r.day_deduction_days)}d / -${money(r.day_deductions)}</td>
            <td style="font-family:var(--mono);color:var(--danger)">-${money(r.manual_deductions)}</td>
            <td style="font-family:var(--mono);color:var(--danger)">-${money(r.deductions)}</td>
            <td style="font-family:var(--mono);font-size:15px;font-weight:700;color:var(--green)">${money(r.net_salary)}</td>
            <td>${r.paid?`<span class="paid-badge">Paid</span>`:`<span class="unpaid-badge">Pending</span>`}</td>
            <td style="display:flex;gap:6px">
                <button class="action-btn purple" onclick="openEditPayFromButton(this)" data-id="${numberValue(r.id)}" data-employee="${escapeHtml(normalizeDashFallback(r.employee))}" data-bonuses="${numberValue(r.bonuses)}" data-deductions="${numberValue(r.manual_deductions)}">Edit</button>
                ${!r.paid && hasPermission("action_hr_mark_paid")?`<button class="action-btn green" onclick="markPaid(${numberValue(r.id)})">Mark Paid</button>`:""}
            </td>
        </tr>`).join("") +
        `<tr style="background:var(--card2)">
            <td colspan="9" style="font-weight:700;color:var(--sub)">Total</td>
            <td style="font-family:var(--mono);font-size:16px;font-weight:700;color:var(--green)">${money(totalNet)}</td>
            <td colspan="2"></td>
        </tr>`;
}

function openEditPayFromButton(btn){
    openEditPayModal(
        numberValue(btn.dataset.id),
        btn.dataset.employee || "",
        numberValue(btn.dataset.bonuses),
        numberValue(btn.dataset.deductions)
    );
}

function openEditPayModal(id,empName,bonuses,deductions){
    editingPayId = id;
    document.getElementById("edit-pay-emp").innerText  = empName;
    document.getElementById("ep-bonuses").value        = bonuses;
    document.getElementById("ep-deductions").value     = deductions;
    document.getElementById("ep-notes").value          = "";
    document.getElementById("edit-pay-modal").classList.add("open");
}
function closeEditPayModal(){ document.getElementById("edit-pay-modal").classList.remove("open"); }

async function savePayrollEdit(){
    let body = {
        bonuses:    parseFloat(document.getElementById("ep-bonuses").value)||0,
        deductions: parseFloat(document.getElementById("ep-deductions").value)||0,
        notes:      document.getElementById("ep-notes").value.trim()||null,
    };
    let res  = await fetch(`/hr/api/payroll/${editingPayId}`,{
        method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body),
    });
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    closeEditPayModal();
    showToast(`Payroll updated. Net: ${money(data.net_salary)}`);
    loadPayrollRecords();
}

async function markPaid(id){
    if(!confirm("Mark this payroll as paid?")) return;
    let res = await fetch(`/hr/api/payroll/${id}/pay`,{method:"PATCH"});
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    showToast(data.warning || `Marked as paid - expense ${data.expense_ref_number || ""}`);
    loadPayrollRecords();
}

/* ── CLEAR HR DATA ── */
function openClearHRDataModal(){
    if(!hasPermission("action_hr_clear_data")){
        showToast("Permission denied: action_hr_clear_data");
        return;
    }
    const input = document.getElementById("clear-hr-confirmation");
    if(input) input.value = "";
    updateClearHRDataConfirmState();
    document.getElementById("clear-hr-modal").classList.add("open");
    if(input) input.focus();
}

function closeClearHRDataModal(){
    document.getElementById("clear-hr-modal").classList.remove("open");
    const input = document.getElementById("clear-hr-confirmation");
    if(input) input.value = "";
    updateClearHRDataConfirmState();
}

function updateClearHRDataConfirmState(){
    const input = document.getElementById("clear-hr-confirmation");
    const btn = document.getElementById("btn-confirm-clear-hr");
    if(!btn || !input) return;
    btn.disabled = input.value !== "CLEAR HR DATA";
}

function clearHRForms(){
    editingEmpId = null;
    editingPayId = null;
    ["e-name","e-position","e-department","e-phone","e-salary","e-hire","a-note","ep-bonuses","ep-deductions","ep-notes"].forEach(id => {
        const el = document.getElementById(id);
        if(el) el.value = "";
    });
    fillEmployeeFarmSelect("");
    const today = new Date().toISOString().split("T")[0];
    const aDate = document.getElementById("a-date");
    if(aDate) aDate.value = today;
}

function activeHRTab(){
    if(document.getElementById("tab-att").classList.contains("active")) return "attendance";
    if(document.getElementById("tab-pay").classList.contains("active")) return "payroll";
    return "employees";
}

async function refreshHRDataAfterClear(){
    clearHRForms();
    employees = [];
    await loadSummary();
    await loadEmployeeFarms();
    await loadEmployees();
    const active = activeHRTab();
    if(active === "attendance" && hasPermission("tab_hr_attendance")) initAttendanceTab();
    if(active === "payroll" && hasPermission("tab_hr_payroll")) initPayrollTab();
}

async function confirmClearHRData(){
    if(!hasPermission("action_hr_clear_data")){
        showToast("Permission denied: action_hr_clear_data");
        return;
    }
    const input = document.getElementById("clear-hr-confirmation");
    const btn = document.getElementById("btn-confirm-clear-hr");
    if(!input || input.value !== "CLEAR HR DATA"){
        showToast("Type CLEAR HR DATA to confirm");
        updateClearHRDataConfirmState();
        return;
    }
    btn.disabled = true;
    try{
        const res = await fetch("/hr/clear-data", {
            method: "POST",
            headers: {"Content-Type":"application/json"},
            body: JSON.stringify({confirmation: input.value}),
        });
        const data = await readApiResponse(res);
        closeClearHRDataModal();
        await refreshHRDataAfterClear();
        const d = data.deleted || {};
        showToast(`HR data cleared - ${numberValue(d.employees)} employees, ${numberValue(d.attendance)} attendance, ${numberValue(d.payroll)} payroll, ${numberValue(d.hr_expenses)} payroll expenses`);
    }catch(err){
        showToast("Error: " + (err.message || "Could not clear HR data"));
        updateClearHRDataConfirmState();
    }
}

/* ── MODAL CLOSE ON BG ── */
["emp-modal","att-modal","pay-run-modal","edit-pay-modal","loan-deduction-modal","clear-hr-modal"].forEach(id=>{
    document.getElementById(id).addEventListener("click",function(e){
        if(e.target!==this) return;
        if(id === "clear-hr-modal") closeClearHRDataModal();
        else this.classList.remove("open");
    });
});

let toastTimer=null;
function showToast(msg){
    let t=document.getElementById("toast");
    t.innerText=msg; t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer=setTimeout(()=>t.classList.remove("show"),3500);
}

init();
</script>
</body>
</html>
"""
    return HTMLResponse(content=html_content, media_type="text/html; charset=utf-8")
