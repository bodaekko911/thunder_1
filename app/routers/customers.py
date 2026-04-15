from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import func, select
from typing import Optional
from pydantic import BaseModel

from app.database import get_async_session
from app.core.permissions import get_current_user, require_permission
from app.models.customer import Customer
from app.models.user import User
from app.models.invoice import Invoice, InvoiceItem
from app.models.refund import RetailRefund
from app.core.log import record
from app.schemas.customer import CustomerCreate, CustomerUpdate

router = APIRouter(
    prefix="/customers-mgmt",
    tags=["Customers"],
    dependencies=[Depends(require_permission("page_customers"))],
)


# ── API ────────────────────────────────────────────────
@router.get("/api/list")
async def get_customers(
    q:     str = "",
    skip:  int = 0,
    limit: int = 50,
    db: AsyncSession = Depends(get_async_session),
):
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

    cust_result = await db.execute(
        select(Customer).where(*conditions).order_by(Customer.name).offset(skip).limit(limit)
    )
    items = cust_result.scalars().all()

    result = []
    for c in items:
        inv_cnt_res = await db.execute(
            select(func.count(Invoice.id)).where(Invoice.customer_id == c.id)
        )
        inv_count = inv_cnt_res.scalar() or 0

        inv_sum_res = await db.execute(
            select(func.sum(Invoice.total)).where(
                Invoice.customer_id == c.id, Invoice.status == "paid"
            )
        )
        inv_total = inv_sum_res.scalar() or 0

        ref_sum_res = await db.execute(
            select(func.sum(RetailRefund.total)).where(RetailRefund.customer_id == c.id)
        )
        ref_total = ref_sum_res.scalar() or 0

        result.append({
            "id":          c.id,
            "name":        c.name,
            "phone":       c.phone or "—",
            "email":       c.email or "—",
            "address":     c.address or "—",
            "discount_pct": float(c.discount_pct or 0),
            "invoices":    inv_count,
            "total_spent": max(0.0, float(inv_total) - float(ref_total)),
            "ref_total":   float(ref_total),
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

@media(max-width:700px){{
    .content{{padding:20px 16px}}
    .cust-name{{font-size:26px}}
    .stats-grid{{grid-template-columns:1fr 1fr}}
    .topbar{{padding:0 16px}}
}}
</style>
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
// Auth guard: redirect to login if the readable session cookie is absent
function _hasAuthCookie() {{
    return document.cookie.split(";").some(c => c.trim().startsWith("logged_in="));
}}
if (!_hasAuthCookie()) {{ window.location.href = "/"; }}

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
                    </tr>
                </thead>
                <tbody id="orders-tbody">
                    ${{d.orders.length === 0 ? '<tr><td colspan="6" style="text-align:center;padding:40px;color:var(--muted)">No orders yet</td></tr>' : ""}}
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
            <td style="text-align:right" class="${{totalCls}}">${{totalSign}}${{fmt(Math.abs(o.total))}}</td>`;

        // items sub-row
        const subTr = document.createElement("tr");
        subTr.className = "items-row";
        subTr.style.display = "none";
        if (hasItems) {{
            subTr.innerHTML = `<td colspan="6">
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
</script>
</body>
</html>"""


# ── UI ─────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
def customers_ui():
    return """
<!DOCTYPE html>
<html>
<head>
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
.user-pill{display:flex;align-items:center;gap:10px;background:var(--card);border:1px solid var(--border);border-radius:40px;padding:7px 16px 7px 10px;}
.user-avatar{width:28px;height:28px;background:linear-gradient(135deg,#7ecb6f,#d4a256);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;color:#0a0c08;}
.user-name{font-size:13px;font-weight:500;color:var(--sub);}
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

::-webkit-scrollbar { width: 4px; }
::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 4px; }
</style>
</head>
<body>

<nav>
    <a href="/home" class="logo" style="text-decoration:none;display:flex;align-items:center;gap:8px;"><svg width="22" height="22" viewBox="0 0 24 24" fill="none"><polygon points="13,2 4,14 11,14 11,22 20,10 13,10" fill="#f59e0b" stroke="#fbbf24" stroke-width="0.5"/></svg>Thunder ERP</a>
    <a href="/dashboard"        class="nav-link">Dashboard</a>
    <a href="/pos"              class="nav-link">POS</a>
    <a href="/products/"        class="nav-link">Products</a>
    <a href="/customers-mgmt/"  class="nav-link active">Customers</a>
    <a href="/import"           class="nav-link">Import</a>
    <span class="nav-spacer"></span>
    <div class="topbar-right">
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
        <button class="btn btn-green" onclick="openAddModal()">+ Add Customer</button>
    </div>

    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>Name</th>
                    <th>Phone</th>
                    <th>Email</th>
                    <th>Discount</th>
                    <th>Address</th>
                    <th>Invoices</th>
                    <th>Total Spent</th>
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
  // Auth guard: redirect to login if the readable session cookie is absent
  function _hasAuthCookie() {
      return document.cookie.split(";").some(c => c.trim().startsWith("logged_in="));
  }
  if (!_hasAuthCookie()) { window.location.href = "/"; }

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
        if (!r.ok) { window.location.href = "/"; return; }
        const u = await r.json();
        const nameEl = document.getElementById("user-name");
        const avatarEl = document.getElementById("user-avatar");
        if (nameEl) nameEl.innerText = u.name;
        if (avatarEl) avatarEl.innerText = u.name.charAt(0).toUpperCase();
        return u;
    } catch(e) { window.location.href = "/"; }
}
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

function escapeJsString(value){
    return String(value == null ? "" : value)
        .replace(/\\/g,"\\\\")
        .replace(/'/g,"\\'")
        .replace(/\r/g,"\\r")
        .replace(/\n/g,"\\n");
}

async function load(){
    let q   = document.getElementById("search").value.trim();
    let url = `/customers-mgmt/api/list?skip=${currentPage*pageSize}&limit=${pageSize}`;
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
        return `
        <a class="inv-card" href="${href}" target="_blank" style="${cardStyle}">
            <div>
                <div class="inv-num" style="color:${isRefund ? "var(--danger)" : ""}">${i.ref_number}</div>
                <div class="inv-date">${i.created_at}</div>
                <div class="inv-method">${i.payment_method}</div>
                ${refundBadge}
            </div>
            <div style="text-align:right">
                <div class="inv-total" style="color:${numColor}">${numText}</div>
                <div style="font-size:11px;color:${statusColor};margin-top:4px">${i.status}</div>
            </div>
        </a>`;
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
