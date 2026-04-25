from collections import defaultdict, deque
from datetime import date, datetime, timedelta
import asyncio
import json
import time
from typing import Any, Dict, Optional

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings
from app.core.permissions import require_permission
from app.core.security import get_current_user
from app.database import get_async_session
from app.models.invoice import Invoice, InvoiceItem
from app.models.product import Product
from app.models.customer import Customer
from app.models.b2b import B2BClient, B2BInvoice
from app.models.farm import FarmDelivery
from app.models.spoilage import SpoilageRecord
from app.models.production import ProductionBatch
from app.models.refund import RetailRefund
from app.models.user import User
from app.services.expense_service import get_summary as get_expense_summary

router = APIRouter(
    tags=["Dashboard"],
    dependencies=[Depends(require_permission("page_dashboard"))],
)

_assistant_rate_limit_fallback: dict[str, deque[float]] = defaultdict(deque)
_assistant_rate_limit_lock = asyncio.Lock()
_assistant_redis_client = None


def _get_assistant_redis_client():
    global _assistant_redis_client
    if _assistant_redis_client is None:
        _assistant_redis_client = aioredis.from_url(
            settings.REDIS_URL,
            socket_connect_timeout=settings.REDIS_SOCKET_CONNECT_TIMEOUT,
            socket_timeout=settings.REDIS_SOCKET_TIMEOUT,
            decode_responses=True,
        )
    return _assistant_redis_client


def _client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip() or "unknown"
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _assistant_rate_limit_key(request: Request, current_user: User) -> str:
    user_id = getattr(current_user, "id", None)
    if user_id is not None:
        return f"user:{user_id}"
    return f"ip:{_client_ip(request)}"


async def _enforce_assistant_rate_limit(request: Request, current_user: User) -> None:
    limit = max(1, int(settings.ASSISTANT_RATE_LIMIT_REQUESTS))
    window = max(1, int(settings.ASSISTANT_RATE_LIMIT_WINDOW_SECONDS))
    key = _assistant_rate_limit_key(request, current_user)
    redis_key = f"assistant:rate_limit:{key}"

    try:
        redis_client = _get_assistant_redis_client()
        current = await redis_client.incr(redis_key)
        if current == 1:
            await redis_client.expire(redis_key, window)
        if current > limit:
            raise HTTPException(
                status_code=429,
                detail=f"Assistant rate limit exceeded. Try again in about {window} seconds.",
            )
        return
    except HTTPException:
        raise
    except Exception:
        pass

    now = time.monotonic()
    async with _assistant_rate_limit_lock:
        bucket = _assistant_rate_limit_fallback[key]
        while bucket and now - bucket[0] >= window:
            bucket.popleft()
        if len(bucket) >= limit:
            raise HTTPException(
                status_code=429,
                detail=f"Assistant rate limit exceeded. Try again in about {window} seconds.",
            )
        bucket.append(now)


def _trim_text(value: Any, *, limit: int = 160) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _trim_list_of_dicts(items: Any, *, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    trimmed: list[dict[str, Any]] = []
    for item in items[: settings.ASSISTANT_CONTEXT_LIST_LIMIT]:
        if not isinstance(item, dict):
            continue
        trimmed.append({key: item.get(key) for key in keys if key in item})
    return trimmed


def _trim_dashboard_context(dashboard_context: dict[str, Any] | None) -> dict[str, Any] | None:
    if not dashboard_context:
        return None

    trimmed: dict[str, Any] = {}

    range_key = dashboard_context.get("range")
    if isinstance(range_key, dict):
        trimmed["range"] = {
            key: range_key.get(key)
            for key in ("key", "label", "date_from", "date_to", "granularity")
            if key in range_key
        }
    elif isinstance(range_key, str):
        trimmed["range"] = range_key

    for key in ("start", "end"):
        value = dashboard_context.get(key)
        if value:
            trimmed[key] = str(value)

    numbers = dashboard_context.get("numbers")
    if isinstance(numbers, dict):
        kept_numbers: dict[str, Any] = {}
        for key in ("sales", "clients_owe", "spent", "stock_alerts"):
            value = numbers.get(key)
            if isinstance(value, dict):
                kept_numbers[key] = {
                    sub_key: value.get(sub_key)
                    for sub_key in ("value", "delta_pct", "overdue_count", "out_count", "low_count")
                    if sub_key in value
                }
        if kept_numbers:
            trimmed["numbers"] = kept_numbers

    panels = dashboard_context.get("panels")
    if isinstance(panels, dict):
        kept_panels: dict[str, Any] = {}
        top_revenue = _trim_list_of_dicts(
            panels.get("top_products_by_revenue"),
            keys=("name", "qty", "revenue"),
        )
        if top_revenue:
            kept_panels["top_products_by_revenue"] = top_revenue
        top_qty = _trim_list_of_dicts(
            panels.get("top_products_by_qty"),
            keys=("name", "qty", "revenue"),
        )
        if top_qty:
            kept_panels["top_products_by_qty"] = top_qty
        recent_activity = _trim_list_of_dicts(
            panels.get("recent_activity"),
            keys=("invoice_number", "customer", "total", "type", "time_relative"),
        )
        if recent_activity:
            kept_panels["recent_activity"] = recent_activity
        if kept_panels:
            trimmed["panels"] = kept_panels

    briefing = dashboard_context.get("briefing")
    if isinstance(briefing, dict):
        kept_briefing = {
            "lead": _trim_text(briefing.get("lead")),
            "body": _trim_text(briefing.get("body"), limit=240),
        }
        trimmed["briefing"] = {key: value for key, value in kept_briefing.items() if value}

    if trimmed:
        serialized = json.dumps(trimmed, default=str, separators=(",", ":"))
        if len(serialized) > settings.ASSISTANT_MAX_CONTEXT_CHARS:
            raise HTTPException(
                status_code=413,
                detail="Assistant dashboard context is too large. Refresh the dashboard and try again.",
            )

    return trimmed or None


def _reset_assistant_rate_limit_state() -> None:
    _assistant_rate_limit_fallback.clear()

# ── legacy data endpoint ───────────────────────────────────────────────

@router.get("/dashboard/data")
async def dashboard_data(db: AsyncSession = Depends(get_async_session)):
    from app.core.log import logger
    from app.core.time_utils import today_local, utc_bounds
    from app.models.accounting import Account, Journal, JournalEntry

    _errors: list[dict] = []

    try:
        today   = today_local()
        month_s = today.replace(day=1)
        year_s  = today.replace(month=1, day=1)

        today_s,   today_e   = utc_bounds(today,   today)
        month_s_u, month_e_u = utc_bounds(month_s, today)
        year_s_u,  year_e_u  = utc_bounds(year_s,  today)

        # ── B2B revenue account (used by multiple sections) ─────────────
        rev_acc = None
        try:
            r = await db.execute(select(Account).where(Account.code == "4000"))
            rev_acc = r.scalar_one_or_none()
        except Exception:
            logger.error("dashboard_data: account lookup failed", exc_info=True)

        async def _jrev(utc_s, utc_e) -> float:
            if not rev_acc:
                return 0.0
            r = await db.execute(
                select(func.sum(JournalEntry.credit))
                .join(Journal, JournalEntry.journal_id == Journal.id)
                .where(
                    JournalEntry.account_id == rev_acc.id,
                    Journal.created_at >= utc_s,
                    Journal.created_at <= utc_e,
                    Journal.ref_type.in_(["b2b", "b2b_invoice", "consignment_payment", "consignment"]),
                )
            )
            return float(r.scalar() or 0)

        # ── POS SALES ──────────────────────────────────────────────────
        pos_today = pos_month = pos_year = 0.0
        ref_today = ref_month = ref_year = 0.0
        ref_count_today = ref_count_month = 0
        invoices_today = invoices_month = 0
        try:
            r = await db.execute(select(func.sum(Invoice.total)).where(Invoice.created_at >= today_s, Invoice.created_at <= today_e, Invoice.status == "paid"))
            pos_today = float(r.scalar() or 0)
            r = await db.execute(select(func.sum(Invoice.total)).where(Invoice.created_at >= month_s_u, Invoice.created_at <= month_e_u, Invoice.status == "paid"))
            pos_month = float(r.scalar() or 0)
            r = await db.execute(select(func.sum(Invoice.total)).where(Invoice.created_at >= year_s_u, Invoice.created_at <= year_e_u, Invoice.status == "paid"))
            pos_year  = float(r.scalar() or 0)

            r = await db.execute(select(func.sum(RetailRefund.total)).where(RetailRefund.created_at >= today_s, RetailRefund.created_at <= today_e))
            ref_today = float(r.scalar() or 0)
            r = await db.execute(select(func.sum(RetailRefund.total)).where(RetailRefund.created_at >= month_s_u, RetailRefund.created_at <= month_e_u))
            ref_month = float(r.scalar() or 0)
            r = await db.execute(select(func.sum(RetailRefund.total)).where(RetailRefund.created_at >= year_s_u, RetailRefund.created_at <= year_e_u))
            ref_year  = float(r.scalar() or 0)

            pos_today = max(0.0, pos_today - ref_today)
            pos_month = max(0.0, pos_month - ref_month)
            pos_year  = max(0.0, pos_year  - ref_year)

            r = await db.execute(select(func.count(Invoice.id)).where(Invoice.created_at >= today_s, Invoice.created_at <= today_e))
            invoices_today = int(r.scalar() or 0)
            r = await db.execute(select(func.count(Invoice.id)).where(Invoice.created_at >= month_s_u, Invoice.created_at <= month_e_u, Invoice.status == "paid"))
            invoices_month = int(r.scalar() or 0)

            r = await db.execute(select(func.count(RetailRefund.id)).where(RetailRefund.created_at >= today_s, RetailRefund.created_at <= today_e))
            ref_count_today = int(r.scalar() or 0)
            r = await db.execute(select(func.count(RetailRefund.id)).where(RetailRefund.created_at >= month_s_u, RetailRefund.created_at <= month_e_u))
            ref_count_month = int(r.scalar() or 0)
        except Exception:
            logger.error("dashboard_data: pos_sales section failed", exc_info=True)
            _errors.append({"section": "pos_sales", "reason": "query failed"})

        # ── B2B SALES ──────────────────────────────────────────────────
        b2b_today = b2b_month = b2b_year = 0.0
        b2b_outstanding = 0.0
        b2b_clients = 0
        try:
            b2b_today = await _jrev(today_s, today_e)
            b2b_month = await _jrev(month_s_u, month_e_u)
            b2b_year  = await _jrev(year_s_u,  year_e_u)

            r = await db.execute(
                select(func.sum(B2BInvoice.total - func.coalesce(B2BInvoice.amount_paid, 0)))
                .where(B2BInvoice.status.in_(["unpaid", "partial"]))
            )
            b2b_outstanding = float(r.scalar() or 0)
            r = await db.execute(select(func.count(B2BClient.id)).where(B2BClient.is_active == True))
            b2b_clients = int(r.scalar() or 0)
        except Exception:
            logger.error("dashboard_data: b2b_sales section failed", exc_info=True)
            _errors.append({"section": "b2b_sales", "reason": "query failed"})

        total_today = pos_today + b2b_today
        total_month = pos_month + b2b_month
        total_year  = pos_year  + b2b_year

        # ── EXPENSES ───────────────────────────────────────────────────
        expenses_month = expenses_last_month = 0.0
        try:
            expense_summary     = await get_expense_summary(db)
            expenses_month      = float(expense_summary["this_month"])
            expenses_last_month = float(expense_summary["last_month"])
        except Exception:
            logger.error("dashboard_data: expenses section failed", exc_info=True)
            _errors.append({"section": "expenses", "reason": "query failed"})

        # ── CUSTOMERS ──────────────────────────────────────────────────
        total_customers = new_customers_month = 0
        try:
            r = await db.execute(select(func.count(Customer.id)))
            total_customers = int(r.scalar() or 0)
            r = await db.execute(
                select(func.count(Customer.id))
                .where(Customer.created_at >= month_s_u, Customer.created_at <= month_e_u)
            )
            new_customers_month = int(r.scalar() or 0)
        except Exception:
            logger.error("dashboard_data: customers section failed", exc_info=True)
            _errors.append({"section": "customers", "reason": "query failed"})

        # ── INVENTORY ──────────────────────────────────────────────────
        total_products = out_of_stock_count = low_stock_count = 0
        stock_value = 0.0
        out_of_stock: list = []
        low_stock_list: list = []
        try:
            r = await db.execute(select(Product).where(Product.is_active == True))
            all_products   = r.scalars().all()
            out_of_stock   = [p for p in all_products if float(p.stock or 0) <= 0]
            low_stock_list = [p for p in all_products if 0 < float(p.stock or 0) <= float(p.min_stock or 5)]
            total_products     = len(all_products)
            out_of_stock_count = len(out_of_stock)
            low_stock_count    = len(low_stock_list)
            stock_value        = sum(float(p.stock or 0) * float(p.price or 0) for p in all_products)
        except Exception:
            logger.error("dashboard_data: inventory section failed", exc_info=True)
            _errors.append({"section": "inventory", "reason": "query failed"})

        # ── FARM / SPOILAGE / PRODUCTION ───────────────────────────────
        farm_month = batches_month = 0
        spoilage_month = 0.0
        try:
            r = await db.execute(select(func.count(FarmDelivery.id)).where(FarmDelivery.delivery_date >= month_s))
            farm_month = int(r.scalar() or 0)
            r = await db.execute(select(func.sum(SpoilageRecord.qty)).where(SpoilageRecord.spoilage_date >= month_s))
            spoilage_month = float(r.scalar() or 0)
            r = await db.execute(select(func.count(ProductionBatch.id)).where(ProductionBatch.created_at >= month_s_u, ProductionBatch.created_at <= month_e_u))
            batches_month = int(r.scalar() or 0)
        except Exception:
            logger.error("dashboard_data: farm_spoilage_production section failed", exc_info=True)
            _errors.append({"section": "farm_spoilage_production", "reason": "query failed"})

        # ── LAST 7 DAYS (POS + B2B) ────────────────────────────────────
        last7: list = []
        try:
            for i in range(6, -1, -1):
                d = today - timedelta(days=i)
                d_s, d_e = utc_bounds(d, d)
                r = await db.execute(select(func.sum(Invoice.total)).where(Invoice.created_at >= d_s, Invoice.created_at <= d_e, Invoice.status == "paid"))
                pos = float(r.scalar() or 0)
                r = await db.execute(select(func.sum(RetailRefund.total)).where(RetailRefund.created_at >= d_s, RetailRefund.created_at <= d_e))
                ref = float(r.scalar() or 0)
                pos = max(0.0, pos - ref)
                b2b = await _jrev(d_s, d_e)
                last7.append({"date": str(d), "pos": round(pos, 2), "b2b": round(b2b, 2), "refunds": round(ref, 2), "total": round(pos + b2b, 2)})
        except Exception:
            logger.error("dashboard_data: chart_last7 section failed", exc_info=True)
            _errors.append({"section": "chart_last7", "reason": "query failed"})

        # ── TOP PRODUCTS ───────────────────────────────────────────────
        top_products: list = []
        try:
            top_result = await db.execute(
                select(InvoiceItem.name,
                       func.sum(InvoiceItem.qty).label("qty_sold"),
                       func.sum(InvoiceItem.total).label("revenue"))
                .join(Invoice, InvoiceItem.invoice_id == Invoice.id)
                .where(Invoice.created_at >= month_s_u, Invoice.created_at <= month_e_u, Invoice.status == "paid")
                .group_by(InvoiceItem.name)
                .order_by(func.sum(InvoiceItem.total).desc())
                .limit(10)
            )
            top_products = [{"name": r.name, "qty": float(r.qty_sold), "revenue": float(r.revenue)} for r in top_result.all()]
        except Exception:
            logger.error("dashboard_data: top_products section failed", exc_info=True)
            _errors.append({"section": "top_products", "reason": "query failed"})

        # ── PAYMENT METHODS ────────────────────────────────────────────
        pay_methods: list = []
        try:
            pay_result = await db.execute(
                select(Invoice.payment_method,
                       func.count(Invoice.id).label("count"),
                       func.sum(Invoice.total).label("total"))
                .where(Invoice.created_at >= month_s_u, Invoice.created_at <= month_e_u, Invoice.status == "paid")
                .group_by(Invoice.payment_method)
            )
            pay_methods = [{"method": r.payment_method or "cash", "count": r.count, "total": float(r.total)} for r in pay_result.all()]
        except Exception:
            logger.error("dashboard_data: pay_methods section failed", exc_info=True)
            _errors.append({"section": "pay_methods", "reason": "query failed"})

        # ── RECENT TRANSACTIONS ────────────────────────────────────────
        recent_sales: list = []
        try:
            inv_result = await db.execute(
                select(Invoice.invoice_number, Invoice.customer_id, Invoice.total,
                       Invoice.payment_method, Invoice.created_at)
                .where(Invoice.status == "paid").order_by(Invoice.created_at.desc()).limit(12)
            )
            recent_invoices = inv_result.all()
            ref_result = await db.execute(
                select(RetailRefund.refund_number, RetailRefund.customer_id, RetailRefund.total,
                       RetailRefund.refund_method, RetailRefund.created_at)
                .order_by(RetailRefund.created_at.desc()).limit(6)
            )
            recent_refunds = ref_result.all()

            for i in recent_invoices:
                cust_r = await db.execute(select(Customer).where(Customer.id == i.customer_id))
                cust   = cust_r.scalar_one_or_none()
                recent_sales.append({
                    "type": "sale", "invoice_number": i.invoice_number,
                    "customer": cust.name if cust else "Walk-in",
                    "total": float(i.total or 0), "method": i.payment_method or "cash",
                    "time": i.created_at.strftime("%H:%M") if i.created_at else "—",
                    "date": i.created_at.strftime("%Y-%m-%d") if i.created_at else "",
                })
            for ref in recent_refunds:
                cust_r = await db.execute(select(Customer).where(Customer.id == ref.customer_id))
                cust   = cust_r.scalar_one_or_none()
                recent_sales.append({
                    "type": "refund", "invoice_number": ref.refund_number,
                    "customer": cust.name if cust else "—",
                    "total": -float(ref.total or 0), "method": ref.refund_method,
                    "time": ref.created_at.strftime("%H:%M") if ref.created_at else "—",
                    "date": ref.created_at.strftime("%Y-%m-%d") if ref.created_at else "",
                })
            recent_sales.sort(key=lambda x: x["date"] + x["time"], reverse=True)
            recent_sales = recent_sales[:10]
        except Exception:
            logger.error("dashboard_data: recent_transactions section failed", exc_info=True)
            _errors.append({"section": "recent_transactions", "reason": "query failed"})

        return {
            "pos_today":    round(pos_today, 2),
            "pos_month":    round(pos_month, 2),
            "pos_year":     round(pos_year, 2),
            "b2b_today":    round(b2b_today, 2),
            "b2b_month":    round(b2b_month, 2),
            "b2b_year":     round(b2b_year, 2),
            "total_today":  round(total_today, 2),
            "total_month":  round(total_month, 2),
            "total_year":   round(total_year, 2),
            "expenses_month":      round(expenses_month, 2),
            "expenses_last_month": round(expenses_last_month, 2),
            "b2b_outstanding": round(b2b_outstanding, 2),
            "ref_today":   round(ref_today, 2),
            "ref_month":   round(ref_month, 2),
            "ref_count_today": ref_count_today,
            "ref_count_month": ref_count_month,
            "invoices_today":   invoices_today,
            "invoices_month":   invoices_month,
            "total_customers":  total_customers,
            "b2b_clients":      b2b_clients,
            "total_products":   total_products,
            "out_of_stock_count": out_of_stock_count,
            "low_stock_count":    low_stock_count,
            "stock_value":        round(stock_value, 2),
            "out_of_stock": [{"sku": p.sku, "name": p.name, "stock": float(p.stock or 0)} for p in out_of_stock[:20]],
            "low_stock":    [{"sku": p.sku, "name": p.name, "stock": float(p.stock or 0)} for p in low_stock_list[:20]],
            "farm_month":     farm_month,
            "spoilage_month": round(spoilage_month, 2),
            "batches_month":  batches_month,
            "last7":          last7,
            "top_products":   top_products,
            "pay_methods":    pay_methods,
            "recent_sales":   recent_sales,
            "_errors":        _errors,
        }

    except Exception:
        logger.exception("dashboard_data endpoint failed — unhandled exception")
        raise HTTPException(
            status_code=500,
            detail={
                "error": "dashboard_data_failed",
                "message": "Internal server error loading dashboard data",
                "hint": "Check server logs for full traceback",
            },
        )


# ── assistant endpoint ─────────────────────────────────────────────────

class CopilotRequest(BaseModel):
    question: str = Field(min_length=1, max_length=settings.ASSISTANT_MAX_QUESTION_CHARS)
    dashboard_context: Optional[Dict[str, Any]] = None

    @field_validator("question", mode="before")
    @classmethod
    def _normalize_question(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("Question cannot be empty.")
        return normalized

@router.post("/dashboard/assistant/ask")
async def dashboard_assistant_ask(
    request: Request,
    payload: CopilotRequest,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    from app.services.copilot.engine import answer_question

    await _enforce_assistant_rate_limit(request, current_user)
    trimmed_context = _trim_dashboard_context(payload.dashboard_context)

    try:
        return await answer_question(
            db,
            question=payload.question,
            current_user=current_user,
            dashboard_context=trimmed_context,
        )
    except HTTPException:
        raise
    except Exception as e:
        return {"type": "text", "content": "An error occurred while processing your request."}

# ── new: /dashboard/summary ────────────────────────────────────────────

@router.get("/dashboard/summary")
async def dashboard_summary(
    range_param: str = Query("today", pattern="^(today|7d|30d|90d|mtd|qtd|year|ytd|custom)$", alias="range"),
    start: Optional[str] = Query(None),
    end:   Optional[str] = Query(None),
    db:    AsyncSession  = Depends(get_async_session),
    current_user: User   = Depends(get_current_user),
):
    import json
    try:
        import redis.asyncio as aioredis
        redis_client = aioredis.from_url(
            settings.REDIS_URL,
            socket_connect_timeout=settings.REDIS_SOCKET_CONNECT_TIMEOUT,
            socket_timeout=settings.REDIS_SOCKET_TIMEOUT,
            decode_responses=True,
        )
        cache_key = f"dash_summary:{current_user.id}:{range_param}:{start}:{end}"
        cached = await redis_client.get(cache_key)
        if cached:
            await redis_client.aclose()
            return json.loads(cached)
    except Exception:
        redis_client = None
        cache_key    = None

    from app.core.log import logger
    from app.services.dashboard_summary_service import get_summary
    try:
        data = await get_summary(db, range_param, start, end, current_user)
    except Exception:
        logger.exception("dashboard_summary service failed")
        raise HTTPException(
            status_code=500,
            detail={
                "error": "dashboard_summary_failed",
                "message": "Internal server error loading dashboard summary",
                "hint": "Check server logs for full traceback",
            },
        )

    if redis_client and cache_key:
        try:
            await redis_client.setex(cache_key, 60, json.dumps(data, default=str))
            await redis_client.aclose()
        except Exception:
            pass

    return data


# ── new: /dashboard/insights ───────────────────────────────────────────

@router.get("/dashboard/insights")
async def dashboard_insights(db: AsyncSession = Depends(get_async_session)):
    from app.core.log import logger
    from app.services.dashboard_insights_service import get_insights
    try:
        return await get_insights(db)
    except Exception:
        logger.exception("dashboard_insights endpoint failed — unhandled exception")
        return {"cards": [], "suggested_chips": [], "_errors": [{"rule": "all", "reason": "internal error"}]}


# ── UI ─────────────────────────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_ui():
    locale_dir = getattr(settings, "APP_LOCALE_DIR", "ltr")
    return f"""<!DOCTYPE html>
<html lang="en" dir="{locale_dir}" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<script src="/static/theme.js"></script>
<title>Dashboard — Thunder ERP</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,600;1,300;1,400&family=DM+Sans:wght@300;400;500;600&family=DM+Mono:wght@400;500&family=Outfit:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/dashboard.css">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<script src="/static/auth-guard.js"></script>
</head>
<body>
<div class="bg-layer">
  <div class="bg-orb"></div>
  <div class="bg-orb"></div>
  <div class="bg-orb"></div>
</div>
<div class="bg-grain"></div>
<div id="loading"><div class="spinner"></div></div>
<nav class="top-nav" aria-label="Primary">
  <a href="/home" class="logo">
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <polygon points="13,2 4,14 11,14 11,22 20,10 13,10" fill="#f59e0b"></polygon>
    </svg>
    <span class="logo-text">Thunder ERP</span>
  </a>
  <div class="nav-links">
    <a href="/dashboard" class="nav-link active">Dashboard</a>
    <a href="/pos" class="nav-link">POS</a>
    <a href="/b2b/" class="nav-link">B2B</a>
    <a href="/reports/" class="nav-link">Reports</a>
    <a href="/inventory/" class="nav-link">Inventory</a>
  </div>
  <div class="nav-actions">
    <button class="mode-btn" id="mode-btn" type="button" aria-label="Toggle color mode" title="Toggle light/dark mode">&#127769;</button>
    <div class="account-menu">
      <button class="user-pill" id="account-trigger" type="button" aria-haspopup="menu" aria-expanded="false">
        <div class="user-avatar" id="user-avatar">A</div>
        <span class="user-name" id="user-name">Admin</span>
        <span class="menu-caret">&#9662;</span>
      </button>
      <div class="account-dropdown" id="account-dropdown" role="menu">
        <div class="account-head">
          <div class="account-label">Signed in as</div>
          <div class="account-email" id="user-email">&#8212;</div>
        </div>
        <a href="/users/password" class="account-item" role="menuitem">Change Password</a>
        <button class="account-item danger" id="signout-btn" type="button" role="menuitem">Sign out</button>
      </div>
    </div>
  </div>
</nav>
<main class="page-shell">
  <header class="header-strip">
    <div>
      <h1 class="greeting" id="greeting">Good morning</h1>
      <p class="date-display" id="date-display"></p>
    </div>
    <div class="header-controls">
      <div class="range-picker" role="group" aria-label="Choose date range">
        <button type="button" class="range-btn" data-range="today">Today</button>
        <button type="button" class="range-btn" data-range="7d">7d</button>
        <button type="button" class="range-btn" data-range="30d">30d</button>
        <button type="button" class="range-btn" data-range="mtd">Month</button>
        <button type="button" class="range-btn" data-range="year">Year</button>
        <button type="button" class="range-btn" data-range="custom">Custom</button>
      </div>
      <span class="updated-pill" id="last-updated">Updated just now</span>
    </div>
  </header>

  <article class="card briefing-card" aria-label="Today's briefing">
    <p class="briefing-lead" id="briefing-lead">Loading today's briefing…</p>
    <p class="briefing-body" id="briefing-body"></p>
    <div class="briefing-actions" id="briefing-actions"></div>
  </article>

  <section class="numbers-grid" aria-label="Key numbers">
    <article class="card number-card" data-card="sales" aria-live="polite"></article>
    <article class="card number-card" data-card="clients_owe" aria-live="polite"></article>
    <article class="card number-card" data-card="spent" aria-live="polite"></article>
    <article class="card number-card" data-card="stock_alerts" aria-live="polite"></article>
  </section>

  <section class="card chart-card" aria-label="Sales over time">
    <div class="panel-head"><h2 id="chart-title">Sales over time</h2></div>
    <div class="chart-wrap"><canvas id="sales-chart" aria-label="Sales over time chart"></canvas></div>
    <table class="sr-only" id="chart-table" aria-label="Sales over time table"></table>
  </section>

  <div class="panel-grid">
    <section class="card panel-card" aria-label="Best sellers">
      <div class="panel-head">
        <h2 id="top-products-title">Best-sellers</h2>
        <div class="panel-tabs" role="tablist" aria-label="Best seller mode">
          <button type="button" class="tab-btn active" data-top-tab="revenue">By revenue</button>
          <button type="button" class="tab-btn" data-top-tab="qty">By quantity</button>
        </div>
      </div>
      <div id="top-products-list" class="panel-body"></div>
    </section>

    <section class="card panel-card" aria-label="Recent transactions">
      <div class="panel-head">
        <h2>Recent transactions</h2>
        <div class="panel-tabs" role="tablist" aria-label="Recent activity filter">
          <button type="button" class="tab-btn active" data-activity-filter="all">All</button>
          <button type="button" class="tab-btn" data-activity-filter="sale">Sales</button>
          <button type="button" class="tab-btn" data-activity-filter="refund">Refunds</button>
        </div>
      </div>
      <div class="panel-body">
        <table class="activity-table">
          <thead><tr><th>Invoice</th><th>Customer</th><th>Amount</th><th>Time</th></tr></thead>
          <tbody id="recent-activity"></tbody>
        </table>
      </div>
    </section>
  </div>
</main>

<div id="custom-range-modal" class="range-modal hidden" role="dialog" aria-modal="true" aria-labelledby="crm-title">
  <div class="range-modal-card">
    <div class="range-modal-header">
      <h3 id="crm-title">Custom Range</h3>
      <button type="button" class="range-modal-close" id="range-modal-close" aria-label="Close">&#215;</button>
    </div>
    <div class="range-modal-body">
      <label class="range-field"><span>Start date</span><input id="custom-range-start" type="date"></label>
      <label class="range-field"><span>End date</span><input id="custom-range-end" type="date"></label>
      <p id="custom-range-error" class="range-error" hidden></p>
    </div>
    <div class="range-modal-actions">
      <button type="button" class="range-secondary-btn" id="range-cancel">Cancel</button>
      <button type="button" class="range-primary-btn" id="range-apply">Apply</button>
    </div>
  </div>
</div>

<div id="ai-chat-widget" class="ai-chat-widget hidden">
  <div class="ai-chat-header">
    <h3>AI Assistant</h3>
    <button id="ai-chat-close" aria-label="Close chat">&#215;</button>
  </div>
  <div class="ai-chat-body" id="ai-chat-body">
    <div class="chat-bubble ai">Hello! I'm your ERP Assistant. How can I help you today?</div>
  </div>
  <div class="ai-chat-input-area">
    <input type="text" id="ai-chat-input" placeholder="Ask a question..." />
    <button id="ai-chat-send" aria-label="Send">📤</button>
  </div>
</div>
<button id="ai-chat-trigger" class="ai-chat-trigger" aria-label="Open AI Assistant">✨</button>

<script src="/static/dashboard.js"></script>
</body>
</html>"""
