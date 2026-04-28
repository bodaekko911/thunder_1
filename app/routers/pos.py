from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import select, or_
from decimal import Decimal

from app.database import get_async_session
from app.core.log import logger
from app.core.permissions import ensure_action_permission, require_action, require_permission
from app.core.security import get_current_user
from app.core.navigation import render_app_header
from app.models.product import Product
from app.models.customer import Customer
from app.models.invoice import Invoice
from app.models.user import User
from app.schemas.invoice import InvoiceCollectionRequest, InvoiceCreate
from app.services.barcode_service import find_product_by_barcode, normalize_barcode_value
from app.services.pos_service import create_invoice

router = APIRouter(
    tags=["POS"],
    dependencies=[Depends(require_permission("page_pos"))],
)


@router.get("/products-cache")
async def products_cache(db: AsyncSession = Depends(get_async_session)):
    _r = await db.execute(
        select(Product).where(or_(Product.is_active.is_(True), Product.is_active.is_(None)))
    )
    products = _r.scalars().all()
    return [
        {
            "sku": p.sku,
            "name": p.name,
            "category": p.category or "",
            "price": float(p.price or 0),
            "stock": float(p.stock or 0),
        }
        for p in products
    ]


@router.get("/search-products")
async def search_products(
    q: str = Query("", max_length=100),
    db: AsyncSession = Depends(get_async_session),
):
    normalized_query = normalize_barcode_value(q)
    if normalized_query:
        exact_match = await find_product_by_barcode(db, normalized_query)
        if exact_match is not None:
            return [
                {"sku": exact_match.sku, "name": exact_match.name, "category": exact_match.category or "", "price": float(exact_match.price), "stock": float(exact_match.stock)}
            ]

    _r = await db.execute(
        select(Product)
        .where(
            or_(Product.is_active.is_(True), Product.is_active.is_(None)),
            or_(Product.name.ilike(f"%{q}%"), Product.sku.ilike(f"%{q}%"))
        )
        .limit(40)
    )
    results = _r.scalars().all()
    return [
        {
            "sku": p.sku,
            "name": p.name,
            "category": p.category or "",
            "price": float(p.price or 0),
            "stock": float(p.stock or 0),
        }
        for p in results
    ]


@router.get("/barcode-lookup")
async def barcode_lookup(
    barcode: str = Query("", max_length=200),
    db: AsyncSession = Depends(get_async_session),
):
    normalized = normalize_barcode_value(barcode)
    if not normalized:
        return {
            "found": False,
            "barcode": normalized,
            "detail": "Scan a barcode or enter a SKU.",
        }

    product = await find_product_by_barcode(db, normalized)
    if product is None:
        logger.warning(
            "Barcode lookup failed",
            extra={"barcode": normalized, "path": "/barcode-lookup"},
        )
        return {
            "found": False,
            "barcode": normalized,
            "detail": f"No product found for barcode '{normalized}'.",
        }

    return {
        "found": True,
        "barcode": normalized,
        "product": {
            "sku": product.sku,
            "name": product.name,
            "price": float(product.price),
            "stock": float(product.stock),
        },
    }


@router.get("/customers")
async def list_customers(db: AsyncSession = Depends(get_async_session)):
    _r = await db.execute(select(Customer).order_by(Customer.name))
    customers = _r.scalars().all()
    return [
        {
            "id": c.id,
            "name": c.name,
            "phone": c.phone,
            "discount_pct": float(c.discount_pct or 0),
        }
        for c in customers
    ]


@router.post("/invoice", dependencies=[Depends(require_action("pos", "sales", "create"))])
async def checkout(
    data: InvoiceCreate,
    db: AsyncSession = Depends(get_async_session),
    user=Depends(get_current_user),
):
    if data.discount_percent > 0:
        await ensure_action_permission(db, user, "pos", "sales", "discount_override", path="/invoice")
    if data.settle_later:
        await ensure_action_permission(db, user, "pos", "sales", "approve", path="/invoice")
    user_id = user.id
    invoice = await create_invoice(db=db, data=data, user_id=user_id, user=user)
    if not isinstance(invoice, dict) or invoice.get("id") is None:
        logger.error(
            "Checkout did not return a persisted invoice",
            extra={
                "path": "/invoice",
                "user_id": user_id,
                "invoice_type": type(invoice).__name__,
            },
        )
        raise HTTPException(status_code=500, detail="Invoice creation failed")
    return {
        "id": invoice["id"],
        "invoice_number": invoice["invoice_number"],
        "status": invoice["status"],
        "payment_method": invoice["payment_method"],
        "total": invoice["total"],
    }


@router.get("/unpaid-invoices")
async def get_unpaid_invoices(db: AsyncSession = Depends(get_async_session)):
    _r = await db.execute(
        select(Invoice)
        .where(Invoice.status == "unpaid")
        .options(selectinload(Invoice.customer), selectinload(Invoice.items))
        .order_by(Invoice.created_at.desc())
        .limit(50)
    )
    invoices = _r.scalars().all()
    result = []
    for i in invoices:
        result.append({
            "id":             i.id,
            "invoice_number": i.invoice_number,
            "customer":       i.customer.name if i.customer else "—",
            "total":          float(i.total),
            "subtotal":       float(i.subtotal),
            "discount":       float(i.discount),
            "created_at":     i.created_at.strftime("%Y-%m-%d %H:%M") if i.created_at else "—",
            "items": [
                {
                    "name":       it.name,
                    "qty":        float(it.qty),
                    "unit_price": float(it.unit_price),
                    "total":      float(it.total),
                }
                for it in i.items
            ],
        })
    return result


@router.post("/invoice/{invoice_id}/collect", dependencies=[Depends(require_action("pos", "sales", "approve"))])
async def collect_payment(
    invoice_id: int,
    data: InvoiceCollectionRequest,
    db: AsyncSession = Depends(get_async_session),
):
    from app.models.accounting import Account, Journal, JournalEntry

    _r = await db.execute(select(Invoice).where(Invoice.id == invoice_id))
    invoice = _r.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.status == "paid":
        raise HTTPException(status_code=400, detail="Invoice already paid")

    payment_method         = data.payment_method
    invoice.status         = "paid"
    invoice.payment_method = payment_method

    total   = float(invoice.total)
    journal = Journal(ref_type="payment",
                      description=f"Payment collected - {invoice.invoice_number}")
    db.add(journal); await db.flush()

    for code, debit, credit in [("1000", total, 0), ("1100", 0, total)]:
        _r = await db.execute(select(Account).where(Account.code == code))
        acc = _r.scalar_one_or_none()
        if acc:
            db.add(JournalEntry(
                journal_id=journal.id,
                account_id=acc.id,
                debit=debit,
                credit=credit,
            ))
            acc.balance += Decimal(str(debit)) - Decimal(str(credit))

    from app.core.log import record as log_record
    log_record(db, "POS", "collect_payment",
               f"Payment collected — {invoice.invoice_number} — {total:.2f} — {payment_method}",
               ref_type="invoice", ref_id=invoice_id)
    await db.commit()
    return {"ok": True, "invoice_number": invoice.invoice_number}


@router.get("/invoice/{invoice_id}", response_class=HTMLResponse)
async def view_invoice(invoice_id: int, db: AsyncSession = Depends(get_async_session)):
    _r = await db.execute(
        select(Invoice)
        .where(Invoice.id == invoice_id)
        .options(selectinload(Invoice.customer), selectinload(Invoice.items))
    )
    inv = _r.scalar_one_or_none()
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    customer = inv.customer
    items    = inv.items

    rows = ""
    for i in items:
        rows += f"""
        <div class="row">
            <span>{i.name}</span>
            <span>{float(i.qty):.0f} x {float(i.unit_price):.2f}</span>
            <span>{float(i.total):.2f}</span>
        </div>"""

    status_badge = ""
    if inv.status == "unpaid":
        status_badge = '<div style="background:rgba(255,181,71,.15);border:1px solid rgba(255,181,71,.3);color:#ffb547;border-radius:8px;padding:8px 12px;text-align:center;font-weight:700;margin-bottom:10px;">&#9203; UNPAID &#8212; Settle Later</div>'

    return f"""<!DOCTYPE html>
<html>
<head>
<script src="/static/theme-init.js"></script>
<title>Receipt {inv.invoice_number}</title>
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ font-family: monospace; background:#060810; color:white; }}
.r {{ width:320px; margin:30px auto; background:#0f1424; border-radius:16px; padding:20px; }}
.center {{ text-align:center; margin-bottom:12px; }}
.center h3 {{ color:#00ff9d; font-size:18px; }}
.row {{ display:flex; justify-content:space-between; font-size:13px; margin:6px 0; }}
.line {{ border-top:1px dashed #445066; margin:10px 0; }}
.total {{ font-size:20px; color:#00ff9d; font-weight:bold; }}
.btn {{ width:100%; padding:14px; margin-top:10px; background:linear-gradient(135deg,#00ff9d,#00d4ff); border:none; border-radius:10px; color:#021a10; font-size:15px; font-weight:800; cursor:pointer; }}
.btn-back {{ background:#151c30; color:#8899bb; margin-top:6px; }}
@media print {{
    .btn {{ display:none; }}
    body {{ background:white; color:black; }}
    .r {{ background:white; color:black; border:none; }}
    .center h3 {{ color:#000; }}
    .total {{ color:#000; }}
}}
</style>
    <script src="/static/auth-guard.js"></script>
</head>
<body>
<div class="r">
    <div class="center">
        <img src="/static/Logo.png" alt="Habiba" style="height:120px;object-fit:contain;margin-bottom:6px;display:block;margin-left:auto;margin-right:auto">
        <div style="font-size:15px;font-weight:900;color:#2a7a2a;margin-bottom:2px">Habiba Organic Farm</div>
        <div style="color:#445066;font-size:12px">{inv.invoice_number}</div>
    </div>
    <div class="line"></div>
    {status_badge}
    <div class="row"><span>Customer</span><span>{customer.name if customer else '-'}</span></div>
    <div class="row"><span>Date</span><span>{inv.created_at.strftime('%Y-%m-%d %H:%M')}</span></div>
    <div class="row"><span>Payment</span><span>{inv.payment_method}</span></div>
    <div class="line"></div>
    {rows}
    <div class="line"></div>
    <div class="row"><span>Subtotal</span><span>{float(inv.subtotal):.2f}</span></div>
    <div class="row"><span>Discount</span><span>{float(inv.discount):.2f}</span></div>
    <div class="row total"><span>Total</span><span>{float(inv.total):.2f}</span></div>
    <button class="btn" onclick="window.print()">&#128424; Print Receipt</button>
    <button class="btn btn-back" onclick="window.location.href='/pos'">&#8592; Back to POS</button>
</div>
</body>
</html>"""


@router.get("/pos-sw.js")
def pos_service_worker():
    from fastapi.responses import Response
    js = r"""
const CACHE = 'pos-v1';
const PRECACHE = ['/pos', '/products-cache', '/customers', '/static/Logo.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(PRECACHE)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;
  const cacheable = ['/pos', '/products-cache', '/customers', '/static/Logo.png'];
  if (!cacheable.some(p => url.pathname === p || url.pathname.startsWith(p))) return;
  e.respondWith(
    fetch(e.request).then(res => {
      if (res.ok) {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
      }
      return res;
    }).catch(() => caches.match(e.request))
  );
});
"""
    return Response(content=js, media_type="application/javascript",
                    headers={"Service-Worker-Allowed": "/"})


@router.get("/pos", response_class=HTMLResponse)
def pos_ui(current_user: User = Depends(require_permission("page_pos"))):
    return """<!DOCTYPE html>
<html>
<head>
<script src="/static/theme-init.js"></script>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>POS — Thunder ERP</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root {
    --bg:#060810;--surface:#0a0d18;--card:#0f1424;--card2:#151c30;
    --border:rgba(255,255,255,0.06);--border2:rgba(255,255,255,0.11);
    --green:#00ff9d;--blue:#4d9fff;--danger:#ff4d6d;--warn:#ffb547;
    --text:#f0f4ff;--sub:#8899bb;--muted:#445066;
    --sans:'Outfit',sans-serif;--mono:'JetBrains Mono',monospace;--r:13px;
}
body.light{
    --bg:#f4f5ef;--surface:#f1f3eb;--card:#eceee6;--card2:#e4e6de;
    --border:rgba(0,0,0,0.08);--border2:rgba(0,0,0,0.14);
    --green:#0f8a43;
    --text:#1a1e14;--sub:#4a5040;--muted:#7b816f;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
body{font-family:var(--sans);background:var(--bg);color:var(--text);height:100vh;overflow:hidden;display:grid;grid-template-columns:1fr 430px;grid-template-rows:auto 58px 1fr;font-size:14px;}
body>*{position:relative;z-index:1;}

/* TOPBAR */
#topbar{grid-column:1/-1;grid-row:2;display:flex;align-items:center;gap:10px;padding:0 18px;background:rgba(10,13,24,.9);backdrop-filter:blur(20px);border-bottom:1px solid var(--border);overflow:visible;z-index:100;}
body.light #topbar{background:rgba(244,245,239,.92);}
.logo{font-size:17px;font-weight:900;background:linear-gradient(135deg,var(--green),var(--blue));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;margin-right:6px;text-decoration:none;display:flex;align-items:center;gap:8px;}
.tb-field{display:flex;align-items:center;gap:9px;background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:0 13px;transition:border-color .2s;}
.tb-field:focus-within{border-color:rgba(0,255,157,.3);}
.tb-field svg{color:var(--muted);flex-shrink:0;}
.tb-field input{background:transparent;border:none;outline:none;color:var(--text);font-family:var(--sans);font-size:14px;font-weight:500;padding:11px 0;width:100%;}
.tb-field input::placeholder{color:var(--muted);}
#barcode_wrap{flex:0 0 200px;}
#barcode_wrap input{font-family:var(--mono);font-size:13px;}
#search_wrap{flex:1;}
.tb-spacer{flex:1;}
.mode-btn{display:flex;align-items:center;justify-content:center;width:38px;height:38px;border-radius:10px;border:1px solid var(--border);background:var(--card);color:var(--sub);font-size:16px;cursor:pointer;transition:all .2s;font-family:var(--sans);}
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
.account-item.danger:hover{color:var(--danger);}
.logout-btn{background:transparent;border:1px solid var(--border);color:var(--muted);font-family:var(--sans);font-size:12px;font-weight:500;padding:8px 16px;border-radius:8px;cursor:pointer;transition:all .2s;letter-spacing:.3px;}
.logout-btn:hover{border-color:#c97a7a;color:#c97a7a;}

/* CUSTOMER */
#cust_wrap{flex:0 0 240px;position:relative;}
#cust_input_box{display:flex;align-items:center;gap:9px;background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:0 13px;transition:border-color .2s;cursor:text;}
#cust_input_box:focus-within{border-color:rgba(0,255,157,.3);}
#cust_input_box svg{color:var(--muted);flex-shrink:0;}
#cust_search{background:transparent;border:none;outline:none;color:var(--text);font-family:var(--sans);font-size:14px;font-weight:500;padding:11px 0;width:100%;}
#cust_search::placeholder{color:var(--muted);}
#cust_dropdown{display:none;position:fixed;width:260px;max-height:280px;overflow-y:auto;background:#0d1220;border:1px solid rgba(255,255,255,.12);border-radius:12px;z-index:99999;box-shadow:0 24px 60px rgba(0,0,0,.95);}
.cust-row{display:flex;align-items:center;gap:10px;padding:11px 14px;font-size:13px;font-weight:500;color:var(--sub);cursor:pointer;border-bottom:1px solid rgba(255,255,255,.05);transition:all .1s;}
.cust-row:last-child{border-bottom:none;}
.cust-row:hover{background:rgba(0,255,157,.08);color:var(--green);}
#selected_badge{display:none;align-items:center;gap:8px;background:rgba(0,255,157,.08);border:1px solid rgba(0,255,157,.25);color:var(--green);font-size:13px;font-weight:700;padding:8px 13px;border-radius:var(--r);white-space:nowrap;flex-shrink:0;}
#selected_badge.show{display:flex;}
#xcust{background:none;border:none;color:var(--green);opacity:.5;font-size:17px;cursor:pointer;padding:0;transition:all .15s;}
#xcust:hover{opacity:1;transform:rotate(90deg);}

/* LEFT */
#left{overflow-y:auto;padding:18px;display:flex;flex-direction:column;gap:14px;scrollbar-width:thin;scrollbar-color:var(--border2) transparent;}
.panel-title{font-size:11px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--muted);display:flex;align-items:center;gap:8px;}
.panel-title::after{content:'';flex:1;height:1px;background:linear-gradient(90deg,var(--border2),transparent);}
#browser_head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;}
#browser_meta{display:flex;align-items:center;gap:10px;flex-wrap:wrap;}
#browser_hint{font-size:12px;color:var(--sub);}
#browser_back{display:none;align-items:center;gap:6px;border:1px solid var(--border2);background:var(--card);color:var(--sub);border-radius:10px;padding:8px 12px;font-family:var(--sans);font-size:12px;font-weight:700;cursor:pointer;transition:all .2s;}
#browser_back.show{display:inline-flex;}
#browser_back:hover{border-color:rgba(0,255,157,.35);color:var(--green);}
#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(145px,1fr));gap:10px;}
.category-tile{background:linear-gradient(180deg,rgba(77,159,255,.12),rgba(77,159,255,.04));border:1px solid rgba(77,159,255,.18);border-radius:16px;padding:18px 16px;cursor:pointer;display:flex;flex-direction:column;gap:10px;min-height:132px;transition:border-color .2s,box-shadow .2s,transform .15s;}
.category-tile:hover{border-color:rgba(77,159,255,.45);box-shadow:0 10px 30px rgba(77,159,255,.16);transform:translateY(-2px);}
.category-kicker{font-size:11px;letter-spacing:1.4px;text-transform:uppercase;color:var(--blue);font-weight:800;}
.category-name{font-size:20px;line-height:1.1;font-weight:800;color:var(--text);}
.category-meta{margin-top:auto;font-size:12px;color:var(--sub);display:flex;align-items:center;justify-content:space-between;gap:8px;}
.product{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:16px 13px 13px;cursor:pointer;display:flex;flex-direction:column;gap:5px;position:relative;overflow:hidden;transition:border-color .2s,box-shadow .2s,transform .15s;}
.product:hover{border-color:rgba(0,255,157,.5);box-shadow:0 8px 30px rgba(0,255,157,.12);transform:translateY(-3px);}
.product:active{transform:translateY(-1px);}
.p-name{font-size:13px;font-weight:700;color:var(--text);line-height:1.35;}
.p-sku{font-family:var(--mono);font-size:10px;color:var(--muted);}
.p-price{font-family:var(--mono);font-size:16px;font-weight:700;color:var(--green);margin-top:4px;}
.p-stock{font-size:10px;margin-top:2px;}
@keyframes ripple{0%{transform:scale(0);opacity:.5}100%{transform:scale(4);opacity:0}}
.ripple-dot{position:absolute;width:40px;height:40px;border-radius:50%;background:radial-gradient(circle,rgba(0,255,157,.5),transparent 70%);transform:scale(0);pointer-events:none;animation:ripple .5s ease-out forwards;}
@keyframes cardFlash{0%{background:rgba(0,255,157,.2);border-color:var(--green);}100%{background:var(--card);border-color:var(--border);}}
.flash{animation:cardFlash .45s ease;}
#no_results{display:none;flex-direction:column;align-items:center;gap:12px;padding:70px 0;color:var(--muted);font-size:14px;}

/* RIGHT */
#right{background:rgba(10,13,24,.9);backdrop-filter:blur(20px);border-left:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;}
body.light #right{background:rgba(244,245,239,.92);}
#cart_header{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--border);flex-shrink:0;gap:8px;}
#cart_count{background:linear-gradient(135deg,#7ecb6f,#d4a256);color:#000;font-size:11px;font-weight:800;padding:2px 7px;border-radius:20px;display:none;}
#clear_btn{display:flex;align-items:center;gap:6px;background:rgba(255,77,109,.08);border:1px solid rgba(255,77,109,.2);color:var(--danger);font-family:var(--sans);font-size:12px;font-weight:700;padding:7px 13px;border-radius:9px;cursor:pointer;transition:all .2s;}
#clear_btn:hover{background:rgba(255,77,109,.18);border-color:var(--danger);}

/* CART BODY */
#cart_scroll{flex:1;overflow-y:auto;padding:12px 16px;display:flex;flex-direction:column;gap:8px;scrollbar-width:thin;scrollbar-color:var(--border2) transparent;}
#cart_empty{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;color:var(--muted);font-size:14px;padding:40px 0;}
#cart_empty svg{opacity:.25;animation:emptyFloat 3s ease-in-out infinite;}
@keyframes emptyFloat{0%,100%{transform:translateY(0)}50%{transform:translateY(-8px)}}
.cart-item{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:11px 13px;display:grid;grid-template-columns:1fr auto;grid-template-rows:auto auto;column-gap:10px;row-gap:7px;align-items:center;animation:itemIn .2s ease;}
@keyframes itemIn{from{opacity:0;transform:translateX(12px)}to{opacity:1;transform:translateX(0)}}
.ci-name{font-size:13px;font-weight:700;color:var(--text);}
.ci-subtotal{font-family:var(--mono);font-size:14px;font-weight:700;color:var(--green);text-align:right;}
.ci-controls{display:flex;align-items:center;gap:5px;grid-column:1/-1;}
.qty-btn{width:28px;height:28px;display:flex;align-items:center;justify-content:center;background:var(--card2);border:1px solid var(--border2);border-radius:8px;color:var(--text);font-size:18px;font-weight:700;font-family:var(--sans);cursor:pointer;transition:all .15s;flex-shrink:0;}
.qty-btn:hover{border-color:var(--green);color:var(--green);transform:scale(1.1);}
.qty-input{width:38px;height:28px;background:var(--card2);border:1px solid var(--border2);border-radius:8px;color:var(--text);font-family:var(--mono);font-size:13px;text-align:center;outline:none;}
.qty-input:focus{border-color:var(--blue);}
.ci-unit{font-family:var(--mono);font-size:11px;color:var(--muted);margin-left:3px;flex:1;}
.rm-btn{width:28px;height:28px;display:flex;align-items:center;justify-content:center;background:transparent;border:1px solid transparent;border-radius:8px;color:var(--muted);font-size:13px;cursor:pointer;transition:all .15s;margin-left:auto;}
.rm-btn:hover{border-color:var(--danger);color:var(--danger);background:rgba(255,77,109,.08);}
.ci-unit-editable{display:inline-flex;align-items:center;gap:4px;}
.ci-price-input{width:64px;font-family:var(--mono);background:var(--surface-raised,rgba(255,255,255,.04));border:1px solid var(--border);border-radius:6px;padding:2px 6px;color:inherit;font-size:12px;text-align:right;}
.ci-price-input:focus{outline:2px solid var(--accent,var(--green));outline-offset:1px;}
.ci-unit-edited .ci-price-input{border-color:var(--warning,#a67418);}
.ci-was{font-size:10px;color:var(--muted);text-decoration:line-through;}
.ci-reset-price{background:none;border:none;color:var(--muted);cursor:pointer;font-size:14px;padding:0 2px;}
.ci-reset-price:hover{color:var(--accent,var(--green));}

/* TOTALS */
#totals{border-top:1px solid var(--border);padding:12px 16px;display:flex;flex-direction:column;gap:8px;flex-shrink:0;}
.inputs-row{display:grid;grid-template-columns:1fr 1fr;gap:8px;}
.fld{display:flex;flex-direction:column;gap:4px;}
.fld-label{font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);}
.fld-input{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:10px 12px;color:var(--text);font-family:var(--mono);font-size:14px;text-align:center;outline:none;width:100%;transition:all .18s;}
.fld-input:focus{border-color:rgba(77,159,255,.5);}
.total-row{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;background:var(--card);border:1px solid var(--border);border-radius:var(--r);}
.total-label{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:var(--sub);}
#total{font-family:var(--mono);font-size:28px;font-weight:700;color:var(--green);}
.change-row{display:flex;align-items:center;justify-content:space-between;padding:8px 14px;background:var(--card);border:1px solid var(--border);border-radius:var(--r);}
.change-label{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:var(--sub);}
#change{font-family:var(--mono);font-size:15px;font-weight:500;color:var(--muted);}

/* PAYMENT TOGGLE */
.pay-toggle{display:grid;grid-template-columns:1fr 1fr;gap:8px;}
.pay-btn{padding:10px;border-radius:10px;border:2px solid var(--border2);background:transparent;color:var(--sub);font-family:var(--sans);font-size:13px;font-weight:700;cursor:pointer;transition:all .2s;}
.pay-btn.active-cash{border-color:var(--green);background:rgba(0,255,157,.1);color:var(--green);}
.pay-btn.active-visa{border-color:var(--blue);background:rgba(77,159,255,.1);color:var(--blue);}

/* CHECKOUT BUTTONS */
#checkout_btn{display:flex;align-items:center;justify-content:center;gap:9px;width:100%;padding:15px;background:linear-gradient(135deg,var(--green),#00d4ff);border:none;border-radius:var(--r);color:#021a10;font-family:var(--sans);font-size:15px;font-weight:900;cursor:pointer;transition:transform .15s,box-shadow .2s;box-shadow:0 4px 20px rgba(0,255,157,.25);}
#checkout_btn:hover:not(:disabled){transform:translateY(-2px);box-shadow:0 8px 30px rgba(0,255,157,.4);}
#checkout_btn:disabled{opacity:.35;cursor:not-allowed;}
#settle_btn{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;padding:12px;background:rgba(255,181,71,.08);border:2px solid rgba(255,181,71,.4);color:var(--warn);font-family:var(--sans);font-size:13px;font-weight:700;border-radius:var(--r);cursor:pointer;transition:all .2s;}
#settle_btn:hover:not(:disabled){background:rgba(255,181,71,.15);border-color:var(--warn);}
#settle_btn:disabled{opacity:.35;cursor:not-allowed;}

/* UNPAID PANEL */
#unpaid-panel{display:none;flex-direction:column;flex:1;overflow-y:auto;padding:14px 16px;gap:8px;scrollbar-width:thin;scrollbar-color:var(--border2) transparent;}
.unpaid-card{background:var(--card);border:1px solid rgba(255,181,71,.2);border-radius:var(--r);padding:14px;cursor:pointer;transition:border-color .2s;}
.unpaid-card:hover{border-color:rgba(255,181,71,.5);}
.unpaid-num{font-family:var(--mono);font-size:11px;color:var(--warn);}
.unpaid-name{font-weight:700;font-size:14px;margin:4px 0 2px;}
.unpaid-date{font-size:11px;color:var(--muted);margin-bottom:8px;}
.unpaid-total{font-family:var(--mono);font-size:20px;font-weight:700;color:var(--warn);}
.collect-row{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:10px;}
.collect-btn{padding:8px;border-radius:8px;font-family:var(--sans);font-size:12px;font-weight:700;cursor:pointer;transition:all .15s;}
.collect-cash{border:1px solid var(--green);background:rgba(0,255,157,.08);color:var(--green);}
.collect-cash:hover{background:rgba(0,255,157,.18);}
.collect-visa{border:1px solid var(--blue);background:rgba(77,159,255,.08);color:var(--blue);}
.collect-visa:hover{background:rgba(77,159,255,.18);}

/* INVOICE DETAIL MODAL */
.modal-bg{position:fixed;inset:0;z-index:500;background:rgba(0,0,0,.8);backdrop-filter:blur(4px);display:none;align-items:center;justify-content:center;}
.modal-bg.open{display:flex;}
.inv-modal{background:var(--card);border:1px solid var(--border2);border-radius:16px;padding:24px;width:380px;max-width:95vw;max-height:85vh;overflow-y:auto;animation:modalIn .2s ease;}
@keyframes modalIn{from{opacity:0;transform:scale(.95)}to{opacity:1;transform:scale(1)}}
.inv-modal-header{text-align:center;margin-bottom:14px;}
.inv-modal-num{font-family:var(--mono);font-size:12px;color:var(--muted);}
.inv-modal-title{font-size:18px;font-weight:800;color:var(--green);margin-bottom:4px;}
.inv-divider{border:none;border-top:1px dashed var(--border2);margin:12px 0;}
.inv-row{display:flex;justify-content:space-between;font-size:13px;padding:4px 0;}
.inv-row .label{color:var(--muted);}
.inv-row .val{font-weight:600;}
.inv-item-row{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border);font-size:13px;}
.inv-item-row:last-child{border-bottom:none;}
.inv-item-name{color:var(--text);font-weight:600;}
.inv-item-meta{font-size:11px;color:var(--muted);margin-top:2px;}
.inv-item-total{font-family:var(--mono);color:var(--green);font-weight:700;}
.inv-total-row{display:flex;justify-content:space-between;font-size:18px;font-weight:800;padding:10px 0 0;}
.inv-modal-actions{display:flex;gap:8px;margin-top:14px;}
.inv-print-btn{flex:1;padding:12px;background:linear-gradient(135deg,var(--green),#00d4ff);border:none;border-radius:10px;color:#021a10;font-family:var(--sans);font-size:13px;font-weight:800;cursor:pointer;}
.inv-close-btn{flex:1;padding:12px;background:var(--card2);border:1px solid var(--border2);border-radius:10px;color:var(--sub);font-family:var(--sans);font-size:13px;font-weight:700;cursor:pointer;}
.inv-close-btn:hover{border-color:var(--danger);color:var(--danger);}
.unpaid-badge{background:rgba(255,181,71,.12);border:1px solid rgba(255,181,71,.3);color:var(--warn);border-radius:8px;padding:8px;text-align:center;font-weight:700;font-size:12px;margin-bottom:8px;}
body.light #cust_dropdown{background:#eceee6;border-color:rgba(0,0,0,.12);box-shadow:0 24px 60px rgba(0,0,0,.18);}
body.light .toast{background:var(--card);}

/* NOTIFICATION BADGE */
.notif-badge{display:none;background:var(--danger);color:white;font-size:10px;font-weight:800;padding:1px 6px;border-radius:20px;margin-left:4px;vertical-align:middle;}
.notif-badge.show{display:inline;}

/* TOAST */
.toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%) translateY(16px);background:#0f1424;border:1px solid var(--border2);border-radius:var(--r);padding:12px 18px;display:flex;align-items:center;gap:12px;font-size:13px;font-weight:600;color:var(--text);box-shadow:0 20px 50px rgba(0,0,0,.5);opacity:0;pointer-events:none;transition:opacity .25s,transform .25s;z-index:999;}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0);pointer-events:auto;}
.toast-undo{background:linear-gradient(135deg,#7ecb6f,#d4a256);color:#021a10;border:none;border-radius:7px;padding:5px 11px;font-family:var(--sans);font-size:12px;font-weight:800;cursor:pointer;}
::-webkit-scrollbar{width:4px;}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:4px;}

@media print{
    .inv-modal-actions{display:none;}
    .modal-bg{position:static;background:white;display:block;}
    .inv-modal{box-shadow:none;border:none;background:white;color:black;max-height:none;}
    .inv-modal-title{color:black;}
    .inv-row .label,.inv-total-row{color:black;}
    .inv-item-name{color:black;}
    .inv-item-total{color:black;}
    .app-nav,#right,#left,#topbar{display:none!important;}
}
</style>
    <script src="/static/auth-guard.js"></script>
</head>
<body>
""" + render_app_header(current_user, "page_pos") + """

<!-- TOPBAR -->
<div id="topbar">
    <a href="/home" class="logo">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
            <polygon points="13,2 4,14 11,14 11,22 20,10 13,10" fill="#f59e0b"/>
        </svg>
        Thunder ERP
    </a>

    <div class="tb-field" id="barcode_wrap">
        <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
            <path d="M3 5v14M7 5v14M11 5v14M15 5v9M19 5v9M15 17v2M19 17v2"/>
        </svg>
        <input id="barcode" placeholder="Scan barcode / SKU…" autocomplete="off">
    </div>

    <div class="tb-field" id="search_wrap">
        <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24">
            <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
        </svg>
        <input id="search" placeholder="Search products…" autocomplete="off">
    </div>

    <span class="tb-spacer"></span>

    <div id="cust_wrap">
        <div id="cust_input_box">
            <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                <circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 3.6-7 8-7s8 3 8 7"/>
            </svg>
            <input id="cust_search" placeholder="Select customer…" autocomplete="off">
        </div>
        <div id="cust_dropdown"></div>
    </div>

    <div id="selected_badge">
        <svg width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
            <circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 3.6-7 8-7s8 3 8 7"/>
        </svg>
        <span id="sel_name"></span>
        <button id="xcust" onclick="clearCustomer()">×</button>
    </div>

    <div class="topbar-right">
        <div id="offline-indicator" style="display:none;align-items:center;gap:6px;background:rgba(255,181,71,.12);border:1px solid rgba(255,181,71,.35);color:#ffb547;font-size:12px;font-weight:700;padding:7px 12px;border-radius:9px;">📴 Offline</div>
        <div id="offline-badge" style="display:none;background:rgba(255,181,71,.12);border:1px solid rgba(255,181,71,.35);color:#ffb547;font-size:12px;font-weight:700;padding:7px 12px;border-radius:9px;cursor:pointer;" onclick="showPendingQueue()" title="Pending offline sales — click to sync"></div>
        <a href="/refunds/" id="refunds-link" style="display:flex;align-items:center;gap:6px;background:rgba(255,77,109,.08);border:1px solid rgba(255,77,109,.25);color:#ff4d6d;font-family:var(--sans);font-size:12px;font-weight:700;padding:8px 14px;border-radius:9px;cursor:pointer;text-decoration:none;transition:all .2s;" onmouseover="this.style.background='rgba(255,77,109,.18)'" onmouseout="this.style.background='rgba(255,77,109,.08)'">↩ Refunds</a>
    </div>
</div>

<!-- LEFT: PRODUCTS -->
<div id="left">
    <div id="browser_head">
        <div id="browser_meta">
            <div class="panel-title" id="browser_title">Categories</div>
            <div id="browser_hint">Tap a category to open products</div>
        </div>
        <button id="browser_back" type="button" onclick="showCategoriesView()">
            <svg width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.3" viewBox="0 0 24 24">
                <path d="M15 18l-6-6 6-6"/>
            </svg>
            All Categories
        </button>
    </div>
    <div id="grid"></div>
    <div id="no_results">
        <svg width="40" height="40" fill="none" stroke="currentColor" stroke-width="1.2" viewBox="0 0 24 24">
            <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
        </svg>
        No products found
    </div>
</div>

<!-- RIGHT: CART + UNPAID -->
<div id="right">

    <!-- HEADER TABS -->
    <div id="cart_header">
        <div style="display:flex;gap:6px;align-items:center;">
            <button onclick="switchPosTab('cart')" id="tab-cart"
                style="padding:6px 14px;border-radius:8px;border:none;font-family:var(--sans);font-size:12px;font-weight:700;cursor:pointer;background:rgba(0,255,157,.15);color:var(--green);">
                🛒 Cart <span id="cart_count"></span>
            </button>
            <button onclick="switchPosTab('unpaid')" id="tab-unpaid"
                style="padding:6px 14px;border-radius:8px;border:1px solid var(--border2);font-family:var(--sans);font-size:12px;font-weight:700;cursor:pointer;background:transparent;color:var(--muted);display:flex;align-items:center;gap:4px;">
                ⏳ Unpaid <span class="notif-badge" id="unpaid-badge"></span>
            </button>
        </div>
        <button id="clear_btn" onclick="clearCart()">
            <svg width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24">
                <polyline points="3 6 5 6 21 6"/>
                <path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/>
            </svg>
            Clear
        </button>
    </div>

    <!-- CART BODY -->
    <div id="cart_scroll">
        <div id="cart_empty">
            <svg width="44" height="44" fill="none" stroke="currentColor" stroke-width="1.2" viewBox="0 0 24 24">
                <path d="M6 2L3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z"/>
                <line x1="3" y1="6" x2="21" y2="6"/>
                <path d="M16 10a4 4 0 0 1-8 0"/>
            </svg>
            Cart is empty
        </div>
        <div id="cart"></div>
    </div>

    <!-- TOTALS -->
    <div id="totals">
        <div class="inputs-row">
            <div class="fld">
                <span class="fld-label">Discount %</span>
                <input id="discount" class="fld-input" type="number" placeholder="0" min="0" max="100">
            </div>
            <div class="fld">
                <span class="fld-label">Cash Received</span>
                <input id="cash" class="fld-input" type="number" placeholder="0.00" min="0">
            </div>
        </div>

        <div class="total-row">
            <span class="total-label">Total</span>
            <span id="total">0.00</span>
        </div>

        <div class="change-row">
            <span class="change-label">Change</span>
            <span id="change">—</span>
        </div>

        <div class="pay-toggle">
            <button class="pay-btn active-cash" id="pay-cash" onclick="setPayMethod('cash')">💵 Cash</button>
            <button class="pay-btn" id="pay-visa" onclick="setPayMethod('visa')">💳 Visa</button>
        </div>

        <button id="checkout_btn" onclick="checkout(false)">
            <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24">
                <polyline points="20 6 9 17 4 12"/>
            </svg>
            Confirm Order
        </button>

        <button id="settle_btn" onclick="checkout(true)">
            ⏳ Settle Later (requires customer)
        </button>
    </div>

    <!-- UNPAID PANEL -->
    <div id="unpaid-panel">
        <div style="font-size:10px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--muted);margin-bottom:4px;">Unpaid Invoices</div>
        <div id="unpaid-list">
            <div style="color:var(--muted);font-size:13px;text-align:center;padding:30px 0">Loading...</div>
        </div>
    </div>
</div>

<!-- INVOICE DETAIL MODAL -->
<div class="modal-bg" id="inv-modal">
    <div class="inv-modal" id="inv-modal-content">
        <div class="inv-modal-header">
            <div style="font-size:16px;font-weight:800;color:var(--green);margin-bottom:2px;">🌿 Habiba Organic Farm</div>
            <div class="inv-modal-num" id="modal-inv-num">—</div>
        </div>
        <hr class="inv-divider">
        <div class="unpaid-badge">⏳ UNPAID — Settle Later</div>
        <div id="modal-meta"></div>
        <hr class="inv-divider">
        <div id="modal-items"></div>
        <hr class="inv-divider">
        <div id="modal-totals"></div>
        <div class="inv-modal-actions">
            <button class="inv-print-btn" onclick="printInvoice()">🖨 Print Receipt</button>
            <button class="inv-close-btn" onclick="closeInvModal()">Close</button>
        </div>
    </div>
</div>

<!-- TOAST -->
<div class="toast" id="toast">
    <span id="toast_msg"></span>
    <button class="toast-undo" id="toast_undo" style="display:none" onclick="undoCart()">UNDO</button>
</div>

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
  function hasPermission(permission, u){
      const role = u ? (u.role || "") : "";
      const perms = new Set(
          u
              ? (typeof u.permissions === "string"
                  ? u.permissions.split(",").map(v => v.trim()).filter(Boolean)
                  : (u.permissions || []))
              : []
      );
      return role === "admin" || perms.has(permission);
  }
  initializeColorMode();
let customers=[], products=[], cart=[], lastCart=[];
let categories=[], selectedCategory=null;
let searchMode=false;
let selectedCustomer = null;
let userCanEditPrice = false;
let selectedPayMethod = "cash";
let toastTimer = null;
let currentInvoiceData = null;
let barcodeLookupTimer = null;
let lastBarcodeInputAt = 0;
let barcodeBurstCount = 0;
let lastProcessedBarcode = "";
let lastProcessedBarcodeAt = 0;
initUser().then(u => {
    if(!u) return;
    if(!hasPermission("action_pos_discount", u)){
        document.getElementById("discount").disabled = true;
        document.getElementById("discount").value = "0";
    }
    if(!hasPermission("action_pos_settle_later", u)){
        document.getElementById("settle_btn").style.display = "none";
    }
    if(!hasPermission("action_pos_refund", u)){
        const refundsLink = document.getElementById("refunds-link");
        if(refundsLink) refundsLink.style.display = "none";
    }
    userCanEditPrice = hasPermission("action_pos_edit_price", u);
    if(userCanEditPrice) drawCart();
});

/* ── OFFLINE / INDEXEDDB ── */
function openDB(){
    return new Promise((res,rej)=>{
        const r = indexedDB.open("pos-offline",1);
        r.onupgradeneeded = e => e.target.result.createObjectStore("queue",{keyPath:"id",autoIncrement:true});
        r.onsuccess = e => res(e.target.result);
        r.onerror   = e => rej(e.target.error);
    });
}
async function queueOfflineSale(payload){
    const db = await openDB();
    return new Promise((res,rej)=>{
        const tx = db.transaction("queue","readwrite");
        tx.objectStore("queue").add(payload);
        tx.oncomplete = res; tx.onerror = e => rej(e.target.error);
    });
}
async function getPendingQueue(){
    const db = await openDB();
    return new Promise((res,rej)=>{
        const tx = db.transaction("queue","readonly");
        const req = tx.objectStore("queue").getAll();
        req.onsuccess = e => res(e.target.result);
        req.onerror   = e => rej(e.target.error);
    });
}
async function removeFromQueue(id){
    const db = await openDB();
    return new Promise((res,rej)=>{
        const tx = db.transaction("queue","readwrite");
        tx.objectStore("queue").delete(id);
        tx.oncomplete = res; tx.onerror = e => rej(e.target.error);
    });
}
async function updateOfflineBadge(){
    try {
        const q = await getPendingQueue();
        const badge = document.getElementById("offline-badge");
        if(q.length>0){
            badge.textContent = `📴 ${q.length} queued`;
            badge.style.display = "";
        } else {
            badge.style.display = "none";
        }
    } catch(e){}
}
async function syncOfflineQueue(){
    if(!navigator.onLine) return;
    let q;
    try { q = await getPendingQueue(); } catch(e){ return; }
    if(!q.length) return;
    let synced=0, failed=0;
    for(const sale of q){
        try {
            const res = await fetch("/invoice",{
                method:"POST",
                headers:{"Content-Type":"application/json"},
                body: JSON.stringify({
                    customer_id:      sale.customer_id,
                    items:            sale.items,
                    discount_percent: sale.discount_percent,
                    notes:            sale.notes||"",
                    payment_method:   sale.payment_method,
                    settle_later:     sale.settle_later||false,
                }),
            });
            if(res.ok){ await removeFromQueue(sale.id); synced++; }
            else { failed++; }
        } catch(e){ break; }
    }
    if(synced>0) showToast(`✅ Synced ${synced} offline sale${synced>1?"s":""}`);
    if(failed>0) showToast(`⚠️ ${failed} sale${failed>1?"s":""} could not sync`);
    updateOfflineBadge();
}
async function showPendingQueue(){
    const q = await getPendingQueue();
    if(!q.length){ showToast("No pending offline sales"); return; }
    if(navigator.onLine){
        showToast(`Syncing ${q.length} offline sale${q.length>1?"s":""}…`, false);
        await syncOfflineQueue();
    } else {
        showToast(`📴 ${q.length} sale${q.length>1?"s":""} waiting for connection`);
    }
}
window.addEventListener("online",()=>{
    const ind = document.getElementById("offline-indicator");
    if(ind) ind.style.display = "none";
    syncOfflineQueue();
});
window.addEventListener("offline",()=>{
    const ind = document.getElementById("offline-indicator");
    if(ind) ind.style.display = "flex";
});

/* ── INIT ── */
async function load(){
    if(!navigator.onLine){
        const ind = document.getElementById("offline-indicator");
        if(ind) ind.style.display = "flex";
    }
    try {
        customers = await (await fetch("/customers")).json();
        products  = await (await fetch("/products-cache")).json();
        buildCategories(products);
        showCategoriesView();
        checkUnpaidCount();
    } catch(e){
        console.error("Load error:", e);
        if(!navigator.onLine) showToast("📴 Working offline — product list may be limited", false);
    }
    updateOfflineBadge();
    if(navigator.onLine) syncOfflineQueue();
}

/* ── CHECK UNPAID COUNT (for notification badge) ── */
async function checkUnpaidCount(){
    try {
        let data = await (await fetch("/unpaid-invoices")).json();
        let badge = document.getElementById("unpaid-badge");
        if(data.length > 0){
            badge.innerText = data.length;
            badge.classList.add("show");
        } else {
            badge.classList.remove("show");
        }
    } catch(e){}
}

/* ── POS TABS ── */
function switchPosTab(tab){
    let isCart = tab === "cart";

    let cartTab   = document.getElementById("tab-cart");
    let unpaidTab = document.getElementById("tab-unpaid");

    cartTab.style.background = isCart ? "rgba(0,255,157,.15)" : "transparent";
    cartTab.style.color      = isCart ? "var(--green)" : "var(--muted)";
    cartTab.style.border     = isCart ? "none" : "1px solid var(--border2)";

    unpaidTab.style.background = !isCart ? "rgba(255,181,71,.15)" : "transparent";
    unpaidTab.style.color      = !isCart ? "var(--warn)" : "var(--muted)";
    unpaidTab.style.border     = !isCart ? "none" : "1px solid var(--border2)";

    document.getElementById("cart_scroll").style.display  = isCart ? "" : "none";
    document.getElementById("totals").style.display       = isCart ? "" : "none";
    document.getElementById("unpaid-panel").style.display = isCart ? "none" : "flex";
    document.getElementById("clear_btn").style.display    = isCart ? "" : "none";

    if(!isCart) loadUnpaidInvoices();
}

/* ── PAYMENT METHOD ── */
function setPayMethod(method){
    selectedPayMethod = method;
    document.getElementById("pay-cash").className = "pay-btn" + (method==="cash"?" active-cash":"");
    document.getElementById("pay-visa").className = "pay-btn" + (method==="visa"?" active-visa":"");
}

/* ── CUSTOMER SEARCH ── */
document.getElementById("cust_search").addEventListener("input", function(){
    let v  = this.value.toLowerCase().trim();
    let dd = document.getElementById("cust_dropdown");
    if(!v){ dd.style.display="none"; dd.innerHTML=""; return; }
    let f = customers.filter(c=>c.name.toLowerCase().includes(v)||(c.phone||"").includes(v)).slice(0,10);
    if(!f.length){ dd.style.display="none"; return; }
    let rect = this.getBoundingClientRect();
    dd.style.top=  (rect.bottom+4)+"px";
    dd.style.left=  rect.left+"px";
    dd.style.width= "260px";
    dd.innerHTML = f.map(c=>`
        <div class="cust-row" onclick="selectCustomer(${c.id},'${c.name.replace(/'/g,"\\'")}',${c.discount_pct || 0})">
            <svg width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                <circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 3.6-7 8-7s8 3 8 7"/>
            </svg>
            ${c.name}
        </div>`).join("");
    dd.style.display = "block";
});

function applyCustomerDiscount(discountPct){
    const discountInput = document.getElementById("discount");
    if(!discountInput || discountInput.disabled) return;
    discountInput.value = Number(discountPct || 0).toFixed(1);
    drawCart();
}

function selectCustomer(id, name, discountPct){
    const edited = cart.filter(c => c.original_price !== undefined && c.price !== c.original_price);
    if(edited.length > 0 && id !== null){
        const reset = confirm(
            `You've edited prices for ${edited.length} item(s). ` +
            `Switching to a named customer will apply their normal pricing. ` +
            `Click OK to reset prices to catalog, or Cancel to keep your edits.`
        );
        if(reset) cart.forEach(c => { c.price = c.original_price; });
    }
    selectedCustomer = id;
    document.getElementById("cust_search").value = name;
    document.getElementById("cust_dropdown").style.display = "none";
    document.getElementById("sel_name").innerText = name;
    document.getElementById("selected_badge").classList.add("show");
    document.getElementById("cust_wrap").style.display = "none";
    applyCustomerDiscount(discountPct);
}

function clearCustomer(){
    selectedCustomer = null;
    document.getElementById("selected_badge").classList.remove("show");
    document.getElementById("cust_wrap").style.display = "";
    document.getElementById("cust_search").value = "";
    applyCustomerDiscount(0);
}

document.addEventListener("click", e=>{
    if(!e.target.closest("#cust_wrap"))
        document.getElementById("cust_dropdown").style.display = "none";
});

/* ── BARCODE ── */
function normalizeBarcodeValue(value){
    return String(value || "")
        .normalize("NFKC")
        .replace(/[\u200B\uFEFF]/g, "")
        .replace(/\s+/g, "")
        .trim()
        .toLowerCase();
}

async function processBarcodeInput(){
    const input = document.getElementById("barcode");
    const rawValue = input.value;
    const normalized = normalizeBarcodeValue(rawValue);
    if(!normalized) return;

    const now = Date.now();
    if(lastProcessedBarcode === normalized && (now - lastProcessedBarcodeAt) < 350){
        input.value = "";
        return;
    }

    let data;
    try {
        const res = await fetch("/barcode-lookup?barcode=" + encodeURIComponent(rawValue));
        data = await res.json();
    } catch(e) {
        showToast("Barcode lookup failed. Please try again.");
        return;
    }

    if(data.found && data.product){
        const p = data.product;
        add(p.sku, p.name, p.price);
        let card = document.querySelector(`[data-sku="${p.sku}"]`);
        if(card){ card.classList.add("flash"); setTimeout(()=>card.classList.remove("flash"),450); }
        lastProcessedBarcode = normalized;
        lastProcessedBarcodeAt = now;
    } else {
        showToast(data.detail || `No product found for barcode '${normalized}'.`);
    }
    input.value = "";
}

document.getElementById("barcode").addEventListener("keydown", function(e){
    if(e.key !== "Enter" && e.key !== "Tab") return;
    e.preventDefault();
    clearTimeout(barcodeLookupTimer);
    processBarcodeInput();
});

document.getElementById("barcode").addEventListener("input", function(){
    const now = Date.now();
    barcodeBurstCount = (now - lastBarcodeInputAt) < 35 ? (barcodeBurstCount + 1) : 1;
    lastBarcodeInputAt = now;
    clearTimeout(barcodeLookupTimer);
    if(normalizeBarcodeValue(this.value).length >= 3 && barcodeBurstCount >= 4){
        barcodeLookupTimer = setTimeout(() => processBarcodeInput(), 60);
    }
});

/* ── PRODUCT GRID ── */
function normalizeCategoryName(value){
    return String(value || "").trim() || "Uncategorized";
}

function buildCategories(list){
    const counts = new Map();
    list.forEach(product => {
        const category = normalizeCategoryName(product.category);
        counts.set(category, (counts.get(category) || 0) + 1);
    });
    categories = Array.from(counts.entries())
        .map(([name, count]) => ({ name, count }))
        .sort((a, b) => a.name.localeCompare(b.name));
}

function setBrowserHeader(title, hint, showBack){
    document.getElementById("browser_title").innerText = title;
    document.getElementById("browser_hint").innerText = hint;
    document.getElementById("browser_back").classList.toggle("show", !!showBack);
}

function setEmptyState(message){
    const nr = document.getElementById("no_results");
    nr.innerHTML = `
        <svg width="40" height="40" fill="none" stroke="currentColor" stroke-width="1.2" viewBox="0 0 24 24">
            <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
        </svg>
        ${message}`;
    nr.style.display = "flex";
}

function renderProducts(list, emptyMessage="No products found"){
    let nr = document.getElementById("no_results");
    if(!list.length){
        document.getElementById("grid").innerHTML="";
        setEmptyState(emptyMessage);
        return;
    }
    nr.style.display="none";
    document.getElementById("grid").innerHTML = list.map(p=>`
        <div class="product" data-sku="${p.sku}"
             onclick="addWithRipple(event,'${p.sku}','${p.name.replace(/'/g,"\\'")}',${p.price})">
            <div class="p-name">${p.name}</div>
            <div class="p-sku">${p.sku}</div>
            <div class="p-price">${parseFloat(p.price).toFixed(2)}</div>
            <div class="p-stock" style="color:${p.stock>0?'#445066':'#ff4d6d'}">Stock: ${parseFloat(p.stock).toFixed(0)}</div>
        </div>`).join("");
}

function renderCategories(){
    const nr = document.getElementById("no_results");
    if(!categories.length){
        document.getElementById("grid").innerHTML = "";
        setEmptyState("No categories available");
        return;
    }
    nr.style.display = "none";
    document.getElementById("grid").innerHTML = categories.map(category => `
        <button class="category-tile" type="button" data-category="${category.name.replace(/"/g, "&quot;")}">
            <div class="category-kicker">Category</div>
            <div class="category-name">${category.name}</div>
            <div class="category-meta">${category.count} product${category.count === 1 ? "" : "s"}</div>
        </button>`).join("");
}

function showCategoriesView(){
    searchMode = false;
    selectedCategory = null;
    setBrowserHeader("Categories", "Tap a category to open products", false);
    renderCategories();
}

function openCategory(categoryName){
    searchMode = false;
    selectedCategory = categoryName;
    const list = products.filter(product => normalizeCategoryName(product.category) === categoryName);
    setBrowserHeader(categoryName, `${list.length} product${list.length === 1 ? "" : "s"} in this category`, true);
    renderProducts(list, "No products found in this category");
}

async function runProductSearch(query){
    searchMode = true;
    setBrowserHeader("Search Results", `Matches for "${query}"`, false);
    let data = await (await fetch("/search-products?q="+encodeURIComponent(query))).json();
    renderProducts(data, "No products found");
}

document.getElementById("grid").addEventListener("click", function(event){
    const categoryButton = event.target.closest(".category-tile");
    if(!categoryButton) return;
    const categoryName = categoryButton.dataset.category;
    if(!categoryName) return;
    openCategory(categoryName);
});

document.getElementById("search").addEventListener("input", async function(){
    let v = this.value.trim();
    if(!v){ showCategoriesView(); return; }
    await runProductSearch(v);
});

/* ── CART ── */
function addWithRipple(e, sku, name, price){
    let card=e.currentTarget, rect=card.getBoundingClientRect(), dot=document.createElement("div");
    dot.className="ripple-dot";
    dot.style.left=(e.clientX-rect.left-20)+"px";
    dot.style.top=(e.clientY-rect.top-20)+"px";
    card.appendChild(dot); setTimeout(()=>dot.remove(),500);
    add(sku,name,price);
}

function add(sku,name,price){
    let ex=cart.find(c=>c.sku===sku);
    ex?ex.qty++:cart.push({sku,name,price:parseFloat(price),original_price:parseFloat(price),qty:1});
    drawCart();
}
function inc(sku){ cart.find(c=>c.sku===sku).qty++; drawCart(); }
function dec(sku){ let i=cart.find(c=>c.sku===sku); if(--i.qty<=0) cart=cart.filter(c=>c.sku!==sku); drawCart(); }
function updateQty(sku,val){ cart.find(c=>c.sku===sku).qty=Math.max(1,parseFloat(val)||1); drawCart(); }
function removeItem(sku){ cart=cart.filter(c=>c.sku!==sku); drawCart(); }
function clearCart(){
    if(!cart.length) return;
    if(!confirm("Clear all items?")) return;
    lastCart=[...cart]; cart=[]; drawCart(); showToast("Cart cleared",true,true);
}
function undoCart(){ cart=[...lastCart]; drawCart(); hideToast(); }
async function logout(){ await fetch("/auth/logout", { method: "POST" }); window.location.href="/"; }

function canEditPrice(){
    return userCanEditPrice && selectedCustomer === null;
}

function updatePrice(sku, val){
    let item = cart.find(c => c.sku === sku);
    if(!item) return;
    let newPrice = parseFloat(val);
    if(isNaN(newPrice) || newPrice < 0.01){
        showToast("Price must be at least 0.01");
        drawCart();
        return;
    }
    if(item.original_price && newPrice > item.original_price * 10){
        if(!confirm(`New price ${newPrice.toFixed(2)} is over 10× the catalog price (${item.original_price.toFixed(2)}). Continue?`)){
            drawCart();
            return;
        }
    }
    item.price = newPrice;
    drawCart();
}

function resetPrice(sku){
    let item = cart.find(c => c.sku === sku);
    if(!item) return;
    item.price = item.original_price;
    drawCart();
}

function drawCart(){
    let empty=document.getElementById("cart_empty"), cartEl=document.getElementById("cart"), countEl=document.getElementById("cart_count"), total=0;
    if(!cart.length){ cartEl.innerHTML=""; empty.style.display="flex"; countEl.style.display="none"; }
    else { empty.style.display="none"; countEl.style.display=""; countEl.innerText=cart.reduce((s,c)=>s+c.qty,0); }
    cartEl.innerHTML=cart.map(c=>{ let t=c.qty*c.price; total+=t;
        const edited = c.original_price !== undefined && c.price !== c.original_price;
        const unitEl = canEditPrice()
            ? `<span class="ci-unit ci-unit-editable${edited?' ci-unit-edited':''}">× <input class="ci-price-input" type="number" step="0.01" min="0.01" value="${c.price.toFixed(2)}" data-sku="${c.sku}" onchange="updatePrice('${c.sku}',this.value)" onfocus="this.select()"/>${edited?`<span class="ci-was">was ${c.original_price.toFixed(2)}</span><button class="ci-reset-price" onclick="resetPrice('${c.sku}')" title="Reset to catalog price">↺</button>`:''}</span>`
            : `<span class="ci-unit">× ${c.price.toFixed(2)}</span>`;
        return `
        <div class="cart-item">
            <div class="ci-name">${c.name}</div>
            <div class="ci-subtotal">${t.toFixed(2)}</div>
            <div class="ci-controls">
                <button class="qty-btn" onclick="dec('${c.sku}')">−</button>
                <input class="qty-input" value="${c.qty}" onchange="updateQty('${c.sku}',this.value)">
                <button class="qty-btn" onclick="inc('${c.sku}')">+</button>
                ${unitEl}
                <button class="rm-btn" onclick="removeItem('${c.sku}')">✕</button>
            </div>
        </div>`;}).join("");

    let disc=parseFloat(document.getElementById("discount").value)||0;
    let final=total-(total*disc/100);
    let cash=parseFloat(document.getElementById("cash").value)||0;
    document.getElementById("total").innerText=final.toFixed(2);
    let changeEl=document.getElementById("change");
    if(cash>0){ let ch=cash-final; changeEl.innerText=ch.toFixed(2); changeEl.style.color=ch>=0?"var(--green)":"var(--danger)"; }
    else { changeEl.innerText="—"; changeEl.style.color="var(--muted)"; }
}

document.getElementById("cash").addEventListener("input",drawCart);
document.getElementById("discount").addEventListener("input",drawCart);

/* ── TOAST ── */
function showToast(msg,autoHide=true,undo=false){
    document.getElementById("toast_msg").innerText=msg;
    document.getElementById("toast_undo").style.display=undo?"":"none";
    document.getElementById("toast").classList.add("show");
    if(toastTimer) clearTimeout(toastTimer);
    if(autoHide) toastTimer=setTimeout(hideToast,4000);
}
function hideToast(){ document.getElementById("toast").classList.remove("show"); }

/* ── CHECKOUT ── */
async function checkout(settleLater=false){
    if(!cart.length){ showToast("Cart is empty"); return; }
    if(settleLater && !selectedCustomer){ showToast("Select a customer to settle later"); return; }
    let btn=document.getElementById(settleLater?"settle_btn":"checkout_btn");
    btn.disabled=true; btn.innerText="Processing…";

    const payload = {
        customer_id:      selectedCustomer?parseInt(selectedCustomer):null,
        items:            cart.map(c=>({sku:c.sku,name:c.name,price:c.price,qty:c.qty,unit_price:c.price,catalog_price:c.original_price||c.price,price_edited:c.price!==(c.original_price||c.price)})),
        discount_percent: parseFloat(document.getElementById("discount").value)||0,
        notes:            "",
        payment_method:   settleLater?"unpaid":selectedPayMethod,
        settle_later:     settleLater,
    };

    try {
        let res=await fetch("/invoice",{
            method:"POST",
            headers:{"Content-Type":"application/json"},
            body:JSON.stringify(payload),
        });
        let data;
        try {
            data = await res.json();
        } catch(e) {
            data = {detail:`Request failed (${res.status})`};
        }
        if(data.detail){ showToast("Error: "+data.detail); return; }
        if(settleLater){
            showToast(`⏳ ${data.invoice_number} saved — settle later`);
            clearCustomer(); cart=[]; drawCart();
            checkUnpaidCount();
        } else {
            if(!Number.isInteger(data.id)){
                showToast("Error: checkout did not return a valid invoice id");
                return;
            }
            window.location.href="/invoice/"+data.id;
        }
    } catch(e){
        // Network failure — queue for later sync
        try {
            await queueOfflineSale(payload);
            showToast("📴 No connection — sale queued, will sync when back online", false);
            cart=[]; drawCart();
            updateOfflineBadge();
        } catch(dbErr){
            showToast("Network error — could not save offline");
        }
    }
    finally { btn.disabled=false; btn.innerText=settleLater?"⏳ Settle Later (requires customer)":"Confirm Order"; }
}

/* ── UNPAID INVOICES ── */
async function loadUnpaidInvoices(){
    let list=document.getElementById("unpaid-list");
    list.innerHTML=`<div style="color:var(--muted);font-size:13px;text-align:center;padding:30px 0">Loading...</div>`;
    try {
        let data=await (await fetch("/unpaid-invoices")).json();

        // Update badge
        let badge=document.getElementById("unpaid-badge");
        if(data.length>0){ badge.innerText=data.length; badge.classList.add("show"); }
        else { badge.classList.remove("show"); }

        if(!data.length){
            list.innerHTML=`<div style="color:var(--muted);font-size:13px;text-align:center;padding:30px 0">✓ No unpaid invoices</div>`;
            return;
        }

        list.innerHTML=data.map(inv=>`
            <div class="unpaid-card" onclick="openInvModal(${JSON.stringify(inv).replace(/"/g,'&quot;')})">
                <div style="display:flex;justify-content:space-between;align-items:flex-start;">
                    <div>
                        <div class="unpaid-num">${inv.invoice_number}</div>
                        <div class="unpaid-name">${inv.customer}</div>
                        <div class="unpaid-date">${inv.created_at}</div>
                    </div>
                    <div class="unpaid-total">${inv.total.toFixed(2)}</div>
                </div>
                <div class="collect-row" onclick="event.stopPropagation()" style="${hasPermission("action_pos_settle_later") ? "" : "display:none"}">
                    <button class="collect-btn collect-cash"
                        onclick="collectPayment(${inv.id},'${inv.invoice_number}','cash')">
                        💵 Cash
                    </button>
                    <button class="collect-btn collect-visa"
                        onclick="collectPayment(${inv.id},'${inv.invoice_number}','visa')">
                        💳 Visa
                    </button>
                </div>
            </div>`).join("");
    } catch(e){
        list.innerHTML=`<div style="color:var(--danger);font-size:13px;text-align:center;padding:20px 0">Error loading invoices</div>`;
    }
}

async function collectPayment(invoiceId, invoiceNumber, method){
    let res=await fetch(`/invoice/${invoiceId}/collect`,{
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({payment_method:method}),
    });
    let data=await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    showToast(`✅ ${invoiceNumber} collected via ${method}!`);
    closeInvModal();
    loadUnpaidInvoices();
}

/* ── INVOICE DETAIL MODAL ── */
function openInvModal(inv){
    if(typeof inv === "string") inv = JSON.parse(inv);
    currentInvoiceData = inv;

    document.getElementById("modal-inv-num").innerText = inv.invoice_number;

    document.getElementById("modal-meta").innerHTML = `
        <div class="inv-row"><span class="label">Customer</span><span class="val">${inv.customer}</span></div>
        <div class="inv-row"><span class="label">Date</span><span class="val">${inv.created_at}</span></div>
    `;

    document.getElementById("modal-items").innerHTML = inv.items.map(item=>`
        <div class="inv-item-row">
            <div>
                <div class="inv-item-name">${item.name}</div>
                <div class="inv-item-meta">${item.qty.toFixed(0)} × ${item.unit_price.toFixed(2)}</div>
            </div>
            <div class="inv-item-total">${item.total.toFixed(2)}</div>
        </div>`).join("");

    document.getElementById("modal-totals").innerHTML = `
        <div class="inv-row"><span class="label">Subtotal</span><span>${inv.subtotal.toFixed(2)}</span></div>
        ${inv.discount>0?`<div class="inv-row"><span class="label">Discount</span><span style="color:var(--danger)">-${inv.discount.toFixed(2)}</span></div>`:""}
        <div class="inv-total-row"><span>Total</span><span style="font-family:var(--mono);color:var(--warn)">${inv.total.toFixed(2)} EGP</span></div>
    `;

    document.getElementById("inv-modal").classList.add("open");
}

function closeInvModal(){
    document.getElementById("inv-modal").classList.remove("open");
    currentInvoiceData = null;
}

function printInvoice(){
    window.print();
}

document.getElementById("inv-modal").addEventListener("click", function(e){
    if(e.target===this) closeInvModal();
});

load();

if("serviceWorker" in navigator){
    navigator.serviceWorker.register("/pos-sw.js", {scope:"/"}).catch(()=>{});
}
</script>
</body>
</html>
"""
