"""
Rule-based dashboard briefing generation.

This module intentionally avoids any AI calls. It turns real database facts into
plain-language sentences plus a small ranked action list.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time_utils import now_local
from app.models.b2b import B2BClient, B2BInvoice, Consignment
from app.models.expense import Expense, ExpenseCategory
from app.models.invoice import Invoice, InvoiceItem
from app.models.product import Product
from app.models.refund import RetailRefund
from app.models.spoilage import SpoilageRecord
from app.models.user import User


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _egp(value: float) -> str:
    return f"EGP {round(_safe_float(value), 2):,.0f}"


async def _utc_range_for(local_start: date, local_end: date) -> tuple[datetime, datetime]:
    from app.services.dashboard_summary_service import _utc_range

    return _utc_range(local_start, local_end)


def _comparison_phrase(current: float, prior: float, label: str = "average") -> str:
    if current <= 0 and prior <= 0:
        return ""
    if abs(prior) < 0.0001:
        return ""
    delta = round(((current - prior) / abs(prior)) * 100)
    if abs(delta) <= 3:
        return f" - in line with your {label}"
    if delta > 0:
        return f" - {abs(delta)}% above your {label}"
    return f" - {abs(delta)}% below your {label}"


async def _sales_and_transactions(db: AsyncSession, utc_s: datetime, utc_e: datetime) -> tuple[float, int]:
    pos_total = await db.execute(
        select(func.coalesce(func.sum(Invoice.total), 0)).where(
            Invoice.created_at >= utc_s,
            Invoice.created_at <= utc_e,
            Invoice.status == "paid",
        )
    )
    refund_total = await db.execute(
        select(func.coalesce(func.sum(RetailRefund.total), 0)).where(
            RetailRefund.created_at >= utc_s,
            RetailRefund.created_at <= utc_e,
        )
    )
    b2b_total = await db.execute(
        select(func.coalesce(func.sum(B2BInvoice.total), 0)).where(
            B2BInvoice.created_at >= utc_s,
            B2BInvoice.created_at <= utc_e,
            B2BInvoice.status == "paid",
        )
    )
    pos_count = await db.execute(
        select(func.count(Invoice.id)).where(
            Invoice.created_at >= utc_s,
            Invoice.created_at <= utc_e,
            Invoice.status == "paid",
        )
    )
    b2b_count = await db.execute(
        select(func.count(B2BInvoice.id)).where(
            B2BInvoice.created_at >= utc_s,
            B2BInvoice.created_at <= utc_e,
            B2BInvoice.status == "paid",
        )
    )
    sales = max(0.0, _safe_float(pos_total.scalar()) - _safe_float(refund_total.scalar())) + _safe_float(b2b_total.scalar())
    txns = _safe_int(pos_count.scalar()) + _safe_int(b2b_count.scalar())
    return round(sales, 2), txns


async def _today_weekday_average(db: AsyncSession, end_local_date: date) -> float:
    weekday = end_local_date.weekday()
    current = end_local_date - timedelta(days=1)
    matches: list[float] = []
    while current >= end_local_date - timedelta(days=35) and len(matches) < 4:
        if current.weekday() == weekday:
            utc_s, utc_e = await _utc_range_for(current, current)
            sales, _ = await _sales_and_transactions(db, utc_s, utc_e)
            matches.append(sales)
        current -= timedelta(days=1)
    return round(sum(matches) / len(matches), 2) if matches else 0.0


async def _prior_period_total(db: AsyncSession, range_label: str, start_date: date, end_date: date) -> float:
    span = (end_date - start_date).days
    if range_label == "7d":
        prior_start = start_date - timedelta(days=7)
        prior_end = start_date - timedelta(days=1)
    elif range_label in {"mtd", "month"}:
        prior_end = start_date - timedelta(days=1)
        prior_start = prior_end.replace(day=1)
        prior_end = min(prior_end, prior_start + timedelta(days=span))
    elif range_label in {"year", "ytd"}:
        prior_start = start_date.replace(year=start_date.year - 1)
        prior_end = end_date.replace(year=end_date.year - 1)
    else:
        prior_end = start_date - timedelta(days=1)
        prior_start = prior_end - timedelta(days=span)
    utc_s, utc_e = await _utc_range_for(prior_start, prior_end)
    total, _ = await _sales_and_transactions(db, utc_s, utc_e)
    return total


async def build_lead_sentence(
    db: AsyncSession,
    range_label: str,
    start_dt: datetime,
    end_dt: datetime,
) -> str:
    local_end = end_dt.astimezone(now_local().tzinfo).date()
    local_start = start_dt.astimezone(now_local().tzinfo).date()
    sales, txns = await _sales_and_transactions(db, start_dt, end_dt)
    if sales <= 0 and txns <= 0:
        return "You haven't recorded any sales yet for this period."

    if range_label == "today":
        avg = await _today_weekday_average(db, local_end)
        weekday_name = local_end.strftime("%A")
        comparison = _comparison_phrase(sales, avg, f"{weekday_name} average")
        return f"Today so far, you've made {_egp(sales)} across {txns} transactions{comparison}."

    if range_label == "7d":
        prior = await _prior_period_total(db, range_label, local_start, local_end)
        comparison = _comparison_phrase(sales, prior, "average")
        return f"This week you've made {_egp(sales)} across {txns} transactions{comparison}."

    if range_label in {"mtd", "month"}:
        prior = await _prior_period_total(db, range_label, local_start, local_end)
        comparison = _comparison_phrase(sales, prior, "average")
        return f"So far this month, {_egp(sales)} from {txns} transactions{comparison}."

    if range_label in {"year", "ytd"}:
        prior = await _prior_period_total(db, range_label, local_start, local_end)
        comparison = _comparison_phrase(sales, prior, "average")
        return f"This year, {_egp(sales)} from {txns} transactions{comparison}."

    prior = await _prior_period_total(db, range_label, local_start, local_end)
    comparison = _comparison_phrase(sales, prior, "average")
    return f"For this period, you've made {_egp(sales)} across {txns} transactions{comparison}."


async def detect_overdue_b2b(db: AsyncSession, *, today: date) -> dict[str, Any] | None:
    result = await db.execute(
        select(
            B2BInvoice.id,
            B2BInvoice.client_id,
            B2BClient.name,
            B2BInvoice.total,
            B2BInvoice.amount_paid,
            B2BInvoice.created_at,
            B2BInvoice.due_date,
        )
        .join(B2BClient, B2BClient.id == B2BInvoice.client_id)
        .where(
            B2BInvoice.status.in_(["unpaid", "partial"]),
            B2BInvoice.invoice_type.in_(["full_payment", "consignment", "credit"]),
        )
        .order_by(B2BInvoice.created_at.asc())
    )
    best = None
    best_days = 0
    for row in result.all():
        base_date = row.due_date or (row.created_at.date() if row.created_at else today)
        days_old = (today - base_date).days
        if days_old > 30 and days_old > best_days:
            best = row
            best_days = days_old
    if not best:
        return None
    outstanding = _safe_float(best.total) - _safe_float(best.amount_paid)
    return {
        "priority": 100,
        "text": f"{best.name}'s invoice is {best_days} days overdue ({_egp(outstanding)}).",
        "link": f"/b2b/#client/{best.client_id}",
        "cta": "Collect",
    }


async def detect_out_of_stock_recent(db: AsyncSession, *, today: date) -> dict[str, Any] | None:
    start = today - timedelta(days=13)
    utc_s, utc_e = await _utc_range_for(start, today)
    result = await db.execute(
        select(Product.id, Product.name, Product.stock)
        .join(InvoiceItem, InvoiceItem.product_id == Product.id)
        .join(Invoice, Invoice.id == InvoiceItem.invoice_id)
        .where(
            Product.is_active == True,
            Product.stock <= 0,
            Invoice.created_at >= utc_s,
            Invoice.created_at <= utc_e,
            Invoice.status == "paid",
        )
        .group_by(Product.id, Product.name, Product.stock)
        .order_by(Product.name.asc())
        .limit(1)
    )
    row = result.one_or_none()
    if not row:
        return None
    return {
        "priority": 90,
        "text": f"{row.name} is out of stock but sold recently.",
        "link": "/inventory/?filter=out-of-stock",
        "cta": "Reorder",
    }


async def detect_low_stock(db: AsyncSession, *, today: date) -> dict[str, Any] | None:
    start = today - timedelta(days=13)
    utc_s, utc_e = await _utc_range_for(start, today)
    result = await db.execute(
        select(
            Product.id,
            Product.name,
            Product.stock,
            func.coalesce(func.sum(InvoiceItem.qty), 0).label("qty_sold"),
        )
        .join(InvoiceItem, InvoiceItem.product_id == Product.id)
        .join(Invoice, Invoice.id == InvoiceItem.invoice_id)
        .where(
            Product.is_active == True,
            Product.stock > 0,
            Product.stock <= 5,
            Invoice.created_at >= utc_s,
            Invoice.created_at <= utc_e,
            Invoice.status == "paid",
        )
        .group_by(Product.id, Product.name, Product.stock)
        .order_by(func.sum(InvoiceItem.qty).desc(), Product.name.asc())
        .limit(1)
    )
    row = result.one_or_none()
    if not row:
        return None
    avg_daily = round(_safe_float(row.qty_sold) / 14, 1)
    if avg_daily <= 0:
        return None
    return {
        "priority": 80,
        "text": f"{row.name} is down to {_safe_int(row.stock)} units - usually sells {avg_daily:g}/day.",
        "link": "/inventory/?filter=low-stock",
        "cta": "Reorder",
    }


async def detect_spoilage_spike(db: AsyncSession, *, today: date) -> dict[str, Any] | None:
    week_start = today - timedelta(days=today.weekday())
    last_week_start = week_start - timedelta(days=7)
    this_week_total = await db.execute(
        select(func.coalesce(func.sum(SpoilageRecord.qty), 0)).where(
            SpoilageRecord.spoilage_date >= week_start,
            SpoilageRecord.spoilage_date <= today,
        )
    )
    last_week_total = await db.execute(
        select(func.coalesce(func.sum(SpoilageRecord.qty), 0)).where(
            SpoilageRecord.spoilage_date >= last_week_start,
            SpoilageRecord.spoilage_date < week_start,
        )
    )
    current = _safe_float(this_week_total.scalar())
    prior = _safe_float(last_week_total.scalar())
    if prior <= 0 or current <= prior * 1.5:
        return None
    biggest = await db.execute(
        select(Product.name, func.coalesce(func.sum(SpoilageRecord.qty), 0).label("qty"))
        .join(Product, Product.id == SpoilageRecord.product_id)
        .where(SpoilageRecord.spoilage_date >= week_start, SpoilageRecord.spoilage_date <= today)
        .group_by(Product.name)
        .order_by(func.sum(SpoilageRecord.qty).desc())
        .limit(1)
    )
    row = biggest.one_or_none()
    change_pct = round(((current - prior) / prior) * 100)
    product_name = row.name if row else "one product"
    return {
        "priority": 70,
        "text": f"Spoilage up {change_pct}% this week - mostly {product_name}.",
        "link": "/reports/?tab=spoilage",
        "cta": "Review",
    }


async def detect_big_expense(db: AsyncSession, *, today: date) -> dict[str, Any] | None:
    current_start = today - timedelta(days=6)
    trailing_start = today - timedelta(days=89)
    current_rows = await db.execute(
        select(
            ExpenseCategory.name,
            Expense.category_id,
            func.coalesce(func.sum(Expense.amount), 0).label("total"),
        )
        .join(ExpenseCategory, ExpenseCategory.id == Expense.category_id)
        .where(Expense.expense_date >= current_start, Expense.expense_date <= today)
        .group_by(ExpenseCategory.name, Expense.category_id)
    )
    best = None
    best_ratio = 0.0
    for row in current_rows.all():
        historical = await db.execute(
            select(func.coalesce(func.sum(Expense.amount), 0)).where(
                Expense.category_id == row.category_id,
                Expense.expense_date >= trailing_start,
                Expense.expense_date < current_start,
            )
        )
        average = _safe_float(historical.scalar()) / 12.0
        current_total = _safe_float(row.total)
        if average > 0 and current_total > average * 1.5:
            ratio = current_total / average
            if ratio > best_ratio:
                best = (row.name, current_total, average)
                best_ratio = ratio
    if not best:
        return None
    category_name, current_total, average = best
    return {
        "priority": 60,
        "text": f"{category_name} expenses jumped to {_egp(current_total)} this week (usual: {_egp(average)}).",
        "link": "/expenses/",
        "cta": "Review",
    }


async def detect_stale_consignment(db: AsyncSession, *, today: date) -> dict[str, Any] | None:
    result = await db.execute(
        select(Consignment.id, Consignment.client_id, B2BClient.name, Consignment.created_at)
        .join(B2BClient, B2BClient.id == Consignment.client_id)
        .where(Consignment.status == "active", Consignment.settled_at.is_(None))
        .order_by(Consignment.created_at.asc())
    )
    for row in result.all():
        if not row.created_at:
            continue
        days_old = (today - row.created_at.date()).days
        if days_old > 30:
            return {
                "priority": 55,
                "text": f"{row.name} has a consignment untouched for {days_old} days.",
                "link": f"/b2b/#client/{row.client_id}",
                "cta": "Settle",
            }
    return None


async def detect_big_b2b_client(db: AsyncSession, *, today: date) -> dict[str, Any] | None:
    month_start = today.replace(day=1)
    utc_s, utc_e = await _utc_range_for(month_start, today)
    rows = await db.execute(
        select(
            B2BClient.id,
            B2BClient.name,
            func.count(B2BInvoice.id).label("order_count"),
            func.coalesce(func.sum(B2BInvoice.total), 0).label("month_total"),
        )
        .join(B2BInvoice, B2BInvoice.client_id == B2BClient.id)
        .where(B2BInvoice.created_at >= utc_s, B2BInvoice.created_at <= utc_e)
        .group_by(B2BClient.id, B2BClient.name)
        .order_by(func.coalesce(func.sum(B2BInvoice.total), 0).desc())
        .limit(5)
    )
    ranked = list(rows.all())
    for index, row in enumerate(ranked, start=1):
        if _safe_int(row.order_count) >= 3:
            return {
                "priority": 20,
                "text": f"{row.name} placed {_safe_int(row.order_count)} orders this month, now your #{index} B2B client.",
                "link": f"/b2b/#client/{row.id}",
                "cta": "View",
            }
    return None


async def build_briefing(
    db: AsyncSession,
    user: User,
    range_label: str,
    start_dt: datetime,
    end_dt: datetime,
) -> dict[str, Any]:
    from app.core.log import logger

    lead = await build_lead_sentence(db, range_label, start_dt, end_dt)
    today = end_dt.astimezone(now_local().tzinfo).date()
    rules = [
        ("overdue_b2b", detect_overdue_b2b),
        ("out_of_stock", detect_out_of_stock_recent),
        ("low_stock", detect_low_stock),
        ("spoilage_spike", detect_spoilage_spike),
        ("big_expense", detect_big_expense),
        ("stale_consignment", detect_stale_consignment),
        ("big_b2b_client", detect_big_b2b_client),
    ]

    actions: list[dict[str, Any]] = []
    for rule_name, rule in rules:
        try:
            action = await rule(db, today=today)
            if action:
                actions.append(action)
        except Exception:
            logger.error("dashboard_briefing: rule failed", extra={"rule": rule_name}, exc_info=True)
            try:
                await db.rollback()
            except Exception:
                pass

    actions.sort(key=lambda action: action.get("priority", 0), reverse=True)
    trimmed = [{"text": action["text"], "link": action["link"], "cta": action["cta"]} for action in actions[:4]]

    if not trimmed:
        return {
            "lead": lead,
            "actions": [],
            "body": "Everything looks healthy - no urgent action needed.",
        }

    return {
        "lead": lead,
        "actions": trimmed,
        "body": f"{len(trimmed)} things need your attention.",
    }


__all__ = [
    "build_briefing",
    "build_lead_sentence",
    "detect_big_b2b_client",
    "detect_big_expense",
    "detect_low_stock",
    "detect_out_of_stock_recent",
    "detect_overdue_b2b",
    "detect_spoilage_spike",
    "detect_stale_consignment",
]
