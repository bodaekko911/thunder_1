"""
"What changed" insight detection for GET /dashboard/insights.
Each rule returns an InsightCard dict or None.
Only cards whose condition is truly met are returned (max 5, ranked by |z_score|).
"""
from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta
from typing import TypedDict

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.accounting import Account, Journal, JournalEntry
from app.models.b2b import B2BClient, B2BInvoice, Consignment
from app.models.customer import Customer
from app.models.expense import Expense, ExpenseCategory
from app.models.invoice import Invoice, InvoiceItem
from app.models.product import Product
from app.models.refund import RetailRefund
from app.models.spoilage import SpoilageRecord
from app.models.user import User

_B2B_REF_TYPES = ("b2b", "b2b_invoice", "consignment_payment", "consignment")


class InsightCard(TypedDict):
    id:                 str
    icon:               str
    text:               str
    sparkline:          list[float]
    suggested_question: str
    action_url:         str | None
    z_score:            float


def _tz():
    from zoneinfo import ZoneInfo
    return ZoneInfo(settings.APP_TIMEZONE)


def _utc_range(local_start: date, local_end: date):
    from zoneinfo import ZoneInfo
    tz  = _tz()
    utc = ZoneInfo("UTC")
    s = datetime(local_start.year, local_start.month, local_start.day, 0, 0, 0, tzinfo=tz).astimezone(utc)
    e = datetime(local_end.year,   local_end.month,   local_end.day,   23, 59, 59, 999999, tzinfo=tz).astimezone(utc)
    return s, e


def _z(value: float, mean: float, std: float) -> float:
    if std == 0:
        return 0.0
    return (value - mean) / std


# ── rule helpers ───────────────────────────────────────────────────────────

async def _daily_pos_b2b(
    db: AsyncSession,
    utc_s: datetime,
    utc_e: datetime,
    acc_id: int | None,
) -> list[float]:
    tz_name = settings.APP_TIMEZONE
    from zoneinfo import ZoneInfo
    tz = _tz()

    pos_r = await db.execute(
        select(
            func.date(func.timezone(tz_name, Invoice.created_at)).label("day"),
            func.sum(Invoice.total).label("t"),
        )
        .where(Invoice.created_at >= utc_s, Invoice.created_at <= utc_e, Invoice.status == "paid")
        .group_by(func.date(func.timezone(tz_name, Invoice.created_at)))
    )
    pos_by = {str(r.day): float(r.t) for r in pos_r}

    ref_r = await db.execute(
        select(
            func.date(func.timezone(tz_name, RetailRefund.created_at)).label("day"),
            func.sum(RetailRefund.total).label("t"),
        )
        .where(RetailRefund.created_at >= utc_s, RetailRefund.created_at <= utc_e)
        .group_by(func.date(func.timezone(tz_name, RetailRefund.created_at)))
    )
    ref_by = {str(r.day): float(r.t) for r in ref_r}

    b2b_by: dict[str, float] = {}
    if acc_id:
        b2b_r = await db.execute(
            select(
                func.date(func.timezone(tz_name, Journal.created_at)).label("day"),
                func.sum(JournalEntry.credit).label("t"),
            )
            .join(JournalEntry, JournalEntry.journal_id == Journal.id)
            .where(
                JournalEntry.account_id == acc_id,
                Journal.created_at >= utc_s,
                Journal.created_at <= utc_e,
                Journal.ref_type.in_(_B2B_REF_TYPES),
            )
            .group_by(func.date(func.timezone(tz_name, Journal.created_at)))
        )
        b2b_by = {str(r.day): float(r.t) for r in b2b_r}

    ls = utc_s.astimezone(tz).date()
    le = utc_e.astimezone(tz).date()
    result, d = [], ls
    while d <= le:
        ds  = str(d)
        pos = max(0.0, pos_by.get(ds, 0.0) - ref_by.get(ds, 0.0))
        result.append(round(pos + b2b_by.get(ds, 0.0), 2))
        d += timedelta(days=1)
    return result


# ── individual rules ───────────────────────────────────────────────────────

async def _rule_revenue_anomaly(db: AsyncSession, today: date, acc_id: int | None) -> InsightCard | None:
    utc_s14, utc_e14 = _utc_range(today - timedelta(days=14), today - timedelta(days=1))
    history = await _daily_pos_b2b(db, utc_s14, utc_e14, acc_id)
    if len(history) < 3:
        return None
    mean = statistics.mean(history)
    std  = statistics.stdev(history) if len(history) > 1 else 0.0

    utc_s, utc_e = _utc_range(today, today)
    today_vals = await _daily_pos_b2b(db, utc_s, utc_e, acc_id)
    today_rev  = today_vals[0] if today_vals else 0.0

    if std == 0 or abs(today_rev - mean) <= 1.5 * std:
        return None

    z = _z(today_rev, mean, std)
    direction = "up" if today_rev > mean else "down"
    pct       = abs(round((today_rev - mean) / mean * 100, 1)) if mean > 0 else 0.0
    word      = "above" if direction == "up" else "below"

    return InsightCard(
        id="revenue_anomaly",
        icon=direction,
        text=f"Today's revenue is **{direction} {pct}%** vs your 14-day average.",
        sparkline=history + [today_rev],
        suggested_question="show me sales by day for the last 14 days",
        action_url=None,
        z_score=abs(z),
    )


async def _rule_margin_shift(db: AsyncSession, today: date, acc_id: int | None) -> InsightCard | None:
    week_start      = today - timedelta(days=today.weekday())
    last_week_start = week_start - timedelta(days=7)
    last_week_end   = week_start - timedelta(days=1)

    async def _margin(utc_s, utc_e):
        r = await db.execute(
            select(func.sum(Invoice.total))
            .where(Invoice.created_at >= utc_s, Invoice.created_at <= utc_e, Invoice.status == "paid")
        )
        rev  = float(r.scalar() or 0)
        cogs_r = await db.execute(
            select(func.sum(InvoiceItem.qty * Product.cost))
            .join(Invoice, InvoiceItem.invoice_id == Invoice.id)
            .join(Product,  InvoiceItem.product_id == Product.id)
            .where(Invoice.created_at >= utc_s, Invoice.created_at <= utc_e, Invoice.status == "paid")
        )
        cogs = float(cogs_r.scalar() or 0)
        b2b  = await _b2b_rev(db, utc_s, utc_e, acc_id)
        total_rev = rev + b2b
        return (total_rev - cogs) / total_rev * 100 if total_rev > 0 else 0.0

    utc_s_tw, utc_e_tw = _utc_range(week_start, today)
    utc_s_lw, utc_e_lw = _utc_range(last_week_start, last_week_end)
    margin_this = await _margin(utc_s_tw, utc_e_tw)
    margin_last = await _margin(utc_s_lw, utc_e_lw)
    delta = margin_this - margin_last

    if abs(delta) <= 3:
        return None

    direction = "up" if delta > 0 else "down"
    word      = "improved" if delta > 0 else "dropped"
    return InsightCard(
        id="margin_shift",
        icon=direction,
        text=f"Gross margin **{word} {abs(round(delta, 1))} pp** vs last week ({round(margin_last, 1)}% → {round(margin_this, 1)}%).",
        sparkline=[],
        suggested_question="show me gross margin this week vs last week",
        action_url="/reports/",
        z_score=abs(delta / 3),
    )


async def _b2b_rev(db: AsyncSession, utc_s, utc_e, acc_id: int | None) -> float:
    if not acc_id:
        return 0.0
    r = await db.execute(
        select(func.sum(JournalEntry.credit))
        .join(Journal, JournalEntry.journal_id == Journal.id)
        .where(
            JournalEntry.account_id == acc_id,
            Journal.created_at >= utc_s,
            Journal.created_at <= utc_e,
            Journal.ref_type.in_(_B2B_REF_TYPES),
        )
    )
    return float(r.scalar() or 0)


async def _rule_top_product_surge(db: AsyncSession, today: date) -> InsightCard | None:
    week_start  = today - timedelta(days=today.weekday())
    utc_s_tw, utc_e_tw = _utc_range(week_start, today)

    this_week_r = await db.execute(
        select(InvoiceItem.product_id, InvoiceItem.name, func.sum(InvoiceItem.qty).label("qty"))
        .join(Invoice, InvoiceItem.invoice_id == Invoice.id)
        .where(Invoice.created_at >= utc_s_tw, Invoice.created_at <= utc_e_tw, Invoice.status == "paid")
        .group_by(InvoiceItem.product_id, InvoiceItem.name)
        .order_by(func.sum(InvoiceItem.qty).desc())
        .limit(20)
    )
    this_week = {r.product_id: (r.name, float(r.qty)) for r in this_week_r}
    if not this_week:
        return None

    # 4-week trailing average (excluding current week)
    w4_start = week_start - timedelta(weeks=4)
    utc_s_4w, utc_e_4w = _utc_range(w4_start, week_start - timedelta(days=1))
    prior_r = await db.execute(
        select(InvoiceItem.product_id, func.sum(InvoiceItem.qty).label("qty"))
        .join(Invoice, InvoiceItem.invoice_id == Invoice.id)
        .where(Invoice.created_at >= utc_s_4w, Invoice.created_at <= utc_e_4w, Invoice.status == "paid")
        .group_by(InvoiceItem.product_id)
    )
    prior_avg: dict[int, float] = {r.product_id: float(r.qty) / 4 for r in prior_r}

    best_name, best_ratio, best_qty = None, 0.0, 0.0
    for pid, (name, qty) in this_week.items():
        avg = prior_avg.get(pid, 0.0)
        if avg > 0 and qty / avg >= 2.0:
            ratio = qty / avg
            if ratio > best_ratio:
                best_ratio, best_name, best_qty = ratio, name, qty

    if not best_name:
        return None

    return InsightCard(
        id="top_product_surge",
        icon="up",
        text=f"**{best_name}** is selling {round(best_ratio, 1)}× faster this week.",
        sparkline=[],
        suggested_question=f"show product details for {best_name}",
        action_url="/products/",
        z_score=best_ratio - 1,
    )


async def _rule_unpaid_b2b_aging(db: AsyncSession, today: date) -> InsightCard | None:
    cutoff = today - timedelta(days=30)
    rows = await db.execute(
        select(
            B2BClient.name,
            func.sum(B2BInvoice.total - B2BInvoice.amount_paid).label("owed"),
            func.min(B2BInvoice.due_date).label("oldest_due"),
        )
        .join(B2BInvoice, B2BInvoice.client_id == B2BClient.id)
        .where(
            B2BInvoice.status.in_(["unpaid", "partial"]),
            B2BInvoice.due_date <= cutoff,
        )
        .group_by(B2BClient.name)
        .order_by(func.sum(B2BInvoice.total - B2BInvoice.amount_paid).desc())
        .limit(1)
    )
    row = rows.one_or_none()
    if not row:
        return None

    owed = float(row.owed)
    days_late = (today - row.oldest_due).days if row.oldest_due else 0

    return InsightCard(
        id="unpaid_b2b_aging",
        icon="warning",
        text=f"**{row.name}** has {round(owed):,.0f} EGP unpaid for {days_late} days.",
        sparkline=[],
        suggested_question=f"show outstanding invoices for {row.name}",
        action_url="/b2b/",
        z_score=days_late / 30,
    )


async def _rule_stockout_risk(db: AsyncSession, today: date) -> InsightCard | None:
    d14_s, d14_e = _utc_range(today - timedelta(days=13), today)
    sales_r = await db.execute(
        select(InvoiceItem.product_id, func.sum(InvoiceItem.qty).label("qty"))
        .join(Invoice, InvoiceItem.invoice_id == Invoice.id)
        .where(Invoice.created_at >= d14_s, Invoice.created_at <= d14_e, Invoice.status == "paid")
        .group_by(InvoiceItem.product_id)
    )
    avg_daily = {r.product_id: float(r.qty) / 14.0 for r in sales_r}

    prod_r = await db.execute(
        select(Product.id, Product.name, Product.stock)
        .where(Product.is_active == True, Product.stock > 0)
    )
    best_name, best_days = None, float("inf")
    for p in prod_r:
        ad = avg_daily.get(p.id, 0.0)
        if ad > 0:
            days_left = float(p.stock) / ad
            if days_left < 7 and days_left < best_days:
                best_days, best_name = days_left, p.name

    if not best_name:
        return None

    return InsightCard(
        id="stockout_risk",
        icon="warning",
        text=f"**{best_name}** will stock out in ~{round(best_days)} day(s) at current pace.",
        sparkline=[],
        suggested_question=f"show stock details for {best_name}",
        action_url="/inventory/",
        z_score=7 / max(best_days, 0.1),
    )


async def _rule_expense_spike(db: AsyncSession, today: date) -> InsightCard | None:
    month_start = today.replace(day=1)
    # 3-month trailing average per category
    m3_start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    m3_start = (m3_start - timedelta(days=1)).replace(day=1)
    m3_start = (m3_start - timedelta(days=1)).replace(day=1)

    this_m_r = await db.execute(
        select(ExpenseCategory.name, func.sum(Expense.amount).label("total"))
        .join(Expense, Expense.category_id == ExpenseCategory.id)
        .where(Expense.expense_date >= month_start, Expense.expense_date <= today)
        .group_by(ExpenseCategory.name)
    )
    this_month: dict[str, float] = {r.name: float(r.total) for r in this_m_r}

    prior_3m_r = await db.execute(
        select(ExpenseCategory.name, func.sum(Expense.amount).label("total"))
        .join(Expense, Expense.category_id == ExpenseCategory.id)
        .where(Expense.expense_date >= m3_start, Expense.expense_date < month_start)
        .group_by(ExpenseCategory.name)
    )
    prior_avg: dict[str, float] = {r.name: float(r.total) / 3.0 for r in prior_3m_r}

    best_cat, best_ratio = None, 0.0
    for cat, amt in this_month.items():
        avg = prior_avg.get(cat, 0.0)
        if avg > 0 and amt / avg >= 1.5:
            ratio = amt / avg
            if ratio > best_ratio:
                best_ratio, best_cat = ratio, cat

    if not best_cat:
        return None

    pct = round((best_ratio - 1) * 100, 1)
    return InsightCard(
        id="expense_spike",
        icon="warning",
        text=f"**{best_cat}** expenses jumped {pct}% this month.",
        sparkline=[],
        suggested_question=f"show {best_cat} expenses this month vs last 3 months",
        action_url="/expenses/",
        z_score=best_ratio - 1,
    )


async def _rule_spoilage_streak(db: AsyncSession, today: date) -> InsightCard | None:
    week_start      = today - timedelta(days=today.weekday())
    last_week_start = week_start - timedelta(days=7)
    last_week_end   = week_start - timedelta(days=1)

    r_this = await db.execute(
        select(func.sum(SpoilageRecord.qty))
        .where(SpoilageRecord.spoilage_date >= week_start, SpoilageRecord.spoilage_date <= today)
    )
    this_week = float(r_this.scalar() or 0)

    r_last = await db.execute(
        select(func.sum(SpoilageRecord.qty))
        .where(SpoilageRecord.spoilage_date >= last_week_start,
               SpoilageRecord.spoilage_date <= last_week_end)
    )
    last_week = float(r_last.scalar() or 0)

    if this_week <= last_week or last_week == 0:
        return None

    pct = round((this_week - last_week) / last_week * 100, 1)

    # Top product
    top_r = await db.execute(
        select(Product.name, func.sum(SpoilageRecord.qty).label("qty"))
        .join(Product, SpoilageRecord.product_id == Product.id)
        .where(SpoilageRecord.spoilage_date >= week_start, SpoilageRecord.spoilage_date <= today)
        .group_by(Product.name)
        .order_by(func.sum(SpoilageRecord.qty).desc())
        .limit(1)
    )
    top = top_r.one_or_none()
    top_name = top.name if top else "unknown product"

    return InsightCard(
        id="spoilage_streak",
        icon="warning",
        text=f"Spoilage up {pct}% — mostly **{top_name}**.",
        sparkline=[],
        suggested_question="show spoilage records this week",
        action_url=None,
        z_score=pct / 50,
    )


async def _rule_new_clients(db: AsyncSession, today: date) -> InsightCard | None:
    w7_s, w7_e = _utc_range(today - timedelta(days=6), today)
    r = await db.execute(
        select(func.count(B2BClient.id))
        .where(B2BClient.created_at >= w7_s, B2BClient.created_at <= w7_e)
    )
    n = int(r.scalar() or 0)
    if n < 3:
        return None

    return InsightCard(
        id="new_clients",
        icon="up",
        text=f"{n} new B2B client(s) onboarded this week.",
        sparkline=[],
        suggested_question="show new B2B clients this week",
        action_url="/b2b/",
        z_score=(n - 3) / 2 + 1,
    )


async def _rule_stale_consignment(db: AsyncSession, today: date) -> InsightCard | None:
    cutoff = today - timedelta(days=30)
    rows = await db.execute(
        select(B2BClient.name, Consignment.created_at)
        .join(B2BClient, Consignment.client_id == B2BClient.id)
        .where(Consignment.status == "active",
               func.date(Consignment.created_at) <= cutoff)
        .order_by(Consignment.created_at.asc())
        .limit(1)
    )
    row = rows.one_or_none()
    if not row:
        return None

    days = (today - row.created_at.date()).days
    return InsightCard(
        id="stale_consignment",
        icon="warning",
        text=f"**{row.name}** has a consignment untouched for {days} days.",
        sparkline=[],
        suggested_question=f"show active consignments for {row.name}",
        action_url="/b2b/",
        z_score=days / 30,
    )


# ── chips from current data ────────────────────────────────────────────────

async def _suggested_chips(db: AsyncSession, today: date, acc_id: int | None) -> list[str]:
    chips = [
        "Top customer this month",
        "Products I should reorder",
        "Compare this week to last week",
    ]

    # Conditionally show the "why did sales drop" chip
    utc_s_y, utc_e_y = _utc_range(today - timedelta(days=1), today - timedelta(days=1))
    utc_s14, utc_e14 = _utc_range(today - timedelta(days=14), today - timedelta(days=1))

    yesterday_r = await db.execute(
        select(func.sum(Invoice.total))
        .where(Invoice.created_at >= utc_s_y, Invoice.created_at <= utc_e_y, Invoice.status == "paid")
    )
    yesterday = float(yesterday_r.scalar() or 0)

    hist_r = await db.execute(
        select(func.avg(Invoice.total).label("avg"))
        .where(Invoice.created_at >= utc_s14, Invoice.created_at <= utc_e14, Invoice.status == "paid")
    )
    hist_avg = float(hist_r.scalar() or 0)

    if hist_avg > 0 and yesterday < hist_avg * 0.9:
        chips.insert(0, "Why did sales drop yesterday?")

    return chips


# ── main entry point ───────────────────────────────────────────────────────

async def get_insights(db: AsyncSession) -> dict:
    from app.core.log import logger
    from app.models.accounting import Account

    r = await db.execute(select(Account.id).where(Account.code == "4000"))
    acc_id = r.scalar_one_or_none()

    today = datetime.now(_tz()).date()

    # Rules must run sequentially — asyncio.gather on the same AsyncSession
    # causes concurrent asyncpg queries (InterfaceError) that corrupt session state.
    _rules: list[tuple[str, object]] = [
        ("revenue_anomaly",   _rule_revenue_anomaly(db, today, acc_id)),
        ("margin_shift",      _rule_margin_shift(db, today, acc_id)),
        ("top_product_surge", _rule_top_product_surge(db, today)),
        ("unpaid_b2b_aging",  _rule_unpaid_b2b_aging(db, today)),
        ("stockout_risk",     _rule_stockout_risk(db, today)),
        ("expense_spike",     _rule_expense_spike(db, today)),
        ("spoilage_streak",   _rule_spoilage_streak(db, today)),
        ("new_clients",       _rule_new_clients(db, today)),
        ("stale_consignment", _rule_stale_consignment(db, today)),
    ]

    _errors: list[dict] = []
    cards: list[InsightCard] = []

    for rule_name, coro in _rules:
        try:
            result = await coro
            if isinstance(result, dict):
                cards.append(result)
        except Exception:
            logger.error("insight rule '%s' failed", rule_name, exc_info=True)
            _errors.append({"rule": rule_name, "reason": "query failed"})

    cards.sort(key=lambda c: c["z_score"], reverse=True)
    cards = cards[:5]

    chips: list[str] = []
    try:
        chips = await _suggested_chips(db, today, acc_id)
    except Exception:
        logger.error("insights: suggested_chips failed", exc_info=True)

    return {"cards": cards, "suggested_chips": chips, "_errors": _errors}
