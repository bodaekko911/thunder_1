from decimal import Decimal
from typing import Optional, List
from datetime import date as date_type

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.log import record
from app.core.permissions import get_current_user, require_permission
from app.database import get_async_session
from app.core.navigation import render_app_header
from app.models.accounting import Account, Journal, JournalEntry
from app.models.expense import Expense, ExpenseCategory
from app.models.farm import Farm, FarmDelivery, FarmDeliveryItem
from app.models.product import Product
from app.models.user import User

router = APIRouter(
    prefix="/expenses",
    tags=["Expenses"],
    dependencies=[Depends(require_permission("page_accounting"))],
)


# ── Schemas ─────────────────────────────────────────────────────────────────

class CategoryCreate(BaseModel):
    name:         str
    account_code: Optional[str] = None   # auto-generated if not provided
    description:  Optional[str] = None

class ExpenseCreate(BaseModel):
    category_id:    int
    expense_date:   str          # ISO "YYYY-MM-DD"
    amount:         float
    payment_method: str = "cash"
    vendor:         Optional[str] = None
    description:    Optional[str] = None
    farm_id:        Optional[int] = None   # link to farm for cost allocation

class ExpenseUpdate(BaseModel):
    category_id:    Optional[int]   = None
    expense_date:   Optional[str]   = None
    amount:         Optional[float] = None
    payment_method: Optional[str]   = None
    vendor:         Optional[str]   = None
    description:    Optional[str]   = None
    farm_id:        Optional[int]   = None


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _next_ref(db: AsyncSession) -> str:
    _r = await db.execute(select(func.max(Expense.id)))
    max_id = _r.scalar() or 0
    return f"EXP-{str(max_id + 1).zfill(5)}"


async def _post_expense_journal(
    db: AsyncSession,
    description: str,
    amount: float,
    expense_account_code: str,
    payment_method: str,
    user_id: Optional[int],
) -> Journal:
    # Cash/card → account 1000 (Cash); bank transfer → account 1200 (Bank)
    credit_code = "1000" if payment_method in ("cash", "card") else "1200"

    journal = Journal(
        ref_type="expense",
        description=description,
        user_id=user_id,
    )
    db.add(journal)
    await db.flush()

    for code, debit, credit in [
        (expense_account_code, amount, 0),   # Debit expense account
        (credit_code,          0, amount),   # Credit cash / bank
    ]:
        _r = await db.execute(select(Account).where(Account.code == code))
        acc = _r.scalar_one_or_none()
        if not acc:
            # Auto-create the account if it doesn't exist yet
            type_name = "expense" if code.startswith("5") else "asset"
            acc = Account(code=code, name=f"Account {code}", type=type_name, balance=0)
            db.add(acc)
            await db.flush()
        db.add(JournalEntry(
            journal_id=journal.id,
            account_id=acc.id,
            debit=debit,
            credit=credit,
        ))
        acc.balance += Decimal(str(debit)) - Decimal(str(credit))

    return journal


async def _reverse_expense_journal(db: AsyncSession, expense: Expense) -> None:
    """Post a reversal journal entry for a deleted expense."""
    cat = expense.category
    if not cat:
        return
    credit_code = "1000" if expense.payment_method in ("cash", "card") else "1200"
    journal = Journal(
        ref_type="expense_reversal",
        description=f"Reversal — {expense.ref_number}",
        user_id=expense.user_id,
    )
    db.add(journal)
    await db.flush()
    for code, debit, credit in [
        (credit_code,          float(expense.amount), 0),   # Debit cash back
        (cat.account_code,     0, float(expense.amount)),   # Credit expense account
    ]:
        _r = await db.execute(select(Account).where(Account.code == code))
        acc = _r.scalar_one_or_none()
        if acc:
            db.add(JournalEntry(
                journal_id=journal.id,
                account_id=acc.id,
                debit=debit,
                credit=credit,
            ))
            acc.balance += Decimal(str(debit)) - Decimal(str(credit))


# ── Category API ─────────────────────────────────────────────────────────────

@router.get("/api/categories")
async def list_categories(db: AsyncSession = Depends(get_async_session)):
    _r = await db.execute(
        select(ExpenseCategory)
        .where(ExpenseCategory.is_active == "1")
        .order_by(ExpenseCategory.account_code)
    )
    cats = _r.scalars().all()
    return [
        {
            "id":           c.id,
            "name":         c.name,
            "account_code": c.account_code,
            "description":  c.description or "",
            "count":        len(c.expenses),
            "total":        float(sum(e.amount for e in c.expenses)),
        }
        for c in cats
    ]


@router.post("/api/categories")
async def create_category(
    data: CategoryCreate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    _r = await db.execute(select(ExpenseCategory).where(ExpenseCategory.name == data.name.strip()))
    if _r.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Category name already exists")

    # Auto-generate account code: find highest 5xxx code in use and increment
    if data.account_code and data.account_code.strip():
        account_code = data.account_code.strip()
    else:
        _r2 = await db.execute(select(ExpenseCategory))
        existing_codes = [
            int(c.account_code)
            for c in _r2.scalars().all()
            if c.account_code and c.account_code.isdigit() and 5000 <= int(c.account_code) <= 5999
        ]
        account_code = str(max(existing_codes) + 1) if existing_codes else "5001"

    # Ensure ledger account exists
    _r3 = await db.execute(select(Account).where(Account.code == account_code))
    if not _r3.scalar_one_or_none():
        db.add(Account(code=account_code, name=data.name.strip(), type="expense", balance=0))

    cat = ExpenseCategory(
        name=data.name.strip(),
        account_code=account_code,
        description=(data.description or "").strip() or None,
    )
    db.add(cat)
    await db.commit()
    await db.refresh(cat)
    return {"id": cat.id, "name": cat.name, "account_code": cat.account_code}


@router.delete("/api/categories/{cat_id}")
async def delete_category(
    cat_id: int,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    _r = await db.execute(select(ExpenseCategory).where(ExpenseCategory.id == cat_id))
    cat = _r.scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    if cat.expenses:
        raise HTTPException(status_code=400, detail="Cannot delete a category that has expenses. Archive it instead.")
    cat.is_active = "0"
    await db.commit()
    return {"ok": True}


# ── Expense API ───────────────────────────────────────────────────────────────

@router.get("/api/list")
async def list_expenses(
    category_id: Optional[int] = None,
    month: Optional[str] = None,   # "YYYY-MM"
    db: AsyncSession = Depends(get_async_session),
):
    stmt = select(Expense)
    if category_id:
        stmt = stmt.where(Expense.category_id == category_id)
    if month:
        try:
            y, m = int(month[:4]), int(month[5:7])
            stmt = stmt.where(
                func.extract("year",  Expense.expense_date) == y,
                func.extract("month", Expense.expense_date) == m,
            )
        except (ValueError, IndexError):
            pass
    stmt = stmt.order_by(Expense.expense_date.desc(), Expense.id.desc())
    _r = await db.execute(stmt)
    expenses = _r.scalars().all()
    return [
        {
            "id":             e.id,
            "ref_number":     e.ref_number,
            "category":       e.category.name if e.category else "—",
            "category_id":    e.category_id,
            "account_code":   e.category.account_code if e.category else "—",
            "expense_date":   str(e.expense_date),
            "amount":         float(e.amount),
            "payment_method": e.payment_method,
            "vendor":         e.vendor or "",
            "description":    e.description or "",
            "created_by":     e.user.name if e.user else "—",
            "farm_id":        e.farm_id,
            "farm_name":      e.farm.name if e.farm else None,
        }
        for e in expenses
    ]


@router.get("/api/summary")
async def expense_summary(db: AsyncSession = Depends(get_async_session)):
    """Monthly totals + category breakdown for the current month."""
    from datetime import datetime
    now = datetime.utcnow()
    _r = await db.execute(
        select(func.coalesce(func.sum(Expense.amount), 0)).where(
            func.extract("year",  Expense.expense_date) == now.year,
            func.extract("month", Expense.expense_date) == now.month,
        )
    )
    this_month = _r.scalar() or 0

    last_month_year  = now.year if now.month > 1 else now.year - 1
    last_month_month = now.month - 1 if now.month > 1 else 12
    _r2 = await db.execute(
        select(func.coalesce(func.sum(Expense.amount), 0)).where(
            func.extract("year",  Expense.expense_date) == last_month_year,
            func.extract("month", Expense.expense_date) == last_month_month,
        )
    )
    last_month = _r2.scalar() or 0

    _r3 = await db.execute(select(func.coalesce(func.sum(Expense.amount), 0)))
    total_all = _r3.scalar() or 0

    # Category breakdown this month
    _r4 = await db.execute(select(ExpenseCategory).where(ExpenseCategory.is_active == "1"))
    cats = _r4.scalars().all()
    breakdown = []
    for c in cats:
        _rc = await db.execute(
            select(func.coalesce(func.sum(Expense.amount), 0)).where(
                Expense.category_id == c.id,
                func.extract("year",  Expense.expense_date) == now.year,
                func.extract("month", Expense.expense_date) == now.month,
            )
        )
        cat_total = _rc.scalar() or 0
        if float(cat_total) > 0:
            breakdown.append({"name": c.name, "total": float(cat_total)})
    breakdown.sort(key=lambda x: x["total"], reverse=True)

    return {
        "this_month": float(this_month),
        "last_month": float(last_month),
        "total_all":  float(total_all),
        "breakdown":  breakdown,
    }


@router.post("/api/add")
async def add_expense(
    data: ExpenseCreate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    _r = await db.execute(
        select(ExpenseCategory).where(
            ExpenseCategory.id == data.category_id,
            ExpenseCategory.is_active == "1",
        )
    )
    cat = _r.scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    if data.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than 0")

    try:
        exp_date = date_type.fromisoformat(data.expense_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format — use YYYY-MM-DD")

    ref = await _next_ref(db)
    vendor = (data.vendor or "").strip() or None
    desc   = (data.description or "").strip() or None

    journal = await _post_expense_journal(
        db=db,
        description=f"{cat.name} expense — {ref}" + (f" — {vendor}" if vendor else ""),
        amount=round(float(data.amount), 2),
        expense_account_code=cat.account_code,
        payment_method=data.payment_method,
        user_id=current_user.id,
    )

    expense = Expense(
        ref_number=ref,
        category_id=cat.id,
        user_id=current_user.id,
        expense_date=exp_date,
        amount=round(float(data.amount), 2),
        payment_method=data.payment_method,
        vendor=vendor,
        description=desc,
        journal_id=journal.id,
        farm_id=data.farm_id or None,
    )
    db.add(expense)
    record(
        db, "Expenses", "add_expense",
        f"{cat.name} — {ref} — {float(data.amount):.2f} — {data.payment_method}",
        user=current_user, ref_type="expense", ref_id=0,
    )
    await db.commit()
    await db.refresh(expense)
    # update ref_id in activity log — simpler to just re-record
    return {
        "id":         expense.id,
        "ref_number": expense.ref_number,
        "amount":     float(expense.amount),
        "category":   cat.name,
    }


@router.put("/api/edit/{expense_id}")
async def edit_expense(
    expense_id: int,
    data: ExpenseUpdate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    _r = await db.execute(select(Expense).where(Expense.id == expense_id))
    expense = _r.scalar_one_or_none()
    if not expense:
        raise HTTPException(status_code=404, detail="Expense not found")

    # Reverse original journal
    await _reverse_expense_journal(db, expense)

    # Apply updates
    if data.category_id is not None:
        _rc = await db.execute(
            select(ExpenseCategory).where(
                ExpenseCategory.id == data.category_id,
                ExpenseCategory.is_active == "1",
            )
        )
        cat = _rc.scalar_one_or_none()
        if not cat:
            raise HTTPException(status_code=404, detail="Category not found")
        expense.category_id = data.category_id
    if data.expense_date is not None:
        try:
            expense.expense_date = date_type.fromisoformat(data.expense_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format")
    if data.amount is not None:
        if data.amount <= 0:
            raise HTTPException(status_code=400, detail="Amount must be greater than 0")
        expense.amount = round(float(data.amount), 2)
    if data.payment_method is not None:
        expense.payment_method = data.payment_method
    if data.vendor is not None:
        expense.vendor = data.vendor.strip() or None
    if data.description is not None:
        expense.description = data.description.strip() or None
    if data.farm_id is not None:
        expense.farm_id = data.farm_id or None

    # Post new journal
    _rcat = await db.execute(select(ExpenseCategory).where(ExpenseCategory.id == expense.category_id))
    cat = _rcat.scalar_one_or_none()
    journal = await _post_expense_journal(
        db=db,
        description=f"{cat.name} expense (edited) — {expense.ref_number}",
        amount=float(expense.amount),
        expense_account_code=cat.account_code,
        payment_method=expense.payment_method,
        user_id=current_user.id,
    )
    expense.journal_id = journal.id

    record(db, "Expenses", "edit_expense",
           f"Edited {expense.ref_number} — {float(expense.amount):.2f}",
           user=current_user, ref_type="expense", ref_id=expense.id)
    await db.commit()
    return {"ok": True}


@router.delete("/api/delete/{expense_id}")
async def delete_expense(
    expense_id: int,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    _r = await db.execute(select(Expense).where(Expense.id == expense_id))
    expense = _r.scalar_one_or_none()
    if not expense:
        raise HTTPException(status_code=404, detail="Expense not found")

    ref = expense.ref_number
    await _reverse_expense_journal(db, expense)
    await db.delete(expense)
    record(db, "Expenses", "delete_expense",
           f"Deleted {ref} — journal reversed",
           user=current_user, ref_type="expense", ref_id=expense_id)
    await db.commit()
    return {"ok": True}


# ── Cost Allocation API ──────────────────────────────────────────────────────

@router.get("/api/cost-allocation")
async def cost_allocation(
    farm_id:   int,
    date_from: str,          # YYYY-MM-DD
    date_to:   str,
    db: AsyncSession = Depends(get_async_session),
):
    """
    For a given farm + date range (a "season"):
    1. Sum all expenses tagged to this farm in the period, by category.
    2. Sum all farm deliveries from this farm in the period, by product (kg).
    3. Distribute total farm costs proportionally by kg harvested.
    4. Return cost_per_kg and estimated profit_per_kg for each product.
    """
    from datetime import date as date_type_local
    try:
        d_from = date_type_local.fromisoformat(date_from)
        d_to   = date_type_local.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format — use YYYY-MM-DD")

    _rf = await db.execute(select(Farm).where(Farm.id == farm_id))
    farm = _rf.scalar_one_or_none()
    if not farm:
        raise HTTPException(status_code=404, detail="Farm not found")

    # 1 ── Farm expenses in the period
    _re = await db.execute(
        select(Expense).where(
            Expense.farm_id == farm_id,
            Expense.expense_date >= d_from,
            Expense.expense_date <= d_to,
        )
    )
    expenses = _re.scalars().all()
    total_cost = sum(float(e.amount) for e in expenses)
    cost_by_category = {}
    for e in expenses:
        cat_name = e.category.name if e.category else "Other"
        cost_by_category[cat_name] = cost_by_category.get(cat_name, 0) + float(e.amount)

    # 2 ── Deliveries from this farm in the period
    _rd = await db.execute(
        select(FarmDelivery).where(
            FarmDelivery.farm_id == farm_id,
            FarmDelivery.delivery_date >= d_from,
            FarmDelivery.delivery_date <= d_to,
        )
    )
    deliveries = _rd.scalars().all()
    qty_by_product: dict[int, dict] = {}
    for d in deliveries:
        for item in d.items:
            pid = item.product_id
            if pid not in qty_by_product:
                p = item.product
                qty_by_product[pid] = {
                    "product_id":   pid,
                    "product_name": p.name if p else f"#{pid}",
                    "unit":         item.unit or (p.unit if p else "kg"),
                    "sale_price":   float(p.price) if p else 0,
                    "total_qty":    0,
                }
            qty_by_product[pid]["total_qty"] += float(item.qty)

    total_qty = sum(v["total_qty"] for v in qty_by_product.values())

    # 3 ── Allocate costs proportionally by qty
    products_out = []
    for pid, info in qty_by_product.items():
        share = info["total_qty"] / total_qty if total_qty > 0 else 0
        allocated_cost = total_cost * share
        cost_per_unit  = allocated_cost / info["total_qty"] if info["total_qty"] > 0 else 0
        profit_per_unit = info["sale_price"] - cost_per_unit
        products_out.append({
            "product_id":       pid,
            "product_name":     info["product_name"],
            "unit":             info["unit"],
            "total_qty":        round(info["total_qty"], 3),
            "share_pct":        round(share * 100, 1),
            "allocated_cost":   round(allocated_cost, 2),
            "cost_per_unit":    round(cost_per_unit, 2),
            "sale_price":       round(info["sale_price"], 2),
            "profit_per_unit":  round(profit_per_unit, 2),
            "profit_margin_pct": round((profit_per_unit / info["sale_price"] * 100) if info["sale_price"] > 0 else 0, 1),
        })
    products_out.sort(key=lambda x: x["allocated_cost"], reverse=True)

    return {
        "farm_id":          farm_id,
        "farm_name":        farm.name,
        "date_from":        date_from,
        "date_to":          date_to,
        "total_cost":       round(total_cost, 2),
        "total_qty":        round(total_qty, 3),
        "cost_by_category": [{"name": k, "amount": round(v, 2)} for k, v in sorted(cost_by_category.items(), key=lambda x: -x[1])],
        "products":         products_out,
        "expense_count":    len(expenses),
        "delivery_count":   len(deliveries),
    }


# ── UI ───────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def expenses_ui(current_user: User = Depends(require_permission("page_expenses"))):
    return """<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<script src="/static/theme.js"></script>
<title>Expenses — Thunder ERP</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root {
    --bg:      #060810;
    --card:    #0f1424;
    --card2:   #151c30;
    --border:  rgba(255,255,255,0.06);
    --border2: rgba(255,255,255,0.11);
    --amber:   #ffb547;
    --amber2:  #f59e0b;
    --green:   #00ff9d;
    --blue:    #4d9fff;
    --rose:    #ff6b8a;
    --text:    #f0f4ff;
    --sub:     #8899bb;
    --muted:   #445066;
    --sans:    'Outfit', sans-serif;
    --mono:    'JetBrains Mono', monospace;
}
body.light {
    --bg: #f2f4f8; --card: #ffffff; --card2: #f7f8fb;
    --border: rgba(0,0,0,0.07); --border2: rgba(0,0,0,0.13);
    --green: #0f8a43;
    --text: #141820; --sub: #505870; --muted: #8090a8;
}
body.light nav { background: rgba(242,244,248,.93); }
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: var(--sans); background: var(--bg); color: var(--text); min-height: 100vh; font-size: 14px; }

/* ── NAV ── */
nav {
    position: sticky; top: 0; z-index: 100;
    display: flex; align-items: center; gap: 10px;
    padding: 0 24px; height: 58px;
    background: rgba(6,8,16,.92); backdrop-filter: blur(20px);
    border-bottom: 1px solid var(--border);
}
.logo {
    font-size: 17px; font-weight: 900; text-decoration: none;
    display: flex; align-items: center; gap: 8px; margin-right: 10px;
    background: linear-gradient(135deg, var(--green), var(--blue));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
}
.nav-back {
    display: inline-flex; align-items: center; gap: 7px;
    background: var(--card); border: 1px solid var(--border2);
    color: var(--sub); font-family: var(--sans); font-size: 12px;
    font-weight: 600; padding: 7px 14px; border-radius: 9px;
    text-decoration: none; transition: all .2s;
}
.nav-back:hover { border-color: var(--amber); color: var(--amber); }
.nav-spacer { flex: 1; }
.mode-btn {
    width: 36px; height: 36px; border-radius: 10px;
    border: 1px solid var(--border); background: var(--card);
    color: var(--sub); font-size: 16px; cursor: pointer;
    display: flex; align-items: center; justify-content: center; transition: all .2s;
}
.mode-btn:hover { border-color: var(--border2); transform: scale(1.06); }
.user-pill {
    display: flex; align-items: center; gap: 10px;
    background: var(--card); border: 1px solid var(--border);
    border-radius: 40px; padding: 6px 14px 6px 8px;
}
.user-avatar {
    width: 26px; height: 26px;
    background: linear-gradient(135deg, #f59e0b, #ef4444);
    border-radius: 50%; display: flex; align-items: center; justify-content: center;
    font-size: 11px; font-weight: 700; color: #fff;
}
.user-name { font-size: 13px; font-weight: 500; color: var(--sub); }
.logout-btn {
    background: transparent; border: 1px solid var(--border);
    color: var(--muted); font-family: var(--sans); font-size: 12px;
    padding: 7px 14px; border-radius: 8px; cursor: pointer; transition: all .2s;
}
.logout-btn:hover { border-color: var(--rose); color: var(--rose); }

/* ── PAGE ── */
.page { max-width: 1380px; margin: 0 auto; padding: 28px 24px; }
.page-header { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: 24px; flex-wrap: wrap; gap: 12px; }
.page-title { font-size: 22px; font-weight: 800; display: flex; align-items: center; gap: 10px; }
.page-title-badge {
    font-size: 11px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase;
    background: rgba(255,181,71,.12); border: 1px solid rgba(255,181,71,.28);
    color: var(--amber); padding: 3px 10px; border-radius: 20px;
}
.page-sub { color: var(--muted); font-size: 13px; margin-top: 4px; }
.add-btn {
    display: inline-flex; align-items: center; gap: 8px;
    background: linear-gradient(135deg, var(--amber2), #d97706);
    border: none; border-radius: 11px; padding: 11px 22px;
    font-family: var(--sans); font-size: 13px; font-weight: 700;
    color: #1a0f00; cursor: pointer; transition: all .2s;
    box-shadow: 0 4px 18px rgba(245,158,11,.25);
}
.add-btn:hover { filter: brightness(1.08); transform: translateY(-1px); }

/* ── STATS ROW ── */
.stats-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 20px; }
.stat-card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 14px; padding: 16px 18px;
}
.stat-label { font-size: 10px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; color: var(--muted); margin-bottom: 6px; }
.stat-value { font-family: var(--mono); font-size: 22px; font-weight: 700; color: var(--text); line-height: 1; }
.stat-value.amber { color: var(--amber); }
.stat-value.rose  { color: var(--rose); }
.stat-sub { font-size: 11px; color: var(--muted); margin-top: 4px; }

/* ── LAYOUT ── */
.layout { display: grid; grid-template-columns: 260px 1fr; gap: 16px; align-items: start; }

/* ── SIDEBAR ── */
.sidebar {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 16px; overflow: hidden; position: sticky; top: 74px;
}
.sidebar-head {
    padding: 14px 16px 12px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
}
.sidebar-title { font-size: 12px; font-weight: 700; color: var(--text); }
.add-cat-btn {
    background: var(--card2); border: 1px solid var(--border2);
    color: var(--sub); font-family: var(--sans); font-size: 11px; font-weight: 700;
    padding: 5px 10px; border-radius: 7px; cursor: pointer; transition: all .15s;
}
.add-cat-btn:hover { border-color: var(--amber); color: var(--amber); }
.cat-list { padding: 8px; }
.cat-item {
    display: flex; align-items: center; justify-content: space-between;
    padding: 9px 10px; border-radius: 9px; cursor: pointer;
    transition: background .15s; gap: 8px;
}
.cat-item:hover { background: var(--card2); }
.cat-item.active { background: rgba(255,181,71,.09); border: 1px solid rgba(255,181,71,.2); }
.cat-item-name { font-size: 13px; font-weight: 600; color: var(--text); }
.cat-item-code { font-family: var(--mono); font-size: 10px; color: var(--muted); }
.cat-item-total { font-family: var(--mono); font-size: 11px; font-weight: 700; color: var(--amber); }

/* ── MAIN PANEL ── */
.main-panel { background: var(--card); border: 1px solid var(--border); border-radius: 16px; overflow: hidden; }
.main-head {
    padding: 14px 20px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px;
}
.main-title { font-size: 13px; font-weight: 700; color: var(--text); }
.filter-row { display: flex; align-items: center; gap: 8px; }
.filter-select, .filter-input {
    background: var(--card2); border: 1px solid var(--border2);
    border-radius: 9px; padding: 7px 12px; color: var(--text);
    font-family: var(--sans); font-size: 12px; outline: none;
    transition: border-color .2s;
}
.filter-select:focus, .filter-input:focus { border-color: rgba(255,181,71,.4); }

/* ── TABLE ── */
.exp-table { width: 100%; border-collapse: collapse; }
.exp-table th {
    padding: 10px 16px; text-align: left;
    font-size: 10px; font-weight: 700; letter-spacing: 1px;
    text-transform: uppercase; color: var(--muted);
    border-bottom: 1px solid var(--border);
}
.exp-table td { padding: 13px 16px; border-bottom: 1px solid var(--border); vertical-align: middle; }
.exp-table tr:last-child td { border-bottom: none; }
.exp-table tr:hover td { background: rgba(255,255,255,.02); }
.exp-ref { font-family: var(--mono); font-size: 11px; font-weight: 700; color: var(--amber); }
.exp-cat { font-size: 13px; font-weight: 600; color: var(--text); }
.exp-vendor { font-size: 12px; color: var(--sub); }
.exp-amount { font-family: var(--mono); font-size: 14px; font-weight: 700; color: var(--text); }
.exp-date { font-size: 12px; color: var(--sub); }
.method-pill {
    display: inline-flex; align-items: center; gap: 5px;
    font-size: 10px; font-weight: 700; padding: 3px 8px; border-radius: 20px;
    text-transform: uppercase; letter-spacing: .5px;
}
.method-cash     { background: rgba(0,255,157,.08); color: var(--green); border: 1px solid rgba(0,255,157,.2); }
.method-card     { background: rgba(77,159,255,.08); color: var(--blue);  border: 1px solid rgba(77,159,255,.2); }
.method-bank_transfer { background: rgba(255,181,71,.08); color: var(--amber); border: 1px solid rgba(255,181,71,.2); }
.action-btn {
    background: transparent; border: 1px solid var(--border);
    color: var(--muted); padding: 5px 10px; border-radius: 7px;
    cursor: pointer; font-family: var(--sans); font-size: 11px; font-weight: 600;
    transition: all .15s; margin-left: 4px;
}
.action-btn:hover { border-color: var(--border2); color: var(--sub); }
.action-btn.del:hover { border-color: var(--rose); color: var(--rose); }
.empty-row td { text-align: center; padding: 60px; color: var(--muted); }

/* ── MODAL ── */
.modal-bg {
    display: none; position: fixed; inset: 0;
    background: rgba(0,0,0,.6); backdrop-filter: blur(6px);
    z-index: 200; align-items: center; justify-content: center;
}
.modal-bg.open { display: flex; }
.modal {
    background: var(--card); border: 1px solid var(--border2);
    border-radius: 20px; padding: 28px; width: 460px; max-width: 95vw;
    box-shadow: 0 30px 80px rgba(0,0,0,.5);
    animation: modal-in .2s ease;
}
@keyframes modal-in { from { opacity:0; transform:scale(.95) translateY(8px); } to { opacity:1; transform:scale(1) translateY(0); } }
.modal-title { font-size: 17px; font-weight: 800; margin-bottom: 4px; }
.modal-sub { font-size: 13px; color: var(--muted); margin-bottom: 22px; }
.fld { display: flex; flex-direction: column; gap: 5px; margin-bottom: 14px; }
.fld label { font-size: 10px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; color: var(--muted); }
.fld input, .fld select, .fld textarea {
    background: var(--card2); border: 1px solid var(--border2);
    border-radius: 10px; padding: 10px 13px; color: var(--text);
    font-family: var(--sans); font-size: 13px; outline: none;
    transition: border-color .2s; width: 100%;
}
.fld input:focus, .fld select:focus, .fld textarea:focus { border-color: rgba(255,181,71,.45); }
.fld input::placeholder, .fld textarea::placeholder { color: var(--muted); }
.fld textarea { resize: vertical; min-height: 70px; }
.modal-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.modal-actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 20px; }
.btn-cancel {
    background: var(--card2); border: 1px solid var(--border2);
    color: var(--sub); padding: 10px 20px; border-radius: 10px;
    font-family: var(--sans); font-size: 13px; font-weight: 600; cursor: pointer;
}
.btn-save {
    background: linear-gradient(135deg, var(--amber2), #d97706);
    border: none; border-radius: 10px; padding: 10px 24px;
    font-family: var(--sans); font-size: 13px; font-weight: 700;
    color: #1a0f00; cursor: pointer; transition: all .2s;
}
.btn-save:hover { filter: brightness(1.08); }
.btn-save:disabled { opacity: .45; cursor: not-allowed; }

/* ── TOAST ── */
.toast {
    position: fixed; bottom: 22px; left: 50%;
    transform: translateX(-50%) translateY(12px);
    background: var(--card2); border: 1px solid var(--border2);
    border-radius: 12px; padding: 12px 20px;
    font-size: 13px; font-weight: 600; color: var(--text);
    box-shadow: 0 20px 50px rgba(0,0,0,.5);
    opacity: 0; pointer-events: none;
    transition: opacity .25s, transform .25s; z-index: 999;
}
.toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
.toast.ok  { border-color: rgba(0,255,157,.3);  color: var(--green); }
.toast.err { border-color: rgba(255,107,138,.3); color: var(--rose); }

::-webkit-scrollbar { width: 4px; }
::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 4px; }

@media (max-width: 900px) {
    .layout { grid-template-columns: 1fr; }
    .sidebar { position: static; }
    .stats-row { grid-template-columns: 1fr 1fr; }
}
@media (max-width: 560px) {
    .stats-row { grid-template-columns: 1fr; }
}
</style>
    <script src="/static/auth-guard.js"></script>
</head>
<body>

""" + render_app_header(current_user, "page_expenses") + """

<div class="page">
    <div class="page-header">
        <div>
            <div class="page-title">
                Expenses
                <span class="page-title-badge">Operational Costs</span>
            </div>
            <div class="page-sub">Track water, electricity, gas, rent and all other operating expenses.</div>
        </div>
        <button class="add-btn" id="record-expense-btn" onclick="openAddModal()">
            <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
            Record Expense
        </button>
    </div>

    <!-- Stats -->
    <div class="stats-row">
        <div class="stat-card">
            <div class="stat-label">This Month</div>
            <div class="stat-value amber" id="stat-this-month">—</div>
            <div class="stat-sub">EGP total</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Last Month</div>
            <div class="stat-value" id="stat-last-month">—</div>
            <div class="stat-sub">EGP total</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">All Time</div>
            <div class="stat-value" id="stat-all-time">—</div>
            <div class="stat-sub">EGP total</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Top Category</div>
            <div class="stat-value" id="stat-top-cat" style="font-size:15px;font-family:var(--sans)">—</div>
            <div class="stat-sub" id="stat-top-cat-amount"></div>
        </div>
    </div>

    <div class="layout">
        <!-- Sidebar: categories -->
        <div class="sidebar">
            <div class="sidebar-head">
                <span class="sidebar-title">Categories</span>
                <button class="add-cat-btn" id="new-category-btn" onclick="openCatModal()">+ New</button>
            </div>
            <div class="cat-list">
                <div class="cat-item active" id="cat-all" onclick="filterCategory(null)">
                    <div>
                        <div class="cat-item-name">All Expenses</div>
                        <div class="cat-item-code">5001–5999</div>
                    </div>
                    <div class="cat-item-total" id="cat-all-total">—</div>
                </div>
                <div id="cat-list-body"></div>
            </div>
        </div>

        <!-- Main: expense table -->
        <div class="main-panel">
            <div class="main-head">
                <span class="main-title" id="main-title">All Expenses</span>
                <div class="filter-row">
                    <input type="month" class="filter-input" id="month-filter"
                        oninput="loadExpenses()" style="cursor:pointer">
                    <button class="add-cat-btn" onclick="clearFilter()" title="Clear filter">✕ Clear</button>
                </div>
            </div>
            <div style="overflow-x:auto">
                <table class="exp-table">
                    <thead>
                        <tr>
                            <th>Ref</th>
                            <th>Category</th>
                            <th>Date</th>
                            <th>Vendor</th>
                            <th>Method</th>
                            <th>Amount</th>
                            <th></th>
                        </tr>
                    </thead>
                    <tbody id="exp-tbody">
                        <tr class="empty-row"><td colspan="7">Loading…</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>
</div>

<!-- Add / Edit Expense Modal -->
<div class="modal-bg" id="expense-modal">
    <div class="modal">
        <div class="modal-title" id="modal-title">Record Expense</div>
        <div class="modal-sub" id="modal-sub">Fill in the details below.</div>
        <div class="modal-row">
            <div class="fld">
                <label>Category *</label>
                <select id="m-category"></select>
            </div>
            <div class="fld">
                <label>Amount (EGP) *</label>
                <input id="m-amount" type="number" min="0.01" step="0.01" placeholder="0.00">
            </div>
        </div>
        <div class="modal-row">
            <div class="fld">
                <label>Date *</label>
                <input id="m-date" type="date">
            </div>
            <div class="fld">
                <label>Payment Method</label>
                <select id="m-method">
                    <option value="cash">💵 Cash</option>
                    <option value="bank_transfer">🏦 Bank Transfer</option>
                    <option value="card">💳 Card</option>
                </select>
            </div>
        </div>
        <div class="modal-row">
            <div class="fld">
                <label>Vendor / Supplier</label>
                <input id="m-vendor" placeholder="e.g. Cairo Electric Co.">
            </div>
            <div class="fld">
                <label>Farm (for cost allocation)</label>
                <select id="m-farm">
                    <option value="">— General expense —</option>
                </select>
            </div>
        </div>
        <div class="fld">
            <label>Notes</label>
            <textarea id="m-notes" placeholder="Optional description…"></textarea>
        </div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeModal('expense-modal')">Cancel</button>
            <button class="btn-save" id="modal-save-btn" onclick="saveExpense()">Save Expense</button>
        </div>
    </div>
</div>

<!-- Add Category Modal -->
<div class="modal-bg" id="cat-modal">
    <div class="modal" style="width:380px">
        <div class="modal-title">New Category</div>
        <div class="modal-sub">A ledger account code will be assigned automatically.</div>
        <div class="fld">
            <label>Category Name *</label>
            <input id="cm-name" placeholder="e.g. Internet & Phone">
        </div>
        <div class="fld">
            <label>Description</label>
            <input id="cm-desc" placeholder="Optional">
        </div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeModal('cat-modal')">Cancel</button>
            <button class="btn-save" id="create-category-btn" onclick="saveCategory()">Create</button>
        </div>
    </div>
</div>

<div class="toast" id="toast"></div>

<script>
// ── State ─────────────────────────────────────────────
// Auth guard: redirect to login if the readable session cookie is absent
function _hasAuthCookie() {
    return document.cookie.split(";").some(c => c.trim().startsWith("logged_in="));
}
if (!_hasAuthCookie()) { _redirectToLogin(); }

let categories    = [];
let activeCatId   = null;
let editingId     = null;
let toastTimer    = null;
let currentUserRole = "";
let currentUserPermissions = new Set();

function hasPermission(permission) {
    return currentUserRole === "admin" || currentUserPermissions.has(permission);
}

function applyExpensePermissions() {
    const canCreate = hasPermission("action_expenses_create");
    const recordBtn = document.getElementById("record-expense-btn");
    const newCategoryBtn = document.getElementById("new-category-btn");
    const createCategoryBtn = document.getElementById("create-category-btn");
    if (recordBtn) recordBtn.style.display = canCreate ? "" : "none";
    if (newCategoryBtn) newCategoryBtn.style.display = canCreate ? "" : "none";
    if (createCategoryBtn) createCategoryBtn.style.display = canCreate ? "" : "none";
}

// ── Init ──────────────────────────────────────────────
async function initUser() {
    try {
        const r = await fetch("/auth/me");
        if (!r.ok) { _redirectToLogin(); return; }
        const u = await r.json();
        currentUserRole = u.role || "";
        currentUserPermissions = new Set(
            (typeof u.permissions === "string" ? u.permissions.split(",") : (u.permissions || []))
                .map(v => v.trim())
                .filter(Boolean)
        );
        const nameEl = document.getElementById("user-name");
        const avatarEl = document.getElementById("user-avatar");
        if (nameEl) nameEl.innerText = u.name;
        if (avatarEl) avatarEl.innerText = u.name.charAt(0).toUpperCase();
        applyExpensePermissions();
        return u;
    } catch(e) { _redirectToLogin(); }
}
function toggleMode() {
    if (window.__appTheme) {
        window.__appTheme.toggle();
        return;
    }
    const light = document.body.classList.toggle("light");
    localStorage.setItem("colorMode", light ? "light" : "dark");
    document.getElementById("mode-btn").innerHTML = light ? "&#9728;&#65039;" : "&#127769;";
}
async function logout() {
    await fetch("/auth/logout", { method: "POST" });
    window.location.href = "/";
}
if (window.__appTheme) {
    window.__appTheme.sync();
} else if (localStorage.getItem("colorMode") === "light") {
    document.body.classList.add("light");
    document.getElementById("mode-btn").innerHTML = "&#9728;&#65039;";
}

// Set default month filter to current month
const today = new Date();
document.getElementById("month-filter").value =
    today.getFullYear() + "-" + String(today.getMonth() + 1).padStart(2, "0");

// ── Toast ─────────────────────────────────────────────
function showToast(msg, type = "") {
    const el = document.getElementById("toast");
    el.textContent = msg;
    el.className   = "toast show" + (type ? " " + type : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
}

// ── Modal helpers ─────────────────────────────────────
function openModal(id)  { document.getElementById(id).classList.add("open"); }
function closeModal(id) { document.getElementById(id).classList.remove("open"); }

document.querySelectorAll(".modal-bg").forEach(bg => {
    bg.addEventListener("click", e => { if (e.target === bg) bg.classList.remove("open"); });
});

// ── Categories ────────────────────────────────────────
async function loadCategories() {
    try {
        categories = await (await fetch("/expenses/api/categories")).json();
        renderCategories();
    } catch(e) {
        showToast("Failed to load categories", "err");
    }
}

function renderCategories() {
    const body = document.getElementById("cat-list-body");
    if (!categories.length) {
        body.innerHTML = `<div style="padding:16px;text-align:center;color:var(--muted);font-size:12px">No categories yet</div>`;
        return;
    }
    body.innerHTML = categories.map(c => `
        <div class="cat-item ${activeCatId === c.id ? "active" : ""}" onclick="filterCategory(${c.id})">
            <div>
                <div class="cat-item-name">${c.name}</div>
                <div class="cat-item-code">${c.account_code}</div>
            </div>
            <div class="cat-item-total">${c.total > 0 ? c.total.toFixed(0) : "—"}</div>
        </div>
    `).join("");

    // populate modal select
    const sel = document.getElementById("m-category");
    const prev = sel.value;
    sel.innerHTML = categories.map(c =>
        `<option value="${c.id}">${c.name}</option>`
    ).join("");
    if (prev) sel.value = prev;
}

async function saveCategory() {
    if (!hasPermission("action_expenses_create")) {
        showToast("Permission denied: action_expenses_create", "err");
        return;
    }
    const name = document.getElementById("cm-name").value.trim();
    const desc = document.getElementById("cm-desc").value.trim();
    if (!name) { showToast("Category name is required", "err"); return; }
    try {
        const res  = await fetch("/expenses/api/categories", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name, description: desc }),
        });
        const data = await res.json();
        if (data.detail) { showToast(data.detail, "err"); return; }
        showToast(`✓ "${data.name}" created (code ${data.account_code})`, "ok");
        closeModal("cat-modal");
        document.getElementById("cm-name").value = "";
        document.getElementById("cm-desc").value = "";
        await loadCategories();
    } catch(e) {
        showToast("Failed to create category", "err");
    }
}

function openCatModal() {
    if (!hasPermission("action_expenses_create")) {
        showToast("Permission denied: action_expenses_create", "err");
        return;
    }
    openModal("cat-modal");
}
function filterCategory(id) {
    activeCatId = id;
    // update active state
    document.getElementById("cat-all").classList.toggle("active", id === null);
    renderCategories();
    document.getElementById("main-title").innerText = id === null
        ? "All Expenses"
        : (categories.find(c => c.id === id)?.name || "Expenses");
    loadExpenses();
}

// ── Summary ───────────────────────────────────────────
async function loadSummary() {
    try {
        const d = await (await fetch("/expenses/api/summary")).json();
        document.getElementById("stat-this-month").innerText = d.this_month.toFixed(2);
        document.getElementById("stat-last-month").innerText = d.last_month.toFixed(2);
        document.getElementById("stat-all-time").innerText   = d.total_all.toFixed(2);
        document.getElementById("cat-all-total").innerText   = d.this_month.toFixed(0);
        if (d.breakdown.length) {
            document.getElementById("stat-top-cat").innerText        = d.breakdown[0].name;
            document.getElementById("stat-top-cat-amount").innerText = d.breakdown[0].total.toFixed(2) + " EGP this month";
        } else {
            document.getElementById("stat-top-cat").innerText        = "—";
            document.getElementById("stat-top-cat-amount").innerText = "";
        }
    } catch(e) {}
}

// ── Expenses ──────────────────────────────────────────
async function loadExpenses() {
    const month  = document.getElementById("month-filter").value;
    let url = "/expenses/api/list?";
    if (activeCatId) url += `category_id=${activeCatId}&`;
    if (month)       url += `month=${month}`;
    const tbody = document.getElementById("exp-tbody");
    tbody.innerHTML = `<tr class="empty-row"><td colspan="7">Loading…</td></tr>`;
    try {
        const data = await (await fetch(url)).json();
        if (!data.length) {
            tbody.innerHTML = `<tr class="empty-row"><td colspan="7">No expenses found</td></tr>`;
            return;
        }
        tbody.innerHTML = data.map(e => `
            <tr>
                <td><div class="exp-ref">${e.ref_number}</div></td>
                <td>
                    <div class="exp-cat">${e.category}</div>
                    <div class="exp-vendor">${e.vendor || ""}${e.farm_name ? ` <span style="font-size:10px;padding:1px 7px;border-radius:10px;background:rgba(132,204,22,.1);color:#84cc16;font-weight:700">${e.farm_name}</span>` : ""}</div>
                </td>
                <td><div class="exp-date">${e.expense_date}</div></td>
                <td><div class="exp-vendor">${e.vendor || "—"}</div></td>
                <td><span class="method-pill method-${e.payment_method}">${e.payment_method.replace("_"," ")}</span></td>
                <td><div class="exp-amount">${e.amount.toFixed(2)}</div></td>
                <td>
                    ${hasPermission("action_expenses_update") ? `<button class="action-btn" onclick="openEditModal(${JSON.stringify(e).replace(/"/g,'&quot;')})">Edit</button>` : ""}
                    ${hasPermission("action_expenses_delete") ? `<button class="action-btn del" onclick="deleteExpense(${e.id}, '${e.ref_number}')">Delete</button>` : ""}
                </td>
            </tr>
        `).join("");
    } catch(err) {
        tbody.innerHTML = `<tr class="empty-row"><td colspan="7">Failed to load</td></tr>`;
    }
}

function clearFilter() {
    document.getElementById("month-filter").value = "";
    loadExpenses();
}

// ── Add / Edit ────────────────────────────────────────
function openAddModal() {
    if (!hasPermission("action_expenses_create")) {
        showToast("Permission denied: action_expenses_create", "err");
        return;
    }
    editingId = null;
    document.getElementById("modal-title").innerText = "Record Expense";
    document.getElementById("modal-sub").innerText   = "Fill in the details below.";
    document.getElementById("modal-save-btn").innerText = "Save Expense";
    document.getElementById("m-amount").value   = "";
    document.getElementById("m-date").value     = new Date().toISOString().slice(0,10);
    document.getElementById("m-method").value   = "cash";
    document.getElementById("m-vendor").value   = "";
    document.getElementById("m-notes").value    = "";
    document.getElementById("m-farm").value     = "";
    if (activeCatId) document.getElementById("m-category").value = activeCatId;
    openModal("expense-modal");
}

function openEditModal(e) {
    if (!hasPermission("action_expenses_update")) {
        showToast("Permission denied: action_expenses_update", "err");
        return;
    }
    editingId = e.id;
    document.getElementById("modal-title").innerText = "Edit Expense";
    document.getElementById("modal-sub").innerText   = e.ref_number;
    document.getElementById("modal-save-btn").innerText = "Update";
    document.getElementById("m-category").value = e.category_id;
    document.getElementById("m-amount").value   = e.amount;
    document.getElementById("m-date").value     = e.expense_date;
    document.getElementById("m-method").value   = e.payment_method;
    document.getElementById("m-vendor").value   = e.vendor || "";
    document.getElementById("m-notes").value    = e.description || "";
    document.getElementById("m-farm").value     = e.farm_id || "";
    openModal("expense-modal");
}

async function saveExpense() {
    const requiredPermission = editingId ? "action_expenses_update" : "action_expenses_create";
    if (!hasPermission(requiredPermission)) {
        showToast(`Permission denied: ${requiredPermission}`, "err");
        return;
    }
    const cat    = parseInt(document.getElementById("m-category").value);
    const amount = parseFloat(document.getElementById("m-amount").value);
    const date   = document.getElementById("m-date").value;
    const method = document.getElementById("m-method").value;
    const vendor = document.getElementById("m-vendor").value.trim();
    const notes  = document.getElementById("m-notes").value.trim();

    if (!cat)   { showToast("Select a category", "err"); return; }
    if (!amount || amount <= 0) { showToast("Enter a valid amount", "err"); return; }
    if (!date)  { showToast("Select a date", "err"); return; }

    const btn = document.getElementById("modal-save-btn");
    btn.disabled = true;

    const farmId = parseInt(document.getElementById("m-farm").value) || null;
    const body = {
        category_id: cat, amount, expense_date: date,
        payment_method: method,
        vendor:      vendor || null,
        description: notes  || null,
        farm_id:     farmId,
    };

    try {
        const url    = editingId ? `/expenses/api/edit/${editingId}` : "/expenses/api/add";
        const method2 = editingId ? "PUT" : "POST";
        const res    = await fetch(url, {
            method: method2,
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (data.detail) { showToast(data.detail, "err"); return; }
        showToast(editingId ? "Expense updated" : `✓ ${data.ref_number} recorded`, "ok");
        closeModal("expense-modal");
        await Promise.all([loadExpenses(), loadSummary(), loadCategories()]);
    } catch(e) {
        showToast("Failed to save", "err");
    } finally {
        btn.disabled = false;
    }
}

async function deleteExpense(id, ref) {
    if (!hasPermission("action_expenses_delete")) {
        showToast("Permission denied: action_expenses_delete", "err");
        return;
    }
    if (!confirm(`Delete ${ref}? This will reverse the journal entry.`)) return;
    try {
        const res  = await fetch(`/expenses/api/delete/${id}`, {
            method: "DELETE",
        });
        const data = await res.json();
        if (data.detail) { showToast(data.detail, "err"); return; }
        showToast(`${ref} deleted`, "ok");
        await Promise.all([loadExpenses(), loadSummary(), loadCategories()]);
    } catch(e) {
        showToast("Failed to delete", "err");
    }
}

// ── Farm dropdown for cost allocation ─────────────────
async function loadFarmsDropdown() {
    try {
        const farms = await (await fetch("/farm/api/farms")).json();
        const sel = document.getElementById("m-farm");
        sel.innerHTML = `<option value="">— General expense —</option>` +
            farms.map(f => `<option value="${f.id}">${f.name}</option>`).join("");
    } catch(e) { /* farm endpoint optional */ }
}

// ── Boot ──────────────────────────────────────────────
async function bootstrapExpensesPage() {
    await initUser();
    await Promise.all([loadCategories(), loadSummary(), loadExpenses(), loadFarmsDropdown()]);
}

bootstrapExpensesPage();
</script>
</body>
</html>"""
