"""
Range-based aggregations for GET /dashboard/summary.

The summary payload is intentionally shaped for the dashboard UI:
- one briefing block
- plain-language numbers
- one chart payload
- supporting panels

Each major section is isolated so a single failed query does not break the
whole response.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.sqltypes import Date as SQLDate

from app.core.config import settings
from app.core.log import logger
from app.core.permissions import has_permission
from app.core.time_utils import now_local
from app.models.b2b import B2BClient, B2BInvoice, B2BInvoiceItem, B2BRefund, B2BRefundItem
from app.models.accounting import Account, Journal, JournalEntry
from app.models.customer import Customer
from app.models.expense import Expense
from app.models.invoice import Invoice, InvoiceItem
from app.models.product import Product
from app.models.refund import RetailRefund, RetailRefundItem
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


def _append_error(errors: list[dict[str, str]], section: str, reason: str = "query failed") -> None:
    errors.append({"section": section, "reason": reason})


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


async def _expense_total(db: AsyncSession, local_start: date, local_end: date) -> float:
    result = await db.execute(
        select(func.coalesce(func.sum(Expense.amount), 0)).where(
            # Trace: expense_date="2026-01-15" appears for January 2026, not April 2026.
            Expense.expense_date >= local_start,
            Expense.expense_date <= local_end,
        )
    )
    return round(_safe_float(result.scalar()), 2)


async def _daily_sales_rows(db: AsyncSession, utc_s: datetime, utc_e: datetime) -> list[dict[str, Any]]:
    tz = _tz()
    invoice_bucket = _local_bucket_expr(Invoice.created_at, "day")
    refund_bucket = _local_bucket_expr(RetailRefund.created_at, "day")
    b2b_bucket = _local_bucket_expr(B2BInvoice.created_at, "day")

    pos_rows = await db.execute(
        select(
            invoice_bucket.label("bucket_date"),
            func.coalesce(func.sum(Invoice.total), 0).label("total"),
            func.count(Invoice.id).label("orders"),
        )
        .where(Invoice.created_at >= utc_s, Invoice.created_at <= utc_e, Invoice.status == "paid")
        .group_by(invoice_bucket)
        .order_by(invoice_bucket)
    )
    pos_by_day = {str(row.bucket_date): (_safe_float(row.total), _safe_int(row.orders)) for row in pos_rows}

    refund_rows = await db.execute(
        select(
            refund_bucket.label("bucket_date"),
            func.coalesce(func.sum(RetailRefund.total), 0).label("total"),
            func.count(RetailRefund.id).label("orders"),
        )
        .where(RetailRefund.created_at >= utc_s, RetailRefund.created_at <= utc_e)
        .group_by(refund_bucket)
        .order_by(refund_bucket)
    )
    refund_by_day = {str(row.bucket_date): (_safe_float(row.total), _safe_int(row.orders)) for row in refund_rows}

    b2b_rows = await db.execute(
        select(
            b2b_bucket.label("bucket_date"),
            func.coalesce(func.sum(B2BInvoice.total), 0).label("total"),
            func.count(B2BInvoice.id).label("orders"),
        )
        .where(B2BInvoice.created_at >= utc_s, B2BInvoice.created_at <= utc_e, B2BInvoice.status == "paid")
        .group_by(b2b_bucket)
        .order_by(b2b_bucket)
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


async def _sparkline_sales(db: AsyncSession, rng: dict[str, Any]) -> list[float]:
    daily = await _daily_sales_rows(db, rng["utc_start"], rng["utc_end"])
    return [round(_safe_float(row["pos"]) + _safe_float(row["b2b"]), 2) for row in daily[-14:]]


async def _sparkline_expenses(db: AsyncSession, rng: dict[str, Any]) -> list[float]:
    local_end = date.fromisoformat(rng["end"])
    start = local_end - timedelta(days=13)
    # expense_date is already a local Date, so it is the day bucket.
    bucket_expr = Expense.expense_date
    rows = await db.execute(
        select(
            bucket_expr.label("bucket_date"),
            func.coalesce(func.sum(Expense.amount), 0).label("amount"),
        )
        .where(Expense.expense_date >= start, Expense.expense_date <= local_end)
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


def _cost_expression(item_model) -> tuple[Any | None, bool]:
    for candidate in ("cost", "unit_cost", "cogs"):
        column = getattr(item_model, candidate, None)
        if column is not None:
            return column, False
    product_cost = getattr(Product, "cost", None)
    if product_cost is not None:
        return product_cost, True
    return None, False


async def _gross_profit_for_sales(
    db: AsyncSession,
    invoice_model,
    item_model,
    utc_s: datetime,
    utc_e: datetime,
) -> float | None:
    cost_expr, needs_product_join = _cost_expression(item_model)
    if cost_expr is None:
        return None

    stmt = select(func.coalesce(func.sum(item_model.qty * (item_model.unit_price - cost_expr)), 0))
    stmt = stmt.join(invoice_model, item_model.invoice_id == invoice_model.id)
    if needs_product_join:
        stmt = stmt.join(Product, item_model.product_id == Product.id)
    stmt = stmt.where(invoice_model.created_at >= utc_s, invoice_model.created_at <= utc_e, invoice_model.status == "paid")
    result = await db.execute(stmt)
    return _safe_float(result.scalar())


async def _gross_profit_for_refunds(
    db: AsyncSession,
    refund_model,
    refund_item_model,
    utc_s: datetime,
    utc_e: datetime,
) -> float:
    cost_expr, needs_product_join = _cost_expression(refund_item_model)
    if cost_expr is None:
        return 0.0

    stmt = select(func.coalesce(func.sum(refund_item_model.qty * (refund_item_model.unit_price - cost_expr)), 0))
    stmt = stmt.join(refund_model, refund_item_model.refund_id == refund_model.id)
    if needs_product_join:
        stmt = stmt.join(Product, refund_item_model.product_id == Product.id)
    stmt = stmt.where(refund_model.created_at >= utc_s, refund_model.created_at <= utc_e)
    result = await db.execute(stmt)
    return _safe_float(result.scalar())


async def _gross_profit_total(db: AsyncSession, utc_s: datetime, utc_e: datetime) -> float | None:
    pos_profit = await _gross_profit_for_sales(db, Invoice, InvoiceItem, utc_s, utc_e)
    b2b_profit = await _gross_profit_for_sales(db, B2BInvoice, B2BInvoiceItem, utc_s, utc_e)
    if pos_profit is None or b2b_profit is None:
        return None

    retail_refund_profit = await _gross_profit_for_refunds(db, RetailRefund, RetailRefundItem, utc_s, utc_e)
    b2b_refund_profit = await _gross_profit_for_refunds(db, B2BRefund, B2BRefundItem, utc_s, utc_e)
    return round(pos_profit + b2b_profit - retail_refund_profit - b2b_refund_profit, 2)


async def _build_margin_block(
    db: AsyncSession,
    rng: dict[str, Any],
    sales_value: float,
    sales_prior: float,
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    default_block = {"value_pct": None, "delta_pts": None, "gross_profit": None}
    try:
        current_gp = await _gross_profit_total(db, rng["utc_start"], rng["utc_end"])
        prior_gp = await _gross_profit_total(db, rng["prior_utc_start"], rng["prior_utc_end"])
        if current_gp is None:
            return default_block

        current_pct = round((current_gp / max(sales_value, 1)) * 100, 1)
        margin = {
            "value_pct": current_pct,
            "delta_pts": None,
            "gross_profit": round(current_gp, 2),
        }
        if prior_gp is not None:
            prior_pct = round((prior_gp / max(sales_prior, 1)) * 100, 1)
            margin["delta_pts"] = round(current_pct - prior_pct, 1)
        return margin
    except Exception:
        logger.error("dashboard_summary: margin section failed", exc_info=True)
        try:
            await db.rollback()
        except Exception:
            pass
        _append_error(errors, "numbers.margin")
        return default_block


async def _build_numbers(db: AsyncSession, rng: dict[str, Any], user: User, errors: list[dict[str, str]]) -> dict[str, Any]:
    can_view_b2b = has_permission(user, "page_b2b")
    can_view_pos = has_permission(user, "page_pos")

    sales_value = await _sales_total(db, rng["utc_start"], rng["utc_end"])
    sales_prior = await _sales_total(db, rng["prior_utc_start"], rng["prior_utc_end"])
    spent_value = await _expense_total(db, date.fromisoformat(rng["start"]), date.fromisoformat(rng["end"]))
    spent_prior = await _expense_total(db, date.fromisoformat(rng["prior_start"]), date.fromisoformat(rng["prior_end"]))

    clients_owe_result = await db.execute(
        select(func.coalesce(func.sum(B2BInvoice.total - func.coalesce(B2BInvoice.amount_paid, 0)), 0)).where(
            B2BInvoice.status.in_(["unpaid", "partial"])
        )
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

    out_result = await db.execute(select(func.count(Product.id)).where(Product.is_active == True, Product.stock <= 0))
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

    margin = await _build_margin_block(db, rng, sales_value, sales_prior, errors)

    # Net profit = gross profit - operating expenses
    gross_profit = margin.get("gross_profit")
    net_profit = round(gross_profit - spent_value, 2) if gross_profit is not None else None
    net_margin_pct = round((net_profit / max(sales_value, 1)) * 100, 1) if net_profit is not None else None

    prior_gross_profit = None
    prior_net_profit = None
    prior_net_margin_pct = None
    try:
        prior_gross_profit = await _gross_profit_total(db, rng["prior_utc_start"], rng["prior_utc_end"])
        prior_spent = await _expense_total(db, date.fromisoformat(rng["prior_start"]), date.fromisoformat(rng["prior_end"]))
        if prior_gross_profit is not None:
            prior_net_profit = round(prior_gross_profit - prior_spent, 2)
            prior_net_margin_pct = round((prior_net_profit / max(sales_prior, 1)) * 100, 1)
    except Exception:
        logger.error("dashboard_summary: prior net profit calc failed", exc_info=True)

    # B2B cash collected — debit on account 1000 for payment journals in range
    b2b_cash_value = 0.0
    try:
        from sqlalchemy import text
        cash_r = await db.execute(
            text("""
                SELECT COALESCE(SUM(je.debit), 0)
                FROM journal_entries je
                JOIN journals j ON je.journal_id = j.id
                JOIN accounts a ON je.account_id = a.id
                WHERE a.code = '1000'
                  AND je.debit > 0
                  AND j.created_at >= :utc_s
                  AND j.created_at <= :utc_e
                  AND j.ref_type IN ('b2b_payment','b2b_collection','consignment_payment','consignment_client_payment')
            """),
            {"utc_s": rng["utc_start"], "utc_e": rng["utc_end"]},
        )
        b2b_cash_value = float(cash_r.scalar() or 0)
    except Exception:
        logger.exception("dashboard_summary: b2b_cash query failed")
        _append_error(errors, "numbers.b2b_cash")

    sales_delta = _delta_pct(sales_value, sales_prior)
    spent_delta = _delta_pct(spent_value, spent_prior)
    return {
        "sales": {
            "value": round(sales_value, 2),
            "prev_value": round(sales_prior, 2),
            "delta_pct": sales_delta,
            "direction": _delta_direction(sales_delta, higher_is_better=True),
            "sparkline": await _sparkline_sales(db, rng),
        },
        "clients_owe": {
            "value": round(clients_owe_value, 2),
            "overdue_count": overdue_count,
        },
        "spent": {
            "value": round(spent_value, 2),
            "delta_pct": spent_delta,
            "direction": _delta_direction(spent_delta, higher_is_better=False),
            "sparkline": await _sparkline_expenses(db, rng),
        },
        "stock_alerts": {
            "value": out_count + low_count,
            "out_count": out_count,
            "low_count": low_count,
        },
        "margin": margin,
        "alt_sales_today": {"value": round(sales_today_value, 2)},
        "b2b_cash": {"value": round(b2b_cash_value, 2)},
        "profit": {
            "gross_profit": gross_profit,
            "gross_margin_pct": margin.get("value_pct"),
            "operating_expenses": round(spent_value, 2),
            "net_profit": net_profit,
            "net_margin_pct": net_margin_pct,
            "prior_gross_profit": prior_gross_profit,
            "prior_net_profit": prior_net_profit,
            "prior_net_margin_pct": prior_net_margin_pct,
            "net_margin_delta_pts": round(net_margin_pct - prior_net_margin_pct, 1)
                if net_margin_pct is not None and prior_net_margin_pct is not None else None,
        },
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


async def _top_b2b_clients(db: AsyncSession, rng: dict[str, Any], user: Any, limit: int = 8) -> list[dict[str, Any]]:
    if not has_permission(user, "page_b2b"):
        return []

    # Compute outstanding live from invoices — same method as the B2B page
    # so numbers always match what the user sees there.
    outstanding_sub = (
        select(
            B2BInvoice.client_id,
            func.coalesce(
                func.sum(B2BInvoice.total - func.coalesce(B2BInvoice.amount_paid, 0)), 0
            ).label("outstanding"),
        )
        .where(B2BInvoice.status.in_(["unpaid", "partial"]))
        .group_by(B2BInvoice.client_id)
        .subquery()
    )

    rows = await db.execute(
        select(
            B2BClient.id,
            B2BClient.name,
            B2BClient.payment_terms,
            func.coalesce(outstanding_sub.c.outstanding, 0).label("outstanding"),
            func.coalesce(func.sum(B2BInvoice.total), 0).label("revenue"),
            func.count(B2BInvoice.id).label("invoice_count"),
        )
        .join(B2BInvoice, B2BInvoice.client_id == B2BClient.id)
        .outerjoin(outstanding_sub, outstanding_sub.c.client_id == B2BClient.id)
        .where(
            B2BInvoice.created_at >= rng["utc_start"],
            B2BInvoice.created_at <= rng["utc_end"],
            B2BInvoice.status == "paid",
            B2BClient.is_active == True,
        )
        .group_by(B2BClient.id, B2BClient.name, B2BClient.payment_terms, outstanding_sub.c.outstanding)
        .order_by(func.sum(B2BInvoice.total).desc())
        .limit(limit)
    )
    return [
        {
            "id": r.id,
            "name": r.name,
            "payment_terms": r.payment_terms or "immediate",
            "revenue": round(float(r.revenue), 2),
            "invoice_count": int(r.invoice_count),
            "outstanding": round(float(r.outstanding or 0), 2),
        }
        for r in rows.all()
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


async def _build_panels(db: AsyncSession, rng: dict[str, Any], user: Any) -> dict[str, Any]:
    return {
        "top_products_by_revenue": await _top_products(db, rng, "revenue"),
        "top_products_by_qty": await _top_products(db, rng, "qty"),
        "recent_activity": await _recent_activity(db, rng),
        "top_b2b_clients": await _top_b2b_clients(db, rng, user),
    }


async def _insight_overdue(
    db: AsyncSession,
    numbers: dict[str, Any],
    *,
    errors: list[dict[str, str]] | None = None,
) -> dict[str, str] | None:
    try:
        if _safe_int(numbers.get("clients_owe", {}).get("overdue_count")) <= 0:
            return None

        overdue_cutoff = now_local().astimezone(ZoneInfo("UTC")) - timedelta(days=30)
        stmt = (
            select(
                B2BInvoice.invoice_number,
                B2BInvoice.total,
                B2BInvoice.amount_paid,
                B2BInvoice.created_at,
                B2BClient.name.label("client_name"),
            )
            .join(B2BClient, B2BClient.id == B2BInvoice.client_id)
            .where(B2BInvoice.status.in_(["unpaid", "partial"]), B2BInvoice.created_at <= overdue_cutoff)
            .order_by((B2BInvoice.total - func.coalesce(B2BInvoice.amount_paid, 0)).desc(), B2BInvoice.created_at.asc())
            .limit(1)
        )
        row = (await db.execute(stmt)).first()
        if not row:
            return None

        created_at = row.created_at.astimezone(_tz()) if row.created_at else now_local()
        age_days = max(0, (now_local().date() - created_at.date()).days)
        return {
            "kind": "overdue",
            "text": f"{row.client_name} hasn't paid invoice #{row.invoice_number} for {age_days} days — your largest overdue receivable.",
        }
    except Exception:
        logger.error("dashboard_summary: overdue insight failed", exc_info=True)
        if errors is not None:
            _append_error(errors, "insights.overdue")
        return None


async def _sales_velocity_lookup(
    db: AsyncSession,
    product_ids: list[int],
    utc_s: datetime,
    utc_e: datetime,
) -> dict[int, float]:
    velocity: dict[int, float] = {product_id: 0.0 for product_id in product_ids}
    if not product_ids:
        return velocity

    pos_rows = await db.execute(
        select(InvoiceItem.product_id, func.coalesce(func.sum(InvoiceItem.qty), 0).label("qty"))
        .join(Invoice, InvoiceItem.invoice_id == Invoice.id)
        .where(
            InvoiceItem.product_id.in_(product_ids),
            Invoice.created_at >= utc_s,
            Invoice.created_at <= utc_e,
            Invoice.status == "paid",
        )
        .group_by(InvoiceItem.product_id)
    )
    for row in pos_rows.all():
        velocity[row.product_id] = velocity.get(row.product_id, 0.0) + _safe_float(row.qty)

    b2b_rows = await db.execute(
        select(B2BInvoiceItem.product_id, func.coalesce(func.sum(B2BInvoiceItem.qty), 0).label("qty"))
        .join(B2BInvoice, B2BInvoiceItem.invoice_id == B2BInvoice.id)
        .where(
            B2BInvoiceItem.product_id.in_(product_ids),
            B2BInvoice.created_at >= utc_s,
            B2BInvoice.created_at <= utc_e,
            B2BInvoice.status == "paid",
        )
        .group_by(B2BInvoiceItem.product_id)
    )
    for row in b2b_rows.all():
        velocity[row.product_id] = velocity.get(row.product_id, 0.0) + _safe_float(row.qty)

    return velocity


async def _insight_stockout(
    db: AsyncSession,
    numbers: dict[str, Any],
    *,
    errors: list[dict[str, str]] | None = None,
) -> dict[str, str] | None:
    try:
        out_count = _safe_int(numbers.get("stock_alerts", {}).get("out_count"))
        if out_count <= 0:
            return None

        out_rows = await db.execute(
            select(Product.id, Product.name).where(Product.is_active == True, Product.stock <= 0).order_by(Product.name.asc())
        )
        products = list(out_rows.all())
        if not products:
            return None

        utc_s = now_local().astimezone(ZoneInfo("UTC")) - timedelta(days=30)
        utc_e = now_local().astimezone(ZoneInfo("UTC"))
        velocity = await _sales_velocity_lookup(db, [row.id for row in products], utc_s, utc_e)
        top_product = max(products, key=lambda row: velocity.get(row.id, 0.0))
        if velocity.get(top_product.id, 0.0) <= 0:
            return None

        return {
            "kind": "stockout",
            "text": (
                f"{out_count} products ran out of stock recently. {top_product.name} has been a top seller — "
                "restocking it should be the priority."
            ),
        }
    except Exception:
        logger.error("dashboard_summary: stockout insight failed", exc_info=True)
        if errors is not None:
            _append_error(errors, "insights.stockout")
        return None


async def _insight_pace(
    db: AsyncSession,
    rng: dict[str, Any],
    *,
    errors: list[dict[str, str]] | None = None,
) -> dict[str, str] | None:
    try:
        days = _safe_int(rng.get("days"))
        if days < 14:
            return None

        half = days // 2
        range_end = date.fromisoformat(rng["end"])
        last_start = range_end - timedelta(days=half - 1)
        first_end = last_start - timedelta(days=1)
        first_start = first_end - timedelta(days=half - 1)

        first_utc_s, first_utc_e = _utc_range(first_start, first_end)
        last_utc_s, last_utc_e = _utc_range(last_start, range_end)
        first_sales = await _sales_total(db, first_utc_s, first_utc_e)
        last_sales = await _sales_total(db, last_utc_s, last_utc_e)
        if first_sales <= 0:
            return None

        delta_pct = round(((last_sales - first_sales) / first_sales) * 100, 1)
        if delta_pct <= 5:
            return None
        return {
            "kind": "pace",
            "text": f"Your last {half} days are pacing {delta_pct:.1f}% ahead of the first half of this period.",
        }
    except Exception:
        logger.error("dashboard_summary: pace insight failed", exc_info=True)
        if errors is not None:
            _append_error(errors, "insights.pace")
        return None


async def _insight_margin(
    margin_data: dict[str, Any],
    *,
    errors: list[dict[str, str]] | None = None,
) -> dict[str, str] | None:
    try:
        delta = margin_data.get("delta_pts")
        if delta is None or _safe_float(delta) < 1.0:
            return None
        return {"kind": "margin", "text": f"Margin improved {float(delta):.1f} points versus the previous period."}
    except Exception:
        logger.error("dashboard_summary: margin insight failed", exc_info=True)
        if errors is not None:
            _append_error(errors, "insights.margin")
        return None


def _busiest_weekday_name(rows: list[dict[str, Any]]) -> str | None:
    weekday_totals = {index: 0.0 for index in range(7)}
    for row in rows:
        bucket_date = date.fromisoformat(row["date"])
        weekday_totals[bucket_date.weekday()] += (
            _safe_float(row["pos"]) + _safe_float(row["b2b"]) + _safe_float(row["refunds"])
        )

    if max(weekday_totals.values(), default=0.0) <= 0:
        return None

    weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    busiest_index = max(weekday_totals.items(), key=lambda item: item[1])[0]
    return weekday_names[busiest_index]


async def _insight_weekday(
    db: AsyncSession,
    rng: dict[str, Any],
    *,
    errors: list[dict[str, str]] | None = None,
) -> dict[str, str] | None:
    try:
        if _safe_int(rng.get("days")) < 28:
            return None

        current_rows = await _daily_sales_rows(db, rng["utc_start"], rng["utc_end"])
        prior_rows = await _daily_sales_rows(db, rng["prior_utc_start"], rng["prior_utc_end"])
        current_day = _busiest_weekday_name(current_rows)
        prior_day = _busiest_weekday_name(prior_rows)
        if not current_day or not prior_day or current_day == prior_day:
            return None

        return {"kind": "weekday", "text": f"{current_day}s are now your busiest day, overtaking {prior_day}s."}
    except Exception:
        logger.error("dashboard_summary: weekday insight failed", exc_info=True)
        if errors is not None:
            _append_error(errors, "insights.weekday")
        return None


async def _build_insights(
    db: AsyncSession,
    rng: dict[str, Any],
    numbers: dict[str, Any],
    errors: list[dict[str, str]],
) -> list[dict[str, str]]:
    insights: list[dict[str, str]] = []
    for insight in (
        await _insight_overdue(db, numbers, errors=errors),
        await _insight_stockout(db, numbers, errors=errors),
        await _insight_pace(db, rng, errors=errors),
        await _insight_margin(numbers.get("margin", {}), errors=errors),
        await _insight_weekday(db, rng, errors=errors),
    ):
        if insight:
            insights.append(insight)
        if len(insights) >= 3:
            break
    return insights


async def get_summary(
    db: AsyncSession,
    range_param: str,
    custom_start: str | None,
    custom_end: str | None,
    user: User,
) -> dict[str, Any]:
    from app.services.dashboard_briefing_service import build_briefing

    rng = resolve_range(range_param, custom_start, custom_end)
    user_role = getattr(user, "role", "user")
    can_view_b2b = has_permission(user, "page_b2b")
    can_view_expenses = has_permission(user, "page_expenses") or has_permission(user, "page_accounting")
    can_view_inventory = has_permission(user, "page_inventory") or has_permission(user, "page_products")
    can_view_pos = has_permission(user, "page_pos")
    errors: list[dict[str, str]] = []

    briefing: dict[str, Any] = {"lead": "You haven't recorded any sales yet for this period.", "actions": [], "body": ""}
    try:
        briefing = await build_briefing(db, user, range_param, rng["utc_start"], rng["utc_end"])
    except Exception:
        logger.error("dashboard_summary: briefing section failed", exc_info=True)
        try:
            await db.rollback()
        except Exception:
            pass
        _append_error(errors, "briefing")

    numbers: dict[str, Any] = {
        "sales": {"value": 0.0, "prev_value": 0.0, "delta_pct": None, "direction": "flat", "sparkline": []},
        "clients_owe": {"value": 0.0, "overdue_count": 0},
        "spent": {"value": 0.0, "delta_pct": None, "direction": "flat", "sparkline": []},
        "stock_alerts": {"value": 0, "out_count": 0, "low_count": 0},
        "margin": {"value_pct": None, "delta_pts": None, "gross_profit": None},
        "b2b_cash": {"value": 0.0},
        "profit": {
            "gross_profit": None,
            "gross_margin_pct": None,
            "operating_expenses": 0.0,
            "net_profit": None,
            "net_margin_pct": None,
            "prior_gross_profit": None,
            "prior_net_profit": None,
            "prior_net_margin_pct": None,
            "net_margin_delta_pts": None,
        },
    }
    alt_sales_today = {"value": 0.0}
    try:
        number_payload = await _build_numbers(db, rng, user, errors)
        numbers = {key: number_payload[key] for key in ("sales", "clients_owe", "spent", "stock_alerts", "margin", "b2b_cash", "profit")}
        alt_sales_today = number_payload.get("alt_sales_today", {"value": 0.0})
    except Exception:
        logger.error("dashboard_summary: numbers section failed", exc_info=True)
        try:
            await db.rollback()
        except Exception:
            pass
        _append_error(errors, "numbers")

    chart: dict[str, Any] = {"buckets": []}
    try:
        chart = await _build_chart(db, rng)
    except Exception:
        logger.error("dashboard_summary: chart section failed", exc_info=True)
        try:
            await db.rollback()
        except Exception:
            pass
        _append_error(errors, "chart")

    panels: dict[str, Any] = {"top_products_by_revenue": [], "top_products_by_qty": [], "recent_activity": [], "top_b2b_clients": []}
    try:
        panels = await _build_panels(db, rng, user)
    except Exception:
        logger.error("dashboard_summary: panels section failed", exc_info=True)
        try:
            await db.rollback()
        except Exception:
            pass
        _append_error(errors, "top_products")

    insights: list[dict[str, str]] = []
    try:
        insights = await _build_insights(db, rng, numbers, errors)
    except Exception:
        logger.error("dashboard_summary: insights section failed", exc_info=True)
        try:
            await db.rollback()
        except Exception:
            pass
        _append_error(errors, "insights")

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
        "insights": insights,
        "generated_at": now_local().isoformat(),
        "viewer": {
            "role": user_role,
            "can_view_b2b": can_view_b2b,
            "can_view_expenses": can_view_expenses,
            "can_view_inventory": can_view_inventory,
            "can_view_pos": can_view_pos,
            "alt_sales_today": alt_sales_today,
        },
        "timezone": settings.APP_TIMEZONE,
    }
    if errors:
        response["_errors"] = errors
    return response


__all__ = [
    "_busiest_weekday_name",
    "_insight_margin",
    "_insight_overdue",
    "_insight_pace",
    "_insight_stockout",
    "_insight_weekday",
    "_pick_granularity",
    "_utc_range",
    "get_summary",
    "resolve_range",
]