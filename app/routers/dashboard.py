from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
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


# ── legacy data endpoint (kept intact for backward compat) ─────────────

@router.get("/dashboard/data")
async def dashboard_data(db: AsyncSession = Depends(get_async_session)):
    from zoneinfo import ZoneInfo
    tz    = ZoneInfo(settings.APP_TIMEZONE)
    today = datetime.now(tz).date()           # timezone-correct "today"
    month_s = today.replace(day=1)
    year_s  = today.replace(month=1, day=1)

    # ── POS SALES ──────────────────────────────────────
    r = await db.execute(select(func.sum(Invoice.total)).where(func.date(Invoice.created_at) == today, Invoice.status == "paid"))
    pos_today = float(r.scalar() or 0)
    r = await db.execute(select(func.sum(Invoice.total)).where(func.date(Invoice.created_at) >= month_s, Invoice.status == "paid"))
    pos_month = float(r.scalar() or 0)
    r = await db.execute(select(func.sum(Invoice.total)).where(func.date(Invoice.created_at) >= year_s, Invoice.status == "paid"))
    pos_year  = float(r.scalar() or 0)

    # Subtract retail refunds from POS revenue
    r = await db.execute(select(func.sum(RetailRefund.total)).where(func.date(RetailRefund.created_at) == today))
    ref_today = float(r.scalar() or 0)
    r = await db.execute(select(func.sum(RetailRefund.total)).where(func.date(RetailRefund.created_at) >= month_s))
    ref_month = float(r.scalar() or 0)
    r = await db.execute(select(func.sum(RetailRefund.total)).where(func.date(RetailRefund.created_at) >= year_s))
    ref_year  = float(r.scalar() or 0)
    pos_today = max(0, pos_today - ref_today)
    pos_month = max(0, pos_month - ref_month)
    pos_year  = max(0, pos_year  - ref_year)

    r = await db.execute(select(func.count(Invoice.id)).where(func.date(Invoice.created_at) == today))
    invoices_today = r.scalar() or 0
    r = await db.execute(select(func.count(Invoice.id)).where(func.date(Invoice.created_at) >= month_s, Invoice.status == "paid"))
    invoices_month = r.scalar() or 0

    # ── B2B SALES ──────────────────────────────────────
    from app.models.accounting import Account, Journal, JournalEntry
    rev_result = await db.execute(select(Account).where(Account.code == "4000"))
    rev_acc = rev_result.scalar_one_or_none()

    async def journal_revenue(d_from, d_to):
        if not rev_acc: return 0.0
        stmt = (
            select(func.sum(JournalEntry.credit))
            .join(Journal, JournalEntry.journal_id == Journal.id)
            .where(
                JournalEntry.account_id == rev_acc.id,
                Journal.created_at >= d_from,
                Journal.created_at <= d_to,
                Journal.ref_type.in_(["b2b", "b2b_invoice", "consignment_payment", "consignment"])
            )
        )
        entries = await db.execute(stmt)
        return float(entries.scalar() or 0)

    from datetime import datetime as dt
    today_start = dt.combine(today, dt.min.time())
    today_end   = dt.combine(today, dt.max.time())
    month_start_dt = dt.combine(month_s, dt.min.time())
    year_start_dt  = dt.combine(year_s,  dt.min.time())
    now_dt = dt.now()

    b2b_today = await journal_revenue(today_start, today_end)
    b2b_month = await journal_revenue(month_start_dt, now_dt)
    b2b_year  = await journal_revenue(year_start_dt,  now_dt)

    r = await db.execute(
        select(func.sum(B2BInvoice.total - B2BInvoice.amount_paid))
        .where(B2BInvoice.status.in_(["unpaid", "partial"]))
    )
    b2b_outstanding = float(r.scalar() or 0)
    r = await db.execute(select(func.count(B2BClient.id)).where(B2BClient.is_active == True))
    b2b_clients = r.scalar() or 0

    total_today = pos_today + b2b_today
    total_month = pos_month + b2b_month
    total_year  = pos_year  + b2b_year

    expense_summary = await get_expense_summary(db)
    expenses_month = float(expense_summary["this_month"])
    expenses_last_month = float(expense_summary["last_month"])

    r = await db.execute(select(func.count(Customer.id)))
    total_customers = r.scalar() or 0
    if hasattr(Customer, 'created_at'):
        r = await db.execute(select(func.count(Customer.id)).where(func.date(Customer.created_at) >= month_s))
        new_customers_month = r.scalar() or 0
    else:
        new_customers_month = 0

    r = await db.execute(select(Product).where(Product.is_active == True))
    all_products   = r.scalars().all()
    out_of_stock   = [p for p in all_products if float(p.stock) <= 0]
    low_stock      = [p for p in all_products if 0 < float(p.stock) <= 5]
    total_products = len(all_products)
    stock_value    = sum(float(p.stock) * float(p.price) for p in all_products)

    r = await db.execute(select(func.count(FarmDelivery.id)).where(FarmDelivery.delivery_date >= month_s))
    farm_month = r.scalar() or 0

    r = await db.execute(select(func.sum(SpoilageRecord.qty)).where(SpoilageRecord.spoilage_date >= month_s))
    spoilage_month = float(r.scalar() or 0)

    r = await db.execute(select(func.count(ProductionBatch.id)).where(func.date(ProductionBatch.created_at) >= month_s))
    batches_month = r.scalar() or 0

    # ── LAST 7 DAYS (POS + B2B) ────────────────────────
    last7 = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        r = await db.execute(select(func.sum(Invoice.total)).where(func.date(Invoice.created_at) == d, Invoice.status == "paid"))
        pos = float(r.scalar() or 0)
        r = await db.execute(select(func.sum(RetailRefund.total)).where(func.date(RetailRefund.created_at) == d))
        ref = float(r.scalar() or 0)
        pos = max(0, pos - ref)
        d_start = dt.combine(d, dt.min.time())
        d_end   = dt.combine(d, dt.max.time())
        b2b = await journal_revenue(d_start, d_end)
        last7.append({"date": str(d), "pos": round(pos,2), "b2b": round(b2b,2), "refunds": round(ref,2), "total": round(pos+b2b,2)})

    top_result = await db.execute(
        select(InvoiceItem.name,
               func.sum(InvoiceItem.qty).label("qty_sold"),
               func.sum(InvoiceItem.total).label("revenue"))
        .join(Invoice, InvoiceItem.invoice_id == Invoice.id)
        .where(func.date(Invoice.created_at) >= month_s, Invoice.status == "paid")
        .group_by(InvoiceItem.name)
        .order_by(func.sum(InvoiceItem.total).desc())
        .limit(10)
    )
    top_products = top_result.all()

    pay_result = await db.execute(
        select(Invoice.payment_method,
               func.count(Invoice.id).label("count"),
               func.sum(Invoice.total).label("total"))
        .where(func.date(Invoice.created_at) >= month_s, Invoice.status == "paid")
        .group_by(Invoice.payment_method)
    )
    pay_methods = pay_result.all()

    inv_result = await db.execute(
        select(
            Invoice.invoice_number,
            Invoice.customer_id,
            Invoice.total,
            Invoice.payment_method,
            Invoice.created_at,
        ).where(Invoice.status == "paid").order_by(Invoice.created_at.desc()).limit(12)
    )
    recent_invoices = inv_result.all()
    ref_result = await db.execute(
        select(
            RetailRefund.refund_number,
            RetailRefund.customer_id,
            RetailRefund.total,
            RetailRefund.refund_method,
            RetailRefund.created_at,
        ).order_by(RetailRefund.created_at.desc()).limit(6)
    )
    recent_refunds = ref_result.all()

    recent_sales = []
    for i in recent_invoices:
        cust_result = await db.execute(select(Customer).where(Customer.id == i.customer_id))
        cust = cust_result.scalar_one_or_none()
        recent_sales.append({
            "type":           "sale",
            "invoice_number": i.invoice_number,
            "customer":       cust.name if cust else "Walk-in",
            "total":          float(i.total),
            "method":         i.payment_method or "cash",
            "time":           i.created_at.strftime("%H:%M") if i.created_at else "—",
            "date":           i.created_at.strftime("%Y-%m-%d") if i.created_at else "",
        })
    for ref in recent_refunds:
        cust_result = await db.execute(select(Customer).where(Customer.id == ref.customer_id))
        cust = cust_result.scalar_one_or_none()
        recent_sales.append({
            "type":           "refund",
            "invoice_number": ref.refund_number,
            "customer":       cust.name if cust else "—",
            "total":          -float(ref.total),
            "method":         ref.refund_method,
            "time":           ref.created_at.strftime("%H:%M") if ref.created_at else "—",
            "date":           ref.created_at.strftime("%Y-%m-%d") if ref.created_at else "",
        })
    recent_sales.sort(key=lambda x: x["date"] + x["time"], reverse=True)
    recent_sales = recent_sales[:10]

    r = await db.execute(select(func.count(RetailRefund.id)).where(func.date(RetailRefund.created_at) == today))
    ref_count_today = r.scalar() or 0
    r = await db.execute(select(func.count(RetailRefund.id)).where(func.date(RetailRefund.created_at) >= month_s))
    ref_count_month = r.scalar() or 0

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
        "expenses_month": round(expenses_month, 2),
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
        "out_of_stock_count": len(out_of_stock),
        "low_stock_count":    len(low_stock),
        "stock_value":        round(stock_value, 2),
        "out_of_stock": [{"sku": p.sku, "name": p.name, "stock": float(p.stock)} for p in out_of_stock[:20]],
        "low_stock":    [{"sku": p.sku, "name": p.name, "stock": float(p.stock)} for p in low_stock[:20]],
        "farm_month":     farm_month,
        "spoilage_month": round(spoilage_month, 2),
        "batches_month":  batches_month,
        "last7": last7,
        "top_products": [{"name":r.name,"qty":float(r.qty_sold),"revenue":float(r.revenue)} for r in top_products],
        "pay_methods":  [{"method":r.payment_method or "cash","count":r.count,"total":float(r.total)} for r in pay_methods],
        "recent_sales": recent_sales,
    }


# ── assistant endpoint ─────────────────────────────────────────────────

@router.post("/dashboard/assistant")
@limiter.limit("20/minute")
async def dashboard_assistant(
    request: Request,
    data: DashboardAssistantQuestion,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    return await answer_dashboard_question(
        db,
        question=data.question,
        current_user=current_user,
    )


# ── new: /dashboard/summary ────────────────────────────────────────────

@router.get("/dashboard/summary")
async def dashboard_summary(
    range: str = Query("today", regex="^(today|7d|30d|mtd|qtd|custom)$"),
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
        cache_key = f"dash_summary:{current_user.id}:{range}:{start}:{end}"
        cached = await redis_client.get(cache_key)
        if cached:
            await redis_client.aclose()
            return json.loads(cached)
    except Exception:
        redis_client = None
        cache_key    = None

    from app.services.dashboard_summary_service import get_summary
    data = await get_summary(db, range, start, end, current_user)

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
    from app.services.dashboard_insights_service import get_insights
    return await get_insights(db)


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
