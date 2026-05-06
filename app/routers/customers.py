from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import func, select, asc, desc

from app.database import get_async_session
from app.core.permissions import get_current_user, require_permission
from app.models.customer import Customer
from app.models.user import User
from app.models.invoice import Invoice
from app.models.refund import RetailRefund
from app.core.log import record
from app.core.navigation import render_app_header
from app.schemas.customer import CustomerCreate, CustomerUpdate

router = APIRouter(
    prefix="/customers-mgmt",
    tags=["Customers"],
    dependencies=[Depends(require_permission("page_customers"))],
)


# ── API ────────────────────────────────────────────────
@router.get("/api/list")
async def get_customers(
    q:        str = "",
    skip:     int = 0,
    limit:    int = 50,
    sort_by:  str = "name",
    sort_dir: str = "asc",
    db: AsyncSession = Depends(get_async_session),
):
    # Correlated subqueries — computed once per row in a single SQL round-trip
    inv_count_sq = (
        select(func.count(Invoice.id))
        .where(Invoice.customer_id == Customer.id)
        .correlate(Customer)
        .scalar_subquery()
    )
    inv_total_sq = (
        select(func.coalesce(func.sum(Invoice.total), 0))
        .where(Invoice.customer_id == Customer.id, Invoice.status == "paid")
        .correlate(Customer)
        .scalar_subquery()
    )
    ref_total_sq = (
        select(func.coalesce(func.sum(RetailRefund.total), 0))
        .where(RetailRefund.customer_id == Customer.id)
        .correlate(Customer)
        .scalar_subquery()
    )
    net_spent_expr = func.greatest(inv_total_sq - ref_total_sq, 0)

    SORT_EXPRS = {
        "name":         Customer.name,
        "discount_pct": Customer.discount_pct,
        "invoices":     inv_count_sq,
        "total_spent":  net_spent_expr,
    }

    conditions = []
    if q:
        conditions.append(
            Customer.name.ilike(f"%{q}%") |
            Customer.phone.ilike(f"%{q}%") |
            Customer.email.ilike(f"%{q}%")
        )

    cnt_result = await db.execute(
        select(func.count()).select_from(Customer).where(*conditions)
    )
    total = cnt_result.scalar()

    sort_expr = SORT_EXPRS.get(sort_by, Customer.name)
    order_clause = desc(sort_expr) if sort_dir == "desc" else asc(sort_expr)

    rows = await db.execute(
        select(
            Customer,
            inv_count_sq.label("inv_count"),
            inv_total_sq.label("inv_total"),
            ref_total_sq.label("ref_total"),
        )
        .where(*conditions)
        .order_by(order_clause, asc(Customer.name))
        .offset(skip)
        .limit(limit)
    )

    result = []
    for c, inv_count, inv_total, ref_total in rows:
        inv_total  = float(inv_total  or 0)
        ref_total  = float(ref_total  or 0)
        result.append({
            "id":           c.id,
            "name":         c.name,
            "phone":        c.phone or "—",
            "email":        c.email or "—",
            "address":      c.address or "—",
            "discount_pct": float(c.discount_pct or 0),
            "invoices":     int(inv_count or 0),
            "total_spent":  max(0.0, inv_total - ref_total),
            "ref_total":    ref_total,
        })

    return {"total": total, "items": result}


@router.get("/api/invoices/{customer_id}")
async def get_customer_invoices(customer_id: int, db: AsyncSession = Depends(get_async_session)):
    inv_result = await db.execute(
        select(Invoice)
        .where(Invoice.customer_id == customer_id)
        .order_by(Invoice.created_at.desc())
        .limit(30)
    )
    invoices = inv_result.scalars().all()

    ref_result = await db.execute(
        select(RetailRefund)
        .where(RetailRefund.customer_id == customer_id)
        .order_by(RetailRefund.created_at.desc())
        .limit(20)
    )
    refunds = ref_result.scalars().all()
    rows = []
    for i in invoices:
        rows.append({
            "type":           "invoice",
            "id":             i.id,
            "ref_number":     i.invoice_number or f"#{i.id}",
            "total":          float(i.total),
            "status":         i.status,
            "payment_method": i.payment_method or "cash",
            "created_at":     i.created_at.strftime("%Y-%m-%d %H:%M") if i.created_at else "—",
            "sort_key":       i.created_at.isoformat() if i.created_at else "",
        })
    for r in refunds:
        rows.append({
            "type":           "refund",
            "id":             r.id,
            "ref_number":     r.refund_number,
            "total":          -float(r.total),
            "status":         "refunded",
            "payment_method": r.refund_method,
            "reason":         r.reason or "—",
            "created_at":     r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "—",
            "sort_key":       r.created_at.isoformat() if r.created_at else "",
        })
    rows.sort(key=lambda x: x["sort_key"], reverse=True)
    return rows


@router.get("/api/profile/{customer_id}")
async def customer_profile(customer_id: int, db: AsyncSession = Depends(get_async_session)):
    c_result = await db.execute(select(Customer).where(Customer.id == customer_id))
    c = c_result.scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="Customer not found")

    inv_result = await db.execute(
        select(Invoice)
        .where(Invoice.customer_id == customer_id)
        .order_by(Invoice.created_at.desc())
        .options(selectinload(Invoice.items))
    )
    invoices = inv_result.scalars().all()

    ref_result = await db.execute(
        select(RetailRefund)
        .where(RetailRefund.customer_id == customer_id)
        .order_by(RetailRefund.created_at.desc())
    )
    refunds = ref_result.scalars().all()

    paid = [i for i in invoices if i.status == "paid"]
    total_orders   = len(paid)
    gross_spent    = sum(float(i.total) for i in paid)
    total_refunded = sum(float(r.total) for r in refunds)
    net_spent      = max(0.0, gross_spent - total_refunded)
    avg_basket     = gross_spent / total_orders if total_orders else 0.0
    last_purchase  = max((i.created_at for i in paid), default=None)

    # aggregate qty per product across all paid invoices
    prod_agg = defaultdict(lambda: {"name": "", "sku": "", "qty": 0.0, "revenue": 0.0})
    for inv in paid:
        for it in inv.items:
            prod_agg[it.product_id]["name"]    = it.name or ""
            prod_agg[it.product_id]["sku"]     = it.sku  or ""
            prod_agg[it.product_id]["qty"]    += float(it.qty)
            prod_agg[it.product_id]["revenue"] += float(it.total)
    top_products = sorted(prod_agg.values(), key=lambda x: x["qty"], reverse=True)[:10]

    order_rows = []
    for inv in invoices:
        order_rows.append({
            "type":           "invoice",
            "id":             inv.id,
            "ref_number":     inv.invoice_number or f"#{inv.id}",
            "total":          float(inv.total),
            "status":         inv.status,
            "payment_method": inv.payment_method or "cash",
            "created_at":     inv.created_at.strftime("%Y-%m-%d %H:%M") if inv.created_at else "—",
            "sort_key":       inv.created_at.isoformat() if inv.created_at else "",
            "items": [
                {
                    "name":       it.name,
                    "qty":        float(it.qty),
                    "unit_price": float(it.unit_price),
                    "total":      float(it.total),
                }
                for it in inv.items
            ],
        })
    for ref in refunds:
        order_rows.append({
            "type":           "refund",
            "id":             ref.id,
            "ref_number":     ref.refund_number,
            "total":          -float(ref.total),
            "status":         "refunded",
            "payment_method": ref.refund_method,
            "reason":         ref.reason or "—",
            "created_at":     ref.created_at.strftime("%Y-%m-%d %H:%M") if ref.created_at else "—",
            "sort_key":       ref.created_at.isoformat() if ref.created_at else "",
            "items":          [],
        })
    order_rows.sort(key=lambda x: x["sort_key"], reverse=True)

    return {
        "customer": {
            "id":         c.id,
            "name":       c.name,
            "phone":      c.phone   or "—",
            "email":      c.email   or "—",
            "address":    c.address or "—",
            "discount_pct": float(c.discount_pct or 0),
            "created_at": c.created_at.strftime("%Y-%m-%d") if c.created_at else "—",
        },
        "stats": {
            "total_orders":   total_orders,
            "gross_spent":    round(gross_spent,    2),
            "total_refunded": round(total_refunded, 2),
            "net_spent":      round(net_spent,      2),
            "avg_basket":     round(avg_basket,     2),
            "last_purchase":  last_purchase.strftime("%Y-%m-%d") if last_purchase else None,
        },
        "top_products": top_products,
        "orders":       order_rows,
    }


@router.post("/api/add")
async def add_customer(data: CustomerCreate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    if data.phone:
        phone_result = await db.execute(select(Customer).where(Customer.phone == data.phone))
        if phone_result.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Phone number already exists")
    c = Customer(**data.model_dump())
    db.add(c)
    await db.flush()
    record(db, "Customers", "add_customer",
           f"Added customer: {c.name}" + (f" — {c.phone}" if c.phone else ""),
           ref_type="customer", ref_id=c.id)
    await db.commit()
    await db.refresh(c)
    return {"id": c.id, "name": c.name}


@router.put("/api/edit/{customer_id}")
async def edit_customer(customer_id: int, data: CustomerUpdate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    result = await db.execute(select(Customer).where(Customer.id == customer_id))
    c = result.scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="Customer not found")
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(c, k, v)
    record(db, "Customers", "edit_customer",
           f"Edited customer: {c.name}",
           ref_type="customer", ref_id=customer_id)
    await db.commit()
    return {"ok": True}


@router.delete("/api/delete/{customer_id}")
async def delete_customer(customer_id: int, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    result = await db.execute(select(Customer).where(Customer.id == customer_id))
    c = result.scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="Customer not found")
    record(db, "Customers", "delete_customer",
           f"Deleted customer: {c.name}",
           ref_type="customer", ref_id=customer_id)
    await db.delete(c)
    await db.commit()
    return {"ok": True}


# ── Profile UI ─────────────────────────────────────────
@router.get("/profile/{customer_id}", response_class=HTMLResponse)
def customer_profile_ui(customer_id: int):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<script src="/static/theme-init.js"></script>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Customer Profile — Thunder ERP</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,600;1,300;1,400&family=DM+Sans:wght@300;400;500;600&family=DM+Mono:wght@400;500&family=Outfit:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
<style>
:root{{
    --bg:      #08090c; --card:   #0d1008; --card2:  #111408;
    --border:  rgba(255,255,255,0.055); --border2: rgba(255,255,255,0.10);
    --green:   #7ecb6f; --green2: #a8d97a; --amber:  #d4a256;
    --teal:    #5bbfb5; --rose:   #c97a7a; --blue:   #6a9fd4;
    --text:    #e8eae0; --sub:    #8a9080; --muted:  #4a5040;
    --serif:   'Cormorant Garamond', serif;
    --sans:    'DM Sans', sans-serif;
    --mono:    'DM Mono', monospace;
}}
body.light{{
    --bg:#f4f5ef;--card:#eceee6;--card2:#e4e6de;
    --border:rgba(0,0,0,0.07);--border2:rgba(0,0,0,0.12);
    --green:#0f8a43; --green2:#4f9f69;
    --text:#1a1e14;--sub:#4a5040;--muted:#8a9080;
}}
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:var(--sans);background:var(--bg);color:var(--text);min-height:100vh}}

.topbar{{position:sticky;top:0;z-index:100;display:flex;align-items:center;justify-content:space-between;gap:10px;padding:0 24px;height:58px;border-bottom:1px solid var(--border);background:rgba(10,13,24,.92);backdrop-filter:blur(20px)}}
.logo{{font-family:'Outfit',sans-serif;font-size:17px;font-weight:900;text-decoration:none;display:flex;align-items:center;gap:8px}}
.logo-text{{background:linear-gradient(135deg,var(--green),var(--blue));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}}
.topbar-right{{display:flex;align-items:center;gap:10px}}
.back-btn{{background:var(--card);border:1px solid var(--border);color:var(--sub);font-family:var(--sans);font-size:12px;font-weight:500;padding:8px 14px;border-radius:8px;cursor:pointer;transition:all .2s;text-decoration:none;display:flex;align-items:center;gap:6px}}
.back-btn:hover{{border-color:var(--border2);color:var(--text)}}
.mode-btn{{background:var(--card);border:1px solid var(--border);color:var(--sub);width:36px;height:36px;border-radius:10px;font-size:16px;cursor:pointer;transition:all .2s;display:flex;align-items:center;justify-content:center}}
.mode-btn:hover{{border-color:var(--border2);transform:scale(1.08)}}

.content{{max-width:1100px;margin:0 auto;padding:32px 24px;display:flex;flex-direction:column;gap:28px}}

/* ── Customer header ── */
.cust-header{{display:flex;align-items:flex-start;gap:20px;flex-wrap:wrap}}
.cust-avatar{{width:64px;height:64px;border-radius:50%;background:linear-gradient(135deg,var(--green),var(--amber));display:flex;align-items:center;justify-content:center;font-family:'Outfit',sans-serif;font-size:26px;font-weight:800;color:#0a0c08;flex-shrink:0}}
.cust-info{{flex:1}}
.cust-name{{font-family:var(--serif);font-size:36px;font-weight:300;letter-spacing:-.3px;line-height:1.1}}
.cust-name em{{font-style:italic;color:var(--green2)}}
.cust-meta{{display:flex;gap:16px;flex-wrap:wrap;margin-top:8px}}
.cust-meta-item{{font-size:13px;color:var(--muted);display:flex;align-items:center;gap:5px}}

/* ── Stats ── */
.stats-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:14px}}
.stat-card{{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:20px}}
.stat-label{{font-size:10px;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-bottom:8px}}
.stat-val{{font-family:var(--mono);font-size:26px;font-weight:500;color:var(--text)}}
.stat-val.green{{color:var(--green2)}}
.stat-val.amber{{color:var(--amber)}}
.stat-val.teal{{color:var(--teal)}}
.stat-val.rose{{color:var(--rose)}}
.stat-sub{{font-size:11px;color:var(--muted);margin-top:4px}}

/* ── Section title ── */
.section-title{{font-size:11px;font-weight:600;letter-spacing:2.5px;text-transform:uppercase;color:var(--muted);margin-bottom:16px;display:flex;align-items:center;gap:12px}}
.section-title::after{{content:'';flex:1;height:1px;background:linear-gradient(90deg,var(--border2),transparent)}}

/* ── Top products ── */
.products-list{{display:flex;flex-direction:column;gap:8px}}
.product-row{{display:flex;align-items:center;gap:12px}}
.product-rank{{font-family:var(--mono);font-size:11px;color:var(--muted);width:20px;text-align:right;flex-shrink:0}}
.product-bar-wrap{{flex:1;display:flex;flex-direction:column;gap:3px}}
.product-name{{font-size:13px;color:var(--text);font-weight:500}}
.product-bar-bg{{height:6px;background:var(--border);border-radius:3px;overflow:hidden}}
.product-bar{{height:100%;border-radius:3px;background:linear-gradient(90deg,var(--green),var(--teal));transition:width .6s ease}}
.product-qty{{font-family:var(--mono);font-size:12px;color:var(--sub);white-space:nowrap;flex-shrink:0}}
.product-rev{{font-family:var(--mono);font-size:11px;color:var(--muted);flex-shrink:0;min-width:70px;text-align:right}}

/* ── Orders table ── */
.orders-wrap{{background:var(--card);border:1px solid var(--border);border-radius:14px;overflow:hidden}}
table{{width:100%;border-collapse:collapse}}
thead th{{font-size:10px;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);padding:12px 16px;text-align:left;border-bottom:1px solid var(--border);white-space:nowrap;font-weight:500;background:var(--card2)}}
tbody tr.order-row{{border-bottom:1px solid var(--border);cursor:pointer;transition:background .15s}}
tbody tr.order-row:hover{{background:rgba(255,255,255,.025)}}
tbody tr.order-row.refund-row:hover{{background:rgba(201,122,122,.04)}}
tbody td{{padding:13px 16px;font-size:13px;vertical-align:middle}}
.td-ref{{font-family:var(--mono);font-size:12px;color:var(--sub)}}
.td-date{{font-family:var(--mono);font-size:11px;color:var(--muted);white-space:nowrap}}
.td-total{{font-family:var(--mono);font-size:14px;font-weight:500}}
.td-total.income{{color:var(--green2)}}
.td-total.refund{{color:var(--rose)}}
.badge{{display:inline-block;font-size:10px;font-weight:600;letter-spacing:.8px;text-transform:uppercase;padding:3px 8px;border-radius:6px}}
.b-paid{{background:color-mix(in srgb,var(--green) 12%,transparent);color:var(--green);border:1px solid color-mix(in srgb,var(--green) 25%,transparent)}}
.b-refunded{{background:color-mix(in srgb,var(--rose) 12%,transparent);color:var(--rose);border:1px solid color-mix(in srgb,var(--rose) 25%,transparent)}}
.b-pending{{background:color-mix(in srgb,var(--amber) 12%,transparent);color:var(--amber);border:1px solid color-mix(in srgb,var(--amber) 25%,transparent)}}
.pay-badge{{display:inline-block;font-size:10px;color:var(--muted);background:var(--card2);border:1px solid var(--border);padding:2px 7px;border-radius:5px;text-transform:capitalize}}
.expand-icon{{color:var(--muted);font-size:14px;transition:transform .2s;display:inline-block}}

/* line items sub-row */
tr.items-row td{{padding:0}}
.items-inner{{padding:0 16px 14px 48px;border-top:1px solid var(--border)}}
.items-table{{width:100%;border-collapse:collapse;font-size:12px}}
.items-table th{{color:var(--muted);font-size:10px;letter-spacing:1px;text-transform:uppercase;padding:6px 8px;text-align:left;font-weight:500}}
.items-table td{{padding:6px 8px;color:var(--sub);border-top:1px solid rgba(255,255,255,.03)}}
.items-table td.item-name{{color:var(--text)}}
.items-table td.item-num{{font-family:var(--mono);text-align:right}}

/* ── Monthly chart ── */
.chart-wrap{{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:20px 20px 12px}}
.chart-header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}}
.chart-legend{{display:flex;gap:14px;font-size:11px;color:var(--muted)}}
.chart-legend-dot{{width:8px;height:8px;border-radius:2px;display:inline-block;margin-right:4px;flex-shrink:0}}
.chart-canvas-wrap{{position:relative;width:100%;overflow-x:auto}}
canvas#spend-chart{{display:block}}

@media(max-width:700px){{
    .content{{padding:20px 16px}}
    .cust-name{{font-size:26px}}
    .stats-grid{{grid-template-columns:1fr 1fr}}
    .topbar{{padding:0 16px}}
}}
</style>
    <script src="/static/auth-guard.js"></script>
</head>
<body>

<header class="topbar">
    <a href="/home" class="logo">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
            <polygon points="13,2 4,14 11,14 11,22 20,10 13,10" fill="#f59e0b"/>
        </svg>
        <span class="logo-text">Thunder ERP</span>
    </a>
    <div class="topbar-right">
        <button class="mode-btn" id="mode-btn" onclick="toggleMode()">&#127769;</button>
        <a href="/customers-mgmt/" class="back-btn">&#8592; Customers</a>
    </div>
</header>

<div class="content">
    <div id="loading" style="color:var(--muted);font-size:13px;padding:40px 0;text-align:center">Loading…</div>
    <div id="profile-content" style="display:none;flex-direction:column;gap:28px"></div>
</div>

<script>
const CUSTOMER_ID = {customer_id};
if (localStorage.getItem("colorMode") === "light") {{
    document.body.classList.add("light");
    document.getElementById("mode-btn").innerHTML = "&#9728;&#65039;";
}}
function toggleMode(){{
    const isLight = document.body.classList.toggle("light");
    document.getElementById("mode-btn").innerHTML = isLight ? "&#9728;&#65039;" : "&#127769;";
    localStorage.setItem("colorMode", isLight ? "light" : "dark");
}}

function fmt(n){{ return Number(n).toLocaleString("en-US",{{minimumFractionDigits:2,maximumFractionDigits:2}}); }}
function esc(s){{ return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }}

async function load(){{
    const r = await fetch(`/customers-mgmt/api/profile/${{CUSTOMER_ID}}`);
    if (!r.ok) {{
        document.getElementById("loading").textContent = "Customer not found.";
        return;
    }}
    const d = await r.json();
    render(d);
    document.getElementById("loading").style.display = "none";
    const pc = document.getElementById("profile-content");
    pc.style.display = "flex";
}}

function render(d){{
    const pc = document.getElementById("profile-content");
    const c  = d.customer;
    const s  = d.stats;

    // ── Header ──
    const initials = c.name.split(" ").map(w=>w[0]).slice(0,2).join("").toUpperCase();
    const nameParts = c.name.split(" ");
    const firstName = nameParts.slice(0,-1).join(" ");
    const lastName  = nameParts.slice(-1)[0] || "";
    const nameHtml  = firstName
        ? `${{esc(firstName)}} <em>${{esc(lastName)}}</em>`
        : `<em>${{esc(c.name)}}</em>`;

    pc.innerHTML = `
    <div class="cust-header">
        <div class="cust-avatar">${{initials}}</div>
        <div class="cust-info">
            <div class="cust-name">${{nameHtml}}</div>
            <div class="cust-meta">
                ${{c.phone !== "—" ? `<span class="cust-meta-item">&#128222; ${{esc(c.phone)}}</span>` : ""}}
                ${{c.email !== "—" ? `<span class="cust-meta-item">&#9993; ${{esc(c.email)}}</span>` : ""}}
                ${{c.address !== "—" ? `<span class="cust-meta-item">&#128205; ${{esc(c.address)}}</span>` : ""}}
                ${{c.discount_pct > 0 ? `<span class="cust-meta-item">&#127991;&#65039; Default discount ${{fmt(c.discount_pct)}}%</span>` : ""}}
                <span class="cust-meta-item">&#128197; Customer since ${{esc(c.created_at)}}</span>
            </div>
        </div>
    </div>

    <div>
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-label">Total Orders</div>
                <div class="stat-val green">${{s.total_orders}}</div>
                ${{s.total_refunded > 0 ? `<div class="stat-sub">${{d.orders.filter(o=>o.type==="refund").length}} refund(s)</div>` : ""}}
            </div>
            <div class="stat-card">
                <div class="stat-label">Net Spent</div>
                <div class="stat-val green">${{fmt(s.net_spent)}}</div>
                ${{s.total_refunded > 0 ? `<div class="stat-sub">−${{fmt(s.total_refunded)}} refunded</div>` : ""}}
            </div>
            <div class="stat-card">
                <div class="stat-label">Avg Basket</div>
                <div class="stat-val amber">${{fmt(s.avg_basket)}}</div>
                <div class="stat-sub">per paid order</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Default Discount</div>
                <div class="stat-val amber">${{fmt(c.discount_pct)}}%</div>
                <div class="stat-sub">auto-filled in POS</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Last Purchase</div>
                <div class="stat-val teal" style="font-size:18px">${{s.last_purchase || "Never"}}</div>
            </div>
        </div>
    </div>`;

    // ── Monthly spending chart ──
    if (d.orders.length) {{
        pc.innerHTML += `
        <div>
            <div class="section-title">Monthly Spending</div>
            <div class="chart-wrap">
                <div class="chart-header">
                    <span style="font-size:12px;color:var(--muted)">Last 12 months — paid invoices vs refunds</span>
                    <div class="chart-legend">
                        <span><span class="chart-legend-dot" style="background:var(--green2)"></span>Paid</span>
                        <span><span class="chart-legend-dot" style="background:var(--rose)"></span>Refund</span>
                    </div>
                </div>
                <div class="chart-canvas-wrap">
                    <canvas id="spend-chart" height="180"></canvas>
                </div>
            </div>
        </div>`;
        // Defer so the canvas is in the DOM
        setTimeout(() => drawSpendChart(d.orders), 0);
    }}

    // ── Top products ──
    if (d.top_products.length) {{
        const maxQty = d.top_products[0].qty;
        pc.innerHTML += `
        <div>
            <div class="section-title">Most Bought Products</div>
            <div class="products-list">
                ${{d.top_products.map((p, i) => `
                <div class="product-row">
                    <span class="product-rank">#${{i+1}}</span>
                    <div class="product-bar-wrap">
                        <div class="product-name">${{esc(p.name)}}</div>
                        <div class="product-bar-bg">
                            <div class="product-bar" style="width:${{Math.round(p.qty/maxQty*100)}}%"></div>
                        </div>
                    </div>
                    <span class="product-qty">${{Number(p.qty).toLocaleString("en-US",{{maximumFractionDigits:1}})}} units</span>
                    <span class="product-rev">${{fmt(p.revenue)}}</span>
                </div>`).join("")}}
            </div>
        </div>`;
    }}

    // ── Order history ──
    pc.innerHTML += `
    <div>
        <div class="section-title">Order History</div>
        <div class="orders-wrap">
            <table>
                <thead>
                    <tr>
                        <th style="width:28px"></th>
                        <th>Ref #</th>
                        <th>Date</th>
                        <th>Payment</th>
                        <th>Status</th>
                        <th style="text-align:right">Total</th>
                        <th style="text-align:center">Print</th>
                    </tr>
                </thead>
                <tbody id="orders-tbody">
                    ${{d.orders.length === 0 ? '<tr><td colspan="7" style="text-align:center;padding:40px;color:var(--muted)">No orders yet</td></tr>' : ""}}
                </tbody>
            </table>
        </div>
    </div>`;

    const tbody = document.getElementById("orders-tbody");
    d.orders.forEach((o, idx) => {{
        const isRefund  = o.type === "refund";
        const totalCls  = isRefund ? "td-total refund" : "td-total income";
        const totalSign = isRefund ? "−" : "+";
        const statusBadge = o.status === "paid"     ? "b-paid"
                          : o.status === "refunded" ? "b-refunded" : "b-pending";
        const hasItems = o.items && o.items.length > 0;

        // order row
        const tr = document.createElement("tr");
        tr.className = "order-row" + (isRefund ? " refund-row" : "");
        tr.innerHTML = `
            <td style="padding-left:14px">
                ${{hasItems ? `<span class="expand-icon" id="icon-${{idx}}">&#9654;</span>` : ""}}
            </td>
            <td class="td-ref">
                <a href="${{isRefund ? `/refunds/print/${{o.id}}` : `/invoice/${{o.id}}`}}"
                   target="_blank"
                   style="color:inherit;text-decoration:none"
                   onclick="event.stopPropagation()">
                    ${{esc(o.ref_number)}}
                </a>
            </td>
            <td class="td-date">${{esc(o.created_at)}}</td>
            <td><span class="pay-badge">${{esc(o.payment_method)}}</span></td>
            <td><span class="badge ${{statusBadge}}">${{esc(o.status)}}</span></td>
            <td style="text-align:right" class="${{totalCls}}">${{totalSign}}${{fmt(Math.abs(o.total))}}</td>
            <td style="text-align:center">
                <a href="${{isRefund ? `/refunds/print/${{o.id}}` : `/invoice/${{o.id}}`}}"
                   target="_blank"
                   style="text-decoration:none;font-size:16px"
                   title="Print"
                   onclick="event.stopPropagation()">
                   🖨️
                </a>
            </td>`;

        // items sub-row
        const subTr = document.createElement("tr");
        subTr.className = "items-row";
        subTr.style.display = "none";
        if (hasItems) {{
            subTr.innerHTML = `<td colspan="7">
                <div class="items-inner">
                    <table class="items-table">
                        <thead><tr>
                            <th>Product</th>
                            <th style="text-align:right">Qty</th>
                            <th style="text-align:right">Unit Price</th>
                            <th style="text-align:right">Line Total</th>
                        </tr></thead>
                        <tbody>
                            ${{o.items.map(it => `
                            <tr>
                                <td class="item-name">${{esc(it.name)}}</td>
                                <td class="item-num">${{Number(it.qty).toLocaleString("en-US",{{maximumFractionDigits:3}})}}</td>
                                <td class="item-num">${{fmt(it.unit_price)}}</td>
                                <td class="item-num">${{fmt(it.total)}}</td>
                            </tr>`).join("")}}
                        </tbody>
                    </table>
                </div>
            </td>`;
        }}

        if (hasItems) {{
            tr.style.cursor = "pointer";
            tr.addEventListener("click", () => {{
                const open = subTr.style.display !== "none";
                subTr.style.display = open ? "none" : "";
                const icon = document.getElementById(`icon-${{idx}}`);
                if (icon) icon.style.transform = open ? "" : "rotate(90deg)";
            }});
        }}

        tbody.appendChild(tr);
        tbody.appendChild(subTr);
    }});
}}

load();

function drawSpendChart(orders) {{
    const canvas = document.getElementById("spend-chart");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");

    // Build last-12-months buckets
    const now   = new Date();
    const months = [];
    for (let i = 11; i >= 0; i--) {{
        const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
        months.push({{
            key:   d.getFullYear() + "-" + String(d.getMonth()+1).padStart(2,"0"),
            label: d.toLocaleString("default", {{month:"short"}}),
            paid:  0,
            ref:   0,
        }});
    }}
    const byKey = {{}};
    months.forEach(m => byKey[m.key] = m);

    orders.forEach(o => {{
        const key = (o.created_at || "").slice(0, 7);
        if (!byKey[key]) return;
        if (o.type === "invoice" && o.status === "paid") byKey[key].paid += o.total;
        if (o.type === "refund") byKey[key].ref += Math.abs(o.total);
    }});

    // Remove leading months with no activity
    let firstActive = 0;
    for (let i = 0; i < months.length; i++) {{
        if (months[i].paid > 0 || months[i].ref > 0) {{ firstActive = i; break; }}
        if (i === months.length - 1) firstActive = 0; // all zero — show last 3
    }}
    const visible = months.slice(Math.min(firstActive, months.length - 3));

    const maxVal = Math.max(...visible.map(m => m.paid), 1);

    // Layout
    const isLight    = document.body.classList.contains("light");
    const colorPaid  = isLight ? "#0f8a43" : "#a8d97a";
    const colorRef   = isLight ? "#c97a7a" : "#c97a7a";
    const colorMuted = isLight ? "#8a9080" : "#4a5040";
    const colorGrid  = isLight ? "rgba(0,0,0,0.06)" : "rgba(255,255,255,0.06)";
    const colorText  = isLight ? "#4a5040" : "#8a9080";

    const PAD_L = 52, PAD_R = 16, PAD_T = 12, PAD_B = 40;
    const barGap = 8;
    const n = visible.length;
    const minWidth = PAD_L + PAD_R + n * (28 + barGap);
    const W = Math.max(canvas.parentElement.clientWidth || 600, minWidth);
    const H = 180;
    canvas.width  = W;
    canvas.height = H;
    const chartW = W - PAD_L - PAD_R;
    const chartH = H - PAD_T - PAD_B;
    const barW   = Math.min(40, (chartW / n) - barGap);

    ctx.clearRect(0, 0, W, H);

    // Grid lines + Y labels
    const ticks = 4;
    ctx.textAlign = "right";
    ctx.font = "11px monospace";
    ctx.fillStyle = colorText;
    for (let t = 0; t <= ticks; t++) {{
        const val = maxVal * t / ticks;
        const y   = PAD_T + chartH - (chartH * t / ticks);
        ctx.strokeStyle = colorGrid;
        ctx.lineWidth   = 0.5;
        ctx.beginPath(); ctx.moveTo(PAD_L, y); ctx.lineTo(W - PAD_R, y); ctx.stroke();
        if (val >= 1000) ctx.fillText((val/1000).toFixed(val%1000===0?0:1)+"k", PAD_L - 6, y + 4);
        else ctx.fillText(Math.round(val), PAD_L - 6, y + 4);
    }}

    // Bars
    visible.forEach((m, i) => {{
        const x = PAD_L + i * (chartW / n) + (chartW / n - barW) / 2;

        // Paid bar
        if (m.paid > 0) {{
            const bh = Math.max(2, (m.paid / maxVal) * chartH);
            const by = PAD_T + chartH - bh;
            ctx.fillStyle = colorPaid;
            ctx.beginPath();
            ctx.roundRect(x, by, barW, bh, [3, 3, 0, 0]);
            ctx.fill();
        }}

        // Refund overlay (red, inset slightly)
        if (m.ref > 0) {{
            const rh = Math.max(2, (m.ref / maxVal) * chartH);
            const ry = PAD_T + chartH - rh;
            ctx.fillStyle = colorRef;
            ctx.globalAlpha = 0.75;
            ctx.beginPath();
            ctx.roundRect(x + 2, ry, barW - 4, rh, [2, 2, 0, 0]);
            ctx.fill();
            ctx.globalAlpha = 1;
        }}

        // Value label on tallest bar
        if (m.paid > 0) {{
            const bh  = (m.paid / maxVal) * chartH;
            const by  = PAD_T + chartH - bh;
            const lbl = m.paid >= 1000 ? (m.paid/1000).toFixed(1)+"k" : Math.round(m.paid).toString();
            ctx.fillStyle  = colorMuted;
            ctx.textAlign  = "center";
            ctx.font = "10px monospace";
            if (bh > 20) {{
                // label inside bar
                ctx.fillStyle = isLight ? "rgba(0,0,0,0.5)" : "rgba(255,255,255,0.55)";
                ctx.fillText(lbl, x + barW/2, by + 14);
            }} else {{
                // label above bar
                ctx.fillStyle = colorMuted;
                ctx.fillText(lbl, x + barW/2, by - 4);
            }}
        }}

        // Month label
        ctx.fillStyle  = colorText;
        ctx.textAlign  = "center";
        ctx.font = "11px sans-serif";
        ctx.fillText(m.label, x + barW/2, H - 10);
    }});
}}
</script>
</body>
</html>"""


# ── UI ─────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
def customers_ui(current_user: User = Depends(require_permission("page_customers"))):
    return """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<script src="/static/theme-init.js"></script>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Customers</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root {
    --bg:      #060810;
    --surface: #0a0d18;
    --card:    #0f1424;
    --card2:   #151c30;
    --border:  rgba(255,255,255,0.06);
    --border2: rgba(255,255,255,0.11);
    --green:   #00ff9d;
    --blue:    #4d9fff;
    --purple:  #a855f7;
    --danger:  #ff4d6d;
    --warn:    #ffb547;
    --text:    #f0f4ff;
    --sub:     #8899bb;
    --muted:   #445066;
    --sans:    'Outfit', sans-serif;
    --mono:    'JetBrains Mono', monospace;
    --r:       12px;
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
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: var(--sans); background: var(--bg); color: var(--text); min-height: 100vh; font-size: 14px; }

nav {
    position: sticky; top: 0; z-index: 100;
    display: flex; align-items: center; gap: 10px;
    padding: 0 24px; height: 58px;
    background: rgba(10,13,24,.92);
    backdrop-filter: blur(20px);
    border-bottom: 1px solid var(--border);
}
.logo {
    font-size: 18px; font-weight: 900;
    background: linear-gradient(135deg, var(--green), var(--blue));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    background-clip: text; margin-right: 12px;
}
.nav-link {
    padding: 7px 14px; border-radius: 8px;
    color: var(--sub); font-size: 13px; font-weight: 600;
    text-decoration: none; transition: all .2s;
}
.nav-link:hover { background: rgba(255,255,255,.05); color: var(--text); }
.nav-link.active { background: rgba(0,255,157,.1); color: var(--green); }
.nav-spacer { flex: 1; }

.content { max-width: 1300px; margin: 0 auto; padding: 28px 24px; display: flex; flex-direction: column; gap: 20px; }
.page-title { font-size: 24px; font-weight: 800; letter-spacing: -.5px; }
.page-sub   { color: var(--muted); font-size: 13px; margin-top: 3px; }

.toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.search-box {
    display: flex; align-items: center; gap: 9px;
    background: var(--card); border: 1px solid var(--border);
    border-radius: var(--r); padding: 0 14px; flex: 1; min-width: 200px;
    transition: border-color .2s;
}
.search-box:focus-within { border-color: rgba(0,255,157,.3); }
.search-box svg { color: var(--muted); flex-shrink: 0; }
.search-box input {
    background: transparent; border: none; outline: none;
    color: var(--text); font-family: var(--sans);
    font-size: 14px; padding: 11px 0; width: 100%;
}
.search-box input::placeholder { color: var(--muted); }
.btn {
    display: flex; align-items: center; gap: 7px;
    padding: 10px 16px; border-radius: var(--r);
    font-family: var(--sans); font-size: 13px; font-weight: 700;
    cursor: pointer; border: none; transition: all .2s; white-space: nowrap;
}
.btn-green { background: linear-gradient(135deg, var(--green), #00d4ff); color: #021a10; }
.btn-green:hover { filter: brightness(1.1); transform: translateY(-1px); }
.count-badge {
    background: var(--card2); border: 1px solid var(--border2);
    color: var(--sub); font-family: var(--mono); font-size: 12px;
    padding: 8px 14px; border-radius: var(--r);
}

.table-wrap {
    background: var(--card); border: 1px solid var(--border);
    border-radius: var(--r); overflow: hidden;
}
table { width: 100%; border-collapse: collapse; }
thead { background: var(--card2); }
th {
    text-align: left; font-size: 10px; font-weight: 700;
    letter-spacing: 1px; text-transform: uppercase;
    color: var(--muted); padding: 12px 16px;
}
td { padding: 13px 16px; border-top: 1px solid var(--border); color: var(--sub); font-size: 13px; }
tr:hover td { background: rgba(255,255,255,.02); cursor: pointer; }
td.name  { color: var(--text); font-weight: 600; }
td.mono  { font-family: var(--mono); color: var(--green); }
td.phone { font-family: var(--mono); font-size: 12px; }

.action-btn {
    background: transparent; border: 1px solid var(--border2);
    color: var(--sub); font-size: 12px; font-weight: 600;
    padding: 5px 10px; border-radius: 7px; cursor: pointer;
    transition: all .15s; font-family: var(--sans);
}
.action-btn:hover { border-color: var(--blue); color: var(--blue); }
.action-btn.danger:hover { border-color: var(--danger); color: var(--danger); }

.pagination {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 16px; border-top: 1px solid var(--border);
    font-size: 13px; color: var(--muted);
}
.page-btns { display: flex; gap: 6px; }
.page-btn {
    background: var(--card2); border: 1px solid var(--border2);
    color: var(--sub); font-family: var(--sans); font-size: 12px;
    padding: 6px 12px; border-radius: 7px; cursor: pointer; transition: all .15s;
}
.page-btn:hover { border-color: var(--green); color: var(--green); }
.page-btn:disabled { opacity: .3; cursor: not-allowed; }

/* MODAL */
.modal-bg {
    position: fixed; inset: 0; z-index: 500;
    background: rgba(0,0,0,.7); backdrop-filter: blur(4px);
    display: none; align-items: center; justify-content: center;
}
.modal-bg.open { display: flex; }
.modal {
    background: var(--card); border: 1px solid var(--border2);
    border-radius: 16px; padding: 28px;
    width: 480px; max-width: 95vw;
    animation: modalIn .2s ease;
}
@keyframes modalIn { from{opacity:0;transform:scale(.95)} to{opacity:1;transform:scale(1)} }
.modal-title { font-size: 18px; font-weight: 800; margin-bottom: 20px; }
.fld { display: flex; flex-direction: column; gap: 6px; margin-bottom: 14px; }
.fld label { font-size: 11px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; color: var(--muted); }
.fld input {
    background: var(--card2); border: 1px solid var(--border2);
    border-radius: 10px; padding: 10px 12px;
    color: var(--text); font-family: var(--sans); font-size: 14px;
    outline: none; transition: border-color .2s; width: 100%;
}
.fld input:focus { border-color: rgba(0,255,157,.4); }
.modal-actions { display: flex; gap: 10px; margin-top: 6px; justify-content: flex-end; }
.btn-cancel {
    background: transparent; border: 1px solid var(--border2);
    color: var(--sub); padding: 10px 18px; border-radius: var(--r);
    font-family: var(--sans); font-size: 13px; font-weight: 700; cursor: pointer;
}
.btn-cancel:hover { border-color: var(--danger); color: var(--danger); }

/* SIDE PANEL - invoice history */
.side-bg {
    position: fixed; inset: 0; z-index: 400;
    background: rgba(0,0,0,.5);
    display: none;
}
.side-bg.open { display: block; }
.side-panel {
    position: fixed; right: 0; top: 0; bottom: 0;
    width: 420px; max-width: 95vw;
    background: var(--card);
    border-left: 1px solid var(--border2);
    display: flex; flex-direction: column;
    transform: translateX(100%);
    transition: transform .3s ease;
    z-index: 401;
}
.side-panel.open { transform: translateX(0); }
.side-header {
    padding: 20px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
}
.side-header h3 { font-size: 16px; font-weight: 800; }
.close-btn {
    background: none; border: none; color: var(--muted);
    font-size: 22px; cursor: pointer; padding: 0;
    transition: color .15s;
}
.close-btn:hover { color: var(--danger); }
.side-body { flex: 1; overflow-y: auto; padding: 16px 20px; }
.inv-card {
    background: var(--card2); border: 1px solid var(--border);
    border-radius: 10px; padding: 14px; margin-bottom: 10px;
    display: flex; align-items: center; justify-content: space-between;
    text-decoration: none; transition: border-color .15s;
}
.inv-card:hover { border-color: var(--green); }
.inv-num { font-family: var(--mono); font-size: 12px; color: var(--muted); }
.inv-date { font-size: 12px; color: var(--muted); margin-top: 3px; }
.inv-total { font-family: var(--mono); font-size: 16px; font-weight: 700; color: var(--green); }
.inv-method { font-size: 11px; color: var(--sub); text-transform: capitalize; }
.side-stats {
    padding: 16px 20px; border-top: 1px solid var(--border);
    display: grid; grid-template-columns: 1fr 1fr; gap: 12px;
}
.side-stat { display: flex; flex-direction: column; gap: 4px; }
.side-stat-label { font-size: 10px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; color: var(--muted); }
.side-stat-val { font-family: var(--mono); font-size: 20px; font-weight: 700; color: var(--green); }

.toast {
    position: fixed; bottom: 22px; left: 50%;
    transform: translateX(-50%) translateY(16px);
    background: var(--card2); border: 1px solid var(--border2);
    border-radius: var(--r); padding: 12px 20px;
    font-size: 13px; font-weight: 600; color: var(--text);
    box-shadow: 0 20px 50px rgba(0,0,0,.5);
    opacity: 0; pointer-events: none;
    transition: opacity .25s, transform .25s; z-index: 999;
}
.toast.show { opacity:1; transform: translateX(-50%) translateY(0); }

.btn-export {
    background: var(--card2); border: 1px solid var(--border2);
    color: var(--sub); display: flex; align-items: center; gap: 7px;
    padding: 10px 15px; border-radius: var(--r);
    font-family: var(--sans); font-size: 13px; font-weight: 600;
    cursor: pointer; transition: all .2s; white-space: nowrap;
}
.btn-export:hover { border-color: var(--green); color: var(--green); }
.btn-export:disabled { opacity: .5; cursor: not-allowed; }
::-webkit-scrollbar { width: 4px; }
::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 4px; }
th.sortable {
    cursor: pointer;
    user-select: none;
    white-space: nowrap;
    transition: color .15s;
}
th.sortable:hover { color: var(--text); }
th.sortable.active { color: var(--green); }
.sort-arrow {
    display: inline-block;
    margin-left: 5px;
    font-size: 10px;
    opacity: 0.5;
    transition: opacity .15s, transform .15s;
}
th.sortable.active .sort-arrow { opacity: 1; }
th.sortable.active.desc .sort-arrow { transform: rotate(180deg); }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
    <script src="/static/auth-guard.js"></script>
</head>
<body>

""" + render_app_header(current_user, "page_customers") + """

<div class="content">
    <div>
        <div class="page-title">Customers</div>
        <div class="page-sub">View and manage your customer base</div>
    </div>

    <div class="toolbar">
        <div class="search-box">
            <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24">
                <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
            </svg>
            <input id="search" placeholder="Search by name, phone or email…" oninput="onSearch()">
        </div>
        <span class="count-badge" id="count-badge">— customers</span>
        <button class="btn btn-export" id="export-btn" onclick="exportCSV()" title="Export current filtered list as CSV">
            <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2" viewBox="0 0 24 24" style="flex-shrink:0"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
            Export CSV
        </button>
        <button class="btn btn-green" onclick="openAddModal()">+ Add Customer</button>
    </div>

    <div class="table-wrap">
        <table>
            <thead>
                <tr id="table-head">
                    <th class="sortable active" data-col="name" onclick="setSort('name')">Name <span class="sort-arrow">▲</span></th>
                    <th>Phone</th>
                    <th>Email</th>
                    <th class="sortable" data-col="discount_pct" onclick="setSort('discount_pct')">Discount <span class="sort-arrow">▲</span></th>
                    <th>Address</th>
                    <th class="sortable" data-col="invoices" onclick="setSort('invoices')">Invoices <span class="sort-arrow">▲</span></th>
                    <th class="sortable" data-col="total_spent" onclick="setSort('total_spent')">Total Spent <span class="sort-arrow">▲</span></th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody id="table-body">
                <tr><td colspan="8" style="text-align:center;color:var(--muted);padding:40px">Loading…</td></tr>
            </tbody>
        </table>
        <div class="pagination">
            <span id="page-info">—</span>
            <div class="page-btns">
                <button class="page-btn" id="prev-btn" onclick="prevPage()">← Prev</button>
                <button class="page-btn" id="next-btn" onclick="nextPage()">Next →</button>
            </div>
        </div>
    </div>
</div>

<!-- ADD / EDIT MODAL -->
<div class="modal-bg" id="modal">
    <div class="modal">
        <div class="modal-title" id="modal-title">Add Customer</div>
        <div class="fld">
            <label>Name *</label>
            <input id="f-name" placeholder="Customer name">
        </div>
        <div class="fld">
            <label>Phone</label>
            <input id="f-phone" placeholder="+20 100 000 0000">
        </div>
        <div class="fld">
            <label>Email</label>
            <input id="f-email" placeholder="customer@email.com">
        </div>
        <div class="fld">
            <label>Address</label>
            <input id="f-address" placeholder="City / Area">
        </div>
        <div class="fld">
            <label>Default Discount %</label>
            <input id="f-discount" type="number" min="0" max="100" step="0.5" placeholder="0">
        </div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeModal()">Cancel</button>
            <button class="btn btn-green" onclick="saveCustomer()">Save Customer</button>
        </div>
    </div>
</div>

<!-- INVOICE HISTORY SIDE PANEL -->
<div class="side-bg" id="side-bg" onclick="closeSide()"></div>
<div class="side-panel" id="side-panel">
    <div class="side-header">
        <h3 id="side-name">Customer</h3>
        <button class="close-btn" onclick="closeSide()">×</button>
    </div>
    <div class="side-body" id="side-body">
        <div style="color:var(--muted);font-size:13px">Loading…</div>
    </div>
    <div class="side-stats">
        <div class="side-stat">
            <span class="side-stat-label">Total Invoices</span>
            <span class="side-stat-val" id="side-inv-count">—</span>
        </div>
        <div class="side-stat">
            <span class="side-stat-label">Total Spent</span>
            <span class="side-stat-val" id="side-inv-total">—</span>
        </div>
    </div>
</div>

<div class="toast" id="toast"></div>

<script>
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
  let currentPage = 0;
let pageSize    = 50;
let totalItems  = 0;
let searchTimer = null;
let editingId   = null;
let sortBy      = "name";
let sortDir     = "asc";

function setSort(col) {
    if (sortBy === col) {
        sortDir = sortDir === "asc" ? "desc" : "asc";
    } else {
        sortBy  = col;
        sortDir = "asc";
    }
    // Update header visual state
    document.querySelectorAll("th.sortable").forEach(th => {
        const isActive = th.dataset.col === sortBy;
        th.classList.toggle("active", isActive);
        th.classList.toggle("desc", isActive && sortDir === "desc");
    });
    currentPage = 0;
    load();
}

function escapeJsString(value){
    const text = String(value == null ? "" : value);
    const backslash = String.fromCharCode(92);
    const quote = String.fromCharCode(39);
    const carriageReturn = String.fromCharCode(13);
    const newline = String.fromCharCode(10);
    return text
        .split(backslash).join(backslash + backslash)
        .split(quote).join(backslash + quote)
        .split(carriageReturn).join(backslash + "r")
        .split(newline).join(backslash + "n");
}

async function load(){
    let q   = document.getElementById("search").value.trim();
    let url = `/customers-mgmt/api/list?skip=${currentPage*pageSize}&limit=${pageSize}&sort_by=${sortBy}&sort_dir=${sortDir}`;
    if(q) url += `&q=${encodeURIComponent(q)}`;

    let data = await (await fetch(url)).json();
    totalItems = data.total;

    document.getElementById("count-badge").innerText = `${totalItems} customers`;
    document.getElementById("page-info").innerText =
        `Showing ${Math.min(currentPage*pageSize+1,totalItems)}–${Math.min((currentPage+1)*pageSize, totalItems)} of ${totalItems}`;

    document.getElementById("prev-btn").disabled = currentPage === 0;
    document.getElementById("next-btn").disabled = (currentPage+1)*pageSize >= totalItems;

    if(!data.items.length){
        document.getElementById("table-body").innerHTML =
            `<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:40px">No customers found</td></tr>`;
        return;
    }

    document.getElementById("table-body").innerHTML = data.items.map(c => `
        <tr onclick="openHistory(${c.id},'${escapeJsString(c.name)}',${c.invoices},${c.total_spent})">
            <td class="name">${c.name}</td>
            <td class="phone">${c.phone}</td>
            <td style="font-size:12px">${c.email}</td>
            <td class="mono" style="color:${c.discount_pct>0 ? "var(--warn)" : "var(--muted)"}">${c.discount_pct>0 ? c.discount_pct.toFixed(1) + "%" : "—"}</td>
            <td style="font-size:12px">${c.address}</td>
            <td style="font-family:var(--mono);color:var(--blue)">${c.invoices}</td>
            <td class="mono">${c.total_spent.toFixed(2)}</td>
            <td style="display:flex;gap:6px" onclick="event.stopPropagation()">
                <a class="action-btn" href="/customers-mgmt/profile/${c.id}" style="text-decoration:none;display:inline-flex;align-items:center">Profile</a>
                <button class="action-btn" onclick="openEditModal(${c.id},'${escapeJsString(c.name)}','${escapeJsString(c.phone)}','${escapeJsString(c.email)}','${escapeJsString(c.address)}',${c.discount_pct})">Edit</button>
                <button class="action-btn danger" onclick="deleteCustomer(${c.id},'${escapeJsString(c.name)}')">Delete</button>
            </td>
        </tr>`).join("");
}

function onSearch(){
    clearTimeout(searchTimer);
    searchTimer = setTimeout(()=>{ currentPage=0; load(); }, 300);
}
function prevPage(){ if(currentPage>0){ currentPage--; load(); } }
function nextPage(){ if((currentPage+1)*pageSize<totalItems){ currentPage++; load(); } }

/* ── CSV EXPORT ── */
async function exportCSV(){
    const btn = document.getElementById("export-btn");
    btn.disabled = true;
    btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" style="animation:spin .8s linear infinite;flex-shrink:0"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg> Exporting…`;

    try {
        const q   = document.getElementById("search").value.trim();
        let url   = `/customers-mgmt/api/list?skip=0&limit=10000&sort_by=${sortBy}&sort_dir=${sortDir}`;
        if (q) url += `&q=${encodeURIComponent(q)}`;

        const data  = await (await fetch(url)).json();
        const items = data.items;

        if (!items.length) { showToast("No customers to export"); return; }

        const headers = ["ID","Name","Phone","Email","Address","Discount %","Invoices","Total Spent"];
        const rows    = items.map(c => [
            c.id,
            csvCell(c.name),
            csvCell(c.phone === "—" ? "" : c.phone),
            csvCell(c.email === "—" ? "" : c.email),
            csvCell(c.address === "—" ? "" : c.address),
            c.discount_pct.toFixed(2),
            c.invoices,
            c.total_spent.toFixed(2),
        ]);

        const csv = [headers, ...rows].map(r => r.join(",")).join("\n");
        const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
        const link = document.createElement("a");
        link.href  = URL.createObjectURL(blob);

        const dateStr = new Date().toISOString().slice(0,10);
        const label   = q ? `_${q.replace(/[^a-z0-9]/gi,"_")}` : "";
        link.download = `customers${label}_${dateStr}.csv`;
        link.click();
        URL.revokeObjectURL(link.href);

        showToast(`Exported ${items.length} customers ✓`);
    } finally {
        btn.disabled = false;
        btn.innerHTML = `<svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2" viewBox="0 0 24 24" style="flex-shrink:0"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg> Export CSV`;
    }
}

function csvCell(val) {
    const s = String(val ?? "");
    return (s.includes(",") || s.includes('"') || s.includes("\n"))
        ? `"${s.replace(/"/g, '""')}"` : s;
}

/* ── ADD/EDIT MODAL ── */
function openAddModal(){
    editingId = null;
    document.getElementById("modal-title").innerText = "Add Customer";
    ["f-name","f-phone","f-email","f-address","f-discount"].forEach(id =>
        document.getElementById(id).value = "");
    document.getElementById("f-discount").value = "0";
    document.getElementById("modal").classList.add("open");
}

function openEditModal(id, name, phone, email, address, discountPct){
    editingId = id;
    document.getElementById("modal-title").innerText = "Edit Customer";
    document.getElementById("f-name").value    = name;
    document.getElementById("f-phone").value   = phone === "—" ? "" : phone;
    document.getElementById("f-email").value   = email === "—" ? "" : email;
    document.getElementById("f-address").value = address === "—" ? "" : address;
    document.getElementById("f-discount").value = Number(discountPct || 0).toFixed(1);
    document.getElementById("modal").classList.add("open");
}

function closeModal(){
    document.getElementById("modal").classList.remove("open");
}

async function saveCustomer(){
    let name = document.getElementById("f-name").value.trim();
    if(!name){ showToast("Name is required"); return; }

    let body = {
        name,
        phone:   document.getElementById("f-phone").value.trim() || null,
        email:   document.getElementById("f-email").value.trim() || null,
        address: document.getElementById("f-address").value.trim() || null,
        discount_pct: parseFloat(document.getElementById("f-discount").value) || 0,
    };

    let url    = editingId ? `/customers-mgmt/api/edit/${editingId}` : "/customers-mgmt/api/add";
    let method = editingId ? "PUT" : "POST";

    let res  = await fetch(url, {
        method, headers: {"Content-Type":"application/json"}, body: JSON.stringify(body),
    });
    let data = await res.json();
    if(data.detail){ showToast("Error: " + data.detail); return; }

    closeModal();
    showToast(editingId ? "Customer updated ✓" : "Customer added ✓");
    load();
}

async function deleteCustomer(id, name){
    if(!confirm(`Delete "${name}"? This cannot be undone.`)) return;
    let res = await fetch(`/customers-mgmt/api/delete/${id}`, {method:"DELETE"});
    let data = await res.json();
    if(data.detail){ showToast("Error: " + data.detail); return; }
    showToast("Customer deleted ✓");
    load();
}

/* ── INVOICE HISTORY ── */
async function openHistory(id, name, invCount, totalSpent){
    document.getElementById("side-name").innerText  = name;
    document.getElementById("side-inv-count").innerText = invCount;
    document.getElementById("side-inv-total").innerText = totalSpent.toFixed(2);
    document.getElementById("side-body").innerHTML  = `<div style="color:var(--muted);font-size:13px">Loading…</div>`;
    document.getElementById("side-bg").classList.add("open");
    document.getElementById("side-panel").classList.add("open");

    let rows = await (await fetch(`/customers-mgmt/api/invoices/${id}`)).json();

    if(!rows.length){
        document.getElementById("side-body").innerHTML =
            `<div style="color:var(--muted);font-size:13px;padding:20px 0">No activity yet</div>`;
        return;
    }

    document.getElementById("side-body").innerHTML = rows.map(i => {
        const isRefund = i.type === "refund";
        const numColor = isRefund ? "var(--danger)" : "var(--green)";
        const numText  = isRefund ? "−" + Math.abs(i.total).toFixed(2) : i.total.toFixed(2);
        const statusColor = i.status === "paid" ? "var(--green)"
            : i.status === "refunded" ? "var(--danger)"
            : "var(--warn)";
        const cardStyle = isRefund
            ? "border-color:rgba(255,77,109,.25);background:rgba(255,77,109,.04);"
            : "";
        const refundBadge = isRefund
            ? `<div style="font-size:10px;font-weight:700;color:var(--danger);letter-spacing:.5px;margin-top:2px">↩ REFUND${i.reason && i.reason !== "—" ? " · "+i.reason : ""}</div>`
            : "";
        const href = isRefund
            ? `/refunds/print/${i.id}`
            : `/invoice/${i.id}`;
        const printHref = isRefund
            ? `/refunds/print/${i.id}`
            : `/invoice/${i.id}`;
        return `
        <div class="inv-card" style="${cardStyle}; cursor:pointer" onclick="window.open('${href}', '_blank')">
            <div>
                <div class="inv-num" style="color:${isRefund ? "var(--danger)" : ""}">${i.ref_number}</div>
                <div class="inv-date">${i.created_at}</div>
                <div class="inv-method">${i.payment_method}</div>
                ${refundBadge}
            </div>
            <div style="text-align:right">
                <div class="inv-total" style="color:${numColor}">${numText}</div>
                <div style="font-size:11px;color:${statusColor};margin-top:4px">${i.status}</div>
                <a href="${printHref}" target="_blank" onclick="event.stopPropagation()" class="action-btn" style="text-decoration:none; display:inline-block; margin-top:8px">🖨️ Print</a>
            </div>
        </div>`;
    }).join("");
}

function closeSide(){
    document.getElementById("side-bg").classList.remove("open");
    document.getElementById("side-panel").classList.remove("open");
}

document.getElementById("modal").addEventListener("click", function(e){
    if(e.target === this) closeModal();
});

/* ── TOAST ── */
let toastTimer = null;
function showToast(msg){
    let t = document.getElementById("toast");
    t.innerText = msg; t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(()=>t.classList.remove("show"), 3000);
}

load();
</script>
</body>
</html>
"""


# ═══════════════════════════════════════════════════════
#  CUSTOMER SEGMENTATION
#  GET /customers-mgmt/api/segments  — JSON data
#  GET /customers-mgmt/segments/     — HTML page
# ═══════════════════════════════════════════════════════

@router.get("/api/segments")
async def get_segments(db: AsyncSession = Depends(get_async_session)):
    from datetime import datetime, timezone, timedelta

    now  = datetime.now(timezone.utc)
    d60  = now - timedelta(days=60)
    d180 = now - timedelta(days=180)

    # ── All aggregates in a single SQL round-trip ──────
    last_inv_sq = (
        select(func.max(Invoice.created_at))
        .where(Invoice.customer_id == Customer.id, Invoice.status == "paid")
        .correlate(Customer)
        .scalar_subquery()
    )
    first_inv_sq = (
        select(func.min(Invoice.created_at))
        .where(Invoice.customer_id == Customer.id, Invoice.status == "paid")
        .correlate(Customer)
        .scalar_subquery()
    )
    ref_total_sq = (
        select(func.coalesce(func.sum(RetailRefund.total), 0))
        .where(RetailRefund.customer_id == Customer.id)
        .correlate(Customer)
        .scalar_subquery()
    )
    net_spent_sq = (
        select(
            func.greatest(
                func.coalesce(func.sum(Invoice.total), 0) - ref_total_sq,
                0,
            )
        )
        .where(Invoice.customer_id == Customer.id, Invoice.status == "paid")
        .correlate(Customer)
        .scalar_subquery()
    )
    inv_count_sq = (
        select(func.count(Invoice.id))
        .where(Invoice.customer_id == Customer.id, Invoice.status == "paid")
        .correlate(Customer)
        .scalar_subquery()
    )

    rows_result = await db.execute(
        select(
            Customer.id,
            Customer.name,
            Customer.phone,
            Customer.email,
            Customer.discount_pct,
            last_inv_sq.label("last_purchase"),
            first_inv_sq.label("first_purchase"),
            net_spent_sq.label("net_spent"),
            inv_count_sq.label("inv_count"),
        ).order_by(Customer.name)
    )
    customers_data = rows_result.all()

    # Champion threshold = 75th-percentile net_spent among active customers
    active_spends = sorted(
        [float(r.net_spent or 0) for r in customers_data
         if r.net_spent and float(r.net_spent) > 0],
        reverse=True,
    )
    champion_threshold = (
        active_spends[max(0, int(len(active_spends) * 0.25) - 1)]
        if active_spends else 0
    )

    segments: dict[str, list] = {
        "champion": [], "loyal": [], "new": [], "at_risk": [], "lost": [],
    }

    for r in customers_data:
        last  = r.last_purchase
        first = r.first_purchase
        spent = float(r.net_spent or 0)
        count = int(r.inv_count or 0)

        # Normalise timezone
        if last is not None and last.tzinfo is None:
            from datetime import timezone as _tz
            last  = last.replace(tzinfo=_tz.utc)
        if first is not None and first.tzinfo is None:
            from datetime import timezone as _tz
            first = first.replace(tzinfo=_tz.utc)

        if last is None:
            seg = "lost"
        elif first and first >= d60 and count <= 2:
            seg = "new"
        elif last >= d60 and spent >= champion_threshold and champion_threshold > 0:
            seg = "champion"
        elif last >= d60:
            seg = "loyal"
        elif last >= d180:
            seg = "at_risk"
        else:
            seg = "lost"

        segments[seg].append({
            "id":            r.id,
            "name":          r.name,
            "phone":         r.phone or "—",
            "email":         r.email or "—",
            "discount_pct":  float(r.discount_pct or 0),
            "net_spent":     round(spent, 2),
            "inv_count":     count,
            "last_purchase": last.strftime("%Y-%m-%d") if last else None,
            "first_purchase": first.strftime("%Y-%m-%d") if first else None,
        })

    for seg_list in segments.values():
        seg_list.sort(key=lambda x: x["net_spent"], reverse=True)

    counts   = {k: len(v) for k, v in segments.items()}
    counts["total"] = sum(counts.values())
    avg_spent = {
        k: round(sum(c["net_spent"] for c in v) / len(v), 2) if v else 0
        for k, v in segments.items()
    }

    return {
        "counts":              counts,
        "avg_spent":           avg_spent,
        "champion_threshold":  round(champion_threshold, 2),
        "segments":            segments,
    }


@router.get("/segments/", response_class=HTMLResponse)
def segments_ui(current_user: User = Depends(require_permission("page_customers"))):
    return """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<script src="/static/theme-init.js"></script>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Customer Segments — Thunder ERP</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root {
    --bg:#060810;--surface:#0a0d18;--card:#0f1424;--card2:#151c30;
    --border:rgba(255,255,255,0.06);--border2:rgba(255,255,255,0.11);
    --green:#00ff9d;--blue:#4d9fff;--amber:#ffb547;--rose:#ff4d6d;
    --text:#f0f4ff;--sub:#8899bb;--muted:#445066;
    --sans:'Outfit',sans-serif;--mono:'JetBrains Mono',monospace;--r:12px;
    --c-champion:#ffb547;--c-loyal:#00ff9d;--c-new:#4d9fff;
    --c-at_risk:#ff944d;--c-lost:#ff4d6d;
}
body.light {
    --bg:#f4f5ef;--surface:#f1f3eb;--card:#eceee6;--card2:#e4e6de;
    --border:rgba(0,0,0,0.08);--border2:rgba(0,0,0,0.14);
    --green:#0f8a43;--blue:#185fa5;--amber:#854f0b;--rose:#a32d2d;
    --text:#1a1e14;--sub:#4a5040;--muted:#7b816f;
    --c-champion:#854f0b;--c-loyal:#0f8a43;--c-new:#185fa5;
    --c-at_risk:#8a4800;--c-lost:#a32d2d;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:var(--sans);background:var(--bg);color:var(--text);min-height:100vh;font-size:14px}

.content{max-width:1300px;margin:0 auto;padding:28px 24px;display:flex;flex-direction:column;gap:22px}
.page-title{font-size:24px;font-weight:800;letter-spacing:-.5px}
.page-sub{color:var(--muted);font-size:13px;margin-top:3px}

.seg-tabs{display:flex;gap:10px;flex-wrap:wrap}
.seg-tab{
    display:flex;flex-direction:column;gap:4px;
    background:var(--card);border:1px solid var(--border);
    border-radius:var(--r);padding:14px 18px;cursor:pointer;
    transition:all .2s;min-width:130px;flex:1;
    border-top:3px solid transparent;
}
.seg-tab:hover{border-color:var(--border2)}
.seg-tab.active{border-top-color:var(--seg-color)}
.seg-tab-label{font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted)}
.seg-tab-count{font-family:var(--mono);font-size:28px;font-weight:700;color:var(--seg-color)}
.seg-tab-avg{font-size:11px;color:var(--muted)}

.seg-def{
    background:var(--card);border:1px solid var(--border);
    padding:12px 18px;font-size:12px;color:var(--sub);
    border-left:3px solid var(--seg-color);
    border-radius:0 var(--r) var(--r) 0;display:none;
}

.table-wrap{background:var(--card);border:1px solid var(--border);border-radius:var(--r);overflow:hidden}
table{width:100%;border-collapse:collapse}
thead{background:var(--card2)}
th{font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);padding:12px 16px;text-align:left;white-space:nowrap}
td{padding:12px 16px;border-top:1px solid var(--border);font-size:13px;color:var(--sub)}
tbody tr:hover td{background:rgba(255,255,255,.02);cursor:pointer}
td.name-cell{color:var(--text);font-weight:600}
.seg-pill{display:inline-block;font-size:10px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;padding:3px 9px;border-radius:6px;background:color-mix(in srgb,var(--seg-color) 14%,transparent);color:var(--seg-color);border:1px solid color-mix(in srgb,var(--seg-color) 28%,transparent)}
.empty{text-align:center;padding:48px;color:var(--muted);font-size:13px}
.bar-wrap{display:flex;align-items:center;gap:8px}
.bar-bg{flex:1;height:4px;background:var(--border);border-radius:2px;overflow:hidden;min-width:40px}
.bar-fill{height:100%;border-radius:2px;background:var(--seg-color)}

.toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.search-box{display:flex;align-items:center;gap:9px;background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:0 14px;flex:1;min-width:200px;transition:border-color .2s}
.search-box:focus-within{border-color:rgba(77,159,255,.35)}
.search-box input{background:transparent;border:none;outline:none;color:var(--text);font-family:var(--sans);font-size:14px;padding:11px 0;width:100%}
.search-box input::placeholder{color:var(--muted)}
.count-badge{background:var(--card2);border:1px solid var(--border2);color:var(--sub);font-family:var(--mono);font-size:12px;padding:8px 14px;border-radius:var(--r);white-space:nowrap}
.btn-export{background:var(--card2);border:1px solid var(--border2);color:var(--sub);display:flex;align-items:center;gap:7px;padding:10px 15px;border-radius:var(--r);font-family:var(--sans);font-size:13px;font-weight:600;cursor:pointer;transition:all .2s;white-space:nowrap}
.btn-export:hover{border-color:var(--blue);color:var(--blue)}
@keyframes spin{to{transform:rotate(360deg)}}
::-webkit-scrollbar{width:4px}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:4px}
@media(max-width:640px){
    .seg-tab{min-width:calc(50% - 5px);flex:none}
    .seg-tab-count{font-size:22px}
}
</style>
<script src="/static/auth-guard.js"></script>
</head>
<body>
""" + render_app_header(current_user, "page_customers") + """

<div class="content">
    <div>
        <div class="page-title">Customer Segments</div>
        <div class="page-sub">Auto-classified by recency and spend — updates live from your invoices</div>
    </div>

    <div class="seg-tabs" id="seg-tabs">
        <div style="color:var(--muted);font-size:13px;padding:20px">Loading…</div>
    </div>

    <div class="seg-def" id="seg-def"></div>

    <div class="toolbar">
        <div class="search-box">
            <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24">
                <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
            </svg>
            <input id="search" placeholder="Filter by name, phone or email…" oninput="filterTable()">
        </div>
        <span class="count-badge" id="count-badge">—</span>
        <button class="btn-export" id="export-btn" onclick="exportCSV()">
            <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2" viewBox="0 0 24 24">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                <polyline points="7 10 12 15 17 10"/>
                <line x1="12" y1="15" x2="12" y2="3"/>
            </svg>
            Export CSV
        </button>
    </div>

    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>Name</th>
                    <th>Phone</th>
                    <th>Last Purchase</th>
                    <th>Orders</th>
                    <th>Net Spent</th>
                    <th>Segment</th>
                </tr>
            </thead>
            <tbody id="table-body">
                <tr><td colspan="6" class="empty">Loading…</td></tr>
            </tbody>
        </table>
    </div>
</div>

<script>
const SEG_META = {
    champion: {
        label: "Champions",
        color: "var(--c-champion)",
        def: "Purchased within 60 days AND in the top 25% by net spend. Your most valuable customers — reward them.",
    },
    loyal: {
        label: "Loyal",
        color: "var(--c-loyal)",
        def: "Purchased within the last 60 days. Regular buyers who keep coming back.",
    },
    new: {
        label: "New",
        color: "var(--c-new)",
        def: "First purchase within 60 days, with 2 or fewer orders. Nurture them into loyal regulars.",
    },
    at_risk: {
        label: "At Risk",
        color: "var(--c-at_risk)",
        def: "Last purchase was 61–180 days ago. A targeted offer now can win them back.",
    },
    lost: {
        label: "Lost",
        color: "var(--c-lost)",
        def: "Last purchase over 180 days ago, or never purchased. Worth a re-engagement campaign.",
    },
};

let allData   = {};
let activeSeg = "champion";
let maxSpend  = 1;

const fmt = n => Number(n).toLocaleString("en-US", {minimumFractionDigits:2, maximumFractionDigits:2});
const esc = s => String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");

async function load(){
    const d = await (await fetch("/customers-mgmt/api/segments")).json();
    allData  = d;
    maxSpend = Math.max(1, ...Object.values(d.segments).flat().map(c => c.net_spent));
    renderTabs(d);
    renderSegment(activeSeg);
}

function renderTabs(d){
    const order = ["champion","loyal","new","at_risk","lost"];
    document.getElementById("seg-tabs").innerHTML = order.map(seg => {
        const m = SEG_META[seg];
        return `<div class="seg-tab${seg===activeSeg?" active":""}" style="--seg-color:${m.color}" onclick="selectSeg('${seg}')">
            <span class="seg-tab-label">${m.label}</span>
            <span class="seg-tab-count">${d.counts[seg]||0}</span>
            <span class="seg-tab-avg">avg ${fmt(d.avg_spent[seg]||0)}</span>
        </div>`;
    }).join("");
}

function selectSeg(seg){
    activeSeg = seg;
    document.querySelectorAll(".seg-tab").forEach(el => {
        el.classList.toggle("active", el.querySelector(".seg-tab-label").textContent === SEG_META[seg].label);
    });
    renderSegment(seg);
}

function renderSegment(seg){
    const defEl = document.getElementById("seg-def");
    defEl.style.display = "block";
    defEl.style.setProperty("--seg-color", SEG_META[seg].color);
    defEl.textContent = SEG_META[seg].def;
    renderTable(seg, document.getElementById("search").value.trim());
}

function filterTable(){ renderTable(activeSeg, document.getElementById("search").value.trim()); }

function renderTable(seg, q){
    let rows = [...((allData.segments||{})[seg]||[])];
    if(q){
        const lq = q.toLowerCase();
        rows = rows.filter(c =>
            c.name.toLowerCase().includes(lq) ||
            (c.phone||"").includes(lq) ||
            (c.email||"").toLowerCase().includes(lq)
        );
    }
    document.getElementById("count-badge").textContent = `${rows.length} customers`;
    const color = SEG_META[seg].color;
    const tbody = document.getElementById("table-body");
    if(!rows.length){
        tbody.innerHTML = `<tr><td colspan="6" class="empty">No customers in this segment</td></tr>`;
        return;
    }
    tbody.innerHTML = rows.map(c => `
        <tr style="--seg-color:${color}" onclick="location.href='/customers-mgmt/profile/${c.id}'">
            <td class="name-cell">${esc(c.name)}</td>
            <td style="font-family:var(--mono);font-size:12px">${esc(c.phone)}</td>
            <td style="font-family:var(--mono);font-size:12px;color:var(--muted)">${c.last_purchase||"—"}</td>
            <td style="font-family:var(--mono);color:var(--blue)">${c.inv_count}</td>
            <td>
                <div class="bar-wrap">
                    <span style="font-family:var(--mono);font-size:13px;color:${color};min-width:72px;display:inline-block">${fmt(c.net_spent)}</span>
                    <div class="bar-bg"><div class="bar-fill" style="width:${Math.round(c.net_spent/maxSpend*100)}%"></div></div>
                </div>
            </td>
            <td><span class="seg-pill" style="--seg-color:${color}">${SEG_META[seg].label}</span></td>
        </tr>`).join("");
}

function exportCSV(){
    const rows = (allData.segments||{})[activeSeg]||[];
    if(!rows.length) return;
    const label = SEG_META[activeSeg].label;
    const headers = ["ID","Name","Phone","Email","Segment","Last Purchase","Orders","Net Spent"];
    const csv = [
        headers,
        ...rows.map(c => [
            c.id,
            `"${c.name.replace(/"/g,'""')}"`,
            c.phone==="—"?"":c.phone,
            c.email==="—"?"":c.email,
            label,
            c.last_purchase||"",
            c.inv_count,
            c.net_spent.toFixed(2),
        ])
    ].map(r=>r.join(",")).join("\\n");
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([csv],{type:"text/csv;charset=utf-8;"}));
    link.download = `customers_${activeSeg}_${new Date().toISOString().slice(0,10)}.csv`;
    link.click();
    URL.revokeObjectURL(link.href);
}

load();
</script>
</body>
</html>"""