from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.log import record
from app.core.permissions import get_current_user
from app.database import get_db
from app.models.accounting import Account, Journal, JournalEntry
from app.models.customer import Customer
from app.models.inventory import StockMove
from app.models.invoice import Invoice, InvoiceItem
from app.models.product import Product
from app.models.refund import RetailRefund, RetailRefundItem
from app.models.user import User

router = APIRouter(prefix="/refunds", tags=["Refunds"])


class RefundItemIn(BaseModel):
    product_id: int
    qty: float


class RefundCreate(BaseModel):
    invoice_id: int
    reason: str
    refund_method: str = "cash"
    notes: Optional[str] = None
    items: List[RefundItemIn]


def _next_refund_number(db: Session) -> str:
    max_id = db.query(func.max(RetailRefund.id)).scalar() or 0
    return f"REF-{str(max_id + 1).zfill(5)}"


def _post_journal(db: Session, description: str, amount: float, refund_method: str, user_id: Optional[int], ref_id: int):
    cash_like_code = "1000" if refund_method == "cash" else "1100"
    journal = Journal(
        ref_type="retail_refund",
        ref_id=ref_id,
        description=description,
        user_id=user_id,
    )
    db.add(journal)
    db.flush()

    for code, debit, credit in [("4000", amount, 0), (cash_like_code, 0, amount)]:
        account = db.query(Account).filter(Account.code == code).first()
        if not account:
            continue
        db.add(JournalEntry(
            journal_id=journal.id,
            account_id=account.id,
            debit=debit,
            credit=credit,
        ))
        account.balance += Decimal(str(debit)) - Decimal(str(credit))


def _refunded_qty_by_product(db: Session, invoice_id: int) -> dict[int, float]:
    rows = (
        db.query(
            RetailRefundItem.product_id,
            func.coalesce(func.sum(RetailRefundItem.qty), 0),
        )
        .join(RetailRefund, RetailRefund.id == RetailRefundItem.refund_id)
        .filter(RetailRefund.invoice_id == invoice_id)
        .group_by(RetailRefundItem.product_id)
        .all()
    )
    return {int(product_id): float(qty or 0) for product_id, qty in rows}


@router.get("/api/invoices")
def list_invoices(q: str = "", db: Session = Depends(get_db)):
    query = db.query(Invoice)
    if q:
        like = f"%{q}%"
        query = (
            query.join(Customer, Customer.id == Invoice.customer_id)
            .filter(
                (Invoice.invoice_number.ilike(like))
                | (Customer.name.ilike(like))
            )
        )
    invoices = query.order_by(Invoice.created_at.desc()).limit(60).all()
    result = []
    for inv in invoices:
        refunded_total = float(
            db.query(func.coalesce(func.sum(RetailRefund.total), 0))
            .filter(RetailRefund.invoice_id == inv.id)
            .scalar()
            or 0
        )
        result.append({
            "id": inv.id,
            "invoice_number": inv.invoice_number,
            "customer": inv.customer.name if inv.customer else "Walk-in Customer",
            "date": inv.created_at.strftime("%Y-%m-%d %H:%M") if inv.created_at else "-",
            "status": inv.status,
            "payment_method": inv.payment_method,
            "total": float(inv.total),
            "refunded_total": refunded_total,
            "refundable_total": max(0, float(inv.total) - refunded_total),
        })
    return result


@router.get("/api/invoice/{invoice_id}")
def invoice_detail(invoice_id: int, db: Session = Depends(get_db)):
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    refunded_qty = _refunded_qty_by_product(db, invoice.id)
    items = []
    for item in invoice.items:
        sold_qty = float(item.qty)
        refunded = refunded_qty.get(item.product_id, 0.0)
        refundable_qty = max(0.0, sold_qty - refunded)
        items.append({
            "product_id": item.product_id,
            "sku": item.sku,
            "name": item.name,
            "sold_qty": sold_qty,
            "refunded_qty": refunded,
            "refundable_qty": refundable_qty,
            "unit_price": float(item.unit_price),
            "total": float(item.total),
        })

    return {
        "id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "customer_id": invoice.customer_id,
        "customer": invoice.customer.name if invoice.customer else "Walk-in Customer",
        "date": invoice.created_at.strftime("%Y-%m-%d %H:%M") if invoice.created_at else "-",
        "payment_method": invoice.payment_method,
        "status": invoice.status,
        "subtotal": float(invoice.subtotal),
        "discount": float(invoice.discount),
        "total": float(invoice.total),
        "items": items,
    }


@router.get("/api/refunds")
def list_refunds(db: Session = Depends(get_db)):
    refunds = db.query(RetailRefund).order_by(RetailRefund.created_at.desc(), RetailRefund.id.desc()).limit(50).all()
    return [
        {
            "id": refund.id,
            "refund_number": refund.refund_number,
            "invoice_number": refund.invoice.invoice_number if refund.invoice else "-",
            "customer": refund.customer.name if refund.customer else "Walk-in Customer",
            "refund_method": refund.refund_method,
            "reason": refund.reason or "",
            "total": float(refund.total),
            "created_at": refund.created_at.strftime("%Y-%m-%d %H:%M") if refund.created_at else "-",
        }
        for refund in refunds
    ]


@router.post("/api/create")
def create_refund(
    data: RefundCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    invoice = db.query(Invoice).filter(Invoice.id == data.invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if not data.items:
        raise HTTPException(status_code=400, detail="Select at least one item to refund")

    refund_number = _next_refund_number(db)
    refunded_qty = _refunded_qty_by_product(db, invoice.id)
    invoice_items = {item.product_id: item for item in invoice.items}

    total = 0.0
    parsed_items = []
    for row in data.items:
        if row.qty <= 0:
            continue
        invoice_item = invoice_items.get(row.product_id)
        if not invoice_item:
            raise HTTPException(status_code=400, detail=f"Product {row.product_id} is not part of this invoice")
        available_qty = max(0.0, float(invoice_item.qty) - refunded_qty.get(row.product_id, 0.0))
        if row.qty > available_qty:
            raise HTTPException(
                status_code=400,
                detail=f"Refund qty for {invoice_item.name} exceeds refundable amount ({available_qty:.3f})",
            )
        line_total = float(invoice_item.unit_price) * row.qty
        parsed_items.append((invoice_item, row.qty, line_total))
        total += line_total

    if not parsed_items:
        raise HTTPException(status_code=400, detail="Refund quantities must be greater than zero")

    refund = RetailRefund(
        refund_number=refund_number,
        invoice_id=invoice.id,
        customer_id=invoice.customer_id,
        user_id=current_user.id,
        reason=data.reason.strip(),
        refund_method=(data.refund_method or "cash").strip().lower(),
        notes=(data.notes or "").strip() or None,
        total=round(total, 2),
    )
    db.add(refund)
    db.flush()

    for invoice_item, qty, line_total in parsed_items:
        product = db.query(Product).filter(Product.id == invoice_item.product_id).first()
        if not product:
            raise HTTPException(status_code=404, detail=f"Product not found: {invoice_item.product_id}")
        db.add(RetailRefundItem(
            refund_id=refund.id,
            product_id=invoice_item.product_id,
            qty=qty,
            unit_price=float(invoice_item.unit_price),
            total=round(line_total, 2),
        ))
        before = float(product.stock)
        after = before + qty
        product.stock = after
        db.add(StockMove(
            product_id=product.id,
            type="in",
            qty=qty,
            qty_before=before,
            qty_after=after,
            ref_type="retail_refund",
            ref_id=refund.id,
            note=f"Retail refund {refund_number} - {invoice.invoice_number}",
            user_id=current_user.id,
        ))

    _post_journal(
        db=db,
        description=f"Retail refund - {refund_number} - {invoice.invoice_number}",
        amount=round(total, 2),
        refund_method=refund.refund_method,
        user_id=current_user.id,
        ref_id=refund.id,
    )
    record(
        db,
        "Refunds",
        "create_refund",
        f"Retail refund {refund_number} - {invoice.invoice_number} - {float(refund.total):.2f}",
        user=current_user,
        ref_type="retail_refund",
        ref_id=refund.id,
    )
    db.commit()
    db.refresh(refund)

    return {
        "id": refund.id,
        "refund_number": refund.refund_number,
        "invoice_number": invoice.invoice_number,
        "amount": float(refund.total),
    }


@router.get("/print/{refund_id}", response_class=HTMLResponse)
def print_refund(refund_id: int, db: Session = Depends(get_db)):
    refund = db.query(RetailRefund).filter(RetailRefund.id == refund_id).first()
    if not refund:
        raise HTTPException(status_code=404, detail="Refund not found")

    rows = ""
    for item in refund.items:
        rows += f"""
        <div class="row">
            <span>{item.product.name if item.product else "-"}</span>
            <span>{float(item.qty):.3f} x {float(item.unit_price):.2f}</span>
            <span>{float(item.total):.2f}</span>
        </div>"""

    return f"""<!DOCTYPE html>
<html>
<head>
<title>{refund.refund_number}</title>
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ font-family: monospace; background:#060810; color:white; }}
.r {{ width:340px; margin:30px auto; background:#0f1424; border-radius:16px; padding:20px; }}
.center {{ text-align:center; margin-bottom:12px; }}
.row {{ display:flex; justify-content:space-between; gap:10px; font-size:13px; margin:7px 0; }}
.line {{ border-top:1px dashed #445066; margin:12px 0; }}
.total {{ font-size:20px; color:#ff8da1; font-weight:bold; }}
.badge {{ background:rgba(255,77,109,.12); border:1px solid rgba(255,77,109,.28); color:#ff8da1; padding:8px 12px; border-radius:10px; text-align:center; margin-bottom:10px; font-weight:700; }}
.btn {{ width:100%; padding:14px; margin-top:10px; background:linear-gradient(135deg,#ff8da1,#ffd36a); border:none; border-radius:10px; color:#2b1300; font-size:15px; font-weight:800; cursor:pointer; }}
.btn-back {{ background:#151c30; color:#8899bb; }}
@media print {{
    .btn {{ display:none; }}
    body {{ background:white; color:black; }}
    .r {{ background:white; color:black; border:none; }}
    .total {{ color:#000; }}
}}
</style>
</head>
<body>
<div class="r">
    <div class="center">
        <img src="/static/Logo.png" alt="Habiba" style="height:120px;object-fit:contain;margin-bottom:6px;display:block;margin-left:auto;margin-right:auto">
        <div style="font-size:15px;font-weight:900;color:#ff8da1;margin-bottom:2px">Retail Refund</div>
        <div style="color:#8899bb;font-size:12px">{refund.refund_number}</div>
    </div>
    <div class="line"></div>
    <div class="badge">Refund for invoice {refund.invoice.invoice_number if refund.invoice else "-"}</div>
    <div class="row"><span>Customer</span><span>{refund.customer.name if refund.customer else "-"}</span></div>
    <div class="row"><span>Date</span><span>{refund.created_at.strftime('%Y-%m-%d %H:%M') if refund.created_at else '-'}</span></div>
    <div class="row"><span>Method</span><span>{refund.refund_method}</span></div>
    <div class="row"><span>Reason</span><span>{refund.reason or '-'}</span></div>
    <div class="line"></div>
    {rows}
    <div class="line"></div>
    <div class="row total"><span>Total</span><span>{float(refund.total):.2f}</span></div>
    <button class="btn" onclick="window.print()">Print Refund</button>
    <button class="btn btn-back" onclick="window.location.href='/refunds/'">Back to Refunds</button>
</div>
</body>
</html>"""


@router.get("/", response_class=HTMLResponse)
def refunds_ui():
    return """<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Retail Refunds</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root{
    --bg:#f3f5f8;--surface:#ffffff;--card:#ffffff;--card2:#f8fafc;--border:#d9e0e8;
    --border2:#c7d0da;--text:#18212b;--sub:#5e6b78;--muted:#8c98a4;--danger:#c84444;
    --warn:#b7791f;--accent:#244b74;--green:#1d7a46;--sans:'Outfit',sans-serif;--mono:'JetBrains Mono',monospace;--r:12px;
}
body.light{
    --bg:#f3f5f8;--surface:#ffffff;--card:#ffffff;--card2:#f8fafc;--border:#d9e0e8;--border2:#c7d0da;
    --text:#18212b;--sub:#5e6b78;--muted:#8c98a4;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:var(--sans);background:var(--bg);color:var(--text);min-height:100vh}
.wrap{max-width:1360px;margin:0 auto;padding:24px}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:20px}
.title h1{font-size:28px;font-weight:800;letter-spacing:-.02em}
.title p{color:var(--sub);margin-top:6px;font-size:14px}
.actions{display:flex;gap:10px;align-items:center}
.mode-btn,.nav-btn,.submit-btn,button{font-family:var(--sans)}
.nav-btn,.mode-btn{display:inline-flex;align-items:center;justify-content:center;text-decoration:none;border:1px solid var(--border);background:var(--surface);color:var(--sub);height:40px;padding:0 16px;border-radius:10px}
.mode-btn{width:42px;padding:0;cursor:pointer}
.nav-btn:hover,.mode-btn:hover{border-color:var(--accent);color:var(--accent)}
.grid{display:grid;grid-template-columns:320px 1fr 320px;gap:16px}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:18px;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.panel h2{font-size:16px;font-weight:700;margin-bottom:6px}
.panel-sub{font-size:12px;color:var(--sub);margin-bottom:14px}
.search{display:flex;gap:10px;margin-bottom:14px}
.search input,.field input,.field textarea,.field select{width:100%;border:1px solid var(--border2);background:var(--surface);color:var(--text);padding:12px 14px;border-radius:10px;outline:none;font-family:var(--sans)}
.search input:focus,.field input:focus,.field textarea:focus,.field select:focus{border-color:var(--accent)}
.field textarea{min-height:90px;resize:vertical}
.list{display:flex;flex-direction:column;gap:10px;max-height:72vh;overflow:auto;padding-right:4px}
.inv-card,.refund-card,.item-row{border:1px solid var(--border);background:var(--card2);border-radius:12px;padding:14px}
.inv-card{cursor:pointer;transition:border-color .15s,background .15s}
.inv-card:hover,.inv-card.active{border-color:var(--accent);background:#f5f8fc}
.num{font-family:var(--mono);font-size:12px;color:var(--accent);font-weight:700}
.sub{color:var(--sub);font-size:12px}
.row{display:flex;justify-content:space-between;gap:12px;align-items:center}
.meta{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:14px 0}
.stat{border:1px solid var(--border);background:var(--card2);border-radius:12px;padding:12px}
.stat label{display:block;font-size:11px;color:var(--muted);margin-bottom:5px}
.stat strong{font-size:15px}
.items{display:flex;flex-direction:column;gap:10px}
.items-head{display:grid;grid-template-columns:minmax(0,1.6fr) 90px 90px 110px;gap:10px;padding:0 4px 6px;color:var(--muted);font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.04em}
.item-row{display:grid;grid-template-columns:minmax(0,1.6fr) 90px 90px 110px;gap:10px;align-items:center}
.item-row input{width:100%;border:1px solid var(--border2);background:var(--surface);color:var(--text);padding:10px;border-radius:8px}
.totals{margin-top:14px;border:1px solid var(--border);background:var(--card2);border-radius:12px;padding:14px}
.totals .row{margin:8px 0}
.grand{font-size:22px;font-weight:900;color:var(--danger);font-family:var(--mono)}
.submit-btn{width:100%;margin-top:14px;border:none;background:var(--accent);color:#fff;padding:14px;border-radius:10px;font-size:15px;font-weight:700;cursor:pointer}
.submit-btn:hover{background:#1f3f62}
.submit-btn:disabled{opacity:.6;cursor:not-allowed}
.empty{padding:30px 10px;text-align:center;color:var(--muted)}
.chip{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:5px 10px;font-size:11px;font-weight:700;background:#eef3f8;color:var(--accent);border:1px solid var(--border)}
.section-note{padding:12px 14px;border:1px dashed var(--border2);border-radius:10px;background:#fbfcfe;color:var(--sub);font-size:13px}
#toast{position:fixed;bottom:18px;right:18px;background:#1f2937;color:white;padding:12px 16px;border-radius:10px;border:1px solid rgba(255,255,255,.08);opacity:0;pointer-events:none;transform:translateY(10px);transition:all .2s}
#toast.show{opacity:1;transform:translateY(0)}
@media (max-width:1200px){.grid{grid-template-columns:1fr}.meta{grid-template-columns:repeat(2,minmax(0,1fr))}.list{max-height:none}}
@media (max-width:720px){.wrap{padding:14px}.top{flex-direction:column;align-items:stretch}.meta{grid-template-columns:1fr}.items-head{display:none}.item-row{grid-template-columns:1fr}.row{align-items:flex-start}}
</style>
</head>
<body>
<div class="wrap">
    <div class="top">
        <div class="title">
            <h1>Retail Refunds</h1>
            <p>Process POS returns with a clear invoice lookup, item selection, and printable refund receipt.</p>
        </div>
        <div class="actions">
            <a class="nav-btn" href="/pos">Back to POS</a>
            <button class="mode-btn" onclick="toggleMode()">◐</button>
        </div>
    </div>

    <div class="grid">
        <section class="panel">
            <h2>Invoices</h2>
            <div class="panel-sub">Search recent POS invoices by number or customer.</div>
            <div class="search">
                <input id="search" placeholder="Search invoice or customer" oninput="loadInvoices()">
            </div>
            <div id="invoice-list" class="list"><div class="empty">Loading invoices...</div></div>
        </section>

        <section class="panel">
            <h2>Refund Builder</h2>
            <div class="panel-sub">Refund only quantities that were sold and still available to return.</div>
            <div id="invoice-empty" class="section-note">Select an invoice from the left to start a refund.</div>
            <div id="invoice-detail" style="display:none">
                <div class="row">
                    <div>
                        <div class="num" id="inv-number">-</div>
                        <div class="sub" id="inv-customer">-</div>
                    </div>
                    <div class="chip" id="inv-method">-</div>
                </div>
                <div class="meta">
                    <div class="stat"><label>Date</label><strong id="inv-date">-</strong></div>
                    <div class="stat"><label>Invoice Total</label><strong id="inv-total">0.00</strong></div>
                    <div class="stat"><label>Status</label><strong id="inv-status">-</strong></div>
                    <div class="stat"><label>Refund Total</label><strong id="refund-total">0.00</strong></div>
                </div>
                <div class="items-head">
                    <div>Item</div>
                    <div>Sold</div>
                    <div>Left</div>
                    <div>Refund Qty</div>
                </div>
                <div id="items" class="items"></div>
                <div class="field" style="margin-top:14px">
                    <input id="reason" placeholder="Reason for refund">
                </div>
                <div class="field" style="margin-top:10px">
                    <select id="refund-method" onchange="recalcRefund()">
                        <option value="cash">Cash</option>
                        <option value="credit">Credit</option>
                        <option value="exchange">Exchange</option>
                    </select>
                </div>
                <div class="field" style="margin-top:10px">
                    <textarea id="notes" placeholder="Optional notes"></textarea>
                </div>
                <div class="totals">
                    <div class="row"><span>Items selected</span><span id="refund-lines">0</span></div>
                    <div class="row"><span>Refund method</span><span id="refund-method-label">cash</span></div>
                    <div class="row"><span>Total</span><span class="grand" id="refund-grand">0.00</span></div>
                </div>
                <button id="submit-btn" class="submit-btn" onclick="submitRefund()">Create Refund</button>
            </div>
        </section>

        <section class="panel">
            <h2>Recent Refunds</h2>
            <div class="panel-sub">Latest recorded retail refunds with quick print access.</div>
            <div id="refund-list" class="list"><div class="empty">Loading refunds...</div></div>
        </section>
    </div>
</div>
<div id="toast"></div>
<script>
let selectedInvoiceId = null;
let selectedInvoice = null;
let token = localStorage.getItem("token") || "";

function showToast(msg){
    const el = document.getElementById("toast");
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(window.toastTimer);
    window.toastTimer = setTimeout(()=>el.classList.remove("show"), 3000);
}

function toggleMode(){
    document.body.classList.toggle("light");
    localStorage.setItem("refund-theme", document.body.classList.contains("light") ? "light" : "dark");
}

if(localStorage.getItem("refund-theme")==="light"){
    document.body.classList.add("light");
}

async function loadInvoices(){
    const q = encodeURIComponent(document.getElementById("search").value.trim());
    const list = document.getElementById("invoice-list");
    list.innerHTML = '<div class="empty">Loading invoices...</div>';
    try{
        const data = await (await fetch(`/refunds/api/invoices?q=${q}`)).json();
        if(!Array.isArray(data) || !data.length){
            list.innerHTML = '<div class="empty">No invoices found.</div>';
            return;
        }
        list.innerHTML = data.map(inv=>`
            <div class="inv-card ${selectedInvoiceId===inv.id?'active':''}" onclick="selectInvoice(${inv.id})">
                <div class="row">
                    <div class="num">${inv.invoice_number}</div>
                    <div class="sub">${inv.date}</div>
                </div>
                <div style="font-weight:700;margin:8px 0 4px">${inv.customer}</div>
                <div class="row">
                    <span class="sub">Invoice ${inv.total.toFixed(2)}</span>
                    <span class="sub" style="color:var(--accent)">Refundable ${inv.refundable_total.toFixed(2)}</span>
                </div>
            </div>
        `).join("");
    }catch(e){
        list.innerHTML = '<div class="empty">Failed to load invoices.</div>';
    }
}

async function selectInvoice(id){
    selectedInvoiceId = id;
    loadInvoices();
    try{
        const data = await (await fetch(`/refunds/api/invoice/${id}`)).json();
        if(data.detail){ showToast(data.detail); return; }
        selectedInvoice = data;
        renderInvoice();
    }catch(e){
        showToast("Failed to load invoice");
    }
}

function renderInvoice(){
    if(!selectedInvoice) return;
    document.getElementById("invoice-empty").style.display = "none";
    document.getElementById("invoice-detail").style.display = "";
    document.getElementById("inv-number").textContent = selectedInvoice.invoice_number;
    document.getElementById("inv-customer").textContent = selectedInvoice.customer;
    document.getElementById("inv-method").textContent = selectedInvoice.payment_method;
    document.getElementById("inv-date").textContent = selectedInvoice.date;
    document.getElementById("inv-total").textContent = selectedInvoice.total.toFixed(2);
    document.getElementById("inv-status").textContent = selectedInvoice.status;
    document.getElementById("reason").value = "";
    document.getElementById("notes").value = "";
    document.getElementById("refund-method").value = "cash";

    const itemsEl = document.getElementById("items");
    itemsEl.innerHTML = selectedInvoice.items.map(item=>`
        <div class="item-row">
            <div>
                <div style="font-weight:800">${item.name}</div>
                <div class="sub">${item.sku || ''}</div>
            </div>
            <div class="sub">${item.sold_qty.toFixed(3)}</div>
            <div class="sub">${item.refundable_qty.toFixed(3)}</div>
            <input type="number" min="0" max="${item.refundable_qty}" step="0.001" value="0" data-product-id="${item.product_id}" data-price="${item.unit_price}" oninput="recalcRefund()">
        </div>
    `).join("");
    recalcRefund();
}

function recalcRefund(){
    let total = 0;
    let lines = 0;
    document.querySelectorAll('#items input').forEach(input=>{
        const qty = parseFloat(input.value) || 0;
        const max = parseFloat(input.max) || 0;
        if(qty > max){
            input.value = max;
        }
        if((parseFloat(input.value) || 0) > 0){
            lines += 1;
            total += (parseFloat(input.value) || 0) * (parseFloat(input.dataset.price) || 0);
        }
    });
    document.getElementById("refund-total").textContent = total.toFixed(2);
    document.getElementById("refund-grand").textContent = total.toFixed(2);
    document.getElementById("refund-lines").textContent = lines;
    document.getElementById("refund-method-label").textContent = document.getElementById("refund-method").value;
}

async function loadRefunds(){
    const list = document.getElementById("refund-list");
    list.innerHTML = '<div class="empty">Loading refunds...</div>';
    try{
        const data = await (await fetch('/refunds/api/refunds')).json();
        if(!Array.isArray(data) || !data.length){
            list.innerHTML = '<div class="empty">No refunds recorded yet.</div>';
            return;
        }
        list.innerHTML = data.map(r=>`
            <div class="refund-card">
                <div class="row">
                    <div class="num">${r.refund_number}</div>
                    <a class="nav-btn" style="height:34px;padding:0 12px" href="/refunds/print/${r.id}" target="_blank">Print</a>
                </div>
                <div style="font-weight:700;margin:8px 0 4px">${r.customer}</div>
                <div class="sub">${r.invoice_number} · ${r.created_at}</div>
                <div class="row" style="margin-top:10px">
                    <span class="sub">${r.reason || r.refund_method}</span>
                    <span style="font-family:var(--mono);font-weight:800;color:var(--text)">${r.total.toFixed(2)}</span>
                </div>
            </div>
        `).join("");
    }catch(e){
        list.innerHTML = '<div class="empty">Failed to load refunds.</div>';
    }
}

async function submitRefund(){
    if(!selectedInvoice){
        showToast("Select an invoice first");
        return;
    }
    const reason = document.getElementById("reason").value.trim();
    if(!reason){
        showToast("Enter a refund reason");
        return;
    }
    const items = Array.from(document.querySelectorAll('#items input'))
        .map(input=>({product_id: parseInt(input.dataset.productId), qty: parseFloat(input.value) || 0}))
        .filter(item=>item.qty > 0);
    if(!items.length){
        showToast("Choose at least one item quantity");
        return;
    }
    const btn = document.getElementById("submit-btn");
    btn.disabled = true;
    btn.textContent = "Creating refund...";
    try{
        const res = await fetch('/refunds/api/create', {
            method:'POST',
            headers:{
                'Content-Type':'application/json',
                'Authorization':'Bearer ' + token
            },
            body:JSON.stringify({
                invoice_id: selectedInvoice.id,
                reason,
                refund_method: document.getElementById("refund-method").value,
                notes: document.getElementById("notes").value.trim(),
                items
            })
        });
        const data = await res.json();
        if(data.detail){
            showToast(data.detail);
            return;
        }
        showToast(`${data.refund_number} created`);
        window.open(`/refunds/print/${data.id}`, '_blank');
        await Promise.all([loadRefunds(), selectInvoice(selectedInvoice.id), loadInvoices()]);
    }catch(e){
        showToast("Failed to create refund");
    }finally{
        btn.disabled = false;
        btn.textContent = "Create Refund";
    }
}

loadInvoices();
loadRefunds();
</script>
</body>
</html>"""
