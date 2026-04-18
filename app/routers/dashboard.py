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
        .where(B2BInvoice.status.in_(["unpaid","partial"]), B2BInvoice.invoice_type.in_(["cash", "full_payment"]))
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
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dashboard — Thunder ERP</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root{
    --bg:#060810;--surface:#0a0d18;--card:#0f1424;--card2:#151c30;
    --border:rgba(255,255,255,0.06);--border2:rgba(255,255,255,0.11);
    --green:#00ff9d;--blue:#4d9fff;--purple:#a855f7;--orange:#fb923c;
    --danger:#ff4d6d;--warn:#ffb547;--teal:#2dd4bf;--lime:#84cc16;
    --text:#f0f4ff;--sub:#8899bb;--muted:#445066;
    --sans:'Outfit',sans-serif;--mono:'JetBrains Mono',monospace;--r:14px;
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
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
body{font-family:var(--sans);background:var(--bg);color:var(--text);min-height:100vh;font-size:14px;}
body::before{content:'';position:fixed;inset:0;
    background:radial-gradient(ellipse 800px 600px at 10% 20%,rgba(0,255,157,.04) 0%,transparent 70%),
               radial-gradient(ellipse 600px 800px at 90% 80%,rgba(77,159,255,.04) 0%,transparent 70%);
    pointer-events:none;z-index:0;}
body>*{position:relative;z-index:1;}

/* NAV */
nav{position:sticky;top:0;z-index:100;display:flex;align-items:center;gap:10px;padding:0 24px;height:58px;background:rgba(10,13,24,.92);backdrop-filter:blur(20px);border-bottom:1px solid var(--border);flex-wrap:wrap;}
.logo{font-size:17px;font-weight:900;background:linear-gradient(135deg,var(--green),var(--blue));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;margin-right:10px;text-decoration:none;display:flex;align-items:center;gap:8px;}
.nav-link{padding:7px 12px;border-radius:8px;color:var(--sub);font-size:12px;font-weight:600;text-decoration:none;transition:all .2s;white-space:nowrap;}
.nav-link:hover{background:rgba(255,255,255,.05);color:var(--text);}
.nav-link.active{background:rgba(0,255,157,.1);color:var(--green);}
.nav-spacer{flex:1;}
.nav-date{font-family:var(--mono);font-size:12px;color:var(--muted);}

/* LAYOUT */
.content{max-width:1400px;margin:0 auto;padding:28px 24px;display:flex;flex-direction:column;gap:20px;}
.page-title{font-size:26px;font-weight:900;letter-spacing:-.5px;}
.page-sub{color:var(--muted);font-size:13px;margin-top:3px;}

/* GRID HELPERS */
.grid-4{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;}
.grid-3{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:14px;}
.grid-2-1{display:grid;grid-template-columns:2fr 1fr;gap:14px;}
.grid-1-2{display:grid;grid-template-columns:1fr 2fr;gap:14px;}
.hero-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;}
.ops-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:14px;}
@media(max-width:1100px){.grid-4{grid-template-columns:repeat(2,1fr);}}
@media(max-width:1200px){.ops-grid{grid-template-columns:repeat(3,minmax(0,1fr));}}
@media(max-width:1100px){.hero-grid{grid-template-columns:repeat(2,minmax(0,1fr));}}
@media(max-width:700px){.grid-4,.grid-3,.grid-2,.grid-2-1,.grid-1-2,.hero-grid,.ops-grid{grid-template-columns:1fr;}}

/* STAT CARDS */
.stat{background:linear-gradient(180deg,color-mix(in srgb,var(--card) 92%,white 8%),var(--card));border:1px solid var(--border);border-radius:var(--r);padding:18px 20px;display:flex;flex-direction:column;gap:8px;position:relative;overflow:hidden;transition:border-color .2s,transform .2s,box-shadow .2s;min-height:148px;}
.stat:hover{border-color:var(--border2);transform:translateY(-2px);}
.stat:hover{box-shadow:0 16px 36px rgba(0,0,0,.18);}
.stat::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;}
.stat::after{content:'';position:absolute;right:-40px;bottom:-48px;width:120px;height:120px;border-radius:50%;background:radial-gradient(circle,rgba(255,255,255,.08),transparent 70%);pointer-events:none;}
.stat.green::before {background:linear-gradient(90deg,var(--green),transparent);}
.stat.blue::before  {background:linear-gradient(90deg,var(--blue),transparent);}
.stat.purple::before{background:linear-gradient(90deg,var(--purple),transparent);}
.stat.orange::before{background:linear-gradient(90deg,var(--orange),transparent);}
.stat.danger::before{background:linear-gradient(90deg,var(--danger),transparent);}
.stat.warn::before  {background:linear-gradient(90deg,var(--warn),transparent);}
.stat.teal::before  {background:linear-gradient(90deg,var(--teal),transparent);}
.stat.lime::before  {background:linear-gradient(90deg,var(--lime),transparent);}
.stat-icon{font-size:22px;margin-bottom:2px;}
.stat-label{font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);}
.stat-value{font-family:var(--mono);font-size:28px;font-weight:700;}
.stat-value.green {color:var(--green);}
.stat-value.blue  {color:var(--blue);}
.stat-value.purple{color:var(--purple);}
.stat-value.orange{color:var(--orange);}
.stat-value.danger{color:var(--danger);}
.stat-value.warn  {color:var(--warn);}
.stat-value.teal  {color:var(--teal);}
.stat-value.lime  {color:var(--lime);}
.stat-sub{font-size:11px;color:var(--muted);margin-top:2px;}
.stat-kicker{font-family:var(--mono);font-size:10px;letter-spacing:1px;text-transform:uppercase;color:var(--muted);}
.stat-expense{background:
    radial-gradient(circle at top right, rgba(251,146,60,.16), transparent 42%),
    linear-gradient(180deg, color-mix(in srgb,var(--card) 90%, var(--orange) 10%), var(--card));}
.stat-expense .stat-sub{display:flex;justify-content:space-between;gap:10px;align-items:center;}
.trend-pill{display:inline-flex;align-items:center;padding:3px 8px;border-radius:999px;font-family:var(--mono);font-size:10px;font-weight:700;border:1px solid transparent;white-space:nowrap;}
.trend-up{background:rgba(255,77,109,.12);color:var(--danger);border-color:rgba(255,77,109,.2);}
.trend-down{background:rgba(0,255,157,.12);color:var(--green);border-color:rgba(0,255,157,.2);}
.trend-flat{background:rgba(255,255,255,.06);color:var(--sub);border-color:var(--border);}

/* PANEL */
.panel{background:var(--card);border:1px solid var(--border);border-radius:var(--r);overflow:hidden;}
.panel-header{display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--border);}
.panel-title{font-size:13px;font-weight:700;letter-spacing:.5px;}
.panel-badge{font-size:11px;font-weight:700;padding:3px 9px;border-radius:20px;}
.panel-body{padding:16px 18px;}
.insights-layout{display:grid;grid-template-columns:1.35fr .95fr .9fr;gap:16px;}
.insights-block{display:flex;flex-direction:column;gap:12px;}
.insights-subtitle{font-size:10px;font-weight:700;letter-spacing:1.3px;text-transform:uppercase;color:var(--muted);}
.insight-list{display:flex;flex-direction:column;gap:10px;}
.insight-item{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 14px;border:1px solid var(--border);border-radius:12px;background:linear-gradient(180deg,color-mix(in srgb,var(--card2) 88%,white 12%),var(--card2));}
.insight-copy{display:flex;flex-direction:column;gap:4px;min-width:0;}
.insight-label{font-size:12px;font-weight:700;color:var(--text);}
.insight-note{font-size:11px;color:var(--muted);line-height:1.35;}
.insight-value{font-family:var(--mono);font-size:14px;font-weight:700;white-space:nowrap;}
.insight-value.good{color:var(--green);}
.insight-value.warn{color:var(--warn);}
.insight-value.bad{color:var(--danger);}
.insight-value.blue{color:var(--blue);}
.action-list{display:grid;grid-template-columns:1fr 1fr;gap:10px;}
.action-link{display:flex;flex-direction:column;gap:5px;min-height:88px;padding:14px;border-radius:12px;border:1px solid var(--border);background:linear-gradient(180deg,color-mix(in srgb,var(--card2) 86%,white 14%),var(--card2));text-decoration:none;transition:border-color .18s,transform .18s,box-shadow .18s;}
.action-link:hover{border-color:var(--border2);transform:translateY(-1px);box-shadow:0 12px 28px rgba(0,0,0,.16);}
.action-title{font-size:13px;font-weight:700;color:var(--text);}
.action-note{font-size:11px;color:var(--sub);line-height:1.35;}
.mini-list{display:flex;flex-direction:column;gap:10px;}
.mini-row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 0;border-bottom:1px solid var(--border);}
.mini-row:last-child{border-bottom:none;padding-bottom:0;}
.mini-primary{display:flex;flex-direction:column;gap:3px;min-width:0;}
.mini-title{font-size:12px;font-weight:700;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.mini-meta{font-size:11px;color:var(--muted);}
.mini-value{font-family:var(--mono);font-size:12px;color:var(--green);white-space:nowrap;}
@media(max-width:1100px){.insights-layout{grid-template-columns:1fr;}.action-list{grid-template-columns:1fr 1fr;}}
@media(max-width:700px){.action-list{grid-template-columns:1fr;}}

/* CHART */
.chart-wrap{display:flex;align-items:flex-end;gap:6px;height:140px;padding:0 18px 14px;border-top:1px solid var(--border);}
.bar-col{display:flex;flex-direction:column;align-items:center;gap:4px;flex:1;}
.bar-outer{width:100%;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;flex:1;}
.bar{width:100%;border-radius:4px 4px 0 0;min-height:2px;transition:height .6s cubic-bezier(.34,1.2,.64,1);}
.bar-val{font-family:var(--mono);font-size:9px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%;text-align:center;}
.bar-day{font-size:10px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;}
.chart-legend{display:flex;gap:12px;padding:8px 18px 14px;font-size:11px;}
.legend-dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:5px;}

/* TABLE */
table{width:100%;border-collapse:collapse;}
thead{background:var(--card2);}
th{text-align:left;font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);padding:10px 14px;}
td{padding:10px 14px;border-top:1px solid var(--border);color:var(--sub);font-size:13px;}
td.bold{color:var(--text);font-weight:600;}
td.mono{font-family:var(--mono);}
tr:hover td{background:rgba(255,255,255,.02);}

/* BADGES */
.badge{display:inline-flex;padding:2px 8px;border-radius:20px;font-size:11px;font-weight:700;}
.badge-out  {background:rgba(255,77,109,.12);color:var(--danger);}
.badge-low  {background:rgba(255,181,71,.12);color:var(--warn);}
.badge-ok   {background:rgba(0,255,157,.1); color:var(--green);}
.badge-cash {background:rgba(0,255,157,.1); color:var(--green);}
.badge-visa {background:rgba(77,159,255,.1);color:var(--blue);}
.badge-unpaid{background:rgba(255,181,71,.1);color:var(--warn);}

/* PAY BARS */
.pay-row{display:flex;flex-direction:column;gap:5px;margin-bottom:12px;}
.pay-row:last-child{margin-bottom:0;}
.pay-info{display:flex;justify-content:space-between;font-size:12px;}
.pay-name{color:var(--sub);font-weight:600;text-transform:capitalize;}
.pay-num {font-family:var(--mono);color:var(--text);font-size:11px;}
.pay-track{height:6px;background:var(--card2);border-radius:4px;overflow:hidden;}
.pay-fill {height:100%;border-radius:4px;transition:width .8s cubic-bezier(.34,1.2,.64,1);}

/* TOP PRODUCTS BAR */
.prod-row{display:flex;align-items:center;gap:10px;margin-bottom:9px;}
.prod-row:last-child{margin-bottom:0;}
.prod-name{font-size:12px;color:var(--sub);width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex-shrink:0;}
.prod-track{flex:1;background:var(--card2);border-radius:4px;height:8px;overflow:hidden;}
.prod-fill {height:100%;border-radius:4px;background:linear-gradient(90deg,var(--green),var(--lime));transition:width .6s ease;}
.prod-val  {font-family:var(--mono);font-size:11px;color:var(--green);width:60px;text-align:right;flex-shrink:0;}

/* SECTION DIVIDER */
.section-label{font-size:11px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--muted);display:flex;align-items:center;gap:10px;}
.section-label::after{content:'';flex:1;height:1px;background:linear-gradient(90deg,var(--border2),transparent);}

/* SPINNER */
#loading{position:fixed;inset:0;z-index:999;background:var(--bg);display:flex;align-items:center;justify-content:center;flex-direction:column;gap:16px;}
.spinner{width:36px;height:36px;border:3px solid var(--border2);border-top-color:var(--green);border-radius:50%;animation:spin .7s linear infinite;}
@keyframes spin{to{transform:rotate(360deg);}}

</style>
    <script src="/static/auth-guard.js"></script>
</head>
<body>

<div id="loading">
    <div class="spinner"></div>
    <div style="color:var(--muted);font-size:13px">Loading dashboard…</div>
</div>

<nav>
    <a href="/home" class="logo">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
            <polygon points="13,2 4,14 11,14 11,22 20,10 13,10" fill="#f59e0b"/>
        </svg>
        Thunder ERP
    </a>
    <a href="/dashboard"  class="nav-link active">Dashboard</a>
    <a href="/pos"        class="nav-link">POS</a>
    <a href="/b2b/"       class="nav-link">B2B</a>
    <a href="/reports/"   class="nav-link">Reports</a>
    <a href="/inventory/" class="nav-link">Inventory</a>
    <span class="nav-spacer"></span>
    <div class="topbar-right">
        <span class="nav-date" id="nav-date"></span>
        <button class="mode-btn" id="mode-btn" onclick="toggleMode()" title="Toggle color mode">??</button>
        <div class="account-menu">
            <button class="user-pill" id="account-trigger" onclick="toggleAccountMenu(event)" aria-haspopup="menu" aria-expanded="false">
                <div class="user-avatar" id="user-avatar">A</div>
                <span class="user-name" id="user-name">Admin</span>
                <span class="menu-caret">&#9662;</span>
            </button>
            <div class="account-dropdown" id="account-dropdown" role="menu">
                <div class="account-head">
                    <div class="account-label">Signed in as</div>
                    <div class="account-email" id="user-email">&mdash;</div>
                </div>
                <a href="/users/password" class="account-item" role="menuitem">Change Password</a>
                <button class="account-item danger" onclick="logout()" role="menuitem">Sign out</button>
            </div>
        </div>
    </div>
</nav>

<div class="content">

    <div>
        <div class="page-title">Dashboard</div>
        <div class="page-sub" id="date-sub"></div>
    </div>

    <!-- ROW 1: REVENUE STATS -->
    <div class="hero-grid">
        <div class="stat green">
            <div class="stat-icon">💰</div>
            <div class="stat-label">Revenue Today</div>
            <div class="stat-value green" id="s-today">—</div>
            <div class="stat-sub" id="s-today-orders">— orders</div>
        </div>
        <div class="stat blue">
            <div class="stat-icon">📅</div>
            <div class="stat-label">Revenue This Month</div>
            <div class="stat-value blue" id="s-month">—</div>
            <div class="stat-sub" id="s-month-orders">POS + B2B</div>
        </div>
        <div class="stat orange stat-expense">
            <div class="stat-icon">🤝</div>
            <div class="stat-label">B2B Outstanding</div>
            <div class="stat-value orange" id="s-b2b-out">—</div>
            <div class="stat-sub" id="s-b2b-clients">— clients</div>
        </div>
        <div class="stat" style="border-color:rgba(255,77,109,.25);background:rgba(255,77,109,.04);">
            <div style="position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--danger),transparent);"></div>
            <div class="stat-icon">↩</div>
            <div class="stat-label" style="color:var(--danger)">Refunds This Month</div>
            <div class="stat-value" style="color:var(--danger)" id="s-ref-month">—</div>
            <div class="stat-sub" id="s-ref-sub" style="color:rgba(255,77,109,.6)">— refunds today: —</div>
        </div>
    </div>

    <!-- ROW 2: OPERATIONS STATS -->
    <div class="ops-grid">
        <div class="stat danger">
            <div class="stat-icon">🚫</div>
            <div class="stat-label">Out of Stock</div>
            <div class="stat-value danger" id="s-oos">—</div>
            <div class="stat-sub">products with 0 stock</div>
        </div>
        <div class="stat warn">
            <div class="stat-icon">⚠️</div>
            <div class="stat-label">Low Stock (≤5)</div>
            <div class="stat-value warn" id="s-low">—</div>
            <div class="stat-sub" id="s-stock-value">stock value —</div>
        </div>
        <div class="stat orange">
            <div class="stat-icon">ðŸ§¾</div>
            <div class="stat-kicker">Accounting</div>
            <div class="stat-label">Expenses This Month</div>
            <div class="stat-value orange" id="s-expenses">â€”</div>
            <div class="stat-sub" id="s-expenses-sub">last month: â€”</div>
        </div>
        <div class="stat teal">
            <div class="stat-icon">🌾</div>
            <div class="stat-label">Farm Deliveries</div>
            <div class="stat-value teal" id="s-farm">—</div>
            <div class="stat-sub">this month</div>
        </div>
        <div class="stat lime">
            <div class="stat-icon">⚙️</div>
            <div class="stat-label">Production Batches</div>
            <div class="stat-value lime" id="s-batches">—</div>
            <div class="stat-sub" id="s-spoilage">spoilage: —</div>
        </div>
    </div>

    <!-- ROW 3: INSIGHTS & ACTIONS -->
    <div class="panel">
        <div class="panel-header">
            <span class="panel-title">Insights &amp; Actions</span>
            <span class="panel-badge" style="background:rgba(77,159,255,.12);color:var(--blue)">operations cockpit</span>
        </div>
        <div class="panel-body">
            <div class="insights-layout">
                <div class="insights-block">
                    <div class="insights-subtitle">Needs Attention</div>
                    <div class="insight-list" id="attention-list"></div>
                </div>
                <div class="insights-block">
                    <div class="insights-subtitle">Quick Actions</div>
                    <div class="action-list">
                        <a class="action-link" href="/inventory/">
                            <span class="action-title">View Inventory</span>
                            <span class="action-note">Review stock gaps and movement details.</span>
                        </a>
                        <a class="action-link" href="/reports/">
                            <span class="action-title">View Reports</span>
                            <span class="action-note">Open financial and operational reporting.</span>
                        </a>
                        <a class="action-link" href="/b2b/">
                            <span class="action-title">Open B2B</span>
                            <span class="action-note">Check receivables and customer accounts.</span>
                        </a>
                        <a class="action-link" href="/expenses/">
                            <span class="action-title">Open Expenses</span>
                            <span class="action-note">Inspect this month’s cost activity.</span>
                        </a>
                        <a class="action-link" href="/pos">
                            <span class="action-title">Open POS</span>
                            <span class="action-note">Jump into live sales operations.</span>
                        </a>
                    </div>
                </div>
                <div class="insights-block">
                    <div class="insights-subtitle">Top Products Snapshot</div>
                    <div class="mini-list" id="insights-top-products"></div>
                </div>
            </div>
        </div>
    </div>

    <!-- ROW 4: SALES CHART -->
    <div class="panel">
        <div class="panel-header">
            <span class="panel-title">Revenue — Last 7 Days</span>
            <div class="chart-legend">
                <span><span class="legend-dot" style="background:var(--green)"></span>POS</span>
                <span><span class="legend-dot" style="background:var(--blue)"></span>B2B</span>
                <span><span class="legend-dot" style="background:var(--danger)"></span>Refunds</span>
            </div>
        </div>
        <div class="chart-wrap" id="chart-wrap"></div>
    </div>

    <!-- ROW 5: TOP PRODUCTS + PAYMENT METHODS -->
    <div class="grid-2">
        <div class="panel">
            <div class="panel-header">
                <span class="panel-title">Top Products This Month</span>
                <span class="panel-badge" style="background:rgba(0,255,157,.1);color:var(--green)">by revenue</span>
            </div>
            <div class="panel-body" id="top-products"></div>
        </div>
        <div class="panel">
            <div class="panel-header">
                <span class="panel-title">Payment Methods This Month</span>
            </div>
            <div class="panel-body" id="pay-methods"></div>
        </div>
    </div>

    <!-- ROW 6: RECENT SALES + OUT OF STOCK -->
    <div class="grid-2">
        <div class="panel">
            <div class="panel-header">
                <span class="panel-title">Recent Sales</span>
                <a href="/pos" style="font-size:12px;color:var(--blue);text-decoration:none">Go to POS →</a>
            </div>
            <table>
                <thead><tr><th>Invoice</th><th>Customer</th><th>Total</th><th>Method</th><th>Time</th></tr></thead>
                <tbody id="recent-body"></tbody>
            </table>
        </div>

        <div class="panel">
            <div class="panel-header">
                <span class="panel-title" style="color:var(--danger)">Out of Stock</span>
                <a href="/inventory/" style="font-size:12px;color:var(--blue);text-decoration:none">View Inventory →</a>
            </div>
            <table>
                <thead><tr><th>SKU</th><th>Product</th><th>Stock</th><th>Status</th></tr></thead>
                <tbody id="oos-body"></tbody>
            </table>
        </div>
    </div>

    <!-- ROW 7: LOW STOCK -->
    <div class="panel">
        <div class="panel-header">
            <span class="panel-title" style="color:var(--warn)">Low Stock Products (1–5 units)</span>
            <a href="/inventory/" style="font-size:12px;color:var(--blue);text-decoration:none">Manage Stock →</a>
        </div>
        <table>
            <thead><tr><th>SKU</th><th>Product</th><th>Current Stock</th><th>Status</th></tr></thead>
            <tbody id="low-body"></tbody>
        </table>
    </div>

</div>

<script>
  // Auth guard: redirect to login if the readable session cookie is absent
  function _hasAuthCookie() {
      return document.cookie.split(";").some(c => c.trim().startsWith("logged_in="));
  }
  if (!_hasAuthCookie()) { _redirectToLogin(); }

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
  initializeColorMode();
  initUser();
  let now = new Date();
document.getElementById("nav-date").innerText =
    now.toLocaleDateString("en-GB",{weekday:"long",year:"numeric",month:"long",day:"numeric"});
document.getElementById("date-sub").innerText =
    now.toLocaleDateString("en-GB",{weekday:"long",year:"numeric",month:"long",day:"numeric"});

function setupDashboardCards(){
    const expenseValue = document.getElementById("s-expenses");
    const expenseSub = document.getElementById("s-expenses-sub");
    if (!expenseValue || !expenseSub) return;

    const expenseCard = expenseValue.closest(".stat");
    if (expenseCard) expenseCard.classList.add("stat-expense");

    const icon = expenseCard ? expenseCard.querySelector(".stat-icon") : null;
    if (icon) icon.innerHTML = "&#128184;";

    const kicker = expenseCard ? expenseCard.querySelector(".stat-kicker") : null;
    if (kicker) kicker.innerText = "Accounting";

    expenseValue.innerText = "0.00";
    expenseSub.innerText = "last month: 0.00";

    if (expenseSub.parentElement && expenseSub.parentElement.classList.contains("stat-sub")) {
        expenseSub.parentElement.innerHTML =
            '<span id="s-expenses-sub">last month: 0.00</span>' +
            '<span class="trend-pill trend-flat" id="s-expenses-trend">vs last month</span>';
    }
}

function renderExpenseTrend(current, previous){
    const trendEl = document.getElementById("s-expenses-trend");
    const subEl = document.getElementById("s-expenses-sub");
    if (subEl) subEl.innerText = "last month: " + Number(previous || 0).toFixed(2);
    if (!trendEl) return;

    const diff = Number(current || 0) - Number(previous || 0);
    trendEl.className = "trend-pill";
    if (Math.abs(diff) < 0.005) {
        trendEl.classList.add("trend-flat");
        trendEl.innerText = "no change";
        return;
    }
    if (diff > 0) {
        trendEl.classList.add("trend-up");
        trendEl.innerText = "+" + diff.toFixed(2);
        return;
    }
    trendEl.classList.add("trend-down");
    trendEl.innerText = diff.toFixed(2);
}

setupDashboardCards();

function formatMoney(value){
    return Number(value || 0).toFixed(2) + " EGP";
}

function renderInsightsPanel(d){
    const attention = [
        {
            label: "Out of stock",
            note: "Products unavailable for immediate sale.",
            value: Number(d.out_of_stock_count || 0),
            tone: Number(d.out_of_stock_count || 0) > 0 ? "bad" : "good",
        },
        {
            label: "Low stock",
            note: "Products at or below the current reorder threshold.",
            value: Number(d.low_stock_count || 0),
            tone: Number(d.low_stock_count || 0) > 0 ? "warn" : "good",
        },
        {
            label: "B2B outstanding",
            note: Number(d.b2b_clients || 0) + " active B2B clients on file.",
            value: formatMoney(d.b2b_outstanding),
            tone: Number(d.b2b_outstanding || 0) > 0 ? "blue" : "good",
        },
        {
            label: "Expenses this month",
            note: "Compared against last month in the main KPI row.",
            value: formatMoney(d.expenses_month),
            tone: Number(d.expenses_month || 0) > 0 ? "warn" : "good",
        },
        {
            label: "Refunds this month",
            note: Number(d.ref_count_month || 0) + " refund transactions recorded.",
            value: formatMoney(d.ref_month),
            tone: Number(d.ref_month || 0) > 0 ? "bad" : "good",
        },
    ];

    const attentionHost = document.getElementById("attention-list");
    if (attentionHost) {
        attentionHost.innerHTML = attention.map(item => `
            <div class="insight-item">
                <div class="insight-copy">
                    <div class="insight-label">${item.label}</div>
                    <div class="insight-note">${item.note}</div>
                </div>
                <div class="insight-value ${item.tone}">${item.value}</div>
            </div>
        `).join("");
    }

    const topProductsHost = document.getElementById("insights-top-products");
    if (topProductsHost) {
        const topProducts = Array.isArray(d.top_products) ? d.top_products.slice(0, 3) : [];
        topProductsHost.innerHTML = topProducts.length
            ? topProducts.map((product, idx) => `
                <div class="mini-row">
                    <div class="mini-primary">
                        <div class="mini-title">${idx + 1}. ${product.name}</div>
                        <div class="mini-meta">${Number(product.qty || 0).toFixed(0)} units sold this month</div>
                    </div>
                    <div class="mini-value">${formatMoney(product.revenue)}</div>
                </div>
            `).join("")
            : `<div style="color:var(--muted);font-size:13px">No product movement to highlight yet this month.</div>`;
    }
}

async function load(){
    try {
        let d = await (await fetch("/dashboard/data")).json();

        // ── REVENUE STATS ──
        document.getElementById("s-today").innerText      = d.total_today.toFixed(2);
        document.getElementById("s-today-orders").innerText = "POS " + d.pos_today.toFixed(0) + "  +  B2B " + d.b2b_today.toFixed(0)
            + (d.ref_today > 0 ? "  −  " + d.ref_today.toFixed(0) + " refunds" : "");
        document.getElementById("s-month").innerText      = d.total_month.toFixed(2);
        document.getElementById("s-month-orders").innerText = "POS " + d.pos_month.toFixed(0) + "  +  B2B " + d.b2b_month.toFixed(0)
            + (d.ref_month > 0 ? "  −  " + d.ref_month.toFixed(0) + " refunds" : "");
        document.getElementById("s-b2b-out").innerText    = d.b2b_outstanding.toFixed(2);
        document.getElementById("s-b2b-clients").innerText= d.b2b_clients + " B2B clients";

        // ── REFUND CARD ──
        document.getElementById("s-ref-month").innerText = d.ref_month > 0 ? "−" + d.ref_month.toFixed(2) : "0.00";
        document.getElementById("s-ref-sub").innerText   = d.ref_count_month + " refunds this month  ·  today: −" + d.ref_today.toFixed(2);

        // ── OPERATIONS STATS ──
        document.getElementById("s-oos").innerText        = d.out_of_stock_count;
        document.getElementById("s-low").innerText        = d.low_stock_count;
        document.getElementById("s-stock-value").innerText= "inventory value: " + d.stock_value.toFixed(0);
        document.getElementById("s-expenses").innerText   = d.expenses_month.toFixed(2);
        renderExpenseTrend(d.expenses_month, d.expenses_last_month);
        document.getElementById("s-farm").innerText       = d.farm_month;
        document.getElementById("s-batches").innerText    = d.batches_month;
        document.getElementById("s-spoilage").innerText   = "spoilage this month: " + d.spoilage_month.toFixed(1);
        renderInsightsPanel(d);

        // ── CHART ──
        let maxVal = Math.max(...d.last7.map(x=>x.total), 1);
        let chartH  = 110; // px available for bars
        document.getElementById("chart-wrap").innerHTML = d.last7.map(x => {
            let posH  = Math.round((x.pos / maxVal) * chartH);
            let b2bH  = Math.round((x.b2b / maxVal) * chartH);
            let refH  = x.refunds ? Math.round((x.refunds / maxVal) * chartH) : 0;
            let dayLbl = new Date(x.date + "T12:00:00").toLocaleDateString("en-GB",{weekday:"short"});
            let isToday = x.date === new Date().toISOString().split("T")[0];
            return `<div class="bar-col">
                <div class="bar-val">${x.total>0?x.total.toFixed(0):""}</div>
                <div class="bar-outer">
                    ${b2bH>0?`<div class="bar" style="height:${b2bH}px;background:var(--blue);opacity:.85;border-radius:3px 3px 0 0"></div>`:""}
                    ${posH>0?`<div class="bar" style="height:${posH}px;background:linear-gradient(180deg,var(--green),var(--lime));border-radius:3px 3px 0 0;${isToday?"box-shadow:0 0 12px rgba(0,255,157,.4)":""}"></div>`:""}
                    ${refH>0?`<div class="bar" style="height:${refH}px;background:var(--danger);opacity:.7;border-radius:3px 3px 0 0;margin-top:2px;" title="Refunds: ${x.refunds}"></div>`:""}
                    ${posH===0&&b2bH===0?`<div style="height:2px;width:100%;background:var(--border2);border-radius:2px"></div>`:""}
                </div>
                <div class="bar-day" style="color:${isToday?"var(--green)":"var(--muted)"}">${dayLbl}</div>
            </div>`;
        }).join("");

        // ── TOP PRODUCTS ──
        let maxRev = d.top_products.length ? d.top_products[0].revenue : 1;
        document.getElementById("top-products").innerHTML = d.top_products.length
            ? d.top_products.map(p=>`<div class="prod-row">
                <div class="prod-name">${p.name}</div>
                <div class="prod-track"><div class="prod-fill" style="width:${(p.revenue/maxRev*100).toFixed(1)}%"></div></div>
                <div class="prod-val">${p.revenue.toFixed(0)}</div>
              </div>`).join("")
            : `<div style="color:var(--muted);font-size:13px">No sales this month yet</div>`;

        // ── PAYMENT METHODS ──
        let totalPay = d.pay_methods.reduce((s,p)=>s+p.total,0) || 1;
        const payColor = m => {
            if(!m) return "var(--green)";
            m=m.toLowerCase();
            if(m.includes("visa")||m.includes("card")) return "var(--blue)";
            if(m.includes("cash")) return "var(--green)";
            return "var(--purple)";
        };
        document.getElementById("pay-methods").innerHTML = d.pay_methods.length
            ? d.pay_methods.map(p=>`<div class="pay-row">
                <div class="pay-info">
                    <span class="pay-name">${p.method}</span>
                    <span class="pay-num">${p.total.toFixed(2)} EGP &nbsp;·&nbsp; ${p.count} orders &nbsp;·&nbsp; ${(p.total/totalPay*100).toFixed(1)}%</span>
                </div>
                <div class="pay-track"><div class="pay-fill" style="width:${(p.total/totalPay*100).toFixed(1)}%;background:${payColor(p.method)}"></div></div>
              </div>`).join("")
            : `<div style="color:var(--muted);font-size:13px">No sales this month yet</div>`;

        // ── RECENT SALES ──
        document.getElementById("recent-body").innerHTML = d.recent_sales.length
            ? d.recent_sales.map(s => {
                const isRefund = s.type === "refund";
                const numColor = isRefund ? "var(--danger)" : "var(--green)";
                const numText  = isRefund ? "−" + Math.abs(s.total).toFixed(2) : s.total.toFixed(2);
                const badge    = isRefund
                    ? `<span class="badge" style="background:rgba(255,77,109,.12);color:var(--danger);border:1px solid rgba(255,77,109,.3)">↩ ${s.method}</span>`
                    : `<span class="badge ${s.method?.toLowerCase().includes("visa")?"badge-visa":"badge-cash"}">${s.method||"cash"}</span>`;
                const refLabel = isRefund
                    ? `<span style="font-size:10px;color:var(--danger);font-weight:700;letter-spacing:.5px">REFUND</span>`
                    : "";
                return `<tr>
                    <td class="mono" style="font-size:11px;color:${isRefund?"var(--danger)":"var(--lime)"}">${s.invoice_number}${refLabel?`<br>${refLabel}`:""}</td>
                    <td class="bold">${s.customer}</td>
                    <td class="mono" style="color:${numColor};font-weight:700">${numText}</td>
                    <td>${badge}</td>
                    <td class="mono" style="font-size:11px;color:var(--muted)">${s.time}</td>
                  </tr>`;
            }).join("")
            : `<tr><td colspan="5" style="color:var(--muted);padding:20px;text-align:center">No sales yet today</td></tr>`;

        // ── OUT OF STOCK ──
        document.getElementById("oos-body").innerHTML = d.out_of_stock.length
            ? d.out_of_stock.map(p=>`<tr>
                <td class="mono" style="font-size:11px;color:var(--muted)">${p.sku}</td>
                <td class="bold">${p.name}</td>
                <td class="mono" style="color:var(--danger);font-weight:700">0</td>
                <td><span class="badge badge-out">Out of Stock</span></td>
              </tr>`).join("")
            : `<tr><td colspan="4" style="color:var(--green);padding:20px;text-align:center">✅ All products in stock</td></tr>`;

        // ── LOW STOCK ──
        document.getElementById("low-body").innerHTML = d.low_stock.length
            ? d.low_stock.map(p=>`<tr>
                <td class="mono" style="font-size:11px;color:var(--muted)">${p.sku}</td>
                <td class="bold">${p.name}</td>
                <td class="mono" style="color:var(--warn);font-weight:700">${p.stock.toFixed(2)}</td>
                <td><span class="badge badge-low">⚠ Low</span></td>
              </tr>`).join("")
            : `<tr><td colspan="4" style="color:var(--green);padding:20px;text-align:center">✅ No low stock items</td></tr>`;

        document.getElementById("loading").style.display = "none";

    } catch(e){
        console.error(e);
        document.getElementById("loading").innerHTML =
            `<div style="color:var(--danger)">Failed to load. <a href="/dashboard" style="color:var(--green)">Retry</a></div>`;
    }
}

load();

</script>
</body>
</html>"""
