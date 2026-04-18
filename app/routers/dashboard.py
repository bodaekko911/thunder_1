from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select
from datetime import date, datetime, timedelta
from pydantic import BaseModel

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


@router.get("/dashboard/data")
async def dashboard_data(db: AsyncSession = Depends(get_async_session)):
    today    = date.today()
    now      = datetime.utcnow()
    month_s  = today.replace(day=1)
    year_s   = today.replace(month=1, day=1)

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

    # ── COMBINED REVENUE ───────────────────────────────
    total_today = pos_today + b2b_today
    total_month = pos_month + b2b_month
    total_year  = pos_year  + b2b_year

    expense_summary = await get_expense_summary(db)
    expenses_month = float(expense_summary["this_month"])
    expenses_last_month = float(expense_summary["last_month"])

    # ── CUSTOMERS ──────────────────────────────────────
    r = await db.execute(select(func.count(Customer.id)))
    total_customers = r.scalar() or 0
    if hasattr(Customer, 'created_at'):
        r = await db.execute(select(func.count(Customer.id)).where(func.date(Customer.created_at) >= month_s))
        new_customers_month = r.scalar() or 0
    else:
        new_customers_month = 0

    # ── INVENTORY ──────────────────────────────────────
    r = await db.execute(select(Product).where(Product.is_active == True))
    all_products   = r.scalars().all()
    out_of_stock   = [p for p in all_products if float(p.stock) <= 0]
    low_stock      = [p for p in all_products if 0 < float(p.stock) <= 5]
    total_products = len(all_products)
    stock_value    = sum(float(p.stock) * float(p.price) for p in all_products)

    # ── FARM ───────────────────────────────────────────
    r = await db.execute(select(func.count(FarmDelivery.id)).where(FarmDelivery.delivery_date >= month_s))
    farm_month = r.scalar() or 0

    # ── SPOILAGE ───────────────────────────────────────
    r = await db.execute(select(func.sum(SpoilageRecord.qty)).where(SpoilageRecord.spoilage_date >= month_s))
    spoilage_month = float(r.scalar() or 0)

    # ── PRODUCTION ─────────────────────────────────────
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

    # ── TOP 10 PRODUCTS THIS MONTH ────────────────────
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

    # ── PAYMENT METHODS THIS MONTH ────────────────────
    pay_result = await db.execute(
        select(Invoice.payment_method,
               func.count(Invoice.id).label("count"),
               func.sum(Invoice.total).label("total"))
        .where(func.date(Invoice.created_at) >= month_s, Invoice.status == "paid")
        .group_by(Invoice.payment_method)
    )
    pay_methods = pay_result.all()

    # ── RECENT TRANSACTIONS (sales + refunds mixed, sorted by time) ──────────
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
    # Sort combined list by date+time descending, take top 10
    recent_sales.sort(key=lambda x: x["date"] + x["time"], reverse=True)
    recent_sales = recent_sales[:10]

    r = await db.execute(select(func.count(RetailRefund.id)).where(func.date(RetailRefund.created_at) == today))
    ref_count_today = r.scalar() or 0
    r = await db.execute(select(func.count(RetailRefund.id)).where(func.date(RetailRefund.created_at) >= month_s))
    ref_count_month = r.scalar() or 0

    return {
        # Revenue
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
        # Refunds
        "ref_today":   round(ref_today, 2),
        "ref_month":   round(ref_month, 2),
        "ref_count_today": ref_count_today,
        "ref_count_month": ref_count_month,
        # Counts
        "invoices_today":   invoices_today,
        "invoices_month":   invoices_month,
        "total_customers":  total_customers,
        "b2b_clients":      b2b_clients,
        # Inventory
        "total_products":   total_products,
        "out_of_stock_count": len(out_of_stock),
        "low_stock_count":    len(low_stock),
        "stock_value":        round(stock_value, 2),
        "out_of_stock": [{"sku": p.sku, "name": p.name, "stock": float(p.stock)} for p in out_of_stock[:20]],
        "low_stock":    [{"sku": p.sku, "name": p.name, "stock": float(p.stock)} for p in low_stock[:20]],
        # Operations
        "farm_month":     farm_month,
        "spoilage_month": round(spoilage_month, 2),
        "batches_month":  batches_month,
        # Charts
        "last7": last7,
        "top_products": [{"name":r.name,"qty":float(r.qty_sold),"revenue":float(r.revenue)} for r in top_products],
        "pay_methods":  [{"method":r.payment_method or "cash","count":r.count,"total":float(r.total)} for r in pay_methods],
        "recent_sales": recent_sales,
    }


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


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_ui():
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Operations Dashboard — Thunder ERP</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root{
    --bg: #090b14;
    --surface: #0f121f;
    --card: #141829;
    --card-hover: #1a1f33;
    --border: rgba(255,255,255,0.08);
    --border-strong: rgba(255,255,255,0.15);
    --accent: #4d9fff;
    --success: #10b981;
    --warning: #f59e0b;
    --error: #ef4444;
    --text: #f1f5f9;
    --text-sub: #94a3b8;
    --text-muted: #64748b;
    --sans: 'Outfit', sans-serif;
    --mono: 'JetBrains Mono', monospace;
    --r: 8px;
}
body.light{
    --bg: #f8fafc; --surface: #ffffff; --card: #ffffff; --card-hover: #f1f5f9;
    --border: #e2e8f0; --border-strong: #cbd5e1;
    --accent: #2563eb; --success: #059669; --warning: #d97706; --error: #dc2626;
    --text: #0f172a; --text-sub: #475569; --text-muted: #64748b;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
body{font-family:var(--sans);background:var(--bg);color:var(--text);min-height:100vh;overflow-y:scroll;line-height:1.5;}

nav{position:sticky;top:0;z-index:100;display:flex;align-items:center;padding:0 24px;height:56px;background:var(--surface);border-bottom:1px solid var(--border);}
.logo{font-size:16px;font-weight:800;color:var(--accent);text-decoration:none;display:flex;align-items:center;gap:8px;margin-right:24px;}
.nav-link{padding:6px 12px;border-radius:var(--r);color:var(--text-sub);font-size:13px;font-weight:500;text-decoration:none;transition:all .15s;}
.nav-link:hover{background:var(--card-hover);color:var(--text);}
.nav-link.active{background:rgba(77,159,255,0.1);color:var(--accent);}
.nav-spacer{flex:1;}

.content{max-width:1400px;margin:0 auto;padding:24px;display:flex;flex-direction:column;gap:24px;}
.header-row{display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:8px;}
.page-title{font-size:22px;font-weight:700;letter-spacing:-0.5px;}
.page-sub{color:var(--text-muted);font-size:13px;}

.top-stats{display:grid;grid-template-columns:repeat(4, 1fr);gap:16px;}
.mini-stat{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:16px;display:flex;flex-direction:column;gap:4px;}
.ms-label{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:var(--text-muted);}
.ms-value{font-family:var(--mono);font-size:20px;font-weight:700;}

.main-grid{display:grid;grid-template-columns:1.8fr 1fr;gap:24px;}
@media(max-width:1000px){.main-grid{grid-template-columns:1fr;}}

.panel{background:var(--card);border:1px solid var(--border);border-radius:var(--r);display:flex;flex-direction:column;overflow:hidden;}
.panel-h{padding:14px 18px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;background:rgba(255,255,255,0.02);}
.panel-t{font-size:13px;font-weight:700;color:var(--text-sub);}
.panel-b{padding:18px;}

.card-group{display:grid;grid-template-columns:repeat(auto-fit, minmax(140px, 1fr));gap:12px;margin-bottom:20px;}
.kpi-box{padding:12px;border-radius:var(--r);border:1px solid var(--border);background:rgba(255,255,255,0.01);}
.kpi-l{font-size:10px;font-weight:700;color:var(--text-muted);text-transform:uppercase;margin-bottom:4px;}
.kpi-v{font-family:var(--mono);font-size:16px;font-weight:700;}

.chart-container{height:180px;display:flex;align-items:flex-end;gap:8px;padding-top:20px;border-bottom:1px solid var(--border);}
.chart-bar-wrap{flex:1;display:flex;flex-direction:column;justify-content:flex-end;height:100%;position:relative;}
.chart-bar{width:100%;background:var(--accent);border-radius:3px 3px 0 0;min-height:2px;transition:height 0.4s ease;}
.chart-label{font-size:10px;color:var(--text-muted);text-align:center;margin-top:8px;font-weight:600;}
.chart-hover-val{position:absolute;top:-20px;left:50%;transform:translateX(-50%);font-family:var(--mono);font-size:9px;color:var(--accent);font-weight:700;}

table{width:100%;border-collapse:collapse;}
th{text-align:left;font-size:10px;font-weight:700;color:var(--text-muted);text-transform:uppercase;padding:10px 12px;border-bottom:1px solid var(--border);}
td{padding:10px 12px;font-size:13px;border-bottom:1px solid var(--border);color:var(--text-sub);}
tr:last-child td{border-bottom:none;}
.bold{color:var(--text);font-weight:600;}
.mono{font-family:var(--mono);}

.alert-item{display:flex;justify-content:space-between;align-items:center;padding:12px;border-radius:var(--r);background:rgba(239,68,68,0.05);border:1px solid rgba(239,68,68,0.1);margin-bottom:8px;}
.alert-item.warn{background:rgba(245,158,11,0.05);border:1px solid rgba(245,158,11,0.1);}
.alert-info{display:flex;flex-direction:column;gap:2px;}
.alert-label{font-size:12px;font-weight:600;color:var(--text);}
.alert-sub{font-size:11px;color:var(--text-muted);}
.alert-val{font-family:var(--mono);font-weight:700;color:var(--error);}
.alert-item.warn .alert-val{color:var(--warning);}

.action-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;}
.action-btn{display:flex;align-items:center;gap:10px;padding:12px;background:var(--surface);border:1px solid var(--border);border-radius:var(--r);text-decoration:none;color:var(--text-sub);transition:all 0.15s;}
.action-btn:hover{border-color:var(--accent);color:var(--accent);background:rgba(77,159,255,0.03);}
.action-btn span{font-size:12px;font-weight:600;}

.top-prod-row{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border);}
.top-prod-name{font-size:12px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:160px;}
.top-prod-rev{font-family:var(--mono);font-size:12px;font-weight:600;color:var(--success);}

.badge{padding:2px 6px;border-radius:4px;font-size:10px;font-weight:700;text-transform:uppercase;}
.badge-pos{background:rgba(16,185,129,0.1);color:var(--success);}
.badge-b2b{background:rgba(77,159,255,0.1);color:var(--accent);}
.badge-refund{background:rgba(239,68,68,0.1);color:var(--error);}

.account-menu{position:relative;}
.user-pill{display:flex;align-items:center;gap:8px;background:var(--surface);border:1px solid var(--border);border-radius:20px;padding:4px 12px 4px 4px;cursor:pointer;}
.user-avatar{width:24px;height:24px;background:var(--accent);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;color:white;}
.user-name{font-size:12px;font-weight:600;color:var(--text-sub);}
.account-dropdown{position:absolute;right:0;top:100%;margin-top:8px;width:180px;background:var(--surface);border:1px solid var(--border-strong);border-radius:var(--r);display:none;flex-direction:column;overflow:hidden;box-shadow:0 10px 25px rgba(0,0,0,0.1);}
.account-dropdown.open{display:flex;}
.account-item{padding:10px 14px;font-size:12px;color:var(--text-sub);text-decoration:none;transition:background 0.1s;}
.account-item:hover{background:var(--card-hover);color:var(--text);}
.mode-btn{background:none;border:none;color:var(--text-muted);cursor:pointer;font-size:16px;margin-right:8px;display:flex;align-items:center;justify-content:center;}

#loading{position:fixed;inset:0;background:var(--bg);display:flex;align-items:center;justify-content:center;z-index:1000;}
.spinner{width:24px;height:24px;border:2px solid var(--border-strong);border-top-color:var(--accent);border-radius:50%;animation:spin 0.8s linear infinite;}
@keyframes spin{to{transform:rotate(360deg);}}
</style>
<script src="/static/auth-guard.js"></script>
</head>
<body>

<div id="loading">
    <div class="spinner"></div>
</div>

<nav>
    <a href="/home" class="logo">Thunder ERP</a>
    <a href="/dashboard"  class="nav-link active">Dashboard</a>
    <a href="/pos"        class="nav-link">POS</a>
    <a href="/b2b/"       class="nav-link">B2B</a>
    <a href="/reports/"   class="nav-link">Reports</a>
    <a href="/inventory/" class="nav-link">Inventory</a>
    <span class="nav-spacer"></span>
    <button class="mode-btn" id="mode-btn" onclick="toggleMode()">🌙</button>
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

<div class="content">
    <div class="header-row">
        <div>
            <div class="page-title">Operations Overview</div>
            <div class="page-sub" id="date-display"></div>
        </div>
    </div>

    <div class="top-stats">
        <div class="mini-stat">
            <span class="ms-label">Revenue This Month</span>
            <span class="ms-value" id="ms-revenue">—</span>
        </div>
        <div class="mini-stat">
            <span class="ms-label">Expenses This Month</span>
            <span class="ms-value" id="ms-expenses">—</span>
        </div>
        <div class="mini-stat">
            <span class="ms-label">Outstanding Receivables</span>
            <span class="ms-value" id="ms-receivables">—</span>
        </div>
        <div class="mini-stat">
            <span class="ms-label">Low Stock Alerts</span>
            <span class="ms-value" id="ms-lowstock">—</span>
        </div>
    </div>

    <div class="main-grid">
        <!-- LEFT COLUMN -->
        <div style="display:flex;flex-direction:column;gap:24px;">
            <div class="panel">
                <div class="panel-h"><span class="panel-t">Business Performance</span></div>
                <div class="panel-b">
                    <div class="card-group">
                        <div class="kpi-box">
                            <div class="kpi-l">Today's Revenue</div>
                            <div class="kpi-v" id="kpi-today-rev">—</div>
                        </div>
                        <div class="kpi-box">
                            <div class="kpi-l">Retail Refunds</div>
                            <div class="kpi-v" style="color:var(--error)" id="kpi-refunds">—</div>
                        </div>
                        <div class="kpi-box">
                            <div class="kpi-l">Farm Intake</div>
                            <div class="kpi-v" id="kpi-farm">—</div>
                        </div>
                    </div>
                    <div style="font-size:11px;font-weight:700;color:var(--text-muted);text-transform:uppercase;margin-bottom:12px;">7-Day Revenue Trend</div>
                    <div class="chart-container" id="revenue-chart"></div>
                </div>
            </div>

            <div class="panel">
                <div class="panel-h"><span class="panel-t">Recent Transactions</span></div>
                <div class="panel-b" style="padding:0">
                    <table>
                        <thead><tr><th>Invoice</th><th>Customer</th><th>Total</th><th>Method</th><th>Time</th></tr></thead>
                        <tbody id="recent-transactions"></tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- RIGHT COLUMN -->
        <div style="display:flex;flex-direction:column;gap:24px;">
            <div class="panel">
                <div class="panel-h"><span class="panel-t">Needs Attention</span></div>
                <div class="panel-b" id="alerts-container"></div>
            </div>

            <div class="panel">
                <div class="panel-h"><span class="panel-t">Quick Actions</span></div>
                <div class="panel-b">
                    <div class="action-grid">
                        <a href="/inventory/" class="action-btn"><span>Inventory Health</span></a>
                        <a href="/reports/" class="action-btn"><span>Financial Reports</span></a>
                        <a href="/pos/" class="action-btn"><span>Launch POS</span></a>
                        <a href="/accounting/" class="action-btn"><span>Accounting</span></a>
                        <a href="/b2b/" class="action-btn"><span>B2B Accounts</span></a>
                        <a href="/production/" class="action-btn"><span>Production</span></a>
                    </div>
                </div>
            </div>

            <div class="panel">
                <div class="panel-h"><span class="panel-t">Top Products (Month)</span></div>
                <div class="panel-b" id="top-products-list"></div>
            </div>
        </div>
    </div>

    <!-- LOWER SECTION -->
    <div class="panel">
        <div class="panel-h"><span class="panel-t">Inventory Status: Out of Stock</span></div>
        <div class="panel-b" style="padding:0">
            <table>
                <thead><tr><th>SKU</th><th>Product</th><th>On Hand</th><th>Status</th></tr></thead>
                <tbody id="oos-table"></tbody>
            </table>
        </div>
    </div>

    <div class="panel">
        <div class="panel-h"><span class="panel-t">Inventory Status: Low Stock (≤ 5)</span></div>
        <div class="panel-b" style="padding:0">
            <table>
                <thead><tr><th>SKU</th><th>Product</th><th>On Hand</th><th>Status</th></tr></thead>
                <tbody id="lowstock-table"></tbody>
            </table>
        </div>
    </div>
</div>

<script>
function toggleAccountMenu(e){
    e.stopPropagation();
    document.getElementById("account-dropdown").classList.toggle("open");
}
document.addEventListener("click", () => document.getElementById("account-dropdown").classList.remove("open"));

function toggleMode(){
    const isLight = document.body.classList.toggle("light");
    localStorage.setItem("colorMode", isLight ? "light" : "dark");
    document.getElementById("mode-btn").innerText = isLight ? "☀️" : "🌙";
}

async function initUser(){
    const r = await fetch("/auth/me");
    if(!r.ok) return;
    const u = await r.json();
    document.getElementById("user-name").innerText = u.name;
    document.getElementById("user-avatar").innerText = u.name[0].toUpperCase();
}

async function logout(){
    await fetch("/auth/logout", { method: "POST" });
    window.location.href = "/";
}

function formatCurrency(val){
    return Number(val || 0).toLocaleString('en-GB', {minimumFractionDigits:2}) + " EGP";
}

async function loadDashboard(){
    try {
        const res = await fetch("/dashboard/data");
        const d = await res.json();

        // Header Metrics
        document.getElementById("ms-revenue").innerText = formatCurrency(d.total_month);
        document.getElementById("ms-expenses").innerText = formatCurrency(d.expenses_month);
        document.getElementById("ms-receivables").innerText = formatCurrency(d.b2b_outstanding);
        document.getElementById("ms-lowstock").innerText = d.low_stock_count;

        // Performance KPIs
        document.getElementById("kpi-today-rev").innerText = formatCurrency(d.total_today);
        document.getElementById("kpi-refunds").innerText = formatCurrency(d.ref_month);
        document.getElementById("kpi-farm").innerText = d.farm_month + " deliveries";

        // Revenue Chart
        const maxVal = Math.max(...d.last7.map(x => x.total), 1);
        document.getElementById("revenue-chart").innerHTML = d.last7.map(day => {
            const h = (day.total / maxVal) * 100;
            const date = new Date(day.date).toLocaleDateString('en-GB', {weekday:'short'});
            return `
                <div class="chart-bar-wrap">
                    <div class="chart-hover-val">${Math.round(day.total)}</div>
                    <div class="chart-bar" style="height:${h}%"></div>
                    <div class="chart-label">${date}</div>
                </div>
            `;
        }).join("");

        // Recent Transactions
        document.getElementById("recent-transactions").innerHTML = d.recent_sales.map(s => `
            <tr>
                <td class="bold">${s.invoice_number}</td>
                <td>${s.customer}</td>
                <td class="mono bold" style="color:${s.total < 0 ? 'var(--error)' : 'var(--success)'}">${formatCurrency(s.total)}</td>
                <td><span class="badge ${s.type === 'refund' ? 'badge-refund' : 'badge-pos'}">${s.method}</span></td>
                <td class="mono">${s.time}</td>
            </tr>
        `).join("") || '<tr><td colspan="5" style="text-align:center">No recent activity</td></tr>';

        // Alerts (Needs Attention)
        const alerts = [
            {label: 'Out of Stock Items', val: d.out_of_stock_count, type: 'error', sub: 'Immediate restocking required'},
            {label: 'Low Stock Products', val: d.low_stock_count, type: 'warn', sub: 'Below threshold level (5)'},
            {label: 'Total Receivables', val: formatCurrency(d.b2b_outstanding), type: 'warn', sub: 'Unpaid B2B invoices'}
        ];
        document.getElementById("alerts-container").innerHTML = alerts.map(a => `
            <div class="alert-item ${a.type === 'warn' ? 'warn' : ''}">
                <div class="alert-info">
                    <span class="alert-label">${a.label}</span>
                    <span class="alert-sub">${a.sub}</span>
                </div>
                <span class="alert-val">${a.val}</span>
            </div>
        `).join("");

        // Top Products
        document.getElementById("top-products-list").innerHTML = d.top_products.slice(0, 5).map(p => `
            <div class="top-prod-row">
                <span class="top-prod-name">${p.name}</span>
                <span class="top-prod-rev">${formatCurrency(p.revenue)}</span>
            </div>
        `).join("");

        // Inventory Tables
        document.getElementById("oos-table").innerHTML = d.out_of_stock.map(p => `
            <tr>
                <td class="mono">${p.sku}</td>
                <td class="bold">${p.name}</td>
                <td class="mono bold" style="color:var(--error)">0</td>
                <td><span class="badge badge-refund">Critical</span></td>
            </tr>
        `).join("") || '<tr><td colspan="4" style="text-align:center">Full availability</td></tr>';

        document.getElementById("lowstock-table").innerHTML = d.low_stock.map(p => `
            <tr>
                <td class="mono">${p.sku}</td>
                <td class="bold">${p.name}</td>
                <td class="mono bold" style="color:var(--warning)">${p.stock}</td>
                <td><span class="badge badge-b2b">Low</span></td>
            </tr>
        `).join("") || '<tr><td colspan="4" style="text-align:center">Healthy levels</td></tr>';

        document.getElementById("date-display").innerText = new Date().toLocaleDateString('en-GB', {weekday:'long', year:'numeric', month:'long', day:'numeric'});
        document.getElementById("loading").style.display = "none";
    } catch(err) {
        console.error(err);
        document.getElementById("loading").innerHTML = `<div style="color:var(--error)">Initialization error. Please refresh.</div>`;
    }
}

const isLight = localStorage.getItem("colorMode") === "light";
document.body.classList.toggle("light", isLight);
document.getElementById("mode-btn").innerText = isLight ? "☀️" : "🌙";

initUser();
loadDashboard();
</script>
</body>
</html>"""
