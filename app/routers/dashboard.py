from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.core.config import settings
from app.core.permissions import require_permission
from app.core.rate_limit import limiter
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
from app.services.dashboard_assistant_service import answer_dashboard_question

router = APIRouter(
    tags=["Dashboard"],
    dependencies=[Depends(require_permission("page_dashboard"))],
)


class DashboardAssistantQuestion(BaseModel):
    question: str


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

@router.post("/dashboard/assistant")
@limiter.limit("20/minute")
async def dashboard_assistant(
    request: Request,
    data: DashboardAssistantQuestion,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    from app.core.log import logger
    from fastapi import HTTPException

    try:
        return await answer_dashboard_question(
            db,
            question=data.question,
            current_user=current_user,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
        logger.warning(
            "dashboard_assistant request failed",
            extra={"status_code": exc.status_code, "detail": detail},
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "supported": False,
                "intent": None,
                "parameters": {},
                "result": None,
                "message": detail,
                "confidence": 0.0,
                "suggestions": [],
                "highlights": [],
                "table": None,
            },
        )
    except Exception:
        logger.exception("dashboard_assistant request failed")
        try:
            await db.rollback()
        except Exception:
            pass
        return JSONResponse(
            status_code=500,
            content={
                "supported": False,
                "intent": None,
                "parameters": {},
                "result": None,
                "message": "I couldn't answer that because the dashboard assistant hit an internal error. Please try again.",
                "confidence": 0.0,
                "suggestions": [],
                "highlights": [],
                "table": None,
            },
        )




# ── new: /dashboard/summary ────────────────────────────────────────────

@router.get("/dashboard/summary")
async def dashboard_summary(
    range_param: str = Query("today", pattern="^(today|7d|30d|mtd|qtd|custom)$", alias="range"),
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
<html lang="en" dir="{locale_dir}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dashboard — Thunder ERP</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/dashboard.css">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<script src="/static/auth-guard.js"></script>
</head>
<body>

<div id="loading"><div class="spinner"></div></div>

<!-- ── Nav ──────────────────────────────────────────────────────────── -->
<nav>
  <a href="/home" class="logo">Thunder ERP</a>
  <a href="/dashboard"  class="nav-link active">Dashboard</a>
  <a href="/pos"        class="nav-link">POS</a>
  <a href="/b2b/"       class="nav-link">B2B</a>
  <a href="/reports/"   class="nav-link">Reports</a>
  <a href="/inventory/" class="nav-link">Inventory</a>
  <span class="nav-spacer"></span>
  <button class="mode-btn" id="mode-btn" onclick="toggleMode()" aria-label="Toggle color mode">🌙</button>
  <div class="account-menu">
    <div class="user-pill" onclick="toggleAccountMenu(event)">
      <div class="user-avatar" id="user-avatar">A</div>
      <span class="user-name" id="user-name">Admin</span>
    </div>
    <div class="account-dropdown" id="account-dropdown">
      <a href="/users/password" class="account-item">Security Settings</a>
      <a href="#" class="account-item" onclick="logout()">Sign Out</a>
    </div>
  </div>
</nav>

<!-- ── Main content ───────────────────────────────────────────────── -->
<div class="content">

  <!-- 1. Header strip -->
  <div class="header-strip">
    <div class="header-left">
      <span class="greeting" id="greeting">Good morning</span>
      <div class="header-meta">
        <span class="date-display" id="date-display"></span>
        <span class="last-updated-pill" id="last-updated">Loading…</span>
      </div>
    </div>
    <div style="display:flex;flex-direction:column;align-items:flex-end;gap:6px">
      <div class="range-picker">
        <button class="range-btn active" data-range="today">Today</button>
        <button class="range-btn" data-range="7d">7 days</button>
        <button class="range-btn" data-range="30d">30 days</button>
        <button class="range-btn" data-range="mtd">This month</button>
        <button class="range-btn" data-range="qtd">This quarter</button>
        <button class="range-btn" data-range="custom">Custom…</button>
      </div>
      <span id="range-label" class="text-muted" style="font-size:12px"></span>
    </div>
  </div>

  <!-- 2. Insights strip -->
  <section aria-label="What changed">
    <div id="insights-strip" class="insights-strip">
      <div class="skeleton" style="width:260px;height:100px"></div>
      <div class="skeleton" style="width:260px;height:100px"></div>
      <div class="skeleton" style="width:260px;height:100px"></div>
    </div>
  </section>

  <!-- 3. Hero row (4 cards) -->
  <section aria-label="Key metrics" class="hero-row">
    <!-- Hero cards rendered by JS; placeholders here for layout -->
    <div class="hero-card" id="hero-0" role="region" aria-label="Primary metric">
      <span class="hero-label">—</span>
      <div class="hero-value skeleton" style="height:32px;width:120px"></div>
      <span class="hero-chip chip chip-flat">—</span>
      <span class="hero-subtitle"></span>
      <div class="hero-sparkline"><canvas></canvas></div>
    </div>
    <div class="hero-card" id="hero-1" role="region" aria-label="Metric 2">
      <span class="hero-label">—</span>
      <div class="hero-value skeleton" style="height:32px;width:120px"></div>
      <span class="hero-chip chip chip-flat">—</span>
      <span class="hero-subtitle"></span>
      <div class="hero-sparkline"><canvas></canvas></div>
    </div>
    <div class="hero-card" id="hero-2" role="region" aria-label="Metric 3">
      <span class="hero-label">—</span>
      <div class="hero-value skeleton" style="height:32px;width:120px"></div>
      <span class="hero-chip chip chip-flat">—</span>
      <span class="hero-subtitle"></span>
      <div class="hero-sparkline"></div>
    </div>
    <div class="hero-card" id="hero-3" role="region" aria-label="Metric 4">
      <span class="hero-label">—</span>
      <div class="hero-value skeleton" style="height:32px;width:120px"></div>
      <span class="hero-chip chip chip-flat">—</span>
      <span class="hero-subtitle"></span>
      <div class="hero-sparkline"></div>
    </div>
  </section>

  <!-- 4. Primary chart -->
  <div class="chart-panel">
    <div class="panel-header">
      <span class="panel-title">Revenue Trend</span>
    </div>
    <div class="chart-wrap">
      <canvas id="main-chart" aria-label="Revenue trend chart"></canvas>
    </div>
    <!-- Accessibility fallback -->
    <table class="sr-only" aria-label="Revenue trend data table" id="chart-table"></table>
  </div>

  <!-- 5. Secondary grid (4 panels) -->
  <div class="secondary-grid">

    <!-- Panel A: Top Products -->
    <div class="panel" role="region" aria-label="Top products">
      <div class="panel-header"><span class="panel-title">Top Products</span></div>
      <div class="tab-bar">
        <button class="tab-btn active" data-pane="top-by-revenue-pane">By Revenue</button>
        <button class="tab-btn"        data-pane="top-by-qty-pane">By Qty</button>
        <button class="tab-btn"        data-pane="top-by-margin-pane">By Margin</button>
      </div>
      <div class="panel-body">
        <div class="tab-pane active" id="top-by-revenue-pane">
          <table><thead></thead><tbody id="top-by-revenue"></tbody></table>
        </div>
        <div class="tab-pane" id="top-by-qty-pane">
          <table><thead></thead><tbody id="top-by-qty"></tbody></table>
        </div>
        <div class="tab-pane" id="top-by-margin-pane">
          <table><thead></thead><tbody id="top-by-margin"></tbody></table>
        </div>
      </div>
    </div>

    <!-- Panel B: Receivables -->
    <div class="panel" role="region" aria-label="Receivables">
      <div class="panel-header"><span class="panel-title">Who Owes Us</span></div>
      <div class="tab-bar">
        <button class="tab-btn active" data-pane="recv-b2b-pane">B2B</button>
        <button class="tab-btn"        data-pane="recv-retail-pane">Retail Credit</button>
      </div>
      <div class="panel-body">
        <div class="tab-pane active" id="recv-b2b-pane">
          <table><thead></thead><tbody id="recv-b2b"></tbody></table>
        </div>
        <div class="tab-pane" id="recv-retail-pane">
          <table><thead></thead><tbody id="recv-retail"></tbody></table>
        </div>
      </div>
    </div>

    <!-- Panel C: Stock Pressure -->
    <div class="panel" role="region" aria-label="Stock pressure">
      <div class="panel-header"><span class="panel-title">Stock Pressure</span></div>
      <div class="tab-bar">
        <button class="tab-btn active" data-pane="stock-risk-pane">Stock-out Risk</button>
        <button class="tab-btn"        data-pane="stock-low-pane">Low Stock</button>
        <button class="tab-btn"        data-pane="stock-dead-pane">Dead Stock</button>
      </div>
      <div class="panel-body">
        <div class="tab-pane active" id="stock-risk-pane">
          <table><thead></thead><tbody id="stock-risk"></tbody></table>
        </div>
        <div class="tab-pane" id="stock-low-pane">
          <table><thead></thead><tbody id="stock-low"></tbody></table>
        </div>
        <div class="tab-pane" id="stock-dead-pane">
          <table><thead></thead><tbody id="stock-dead"></tbody></table>
        </div>
      </div>
    </div>

    <!-- Panel D: Recent Activity -->
    <div class="panel" role="region" aria-label="Recent activity">
      <div class="panel-header"><span class="panel-title">Today's Operations</span></div>
      <div class="panel-body">
        <table>
          <thead>
            <tr>
              <th>Ref</th><th>Customer</th><th>Total</th><th>Method</th><th>Time</th>
            </tr>
          </thead>
          <tbody id="recent-activity"></tbody>
        </table>
      </div>
    </div>

  </div><!-- /secondary-grid -->

</div><!-- /content -->

<div id="custom-range-modal" class="range-modal hidden" role="dialog" aria-modal="true" aria-labelledby="custom-range-title">
  <div class="range-modal-card">
    <div class="range-modal-header">
      <div>
        <h3 id="custom-range-title">Custom Range</h3>
        <p>Select a start date and end date for the dashboard summary.</p>
      </div>
      <button type="button" class="range-modal-close" onclick="closeCustomRangePicker()" aria-label="Close custom range">×</button>
    </div>
    <div class="range-modal-body">
      <label class="range-field">
        <span>Start date</span>
        <input id="custom-range-start" type="date">
      </label>
      <label class="range-field">
        <span>End date</span>
        <input id="custom-range-end" type="date">
      </label>
      <p id="custom-range-error" class="range-error" hidden></p>
    </div>
    <div class="range-modal-actions">
      <button type="button" class="range-secondary-btn" onclick="closeCustomRangePicker()">Cancel</button>
      <button type="button" class="range-primary-btn" onclick="applyCustomRange()">Apply</button>
    </div>
  </div>
</div>

<!-- ── Assistant drawer ───────────────────────────────────────────── -->
<aside id="assistant-drawer" aria-label="AI Assistant" role="complementary">
  <div class="drawer-header">
    <span class="drawer-title">AI Assistant</span>
    <button class="drawer-close" onclick="closeDrawer()" aria-label="Close assistant">✕</button>
  </div>
  <div class="drawer-body" id="chat-body">
    <div class="preset-chips" id="preset-chips">
      <!-- Chips rendered by JS from /dashboard/insights -->
    </div>
  </div>
  <div class="drawer-footer">
    <div class="chat-input-wrap">
      <input class="chat-input" id="chat-input" type="text"
             placeholder="Ask anything about your business…" autocomplete="off">
      <button class="chat-send" id="chat-send" aria-label="Send">→</button>
    </div>
  </div>
</aside>

<!-- FAB (mobile) -->
<button class="fab" onclick="openDrawer()" aria-label="Open AI assistant">💬</button>

<script src="/static/dashboard.js"></script>
</body>
</html>"""
