from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import func, select
from typing import Optional, List
from pydantic import BaseModel
from datetime import date

from app.database import get_async_session
from app.core.permissions import get_current_user, require_permission
from app.core.log import record
from app.core.navigation import render_app_header
from app.models.hr import Employee, Attendance, Payroll
from app.models.user import User

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

class EmployeeUpdate(BaseModel):
    name:        Optional[str]   = None
    phone:       Optional[str]   = None
    position:    Optional[str]   = None
    department:  Optional[str]   = None
    base_salary: Optional[float] = None
    is_active:   Optional[bool]  = None

class AttendanceCreate(BaseModel):
    employee_id: int
    date:        str
    status:      str = "present"
    note:        Optional[str] = None

class PayrollRun(BaseModel):
    period:  str  # "2025-01"
    emp_ids: Optional[List[int]] = None  # None = all employees

class PayrollUpdate(BaseModel):
    bonuses:    float = 0
    deductions: float = 0
    notes:      Optional[str] = None


# ── EMPLOYEE API ───────────────────────────────────────
@router.get("/api/employees")
async def get_employees(q: str = "", db: AsyncSession = Depends(get_async_session)):
    stmt = select(Employee).where(Employee.is_active == True)
    if q:
        stmt = stmt.where(
            Employee.name.ilike(f"%{q}%") |
            Employee.position.ilike(f"%{q}%") |
            Employee.department.ilike(f"%{q}%")
        )
    stmt = stmt.order_by(Employee.name)
    _r = await db.execute(stmt)
    emps = _r.scalars().all()
    return [
        {
            "id":          e.id,
            "name":        e.name,
            "phone":       e.phone or "—",
            "position":    e.position or "—",
            "department":  e.department or "—",
            "hire_date":   str(e.hire_date) if e.hire_date else "—",
            "base_salary": float(e.base_salary),
            "is_active":   e.is_active,
        }
        for e in emps
    ]

@router.post("/api/employees")
async def add_employee(data: EmployeeCreate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    hire = date.fromisoformat(data.hire_date) if data.hire_date else None
    e = Employee(
        name=data.name, phone=data.phone,
        position=data.position, department=data.department,
        hire_date=hire, base_salary=data.base_salary,
    )
    db.add(e); await db.flush()
    record(db, "HR", "add_employee",
           f"Added employee: {e.name} — {e.position or ''} / {e.department or ''} — salary: {float(e.base_salary):.2f}",
           ref_type="employee", ref_id=e.id)
    await db.commit(); await db.refresh(e)
    return {"id": e.id, "name": e.name}

@router.put("/api/employees/{emp_id}")
async def edit_employee(emp_id: int, data: EmployeeUpdate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    _r = await db.execute(select(Employee).where(Employee.id == emp_id))
    e = _r.scalar_one_or_none()
    if not e:
        raise HTTPException(status_code=404, detail="Employee not found")
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(e, k, v)
    record(db, "HR", "edit_employee",
           f"Edited employee: {e.name}",
           ref_type="employee", ref_id=emp_id)
    await db.commit()
    return {"ok": True}

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
    # Check if already logged for that day
    _r = await db.execute(select(Attendance).where(
        Attendance.employee_id == data.employee_id,
        Attendance.date == date.fromisoformat(data.date),
    ))
    existing = _r.scalar_one_or_none()
    if existing:
        existing.status = data.status
        existing.note   = data.note
        await db.commit()
        return {"id": existing.id, "updated": True}

    a = Attendance(
        employee_id=data.employee_id,
        date=date.fromisoformat(data.date),
        status=data.status,
        note=data.note,
    )
    db.add(a); await db.commit(); await db.refresh(a)
    return {"id": a.id, "updated": False}


@router.post("/api/attendance/auto-today")
async def auto_mark_today(db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    """Auto-mark all active employees as present today if not already logged."""
    today = date.today()
    _r = await db.execute(select(Employee).where(Employee.is_active == True))
    employees = _r.scalars().all()
    created = 0
    for emp in employees:
        _ex = await db.execute(select(Attendance).where(
            Attendance.employee_id == emp.id,
            Attendance.date == today,
        ))
        exists = _ex.scalar_one_or_none()
        if not exists:
            db.add(Attendance(employee_id=emp.id, date=today, status="present"))
            created += 1
    await db.commit()
    return {"ok": True, "created": created, "date": str(today)}

@router.post("/api/attendance/mark-absent")
async def mark_absent_today(data: AttendanceCreate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    """Mark a specific employee as absent today (overrides auto-present)."""
    today = date.today()
    _r = await db.execute(select(Attendance).where(
        Attendance.employee_id == data.employee_id,
        Attendance.date == today,
    ))
    existing = _r.scalar_one_or_none()
    if existing:
        existing.status = "absent"
        existing.note   = data.note
        await db.commit()
        return {"id": existing.id, "updated": True}
    db.add(Attendance(
        employee_id=data.employee_id,
        date=today, status="absent", note=data.note,
    ))
    await db.commit()
    return {"ok": True}


# ── PAYROLL API ────────────────────────────────────────
@router.get("/api/payroll")
async def get_payroll(period: str = None, db: AsyncSession = Depends(get_async_session)):
    stmt = select(Payroll).options(selectinload(Payroll.employee))
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
            "period":      r.period,
            "base_salary": float(r.base_salary) if r.base_salary else 0,
            "days_worked": r.days_worked or 0,
            "working_days":r.working_days or 0,
            "bonuses":     float(r.bonuses)     if r.bonuses     else 0,
            "deductions":  float(r.deductions)  if r.deductions  else 0,
            "net_salary":  float(r.net_salary)  if r.net_salary  else 0,
            "paid":        r.paid,
            "paid_at":     str(r.paid_at) if r.paid_at else None,
        }
        for r in records
    ]

@router.get("/api/payroll/preview")
async def preview_payroll(period: str, db: AsyncSession = Depends(get_async_session)):
    """
    Preview payroll for a period without saving.
    Calculates each employee's salary based on days worked.
    period format: "2026-04"
    """
    from calendar import monthrange
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
    for emp in employees:
        # Count present days in this period
        _dp = await db.execute(select(func.count(Attendance.id)).where(
            Attendance.employee_id == emp.id,
            Attendance.status == "present",
            func.extract("year",  Attendance.date) == year,
            func.extract("month", Attendance.date) == month,
        ))
        days_present = _dp.scalar() or 0

        _ar = await db.execute(select(Payroll).where(
            Payroll.employee_id == emp.id,
            Payroll.period == period,
        ))
        already_run = _ar.scalar_one_or_none() is not None

        daily_rate  = float(emp.base_salary) / working_days if working_days > 0 else 0
        earned      = round(daily_rate * days_present, 2)
        total_to_pay += earned
        result.append({
            "employee_id":  emp.id,
            "employee":     emp.name,
            "position":     emp.position or "—",
            "base_salary":  float(emp.base_salary),
            "working_days": working_days,
            "days_elapsed": days_elapsed,
            "days_present": days_present,
            "days_absent":  days_elapsed - days_present,
            "daily_rate":   round(daily_rate, 2),
            "earned":       earned,
            "already_run":  already_run,
        })
    return {
        "period":       period,
        "working_days": working_days,
        "days_elapsed": days_elapsed,
        "employees":    result,
        "total_to_pay": round(total_to_pay, 2),
    }

@router.post("/api/payroll/run", dependencies=[Depends(require_permission("action_hr_run_payroll"))])
async def run_payroll(data: PayrollRun, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    from calendar import monthrange
    year, month = int(data.period.split("-")[0]), int(data.period.split("-")[1])
    total_days   = monthrange(year, month)[1]
    working_days = sum(1 for d in range(1, total_days+1)
                       if date(year, month, d).weekday() < 5)
    today = date.today()
    if today.year == year and today.month == month:
        days_elapsed = sum(1 for d in range(1, today.day+1)
                           if date(year, month, d).weekday() < 5)
    else:
        days_elapsed = working_days

    emp_stmt = select(Employee).where(Employee.is_active == True)
    if data.emp_ids:
        emp_stmt = emp_stmt.where(Employee.id.in_(data.emp_ids))
    _re = await db.execute(emp_stmt)
    employees = _re.scalars().all()

    created = 0; skipped = 0
    for emp in employees:
        _ex = await db.execute(select(Payroll).where(
            Payroll.employee_id == emp.id,
            Payroll.period == data.period,
        ))
        exists = _ex.scalar_one_or_none()
        if exists:
            # Update existing with latest attendance
            _dp = await db.execute(select(func.count(Attendance.id)).where(
                Attendance.employee_id == emp.id,
                Attendance.status == "present",
                func.extract("year",  Attendance.date) == year,
                func.extract("month", Attendance.date) == month,
            ))
            days_present = _dp.scalar() or 0
            daily_rate      = float(emp.base_salary) / working_days if working_days > 0 else 0
            earned          = round(daily_rate * days_present, 2)
            exists.base_salary  = emp.base_salary
            exists.net_salary   = earned + float(exists.bonuses or 0) - float(exists.deductions or 0)
            exists.days_worked  = days_present
            exists.working_days = working_days
            skipped += 1
            continue

        _dp = await db.execute(select(func.count(Attendance.id)).where(
            Attendance.employee_id == emp.id,
            Attendance.status == "present",
            func.extract("year",  Attendance.date) == year,
            func.extract("month", Attendance.date) == month,
        ))
        days_present = _dp.scalar() or 0

        daily_rate = float(emp.base_salary) / working_days if working_days > 0 else 0
        earned     = round(daily_rate * days_present, 2)

        p = Payroll(
            employee_id=emp.id,
            period=data.period,
            base_salary=emp.base_salary,
            bonuses=0, deductions=0,
            net_salary=earned,
            paid=False,
            days_worked=days_present,
            working_days=working_days,
        )
        db.add(p)
        created += 1

    record(db, "HR", "run_payroll",
           f"Payroll run for {data.period} — {created} created, {skipped} updated",
           ref_type="payroll", ref_id=data.period)
    await db.commit()
    return {"created": created, "skipped": skipped, "period": data.period}


@router.put("/api/payroll/{payroll_id}", dependencies=[Depends(require_permission("action_hr_run_payroll"))])
async def update_payroll(payroll_id: int, data: PayrollUpdate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    _r = await db.execute(select(Payroll).where(Payroll.id == payroll_id))
    p = _r.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Payroll record not found")
    # Back-calculate the attendance-earned amount from the previously stored values
    # before applying the new bonuses/deductions
    old_bonuses    = float(p.bonuses    or 0)
    old_deductions = float(p.deductions or 0)
    earned         = float(p.net_salary) - old_bonuses + old_deductions

    p.bonuses    = data.bonuses
    p.deductions = data.deductions
    p.net_salary = round(earned + data.bonuses - data.deductions, 2)
    if data.notes:
        p.notes = data.notes
    record(db, "HR", "update_payroll",
           f"Updated payroll #{payroll_id} — bonuses: {data.bonuses}, deductions: {data.deductions}, net: {p.net_salary:.2f}",
           ref_type="payroll", ref_id=payroll_id)
    await db.commit()
    return {"ok": True, "net_salary": float(p.net_salary)}

@router.patch("/api/payroll/{payroll_id}/pay", dependencies=[Depends(require_permission("action_hr_mark_paid"))])
async def mark_paid(payroll_id: int, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    from datetime import datetime, timezone
    _r = await db.execute(select(Payroll).where(Payroll.id == payroll_id))
    p = _r.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Payroll record not found")
    p.paid    = True
    p.paid_at = datetime.now(timezone.utc)
    record(db, "HR", "mark_payroll_paid",
           f"Marked payroll #{payroll_id} as paid — net: {float(p.net_salary):.2f}",
           ref_type="payroll", ref_id=payroll_id)
    await db.commit()
    return {"ok": True}

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
@keyframes modalIn { from{opacity:0;transform:scale(.95)} to{opacity:1;transform:scale(1)} }
.modal-title { font-size: 18px; font-weight: 800; margin-bottom: 20px; }
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
        <div class="table-wrap">
            <table>
                <thead><tr><th>Name</th><th>Position</th><th>Department</th><th>Phone</th><th>Hire Date</th><th>Base Salary</th><th>Actions</th></tr></thead>
                <tbody id="emp-body"><tr><td colspan="7" style="text-align:center;color:var(--muted);padding:40px">Loading...</td></tr></tbody>
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
                    <thead><tr><th>Employee</th><th>Base Salary</th><th>Working Days</th><th>Days Present</th><th>Days Absent</th><th>Daily Rate</th><th>Earned</th><th>Status</th></tr></thead>
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
                <thead><tr><th>Employee</th><th>Period</th><th>Base Salary</th><th>Days</th><th>Bonuses</th><th>Deductions</th><th>Net Salary</th><th>Status</th><th>Actions</th></tr></thead>
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
  function hasPermission(permission, u = currentUser){
      const role = u ? (u.role || "") : "";
      const perms = new Set(u ? (u.permissions || []) : []);
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
  }
  initializeColorMode();
  initUser().then(u => { if(u) configureHRPermissions(u); });
  let employees    = [];
let editingEmpId = null;
let editingPayId = null;
let empSearchTimer = null;

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
    await loadSummary();
    await loadEmployees();
    // Auto-mark all present today on page load
    await fetch("/hr/api/attendance/auto-today", {method:"POST"});
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
            `<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:40px">No employees found</td></tr>`;
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
            <td style="font-family:var(--mono);font-size:12px">${displayText(e.phone)}</td>
            <td style="font-size:12px;color:var(--muted)">${displayText(e.hire_date)}</td>
            <td class="mono">${money(salary)}</td>
            <td style="display:flex;gap:6px">
                <button class="action-btn" onclick="openEditEmpFromButton(this)" data-id="${id}" data-name="${escapeHtml(normalizeDashFallback(e.name))}" data-position="${escapeHtml(normalizeDashFallback(e.position))}" data-department="${escapeHtml(normalizeDashFallback(e.department))}" data-phone="${escapeHtml(normalizeDashFallback(e.phone))}" data-salary="${salary}">Edit</button>
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
        numberValue(btn.dataset.salary)
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
    document.getElementById("emp-modal").classList.add("open");
}

function openEditEmpModal(id,name,position,department,phone,salary){
    editingEmpId = id;
    document.getElementById("emp-modal-title").innerText = "Edit Employee";
    document.getElementById("e-name").value       = name;
    document.getElementById("e-position").value   = normalizeDashFallback(position);
    document.getElementById("e-department").value = normalizeDashFallback(department);
    document.getElementById("e-phone").value      = normalizeDashFallback(phone);
    document.getElementById("e-salary").value     = salary;
    document.getElementById("emp-modal").classList.add("open");
}

function closeEmpModal(){ document.getElementById("emp-modal").classList.remove("open"); }

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
    };
    let url    = editingEmpId ? `/hr/api/employees/${editingEmpId}` : "/hr/api/employees";
    let method = editingEmpId ? "PUT" : "POST";
    let res    = await fetch(url,{method,headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    let data   = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    closeEmpModal();
    showToast(editingEmpId?"Employee updated":"Employee added");
    loadEmployees(); loadSummary();
}

async function deactivateEmployee(id,name){
    if(!confirm(`Remove "${name}" from active employees?`)) return;
    await fetch(`/hr/api/employees/${id}`,{method:"DELETE"});
    showToast("Employee removed");
    loadEmployees(); loadSummary();
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
    loadAttendance(); loadSummary();
}

/* ── ATTENDANCE TODAY CARD ── */
async function loadTodayAttendance(){
    let today = new Date().toISOString().split("T")[0];
    let records = await (await fetch(`/hr/api/attendance?period=${today.slice(0,7)}`)).json();
    let todayRecs = records.filter(r => r.date === today);
    let grid = document.getElementById("today-attendance-grid");
    if(!grid) return;
    if(!employees.length){ grid.innerHTML = `<div style="color:var(--muted);font-size:13px">No employees found</div>`; return; }
    grid.innerHTML = employees.map(emp => {
        let rec    = todayRecs.find(r => r.employee_id === emp.id);
        let status = rec ? rec.status : "present";
        let isAbs  = status === "absent";
        return `<div style="display:flex;align-items:center;justify-content:space-between;padding:8px 12px;background:var(--card2);border:1px solid ${isAbs?"rgba(255,77,109,.2)":"rgba(0,255,157,.1)"};border-radius:9px;">
            <div>
                <span style="font-weight:600;font-size:13px;color:var(--text)">${displayText(emp.name)}</span>
                <span style="font-size:11px;color:var(--muted);margin-left:8px">${escapeHtml(normalizeDashFallback(emp.position))}</span>
            </div>
            <div style="display:flex;align-items:center;gap:10px">
                <span style="font-size:12px;font-weight:700;color:${isAbs?"var(--danger)":"var(--green)"}">
                    ${isAbs?"Absent":"Present"}
                </span>
                ${hasPermission("action_hr_run_payroll") ? (isAbs
                    ? `<button class="action-btn green" onclick="markPresentToday(${emp.id})">Mark Present</button>`
                    : `<button class="action-btn danger" onclick="markAbsentToday(${emp.id})">Mark Absent</button>`
                ) : ""}
            </div>
        </div>`;
    }).join("");
}

async function markAbsentToday(empId){
    await fetch("/hr/api/attendance/mark-absent",{
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({employee_id: empId, date: new Date().toISOString().split("T")[0], status:"absent"}),
    });
    showToast("Marked absent");
    loadTodayAttendance(); loadAttendance(); loadSummary();
}

async function markPresentToday(empId){
    await fetch("/hr/api/attendance",{
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({employee_id: empId, date: new Date().toISOString().split("T")[0], status:"present"}),
    });
    showToast("Marked present");
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
        `<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:20px">Loading preview...</td></tr>`;

    let d    = await (await fetch(`/hr/api/payroll/preview?period=${period}`)).json();
    let [yr, mo] = period.split("-");
    let monthName = new Date(parseInt(yr), parseInt(mo)-1, 1).toLocaleDateString("en-GB",{month:"long",year:"numeric"});

    document.getElementById("preview-meta").innerHTML =
        `<b>${escapeHtml(monthName)}</b> &nbsp;-&nbsp; ${numberValue(d.days_elapsed)} of ${numberValue(d.working_days)} working days elapsed`;
    document.getElementById("preview-total").innerText = money(d.total_to_pay) + " EGP";

    document.getElementById("preview-body").innerHTML = d.employees.map(e => `
        <tr>
            <td class="name">${displayText(e.employee)}<br><span style="font-size:11px;color:var(--muted)">${displayText(e.position)}</span></td>
            <td style="font-family:var(--mono)">${money(e.base_salary)}</td>
            <td style="font-family:var(--mono);color:var(--sub)">${numberValue(e.working_days)}</td>
            <td style="font-family:var(--mono);color:var(--green);font-weight:700">${numberValue(e.days_present)}</td>
            <td style="font-family:var(--mono);color:${numberValue(e.days_absent)>0?"var(--danger)":"var(--muted)"}">${numberValue(e.days_absent)}</td>
            <td style="font-family:var(--mono);color:var(--blue)">${money(e.daily_rate)}</td>
            <td style="font-family:var(--mono);font-size:15px;font-weight:700;color:var(--green)">${money(e.earned)}</td>
            <td><span style="font-size:11px;color:${e.already_run?"var(--warn)":"var(--muted)"}">${e.already_run?"Will update":"New"}</span></td>
        </tr>`).join("") +
        `<tr style="background:var(--card2)">
            <td colspan="6" style="font-weight:700;color:var(--sub)">Total to Pay</td>
            <td style="font-family:var(--mono);font-size:16px;font-weight:700;color:var(--green)">${money(d.total_to_pay)}</td>
            <td></td>
        </tr>`;
}

async function confirmRunPayroll(){
    let period = document.getElementById("pay-period").value;
    if(!period){ showToast("Select a period first"); return; }
    let res  = await fetch("/hr/api/payroll/run",{
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({period}),
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
            `<tr><td colspan="9" style="text-align:center;color:var(--muted);padding:40px">No payroll records. Use preview above to generate.</td></tr>`;
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
            <td style="font-family:var(--mono);color:var(--danger)">-${money(r.deductions)}</td>
            <td style="font-family:var(--mono);font-size:15px;font-weight:700;color:var(--green)">${money(r.net_salary)}</td>
            <td>${r.paid?`<span class="paid-badge">Paid</span>`:`<span class="unpaid-badge">Pending</span>`}</td>
            <td style="display:flex;gap:6px">
                <button class="action-btn purple" onclick="openEditPayFromButton(this)" data-id="${numberValue(r.id)}" data-employee="${escapeHtml(normalizeDashFallback(r.employee))}" data-bonuses="${numberValue(r.bonuses)}" data-deductions="${numberValue(r.deductions)}">Edit</button>
                ${!r.paid && hasPermission("action_hr_mark_paid")?`<button class="action-btn green" onclick="markPaid(${numberValue(r.id)})">Mark Paid</button>`:""}
            </td>
        </tr>`).join("") +
        `<tr style="background:var(--card2)">
            <td colspan="6" style="font-weight:700;color:var(--sub)">Total</td>
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
    await fetch(`/hr/api/payroll/${id}/pay`,{method:"PATCH"});
    showToast("Marked as paid");
    loadPayrollRecords();
}

/* ── MODAL CLOSE ON BG ── */
["emp-modal","att-modal","pay-run-modal","edit-pay-modal"].forEach(id=>{
    document.getElementById(id).addEventListener("click",function(e){
        if(e.target===this) this.classList.remove("open");
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
