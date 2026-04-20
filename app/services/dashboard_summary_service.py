"""
Range-based aggregations for GET /dashboard/summary.

The summary payload is intentionally shaped for the dashboard UI:
- one briefing block
- four plain-language numbers
- one chart payload
- two panel payloads

Each major section is isolated so a single failed query does not break the whole
response.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.sqltypes import Date as SQLDate

from app.core.config import settings
from app.core.permissions import has_permission
from app.core.time_utils import now_local
from app.models.b2b import B2BInvoice
from app.models.customer import Customer
from app.models.expense import Expense
from app.models.invoice import Invoice, InvoiceItem
from app.models.product import Product
from app.models.refund import RetailRefund
from app.models.user import User


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.APP_TIMEZONE)


def _utc_range(local_start: date, local_end: date) -> tuple[datetime, datetime]:
    tz = _tz()
    utc = ZoneInfo("UTC")
    start = datetime(local_start.year, local_start.month, local_start.day, 0, 0, 0, tzinfo=tz).astimezone(utc)
    end = datetime(local_end.year, local_end.month, local_end.day, 23, 59, 59, 999999, tzinfo=tz).astimezone(utc)
    return start, end


def _local_bucket_expr(column, part: str = "day"):
    localized = func.timezone(settings.APP_TIMEZONE, column)
    return cast(func.date_trunc(part, localized), SQLDate)


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


def _period_label(range_param: str, rs: date, re: date) -> str:
    if range_param == "today":
        return "Today"
    if range_param == "7d":
        return "Last 7 days"
    if range_param == "30d":
        return "Last 30 days"
    if range_param in {"mtd", "month"}:
        return "This month"
    if range_param in {"year", "ytd"}:
        return "This year"
    if range_param == "custom":
        return f"{rs.isoformat()} to {re.isoformat()}"
    return "Today"


def resolve_range(
    range_param: str,
    custom_start: str | None = None,
    custom_end: str | None = None,
) -> dict[str, Any]:
    today = now_local().date()

    if range_param == "7d":
        rs, re = today - timedelta(days=6), today
    elif range_param == "30d":
        rs, re = today - timedelta(days=29), today
    elif range_param in {"mtd", "month"}:
        rs, re = today.replace(day=1), today
    elif range_param in {"year", "ytd"}:
        rs, re = today.replace(month=1, day=1), today
    elif range_param == "custom" and custom_start and custom_end:
        rs, re = date.fromisoformat(custom_start), date.fromisoformat(custom_end)
    else:
        rs, re = today, today

    label = _period_label(range_param, rs, re)
    num_days = (re - rs).days + 1

    if range_param == "7d":
        prior_start = rs - timedelta(days=7)
        prior_end = rs - timedelta(days=1)
    elif range_param in {"mtd", "month"}:
        prior_month_end = rs - timedelta(days=1)
        prior_month_start = prior_month_end.replace(day=1)
        span = (re - rs).days
        prior_start = prior_month_start
        prior_end = min(prior_month_end, prior_month_start + timedelta(days=span))
    elif range_param in {"year", "ytd"}:
        prior_start = rs.replace(year=rs.year - 1)
        prior_end = re.replace(year=re.year - 1)
    else:
        prior_end = rs - timedelta(days=1)
        prior_start = prior_end - timedelta(days=num_days - 1)

    utc_s, utc_e = _utc_range(rs, re)
    prior_utc_s, prior_utc_e = _utc_range(prior_start, prior_end)

    return {
        "key": range_param,
        "label": label,
        "start": rs.isoformat(),
        "end": re.isoformat(),
        "days": num_days,
        "prior_start": prior_start.isoformat(),
        "prior_end": prior_end.isoformat(),
        "utc_start": utc_s,
        "utc_end": utc_e,
        "prior_utc_start": prior_utc_s,
        "prior_utc_end": prior_utc_e,
    }


def _pick_granularity(rng: dict[str, Any]) -> str:
    if rng.get("key") in {"year", "ytd"}:
        return "month"
    days = rng["days"]
    if days <= 31:
        return "day"
    if days <= 180:
        return "week"
    return "month"


def _aggregate_buckets(daily: list[dict[str, Any]], granularity: str) -> list[dict[str, Any]]:
    if granularity == "day":
        return daily

    groups: dict[str, dict[str, Any]] = {}
    for bucket in daily:
        bucket_date = date.fromisoformat(bucket["date"])
        if granularity == "week":
            group_key = (bucket_date - timedelta(days=bucket_date.weekday())).isoformat()
        else:
            group_key = f"{bucket_date.year}-{bucket_date.month:02d}-01"

        if group_key not in groups:
            groups[group_key] = {"date": group_key, "pos": 0.0, "b2b": 0.0, "refunds": 0.0, "orders": 0}

        row = groups[group_key]
        row["pos"] = round(row["pos"] + _safe_float(bucket["pos"]), 2)
        row["b2b"] = round(row["b2b"] + _safe_float(bucket["b2b"]), 2)
        row["refunds"] = round(row["refunds"] + _safe_float(bucket["refunds"]), 2)
        row["orders"] += _safe_int(bucket["orders"])

    return list(groups.values())


def _delta_pct(current: float, prior: float) -> float | None:
    if abs(prior) < 0.0001:
        return None
    return round(((current - prior) / abs(prior)) * 100, 1)


def _delta_direction(delta_pct: float | None, *, higher_is_better: bool) -> str:
    if delta_pct is None or abs(delta_pct) <= 1:
        return "flat"
    if delta_pct > 0:
        return "up" if higher_is_better else "down"
    return "down" if higher_is_better else "up"


async def _sales_total(db: AsyncSession, utc_s: datetime, utc_e: datetime) -> float:
    pos_result = await db.execute(
        select(func.coalesce(func.sum(Invoice.total), 0)).where(
            Invoice.created_at >= utc_s,
            Invoice.created_at <= utc_e,
            Invoice.status == "paid",
        )
    )
    refund_result = await db.execute(
        select(func.coalesce(func.sum(RetailRefund.total), 0)).where(
            RetailRefund.created_at >= utc_s,
            RetailRefund.created_at <= utc_e,
        )
    )
    b2b_result = await db.execute(
        select(func.coalesce(func.sum(B2BInvoice.total), 0)).where(
            B2BInvoice.created_at >= utc_s,
            B2BInvoice.created_at <= utc_e,
            B2BInvoice.status == "paid",
        )
    )
    return round(
        max(0.0, _safe_float(pos_result.scalar()) - _safe_float(refund_result.scalar())) + _safe_float(b2b_result.scalar()),
        2,
    )


async def _sales_count(db: AsyncSession, utc_s: datetime, utc_e: datetime) -> int:
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
    return _safe_int(pos_count.scalar()) + _safe_int(b2b_count.scalar())


async def _expense_total(db: AsyncSession, utc_s: datetime, utc_e: datetime) -> float:
    result = await db.execute(
        select(func.coalesce(func.sum(Expense.amount), 0)).where(
            Expense.created_at >= utc_s,
            Expense.created_at <= utc_e,
        )
    )
    return round(_safe_float(result.scalar()), 2)


async def _sparkline_sales(db: AsyncSession, rng: dict[str, Any]) -> list[float]:
    daily = await _daily_sales_rows(db, rng["utc_start"], rng["utc_end"])
    return [round(_safe_float(row["pos"]) + _safe_float(row["b2b"]), 2) for row in daily[-14:]]


async def _sparkline_expenses(db: AsyncSession, rng: dict[str, Any]) -> list[float]:
    local_end = date.fromisoformat(rng["end"])
    start = local_end - timedelta(days=13)
    utc_s, utc_e = _utc_range(start, local_end)
    bucket_expr = _local_bucket_expr(Expense.created_at, "day")
    rows = await db.execute(
        select(
            bucket_expr.label("bucket_date"),
            func.coalesce(func.sum(Expense.amount), 0).label("amount"),
        )
        .where(Expense.created_at >= utc_s, Expense.created_at <= utc_e)
        .group_by(bucket_expr)
        .order_by(bucket_expr)
    )
    by_day = {str(row.bucket_date): _safe_float(row.amount) for row in rows}

    values: list[float] = []
    current = start
    while current <= local_end:
        values.append(round(by_day.get(current.isoformat(), 0.0), 2))
        current += timedelta(days=1)
    return values


async def _daily_sales_rows(db: AsyncSession, utc_s: datetime, utc_e: datetime) -> list[dict[str, Any]]:
    tz = _tz()

    pos_rows = await db.execute(
        select(
            _local_bucket_expr(Invoice.created_at, "day").label("bucket_date"),
            func.coalesce(func.sum(Invoice.total), 0).label("total"),
            func.count(Invoice.id).label("orders"),
        )
        .where(Invoice.created_at >= utc_s, Invoice.created_at <= utc_e, Invoice.status == "paid")
        .group_by(_local_bucket_expr(Invoice.created_at, "day"))
        .order_by(_local_bucket_expr(Invoice.created_at, "day"))
    )
    pos_by_day = {str(row.bucket_date): (_safe_float(row.total), _safe_int(row.orders)) for row in pos_rows}

    refund_rows = await db.execute(
        select(
            _local_bucket_expr(RetailRefund.created_at, "day").label("bucket_date"),
            func.coalesce(func.sum(RetailRefund.total), 0).label("total"),
            func.count(RetailRefund.id).label("orders"),
        )
        .where(RetailRefund.created_at >= utc_s, RetailRefund.created_at <= utc_e)
        .group_by(_local_bucket_expr(RetailRefund.created_at, "day"))
        .order_by(_local_bucket_expr(RetailRefund.created_at, "day"))
    )
    refund_by_day = {str(row.bucket_date): (_safe_float(row.total), _safe_int(row.orders)) for row in refund_rows}

    b2b_rows = await db.execute(
        select(
            _local_bucket_expr(B2BInvoice.created_at, "day").label("bucket_date"),
            func.coalesce(func.sum(B2BInvoice.total), 0).label("total"),
            func.count(B2BInvoice.id).label("orders"),
        )
        .where(B2BInvoice.created_at >= utc_s, B2BInvoice.created_at <= utc_e, B2BInvoice.status == "paid")
        .group_by(_local_bucket_expr(B2BInvoice.created_at, "day"))
        .order_by(_local_bucket_expr(B2BInvoice.created_at, "day"))
    )
    b2b_by_day = {str(row.bucket_date): (_safe_float(row.total), _safe_int(row.orders)) for row in b2b_rows}

    local_start = utc_s.astimezone(tz).date()
    local_end = utc_e.astimezone(tz).date()
    buckets: list[dict[str, Any]] = []
    current = local_start
    while current <= local_end:
        key = current.isoformat()
        pos_total, pos_orders = pos_by_day.get(key, (0.0, 0))
        refund_total, refund_orders = refund_by_day.get(key, (0.0, 0))
        b2b_total, b2b_orders = b2b_by_day.get(key, (0.0, 0))
        buckets.append(
            {
                "date": key,
                "pos": round(max(0.0, pos_total - refund_total), 2),
                "b2b": round(b2b_total, 2),
                "refunds": round(-refund_total, 2),
                "orders": pos_orders + refund_orders + b2b_orders,
            }
        )
        current += timedelta(days=1)
    return buckets


async def _build_numbers(db: AsyncSession, rng: dict[str, Any], user: User) -> dict[str, Any]:
    can_view_b2b = has_permission(user, "page_b2b")
    can_view_pos = has_permission(user, "page_pos")

    sales_value = await _sales_total(db, rng["utc_start"], rng["utc_end"])
    sales_prior = await _sales_total(db, rng["prior_utc_start"], rng["prior_utc_end"])
    spent_value = await _expense_total(db, rng["utc_start"], rng["utc_end"])
    spent_prior = await _expense_total(db, rng["prior_utc_start"], rng["prior_utc_end"])

    clients_owe_result = await db.execute(
        select(
            func.coalesce(func.sum(B2BInvoice.total - func.coalesce(B2BInvoice.amount_paid, 0)), 0).label("value"),
        ).where(B2BInvoice.status.in_(["unpaid", "partial"]))
    )
    overdue_cutoff = now_local().astimezone(ZoneInfo("UTC")) - timedelta(days=30)
    overdue_result = await db.execute(
        select(
            func.coalesce(
                func.sum(case((B2BInvoice.created_at <= overdue_cutoff, 1), else_=0)),
                0,
            )
        ).where(B2BInvoice.status.in_(["unpaid", "partial"]))
    )
    clients_owe_value = _safe_float(clients_owe_result.scalar())
    overdue_count = _safe_int(overdue_result.scalar())

    out_result = await db.execute(
        select(func.count(Product.id)).where(Product.is_active == True, Product.stock <= 0)
    )
    low_result = await db.execute(
        select(func.count(Product.id)).where(Product.is_active == True, Product.stock > 0, Product.stock <= 5)
    )
    out_count = _safe_int(out_result.scalar())
    low_count = _safe_int(low_result.scalar())

    sales_today_value = 0.0
    if not can_view_b2b and can_view_pos:
        today = now_local().date()
        utc_s, utc_e = _utc_range(today, today)
        user_sales = await db.execute(
            select(func.coalesce(func.sum(Invoice.total), 0)).where(
                Invoice.user_id == user.id,
                Invoice.created_at >= utc_s,
                Invoice.created_at <= utc_e,
                Invoice.status == "paid",
            )
        )
        sales_today_value = _safe_float(user_sales.scalar())

    return {
        "sales": {
            "value": round(sales_value, 2),
            "delta_pct": _delta_pct(sales_value, sales_prior),
            "direction": _delta_direction(_delta_pct(sales_value, sales_prior), higher_is_better=True),
            "sparkline": await _sparkline_sales(db, rng),
        },
        "clients_owe": {
            "value": round(clients_owe_value, 2),
            "overdue_count": overdue_count,
        },
        "spent": {
            "value": round(spent_value, 2),
            "delta_pct": _delta_pct(spent_value, spent_prior),
            "direction": _delta_direction(_delta_pct(spent_value, spent_prior), higher_is_better=False),
            "sparkline": await _sparkline_expenses(db, rng),
        },
        "stock_alerts": {
            "value": out_count + low_count,
            "out_count": out_count,
            "low_count": low_count,
        },
        "alt_sales_today": {"value": round(sales_today_value, 2)},
    }


async def _build_chart(db: AsyncSession, rng: dict[str, Any]) -> dict[str, Any]:
    daily = await _daily_sales_rows(db, rng["utc_start"], rng["utc_end"])
    granularity = _pick_granularity(rng)
    return {"buckets": _aggregate_buckets(daily, granularity)}


async def _top_products(db: AsyncSession, rng: dict[str, Any], metric: str) -> list[dict[str, Any]]:
    order_expr = func.sum(InvoiceItem.total).desc() if metric == "revenue" else func.sum(InvoiceItem.qty).desc()
    rows = await db.execute(
        select(
            InvoiceItem.name.label("name"),
            func.coalesce(func.sum(InvoiceItem.qty), 0).label("qty"),
            func.coalesce(func.sum(InvoiceItem.total), 0).label("revenue"),
        )
        .join(Invoice, InvoiceItem.invoice_id == Invoice.id)
        .where(
            Invoice.created_at >= rng["utc_start"],
            Invoice.created_at <= rng["utc_end"],
            Invoice.status == "paid",
        )
        .group_by(InvoiceItem.name)
        .order_by(order_expr, InvoiceItem.name.asc())
        .limit(8)
    )
    return [
        {
            "name": row.name,
            "qty": round(_safe_float(row.qty), 2),
            "revenue": round(_safe_float(row.revenue), 2),
        }
        for row in rows.all()
    ]


def _relative_time(iso_timestamp: str) -> str:
    if not iso_timestamp:
        return "-"
    timestamp = datetime.fromisoformat(iso_timestamp)
    delta = now_local().astimezone(timestamp.tzinfo or _tz()) - timestamp
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return f"{seconds} sec ago"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} hr ago"
    return f"{seconds // 86400} day ago" if seconds < 172800 else f"{seconds // 86400} days ago"


async def _recent_activity(db: AsyncSession, rng: dict[str, Any]) -> list[dict[str, Any]]:
    invoice_result = await db.execute(
        select(
            Invoice.id,
            Invoice.invoice_number,
            Invoice.customer_id,
            Invoice.total,
            Invoice.payment_method,
            Invoice.created_at,
        )
        .where(Invoice.created_at >= rng["utc_start"], Invoice.created_at <= rng["utc_end"], Invoice.status == "paid")
        .order_by(Invoice.created_at.desc())
        .limit(10)
    )
    refund_result = await db.execute(
        select(
            RetailRefund.id,
            RetailRefund.refund_number,
            RetailRefund.customer_id,
            RetailRefund.total,
            RetailRefund.refund_method,
            RetailRefund.created_at,
            RetailRefund.invoice_id,
        )
        .where(RetailRefund.created_at >= rng["utc_start"], RetailRefund.created_at <= rng["utc_end"])
        .order_by(RetailRefund.created_at.desc())
        .limit(10)
    )
    invoice_rows = invoice_result.all()
    refund_rows = refund_result.all()

    customer_ids = {
        row.customer_id
        for row in invoice_rows + refund_rows
        if getattr(row, "customer_id", None) is not None
    }

    customer_map: dict[int, str] = {}
    if customer_ids:
        customer_names = await db.execute(select(Customer.id, Customer.name).where(Customer.id.in_(customer_ids)))
        customer_map = {row.id: row.name for row in customer_names.all()}

    activity: list[dict[str, Any]] = []
    for row in invoice_rows:
        iso_value = row.created_at.astimezone(_tz()).isoformat() if row.created_at else ""
        activity.append(
            {
                "type": "sale",
                "invoice_id": row.id,
                "invoice_number": row.invoice_number,
                "customer": customer_map.get(row.customer_id, "Walk-in"),
                "total": round(_safe_float(row.total), 2),
                "method": row.payment_method or "cash",
                "time_relative": _relative_time(iso_value),
                "timestamp": iso_value,
                "link": f"/pos/?invoice={row.id}",
            }
        )
    for row in refund_rows:
        iso_value = row.created_at.astimezone(_tz()).isoformat() if row.created_at else ""
        activity.append(
            {
                "type": "refund",
                "invoice_id": row.invoice_id,
                "invoice_number": row.refund_number,
                "customer": customer_map.get(row.customer_id, "-"),
                "total": round(-_safe_float(row.total), 2),
                "method": row.refund_method or "cash",
                "time_relative": _relative_time(iso_value),
                "timestamp": iso_value,
                "link": f"/refunds/?invoice={row.invoice_id}" if row.invoice_id else "/refunds/",
            }
        )
    activity.sort(key=lambda item: item["timestamp"], reverse=True)
    return activity[:10]


async def _build_panels(db: AsyncSession, rng: dict[str, Any]) -> dict[str, Any]:
    return {
        "top_products_by_revenue": await _top_products(db, rng, "revenue"),
        "top_products_by_qty": await _top_products(db, rng, "qty"),
        "recent_activity": await _recent_activity(db, rng),
    }


async def get_summary(
    db: AsyncSession,
    range_param: str,
    custom_start: str | None,
    custom_end: str | None,
    user: User,
) -> dict[str, Any]:
    from app.core.log import logger
    from app.services.dashboard_briefing_service import build_briefing

    rng = resolve_range(range_param, custom_start, custom_end)
    _errors: list[dict[str, str]] = []

    briefing: dict[str, Any] = {"lead": "You haven't recorded any sales yet for this period.", "actions": [], "body": ""}
    try:
        briefing = await build_briefing(db, user, range_param, rng["utc_start"], rng["utc_end"])
    except Exception:
        logger.error("dashboard_summary: briefing section failed", exc_info=True)
        try:
            await db.rollback()
        except Exception:
            pass
        _errors.append({"section": "briefing", "reason": "query failed"})

    numbers: dict[str, Any] = {
        "sales": {"value": 0, "delta_pct": None, "direction": "flat", "sparkline": []},
        "clients_owe": {"value": 0, "overdue_count": 0},
        "spent": {"value": 0, "delta_pct": None, "direction": "flat", "sparkline": []},
        "stock_alerts": {"value": 0, "out_count": 0, "low_count": 0},
    }
    try:
        number_payload = await _build_numbers(db, rng, user)
        numbers = {key: number_payload[key] for key in ("sales", "clients_owe", "spent", "stock_alerts")}
        alt_sales_today = number_payload.get("alt_sales_today", {"value": 0})
    except Exception:
        logger.error("dashboard_summary: numbers section failed", exc_info=True)
        try:
            await db.rollback()
        except Exception:
            pass
        alt_sales_today = {"value": 0}
        _errors.append({"section": "numbers", "reason": "query failed"})

    chart: dict[str, Any] = {"buckets": []}
    try:
        chart = await _build_chart(db, rng)
    except Exception:
        logger.error("dashboard_summary: chart section failed", exc_info=True)
        try:
            await db.rollback()
        except Exception:
            pass
        _errors.append({"section": "chart", "reason": "query failed"})

    panels: dict[str, Any] = {"top_products_by_revenue": [], "top_products_by_qty": [], "recent_activity": []}
    try:
        panels = await _build_panels(db, rng)
    except Exception:
        logger.error("dashboard_summary: panels section failed", exc_info=True)
        try:
            await db.rollback()
        except Exception:
            pass
        _errors.append({"section": "top_products", "reason": "query failed"})

    generated_at = now_local().isoformat()

    response = {
        "range": {
            "label": rng["label"],
            "start": rng["start"],
            "end": rng["end"],
            "days": rng["days"],
            "granularity": _pick_granularity(rng),
        },
        "briefing": briefing,
        "numbers": numbers,
        "chart": chart,
        "panels": panels,
        "generated_at": generated_at,
        "viewer": {
            "role": getattr(user, "role", "user"),
            "can_view_b2b": has_permission(user, "page_b2b"),
            "can_view_expenses": has_permission(user, "page_expenses") or has_permission(user, "page_accounting"),
            "can_view_inventory": has_permission(user, "page_inventory") or has_permission(user, "page_products"),
            "can_view_pos": has_permission(user, "page_pos"),
            "alt_sales_today": alt_sales_today,
        },
        "timezone": settings.APP_TIMEZONE,
    }
    if _errors:
        response["_errors"] = _errors
    return response


__all__ = ["_pick_granularity", "_utc_range", "get_summary", "resolve_range"]
