from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import date, datetime, timedelta

from app.database import get_db
from app.models.invoice import Invoice, InvoiceItem
from app.models.product import Product
from app.models.customer import Customer
from app.models.b2b import B2BClient, B2BInvoice
from app.models.farm import FarmDelivery
from app.models.spoilage import SpoilageRecord
from app.models.production import ProductionBatch

router = APIRouter(tags=["Dashboard"])


@router.get("/dashboard/data")
def dashboard_data(db: Session = Depends(get_db)):
    today    = date.today()
    now      = datetime.utcnow()
    month_s  = today.replace(day=1)
    year_s   = today.replace(month=1, day=1)
    week_ago = today - timedelta(days=7)

    # ── POS SALES ──────────────────────────────────────
    pos_today = float(db.query(func.sum(Invoice.total)).filter(
        func.date(Invoice.created_at) == today, Invoice.status == "paid").scalar() or 0)
    pos_month = float(db.query(func.sum(Invoice.total)).filter(
        func.date(Invoice.created_at) >= month_s, Invoice.status == "paid").scalar() or 0)
    pos_year  = float(db.query(func.sum(Invoice.total)).filter(
        func.date(Invoice.created_at) >= year_s, Invoice.status == "paid").scalar() or 0)

    invoices_today = db.query(func.count(Invoice.id)).filter(
        func.date(Invoice.created_at) == today).scalar() or 0
    invoices_month = db.query(func.count(Invoice.id)).filter(
        func.date(Invoice.created_at) >= month_s, Invoice.status == "paid").scalar() or 0

    # ── B2B SALES ──────────────────────────────────────
    # Cash invoices: revenue on invoice creation date
    # Full payment & consignment: revenue from journal entries dated correctly
    from app.models.accounting import Account, Journal, JournalEntry
    rev_acc = db.query(Account).filter(Account.code == "4000").first()

    def journal_revenue(d_from, d_to):
        if not rev_acc: return 0.0
        entries = db.query(func.sum(JournalEntry.credit)).join(Journal).filter(
            JournalEntry.account_id == rev_acc.id,
            Journal.created_at >= d_from,
            Journal.created_at <= d_to,
            Journal.ref_type.in_(["b2b", "b2b_invoice", "consignment_payment", "consignment"])
        ).scalar()
        return float(entries or 0)

    from datetime import datetime as dt
    today_start = dt.combine(today, dt.min.time())
    today_end   = dt.combine(today, dt.max.time())
    month_start_dt = dt.combine(month_s, dt.min.time())
    year_start_dt  = dt.combine(year_s,  dt.min.time())
    now_dt = dt.now()

    b2b_today = journal_revenue(today_start, today_end)
    b2b_month = journal_revenue(month_start_dt, now_dt)
    b2b_year  = journal_revenue(year_start_dt,  now_dt)

    b2b_outstanding = float(db.query(func.sum(B2BInvoice.total - B2BInvoice.amount_paid)).filter(
        B2BInvoice.status.in_(["unpaid","partial"]),
        B2BInvoice.invoice_type.in_(["cash", "full_payment"])).scalar() or 0)
    b2b_clients = db.query(func.count(B2BClient.id)).filter(B2BClient.is_active == True).scalar() or 0

    # ── COMBINED REVENUE ───────────────────────────────
    total_today = pos_today + b2b_today
    total_month = pos_month + b2b_month
    total_year  = pos_year  + b2b_year

    # ── CUSTOMERS ──────────────────────────────────────
    total_customers   = db.query(func.count(Customer.id)).scalar() or 0
    new_customers_month = db.query(func.count(Customer.id)).filter(
        func.date(Customer.created_at) >= month_s).scalar() or 0 \
        if hasattr(Customer, 'created_at') else 0

    # ── INVENTORY ──────────────────────────────────────
    all_products   = db.query(Product).filter(Product.is_active == True).all()
    out_of_stock   = [p for p in all_products if float(p.stock) <= 0]
    low_stock      = [p for p in all_products if 0 < float(p.stock) <= 5]
    total_products = len(all_products)
    stock_value    = sum(float(p.stock) * float(p.price) for p in all_products)

    # ── FARM ───────────────────────────────────────────
    farm_month = db.query(func.count(FarmDelivery.id)).filter(
        FarmDelivery.delivery_date >= month_s).scalar() or 0

    # ── SPOILAGE ───────────────────────────────────────
    spoilage_month = float(db.query(func.sum(SpoilageRecord.qty)).filter(
        SpoilageRecord.spoilage_date >= month_s).scalar() or 0)

    # ── PRODUCTION ─────────────────────────────────────
    batches_month = db.query(func.count(ProductionBatch.id)).filter(
        func.date(ProductionBatch.created_at) >= month_s).scalar() or 0

    # ── LAST 7 DAYS (POS + B2B) ────────────────────────
    last7 = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        pos = float(db.query(func.sum(Invoice.total)).filter(
            func.date(Invoice.created_at) == d, Invoice.status == "paid").scalar() or 0)
        d_start = dt.combine(d, dt.min.time())
        d_end   = dt.combine(d, dt.max.time())
        b2b = journal_revenue(d_start, d_end)
        last7.append({"date": str(d), "pos": round(pos,2), "b2b": round(b2b,2), "total": round(pos+b2b,2)})

    # ── TOP 10 PRODUCTS THIS MONTH ────────────────────
    top_products = (
        db.query(InvoiceItem.name,
                 func.sum(InvoiceItem.qty).label("qty_sold"),
                 func.sum(InvoiceItem.total).label("revenue"))
        .join(Invoice)
        .filter(func.date(Invoice.created_at) >= month_s, Invoice.status == "paid")
        .group_by(InvoiceItem.name)
        .order_by(func.sum(InvoiceItem.total).desc())
        .limit(10).all()
    )

    # ── PAYMENT METHODS THIS MONTH ────────────────────
    pay_methods = (
        db.query(Invoice.payment_method,
                 func.count(Invoice.id).label("count"),
                 func.sum(Invoice.total).label("total"))
        .filter(func.date(Invoice.created_at) >= month_s, Invoice.status == "paid")
        .group_by(Invoice.payment_method).all()
    )

    # ── RECENT TRANSACTIONS ───────────────────────────
    recent = db.query(Invoice).filter(Invoice.status == "paid").order_by(
        Invoice.created_at.desc()).limit(8).all()

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
        "b2b_outstanding": round(b2b_outstanding, 2),
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
        "recent_sales": [
            {
                "invoice_number": i.invoice_number,
                "customer": db.query(Customer).filter(Customer.id == i.customer_id).first().name if i.customer_id else "Walk-in",
                "total": float(i.total),
                "method": i.payment_method or "cash",
                "time": i.created_at.strftime("%H:%M") if i.created_at else "—",
            }
            for i in recent
        ],
    }


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
    --text:#1a1e14;--sub:#4a5040;--muted:#7b816f;
}
body.light nav{background:rgba(244,245,239,.92);}
body.light .nav-link:hover{background:rgba(0,0,0,.05);}
body.light tr:hover td{background:rgba(0,0,0,.03);}
.mode-btn{display:flex;align-items:center;justify-content:center;width:36px;height:36px;border-radius:10px;border:1px solid var(--border);background:var(--card);color:var(--sub);font-size:16px;cursor:pointer;transition:all .2s;font-family:var(--sans);}
.mode-btn:hover{border-color:var(--border2);transform:scale(1.06);}
.topbar-right{display:flex;align-items:center;gap:12px;}
.user-pill{display:flex;align-items:center;gap:10px;background:var(--card);border:1px solid var(--border);border-radius:40px;padding:7px 16px 7px 10px;}
.user-avatar{width:28px;height:28px;background:linear-gradient(135deg,#7ecb6f,#d4a256);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;color:#0a0c08;}
.user-name{font-size:13px;font-weight:500;color:var(--sub);}
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
@media(max-width:1100px){.grid-4{grid-template-columns:repeat(2,1fr);}}
@media(max-width:700px){.grid-4,.grid-3,.grid-2,.grid-2-1,.grid-1-2{grid-template-columns:1fr;}}

/* STAT CARDS */
.stat{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:18px 20px;display:flex;flex-direction:column;gap:8px;position:relative;overflow:hidden;transition:border-color .2s,transform .2s;}
.stat:hover{border-color:var(--border2);transform:translateY(-2px);}
.stat::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;}
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

/* PANEL */
.panel{background:var(--card);border:1px solid var(--border);border-radius:var(--r);overflow:hidden;}
.panel-header{display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--border);}
.panel-title{font-size:13px;font-weight:700;letter-spacing:.5px;}
.panel-badge{font-size:11px;font-weight:700;padding:3px 9px;border-radius:20px;}
.panel-body{padding:16px 18px;}

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
        <div class="user-pill">
            <div class="user-avatar" id="user-avatar">A</div>
            <span class="user-name" id="user-name">Admin</span>
        </div>
        <button class="logout-btn" onclick="logout()">Sign out</button>
    </div>
</nav>

<div class="content">

    <div>
        <div class="page-title">Dashboard</div>
        <div class="page-sub" id="date-sub"></div>
    </div>

    <!-- ROW 1: REVENUE STATS -->
    <div class="grid-4">
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
        <div class="stat orange">
            <div class="stat-icon">🤝</div>
            <div class="stat-label">B2B Outstanding</div>
            <div class="stat-value orange" id="s-b2b-out">—</div>
            <div class="stat-sub" id="s-b2b-clients">— clients</div>
        </div>
        <div class="stat purple">
            <div class="stat-icon">👥</div>
            <div class="stat-label">Total Customers</div>
            <div class="stat-value purple" id="s-customers">—</div>
            <div class="stat-sub">B2C clients</div>
        </div>
    </div>

    <!-- ROW 2: OPERATIONS STATS -->
    <div class="grid-4">
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

    <!-- ROW 3: SALES CHART -->
    <div class="panel">
        <div class="panel-header">
            <span class="panel-title">Revenue — Last 7 Days</span>
            <div class="chart-legend">
                <span><span class="legend-dot" style="background:var(--green)"></span>POS</span>
                <span><span class="legend-dot" style="background:var(--blue)"></span>B2B</span>
            </div>
        </div>
        <div class="chart-wrap" id="chart-wrap"></div>
    </div>

    <!-- ROW 4: TOP PRODUCTS + PAYMENT METHODS -->
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

    <!-- ROW 5: RECENT SALES + OUT OF STOCK -->
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

    <!-- ROW 6: LOW STOCK -->
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
  const __erpToken = localStorage.getItem("token");
  const __erpUserRole = localStorage.getItem("user_role") || "";
  const __erpUserPermissions = new Set(
      (localStorage.getItem("user_permissions") || "")
          .split(",")
          .map(p => p.trim())
          .filter(Boolean)
  );
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
function setUserInfo(){
    const name = localStorage.getItem("user_name") || "Admin";
    const avatar = document.getElementById("user-avatar");
    const userName = document.getElementById("user-name");
    if(avatar) avatar.innerText = name.charAt(0).toUpperCase();
    if(userName) userName.innerText = name;
}
function logout(){
    localStorage.removeItem("token");
    localStorage.removeItem("user_name");
    localStorage.removeItem("user_role");
    localStorage.removeItem("user_permissions");
    window.location.href = "/";
}
  function requirePageAccess(permission){
      if(!__erpToken){
          window.location.href = "/";
          throw new Error("Not authenticated");
      }
      if(__erpUserRole === "admin" || __erpUserPermissions.has(permission)) return;
      document.body.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100vh;flex-direction:column;gap:16px;color:#445066;font-family:'Outfit',sans-serif;background:#060810"><div style="font-size:48px">🔒</div><div style="font-size:20px;font-weight:800;color:#f0f4ff">Access Restricted</div><div style="font-size:14px">You do not have permission to open this page.</div><a href="/home" style="color:#00ff9d;text-decoration:none;font-weight:700">Back to Home</a></div>`;
      throw new Error("Access denied");
  }
  function applyNavPermissions(){
      const navPermissions = {
          "/home": null,
          "/dashboard": "page_dashboard",
          "/pos": "page_pos",
          "/b2b/": "page_b2b",
          "/inventory/": "page_inventory",
          "/products/": "page_products",
          "/customers-mgmt/": "page_customers",
          "/suppliers/": "page_suppliers",
          "/production/": "page_production",
          "/farm/": "page_farm",
          "/hr/": "page_hr",
          "/accounting/": "page_accounting",
          "/reports/": "page_reports",
          "/import": "page_import",
          "/users/": "admin_only"
      };
      document.querySelectorAll("a.nav-link[href]").forEach(link => {
          const href = link.getAttribute("href");
          const requirement = navPermissions[href];
          if(requirement === undefined || requirement === null) return;
          if(requirement === "admin_only"){
              if(__erpUserRole !== "admin") link.style.display = "none";
              return;
          }
          if(__erpUserRole !== "admin" && !__erpUserPermissions.has(requirement)){
              link.style.display = "none";
          }
      });
  }
  requirePageAccess("page_dashboard");
  applyNavPermissions();
  initializeColorMode();
  setUserInfo();
  let now = new Date();
document.getElementById("nav-date").innerText =
    now.toLocaleDateString("en-GB",{weekday:"long",year:"numeric",month:"long",day:"numeric"});
document.getElementById("date-sub").innerText =
    now.toLocaleDateString("en-GB",{weekday:"long",year:"numeric",month:"long",day:"numeric"});

async function load(){
    try {
        let d = await (await fetch("/dashboard/data")).json();

        // ── REVENUE STATS ──
        document.getElementById("s-today").innerText      = d.total_today.toFixed(2);
        document.getElementById("s-today-orders").innerText = "POS " + d.pos_today.toFixed(0) + "  +  B2B " + d.b2b_today.toFixed(0);
        document.getElementById("s-month").innerText      = d.total_month.toFixed(2);
        document.getElementById("s-month-orders").innerText = "POS " + d.pos_month.toFixed(0) + "  +  B2B " + d.b2b_month.toFixed(0);
        document.getElementById("s-b2b-out").innerText    = d.b2b_outstanding.toFixed(2);
        document.getElementById("s-b2b-clients").innerText= d.b2b_clients + " B2B clients";
        document.getElementById("s-customers").innerText  = d.total_customers;

        // ── OPERATIONS STATS ──
        document.getElementById("s-oos").innerText        = d.out_of_stock_count;
        document.getElementById("s-low").innerText        = d.low_stock_count;
        document.getElementById("s-stock-value").innerText= "inventory value: " + d.stock_value.toFixed(0);
        document.getElementById("s-farm").innerText       = d.farm_month;
        document.getElementById("s-batches").innerText    = d.batches_month;
        document.getElementById("s-spoilage").innerText   = "spoilage this month: " + d.spoilage_month.toFixed(1);

        // ── CHART ──
        let maxVal = Math.max(...d.last7.map(x=>x.total), 1);
        let chartH  = 110; // px available for bars
        document.getElementById("chart-wrap").innerHTML = d.last7.map(x => {
            let posH  = Math.round((x.pos / maxVal) * chartH);
            let b2bH  = Math.round((x.b2b / maxVal) * chartH);
            let dayLbl = new Date(x.date + "T12:00:00").toLocaleDateString("en-GB",{weekday:"short"});
            let isToday = x.date === new Date().toISOString().split("T")[0];
            return `<div class="bar-col">
                <div class="bar-val">${x.total>0?x.total.toFixed(0):""}</div>
                <div class="bar-outer">
                    ${b2bH>0?`<div class="bar" style="height:${b2bH}px;background:var(--blue);opacity:.85;border-radius:3px 3px 0 0"></div>`:""}
                    ${posH>0?`<div class="bar" style="height:${posH}px;background:linear-gradient(180deg,var(--green),var(--lime));border-radius:3px 3px 0 0;${isToday?"box-shadow:0 0 12px rgba(0,255,157,.4)":""}"></div>`:""}
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
            ? d.recent_sales.map(s=>`<tr>
                <td class="mono" style="font-size:11px;color:var(--lime)">${s.invoice_number}</td>
                <td class="bold">${s.customer}</td>
                <td class="mono" style="color:var(--green);font-weight:700">${s.total.toFixed(2)}</td>
                <td><span class="badge ${s.method?.toLowerCase().includes("visa")?"badge-visa":"badge-cash"}">${s.method||"cash"}</span></td>
                <td class="mono" style="font-size:11px;color:var(--muted)">${s.time}</td>
              </tr>`).join("")
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


