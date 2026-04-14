from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.log import record
from app.core.permissions import get_current_user, require_action, require_permission
from app.database import get_async_session
from app.models.accounting import Account, Journal, JournalEntry
from app.models.customer import Customer
from app.models.inventory import StockMove
from app.models.invoice import Invoice, InvoiceItem
from app.models.product import Product
from app.models.refund import RetailRefund, RetailRefundItem
from app.models.user import User

router = APIRouter(
    prefix="/refunds",
    tags=["Refunds"],
    dependencies=[Depends(require_action("pos", "sales", "refund"))],
)


class RefundItemIn(BaseModel):
    product_id: int
    qty: float


class RefundCreate(BaseModel):
    invoice_id: int
    reason: str
    refund_method: str = "cash"
    notes: Optional[str] = None
    items: List[RefundItemIn]


async def _next_refund_number(db: AsyncSession) -> str:
    _r = await db.execute(select(func.max(RetailRefund.id)))
    max_id = _r.scalar() or 0
    return f"REF-{str(max_id + 1).zfill(5)}"


async def _post_journal(db: AsyncSession, description: str, amount: float, refund_method: str, user_id: Optional[int], ref_id: int):
    cash_like_code = "1000" if refund_method == "cash" else "1100"
    journal = Journal(
        ref_type="retail_refund",
        ref_id=ref_id,
        description=description,
        user_id=user_id,
    )
    db.add(journal)
    await db.flush()

    for code, debit, credit in [("4000", amount, 0), (cash_like_code, 0, amount)]:
        _r = await db.execute(select(Account).where(Account.code == code))
        account = _r.scalar_one_or_none()
        if not account:
            continue
        db.add(JournalEntry(
            journal_id=journal.id,
            account_id=account.id,
            debit=debit,
            credit=credit,
        ))
        account.balance += Decimal(str(debit)) - Decimal(str(credit))


async def _refunded_qty_by_product(db: AsyncSession, invoice_id: int) -> dict[int, float]:
    _r = await db.execute(
        select(
            RetailRefundItem.product_id,
            func.coalesce(func.sum(RetailRefundItem.qty), 0),
        )
        .join(RetailRefund, RetailRefund.id == RetailRefundItem.refund_id)
        .where(RetailRefund.invoice_id == invoice_id)
        .group_by(RetailRefundItem.product_id)
    )
    rows = _r.all()
    return {int(product_id): float(qty or 0) for product_id, qty in rows}


@router.get("/api/invoices")
async def list_invoices(q: str = "", db: AsyncSession = Depends(get_async_session)):
    stmt = (
        select(Invoice)
        .options(selectinload(Invoice.customer), selectinload(Invoice.items))
        .order_by(Invoice.created_at.desc())
        .limit(60)
    )
    if q:
        like = f"%{q}%"
        stmt = (
            stmt.join(Customer, Customer.id == Invoice.customer_id)
            .where(
                Invoice.invoice_number.ilike(like)
                | Customer.name.ilike(like)
            )
        )
    _r = await db.execute(stmt)
    invoices = _r.scalars().all()
    result = []
    for inv in invoices:
        _r2 = await db.execute(
            select(func.coalesce(func.sum(RetailRefund.total), 0))
            .where(RetailRefund.invoice_id == inv.id)
        )
        refunded_total = float(_r2.scalar() or 0)
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
async def invoice_detail(invoice_id: int, db: AsyncSession = Depends(get_async_session)):
    _r = await db.execute(
        select(Invoice)
        .where(Invoice.id == invoice_id)
        .options(selectinload(Invoice.customer), selectinload(Invoice.items))
    )
    invoice = _r.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    refunded_qty = await _refunded_qty_by_product(db, invoice.id)
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
async def list_refunds(db: AsyncSession = Depends(get_async_session)):
    _r = await db.execute(
        select(RetailRefund)
        .options(
            selectinload(RetailRefund.invoice),
            selectinload(RetailRefund.customer),
        )
        .order_by(RetailRefund.created_at.desc(), RetailRefund.id.desc())
        .limit(50)
    )
    refunds = _r.scalars().all()
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


@router.post("/api/create", dependencies=[Depends(require_action("pos", "sales", "refund"))])
async def create_refund(
    data: RefundCreate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    _r = await db.execute(
        select(Invoice)
        .where(Invoice.id == data.invoice_id)
        .options(selectinload(Invoice.items))
    )
    invoice = _r.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if not data.items:
        raise HTTPException(status_code=400, detail="Select at least one item to refund")

    refund_number = await _next_refund_number(db)
    refunded_qty = await _refunded_qty_by_product(db, invoice.id)
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
    await db.flush()

    for invoice_item, qty, line_total in parsed_items:
        _r = await db.execute(select(Product).where(Product.id == invoice_item.product_id))
        product = _r.scalar_one_or_none()
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

    await _post_journal(
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
    await db.commit()
    await db.refresh(refund)

    return {
        "id": refund.id,
        "refund_number": refund.refund_number,
        "invoice_number": invoice.invoice_number,
        "amount": float(refund.total),
    }


@router.get("/print/{refund_id}", response_class=HTMLResponse)
async def print_refund(refund_id: int, db: AsyncSession = Depends(get_async_session)):
    _r = await db.execute(
        select(RetailRefund)
        .where(RetailRefund.id == refund_id)
        .options(
            selectinload(RetailRefund.invoice),
            selectinload(RetailRefund.customer),
            selectinload(RetailRefund.items).selectinload(RetailRefundItem.product),
        )
    )
    refund = _r.scalar_one_or_none()
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
<title>Retail Refunds — Thunder ERP</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root {
    --bg:      #060810;
    --card:    #0f1424;
    --card2:   #151c30;
    --border:  rgba(255,255,255,0.06);
    --border2: rgba(255,255,255,0.11);
    --rose:    #ff6b8a;
    --rose2:   #ff4d6d;
    --green:   #00ff9d;
    --blue:    #4d9fff;
    --warn:    #ffb547;
    --text:    #f0f4ff;
    --sub:     #8899bb;
    --muted:   #445066;
    --sans:    'Outfit', sans-serif;
    --mono:    'JetBrains Mono', monospace;
    --r:       12px;
}
body.light {
    --bg: #f2f4f8; --card: #ffffff; --card2: #f7f8fb;
    --border: rgba(0,0,0,0.07); --border2: rgba(0,0,0,0.13);
    --green: #0f8a43;
    --text: #141820; --sub: #505870; --muted: #8090a8;
}
body.light nav { background: rgba(242,244,248,.92); }
body.light .inv-card:hover, body.light .inv-card.active { background: rgba(255,107,138,.06); }

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: var(--sans); background: var(--bg); color: var(--text); min-height: 100vh; font-size: 14px; }

/* ── NAV ── */
nav {
    position: sticky; top: 0; z-index: 100;
    display: flex; align-items: center; gap: 10px;
    padding: 0 24px; height: 58px;
    background: rgba(6,8,16,.92); backdrop-filter: blur(20px);
    border-bottom: 1px solid var(--border);
}
.logo {
    font-size: 17px; font-weight: 900; text-decoration: none;
    display: flex; align-items: center; gap: 8px; margin-right: 10px;
    background: linear-gradient(135deg, var(--green), var(--blue));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
}
.nav-back {
    display: inline-flex; align-items: center; gap: 7px;
    background: var(--card); border: 1px solid var(--border2);
    color: var(--sub); font-family: var(--sans); font-size: 12px;
    font-weight: 600; padding: 7px 14px; border-radius: 9px;
    text-decoration: none; transition: all .2s;
}
.nav-back:hover { border-color: var(--rose); color: var(--rose); }
.nav-spacer { flex: 1; }
.mode-btn {
    width: 36px; height: 36px; border-radius: 10px;
    border: 1px solid var(--border); background: var(--card);
    color: var(--sub); font-size: 16px; cursor: pointer; transition: all .2s;
    display: flex; align-items: center; justify-content: center;
}
.mode-btn:hover { border-color: var(--border2); transform: scale(1.06); }
.user-pill {
    display: flex; align-items: center; gap: 10px;
    background: var(--card); border: 1px solid var(--border);
    border-radius: 40px; padding: 6px 14px 6px 8px;
}
.user-avatar {
    width: 26px; height: 26px;
    background: linear-gradient(135deg, #7ecb6f, #d4a256);
    border-radius: 50%; display: flex; align-items: center; justify-content: center;
    font-size: 11px; font-weight: 700; color: #0a0c08;
}
.user-name { font-size: 13px; font-weight: 500; color: var(--sub); }
.logout-btn {
    background: transparent; border: 1px solid var(--border);
    color: var(--muted); font-family: var(--sans); font-size: 12px;
    font-weight: 500; padding: 7px 14px; border-radius: 8px; cursor: pointer; transition: all .2s;
}
.logout-btn:hover { border-color: var(--rose2); color: var(--rose2); }

/* ── LAYOUT ── */
.page { max-width: 1380px; margin: 0 auto; padding: 28px 24px; }
.page-header { margin-bottom: 24px; }
.page-title { font-size: 22px; font-weight: 800; display: flex; align-items: center; gap: 10px; }
.page-title-badge {
    font-size: 11px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase;
    background: rgba(255,107,138,.12); border: 1px solid rgba(255,107,138,.25);
    color: var(--rose); padding: 3px 10px; border-radius: 20px;
}
.page-sub { color: var(--muted); font-size: 13px; margin-top: 4px; }

.layout { display: grid; grid-template-columns: 300px 1fr 300px; gap: 16px; align-items: start; }

/* ── PANELS ── */
.panel {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 16px; overflow: hidden;
    position: sticky; top: 74px;
}
.panel-head {
    padding: 16px 18px 14px;
    border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
}
.panel-title { font-size: 13px; font-weight: 700; color: var(--text); }
.panel-count {
    font-size: 11px; font-weight: 700; font-family: var(--mono);
    background: var(--card2); border: 1px solid var(--border2);
    color: var(--sub); padding: 2px 8px; border-radius: 20px;
}
.panel-body { padding: 14px; }

/* ── SEARCH ── */
.search-wrap {
    display: flex; align-items: center; gap: 9px;
    background: var(--card2); border: 1px solid var(--border2);
    border-radius: var(--r); padding: 0 12px; margin-bottom: 12px;
    transition: border-color .2s;
}
.search-wrap:focus-within { border-color: rgba(255,107,138,.4); }
.search-wrap svg { color: var(--muted); flex-shrink: 0; }
.search-wrap input {
    background: transparent; border: none; outline: none;
    color: var(--text); font-family: var(--sans); font-size: 13px;
    padding: 10px 0; width: 100%;
}
.search-wrap input::placeholder { color: var(--muted); }

/* ── INVOICE LIST ── */
.inv-list { display: flex; flex-direction: column; gap: 6px; max-height: calc(100vh - 220px); overflow-y: auto; }
.inv-card {
    background: var(--card2); border: 1px solid var(--border);
    border-radius: 10px; padding: 12px 14px; cursor: pointer;
    transition: border-color .15s, background .15s;
}
.inv-card:hover { border-color: rgba(255,107,138,.35); background: rgba(255,107,138,.04); }
.inv-card.active { border-color: var(--rose); background: rgba(255,107,138,.07); }
.inv-card-num { font-family: var(--mono); font-size: 11px; font-weight: 700; color: var(--rose); }
.inv-card-customer { font-size: 13px; font-weight: 600; color: var(--text); margin: 4px 0 3px; }
.inv-card-row { display: flex; justify-content: space-between; align-items: center; }
.inv-card-date { font-size: 11px; color: var(--muted); }
.inv-card-amount { font-family: var(--mono); font-size: 12px; font-weight: 700; color: var(--sub); }
.inv-card-refundable { font-family: var(--mono); font-size: 11px; font-weight: 700; color: var(--green); }

/* ── CENTER: BUILDER ── */
.builder { background: var(--card); border: 1px solid var(--border); border-radius: 16px; overflow: hidden; }
.builder-head {
    padding: 16px 20px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
}
.builder-title { font-size: 13px; font-weight: 700; color: var(--text); }

.empty-state {
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    gap: 12px; padding: 80px 40px; color: var(--muted); text-align: center;
}
.empty-state-icon { font-size: 36px; opacity: .4; }
.empty-state-title { font-size: 15px; font-weight: 700; color: var(--sub); }
.empty-state-sub { font-size: 13px; }

/* ── INVOICE DETAIL BANNER ── */
.inv-banner {
    padding: 14px 20px;
    background: rgba(255,107,138,.05);
    border-bottom: 1px solid rgba(255,107,138,.12);
    display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
}
.inv-banner-num { font-family: var(--mono); font-size: 13px; font-weight: 700; color: var(--rose); }
.inv-banner-customer { font-size: 14px; font-weight: 700; color: var(--text); }
.inv-banner-meta { font-size: 12px; color: var(--muted); }
.inv-banner-spacer { flex: 1; }
.inv-banner-total { font-family: var(--mono); font-size: 18px; font-weight: 700; color: var(--text); }
.inv-banner-total-label { font-size: 10px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; color: var(--muted); }

/* ── ITEMS TABLE ── */
.items-section { padding: 16px 20px 0; }
.items-header {
    display: grid; grid-template-columns: 1fr 80px 80px 110px;
    gap: 10px; padding: 0 0 8px;
    font-size: 10px; font-weight: 700; letter-spacing: 1px;
    text-transform: uppercase; color: var(--muted);
    border-bottom: 1px solid var(--border);
}
.item-row {
    display: grid; grid-template-columns: 1fr 80px 80px 110px;
    gap: 10px; padding: 12px 0;
    border-bottom: 1px solid var(--border);
    align-items: center;
}
.item-row:last-child { border-bottom: none; }
.item-name { font-size: 13px; font-weight: 600; color: var(--text); }
.item-sku { font-family: var(--mono); font-size: 10px; color: var(--muted); margin-top: 2px; }
.item-qty { font-family: var(--mono); font-size: 13px; color: var(--sub); }
.item-refundable { font-family: var(--mono); font-size: 13px; color: var(--green); font-weight: 600; }
.qty-input {
    width: 100%; background: var(--card2); border: 1px solid var(--border2);
    border-radius: 9px; padding: 9px 12px; color: var(--text);
    font-family: var(--mono); font-size: 13px; outline: none; text-align: center;
    transition: border-color .2s;
}
.qty-input:focus { border-color: rgba(255,107,138,.5); }
.qty-input.has-value { border-color: rgba(255,107,138,.4); background: rgba(255,107,138,.06); }

/* ── FORM SECTION ── */
.form-section {
    padding: 16px 20px; border-top: 1px solid var(--border);
    display: grid; grid-template-columns: 1fr 160px; gap: 12px; align-items: start;
}
.fld { display: flex; flex-direction: column; gap: 5px; }
.fld label { font-size: 10px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; color: var(--muted); }
.fld input, .fld select {
    background: var(--card2); border: 1px solid var(--border2);
    border-radius: 10px; padding: 10px 12px; color: var(--text);
    font-family: var(--sans); font-size: 13px; outline: none;
    transition: border-color .2s; width: 100%;
}
.fld input:focus, .fld select:focus { border-color: rgba(255,107,138,.4); }
.fld input::placeholder { color: var(--muted); }

/* ── SUMMARY + SUBMIT ── */
.summary-section {
    padding: 14px 20px 18px;
    border-top: 1px solid var(--border);
    display: flex; align-items: center; gap: 16px;
}
.summary-items {
    display: flex; flex-direction: column; gap: 4px;
}
.summary-label { font-size: 10px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; color: var(--muted); }
.summary-items-count { font-family: var(--mono); font-size: 15px; font-weight: 700; color: var(--sub); }
.summary-divider { width: 1px; height: 36px; background: var(--border2); }
.summary-total-wrap { display: flex; flex-direction: column; gap: 3px; }
.summary-total { font-family: var(--mono); font-size: 28px; font-weight: 700; color: var(--rose); line-height: 1; }
.summary-total-sub { font-size: 11px; color: var(--muted); }
.summary-spacer { flex: 1; }
.submit-btn {
    display: flex; align-items: center; gap: 8px;
    background: linear-gradient(135deg, var(--rose2), #e63060);
    border: none; border-radius: 12px; padding: 14px 28px;
    font-family: var(--sans); font-size: 14px; font-weight: 800;
    color: white; cursor: pointer; transition: all .2s;
    box-shadow: 0 4px 20px rgba(255,77,109,.25);
}
.submit-btn:hover:not(:disabled) { filter: brightness(1.1); transform: translateY(-2px); box-shadow: 0 8px 28px rgba(255,77,109,.35); }
.submit-btn:disabled { opacity: .4; cursor: not-allowed; transform: none; box-shadow: none; }

/* ── RECENT REFUNDS LIST ── */
.refund-card {
    background: var(--card2); border: 1px solid var(--border);
    border-radius: 10px; padding: 12px 14px; margin-bottom: 6px;
    transition: border-color .15s;
}
.refund-card:last-child { margin-bottom: 0; }
.refund-card:hover { border-color: var(--border2); }
.refund-card-top { display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; }
.refund-num { font-family: var(--mono); font-size: 11px; font-weight: 700; color: var(--rose); }
.refund-print {
    display: inline-flex; align-items: center; gap: 5px;
    background: var(--card); border: 1px solid var(--border2);
    color: var(--sub); font-size: 11px; font-weight: 700;
    padding: 4px 10px; border-radius: 7px; text-decoration: none;
    transition: all .15s; font-family: var(--sans);
}
.refund-print:hover { border-color: var(--rose); color: var(--rose); }
.refund-customer { font-size: 13px; font-weight: 600; color: var(--text); margin-bottom: 3px; }
.refund-meta { font-size: 11px; color: var(--muted); margin-bottom: 6px; }
.refund-bottom { display: flex; align-items: center; justify-content: space-between; }
.refund-reason { font-size: 11px; color: var(--sub); font-style: italic; }
.refund-amount { font-family: var(--mono); font-size: 15px; font-weight: 700; color: var(--rose); }

/* ── UTILS ── */
.empty-list {
    text-align: center; padding: 32px 16px;
    color: var(--muted); font-size: 13px;
}
.empty-list svg { margin-bottom: 10px; opacity: .3; }

/* ── TOAST ── */
.toast {
    position: fixed; bottom: 22px; left: 50%;
    transform: translateX(-50%) translateY(12px);
    background: var(--card2); border: 1px solid var(--border2);
    border-radius: 12px; padding: 12px 20px;
    font-size: 13px; font-weight: 600; color: var(--text);
    box-shadow: 0 20px 50px rgba(0,0,0,.5);
    opacity: 0; pointer-events: none;
    transition: opacity .25s, transform .25s; z-index: 999;
}
.toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
.toast.success { border-color: rgba(0,255,157,.3); color: var(--green); }
.toast.error   { border-color: rgba(255,77,109,.3); color: var(--rose); }

/* ── SCROLLBAR ── */
::-webkit-scrollbar { width: 4px; }
::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 4px; }

@media (max-width: 1150px) {
    .layout { grid-template-columns: 280px 1fr; }
    .panel:last-child { display: none; }
}
@media (max-width: 760px) {
    .layout { grid-template-columns: 1fr; }
    .panel { position: static; }
    .items-header, .item-row { grid-template-columns: 1fr 70px 90px; }
    .items-header div:nth-child(2), .item-qty { display: none; }
    .form-section { grid-template-columns: 1fr; }
    .summary-section { flex-wrap: wrap; }
}
</style>
</head>
<body>

<nav>
    <a href="/home" class="logo">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
            <polygon points="13,2 4,14 11,14 11,22 20,10 13,10" fill="#f59e0b"/>
        </svg>
        Thunder ERP
    </a>
    <a href="/pos" class="nav-back">
        <svg width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path d="M19 12H5M12 5l-7 7 7 7"/></svg>
        Back to POS
    </a>
    <div class="nav-spacer"></div>
    <button class="mode-btn" id="mode-btn" onclick="toggleMode()">🌙</button>
    <div class="user-pill">
        <div class="user-avatar" id="user-avatar">A</div>
        <span class="user-name" id="user-name">Admin</span>
    </div>
    <button class="logout-btn" onclick="logout()">Sign out</button>
</nav>

<div class="page">
    <div class="page-header">
        <div class="page-title">
            Retail Refunds
            <span class="page-title-badge">Returns</span>
        </div>
        <div class="page-sub">Find an invoice, select items to return, and issue a refund receipt.</div>
    </div>

    <div class="layout">

        <!-- LEFT: INVOICE SEARCH -->
        <div class="panel">
            <div class="panel-head">
                <span class="panel-title">Invoices</span>
                <span class="panel-count" id="inv-count">—</span>
            </div>
            <div class="panel-body">
                <div class="search-wrap">
                    <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24">
                        <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
                    </svg>
                    <input id="search" placeholder="Invoice # or customer name…" oninput="onSearch()">
                </div>
                <div id="invoice-list" class="inv-list">
                    <div class="empty-list">Loading invoices…</div>
                </div>
            </div>
        </div>

        <!-- CENTER: REFUND BUILDER -->
        <div class="builder">
            <div class="builder-head">
                <span class="builder-title">Refund Builder</span>
                <span id="builder-status" style="font-size:12px;color:var(--muted)">Select an invoice to begin</span>
            </div>

            <!-- Empty state -->
            <div id="empty-state" class="empty-state">
                <div class="empty-state-icon">↩</div>
                <div class="empty-state-title">No invoice selected</div>
                <div class="empty-state-sub">Choose an invoice from the left panel to start processing a return.</div>
            </div>

            <!-- Invoice detail + form (hidden until invoice selected) -->
            <div id="invoice-detail" style="display:none">

                <!-- Invoice banner -->
                <div class="inv-banner">
                    <div>
                        <div class="inv-banner-num" id="ib-num">—</div>
                        <div class="inv-banner-customer" id="ib-customer">—</div>
                        <div class="inv-banner-meta" id="ib-meta">—</div>
                    </div>
                    <div class="inv-banner-spacer"></div>
                    <div style="text-align:right">
                        <div class="inv-banner-total-label">Invoice Total</div>
                        <div class="inv-banner-total" id="ib-total">—</div>
                    </div>
                </div>

                <!-- Items -->
                <div class="items-section">
                    <div class="items-header">
                        <div>Item</div>
                        <div>Sold</div>
                        <div>Available</div>
                        <div style="text-align:center">Return Qty</div>
                    </div>
                    <div id="items"></div>
                </div>

                <!-- Reason + method -->
                <div class="form-section">
                    <div class="fld">
                        <label>Reason for Return *</label>
                        <input id="reason" placeholder="e.g. Wrong item, damaged product…">
                    </div>
                    <div class="fld">
                        <label>Refund Method</label>
                        <select id="refund-method">
                            <option value="cash">💵 Cash</option>
                            <option value="credit">💳 Credit</option>
                            <option value="exchange">🔄 Exchange</option>
                        </select>
                    </div>
                </div>

                <!-- Summary + submit -->
                <div class="summary-section">
                    <div class="summary-items">
                        <div class="summary-label">Items</div>
                        <div class="summary-items-count" id="summary-count">0</div>
                    </div>
                    <div class="summary-divider"></div>
                    <div class="summary-total-wrap">
                        <div class="summary-label">Refund Total</div>
                        <div class="summary-total" id="summary-total">0.00</div>
                        <div class="summary-total-sub">EGP</div>
                    </div>
                    <div class="summary-spacer"></div>
                    <button id="submit-btn" class="submit-btn" onclick="submitRefund()" disabled>
                        <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24">
                            <polyline points="20 6 9 17 4 12"/>
                        </svg>
                        Issue Refund
                    </button>
                </div>

            </div>
        </div>

        <!-- RIGHT: RECENT REFUNDS -->
        <div class="panel">
            <div class="panel-head">
                <span class="panel-title">Recent Refunds</span>
                <span class="panel-count" id="refund-count">—</span>
            </div>
            <div class="panel-body" style="max-height:calc(100vh - 220px);overflow-y:auto">
                <div id="refund-list">
                    <div class="empty-list">Loading…</div>
                </div>
            </div>
        </div>

    </div>
</div>

<div class="toast" id="toast"></div>

<script>
// ── Init ─────────────────────────────────────────────────────
// Auth guard: redirect to login if the readable session cookie is absent
function _hasAuthCookie() {
    return document.cookie.split(";").some(c => c.trim().startsWith("logged_in="));
}
if (!_hasAuthCookie()) { window.location.href = "/"; }

let selectedInvoiceId = null;
let selectedInvoice   = null;
let searchTimer       = null;

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

function toggleMode() {
    const light = document.body.classList.toggle("light");
    localStorage.setItem("refund-theme", light ? "light" : "dark");
    document.getElementById("mode-btn").innerText = light ? "☀️" : "🌙";
}

async function logout() {
    await fetch("/auth/logout", { method: "POST" });
    window.location.href = "/";
}

if (localStorage.getItem("refund-theme") === "light") {
    document.body.classList.add("light");
    document.getElementById("mode-btn").innerText = "☀️";
}

initUser();

// ── Toast ────────────────────────────────────────────────────
let toastTimer = null;
function showToast(msg, type = "") {
    const el = document.getElementById("toast");
    el.textContent = msg;
    el.className   = "toast show" + (type ? " " + type : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
}

// ── Invoice List ─────────────────────────────────────────────
function onSearch() {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(loadInvoices, 280);
}

async function loadInvoices() {
    const q    = encodeURIComponent((document.getElementById("search").value || "").trim());
    const list = document.getElementById("invoice-list");
    try {
        const data = await (await fetch("/refunds/api/invoices?q=" + q)).json();
        document.getElementById("inv-count").innerText = data.length || "0";
        if (!data.length) {
            list.innerHTML = `<div class="empty-list">
                <svg width="28" height="28" fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
                <div>No invoices found</div>
            </div>`;
            return;
        }
        list.innerHTML = data.map(inv => {
            const active = selectedInvoiceId === inv.id;
            const fullyRefunded = inv.refundable_total <= 0.01;
            return `<div class="inv-card ${active ? "active" : ""} ${fullyRefunded ? "fully-refunded" : ""}"
                         onclick="${fullyRefunded ? "" : "selectInvoice(" + inv.id + ")"}">
                <div class="inv-card-num">${inv.invoice_number}</div>
                <div class="inv-card-customer">${inv.customer}</div>
                <div class="inv-card-row" style="margin-top:5px">
                    <span class="inv-card-date">${inv.date.slice(0,10)}</span>
                    ${fullyRefunded
                        ? `<span style="font-size:10px;font-weight:700;color:var(--muted);background:var(--card);border:1px solid var(--border);padding:1px 7px;border-radius:20px">Fully refunded</span>`
                        : `<span class="inv-card-refundable">↩ ${inv.refundable_total.toFixed(2)}</span>`
                    }
                </div>
            </div>`;
        }).join("");
    } catch(e) {
        list.innerHTML = `<div class="empty-list">Failed to load invoices</div>`;
    }
}

// ── Select Invoice ────────────────────────────────────────────
async function selectInvoice(id) {
    selectedInvoiceId = id;
    loadInvoices(); // refresh active state
    document.getElementById("empty-state").style.display   = "none";
    document.getElementById("invoice-detail").style.display = "";
    document.getElementById("builder-status").innerText = "Loading…";
    try {
        const data = await (await fetch("/refunds/api/invoice/" + id)).json();
        if (data.detail) { showToast(data.detail, "error"); return; }
        selectedInvoice = data;
        renderInvoice();
    } catch(e) {
        showToast("Failed to load invoice", "error");
    }
}

function renderInvoice() {
    const inv = selectedInvoice;

    document.getElementById("ib-num").innerText      = inv.invoice_number;
    document.getElementById("ib-customer").innerText = inv.customer;
    document.getElementById("ib-meta").innerText     = inv.date + "  ·  " + inv.payment_method;
    document.getElementById("ib-total").innerText    = inv.total.toFixed(2) + " EGP";
    document.getElementById("builder-status").innerText = "Select items to return";
    document.getElementById("reason").value         = "";
    document.getElementById("refund-method").value  = "cash";

    document.getElementById("items").innerHTML = inv.items.map(item => `
        <div class="item-row">
            <div>
                <div class="item-name">${item.name}</div>
                <div class="item-sku">${item.sku || ""}</div>
            </div>
            <div class="item-qty">${item.sold_qty % 1 === 0 ? item.sold_qty.toFixed(0) : item.sold_qty.toFixed(2)}</div>
            <div class="item-refundable">${item.refundable_qty % 1 === 0 ? item.refundable_qty.toFixed(0) : item.refundable_qty.toFixed(2)}</div>
            <input class="qty-input" type="number"
                min="0" max="${item.refundable_qty}" step="${item.refundable_qty % 1 === 0 ? 1 : 0.001}"
                value="0"
                placeholder="0"
                data-product-id="${item.product_id}"
                data-price="${item.unit_price}"
                data-max="${item.refundable_qty}"
                oninput="onQtyInput(this)"
                ${item.refundable_qty <= 0 ? "disabled style='opacity:.4;cursor:not-allowed'" : ""}>
        </div>
    `).join("");

    recalc();
}

function onQtyInput(el) {
    const val = parseFloat(el.value) || 0;
    const max = parseFloat(el.dataset.max) || 0;
    if (val > max) el.value = max;
    el.classList.toggle("has-value", (parseFloat(el.value) || 0) > 0);
    recalc();
}

function recalc() {
    let total = 0, count = 0;
    document.querySelectorAll("#items .qty-input").forEach(inp => {
        const qty = parseFloat(inp.value) || 0;
        if (qty > 0) {
            total += qty * (parseFloat(inp.dataset.price) || 0);
            count++;
        }
    });
    document.getElementById("summary-count").innerText = count;
    document.getElementById("summary-total").innerText = total.toFixed(2);
    const btn = document.getElementById("submit-btn");
    btn.disabled = count === 0 || !document.getElementById("reason").value.trim();
}

document.addEventListener("input", e => {
    if (e.target.id === "reason") recalc();
});

// ── Submit ────────────────────────────────────────────────────
async function submitRefund() {
    if (!selectedInvoice) return;
    const reason = document.getElementById("reason").value.trim();
    if (!reason) { showToast("Enter a reason for the return", "error"); return; }

    const items = Array.from(document.querySelectorAll("#items .qty-input"))
        .map(el => ({ product_id: parseInt(el.dataset.productId), qty: parseFloat(el.value) || 0 }))
        .filter(it => it.qty > 0);

    if (!items.length) { showToast("Select at least one item to return", "error"); return; }

    const btn = document.getElementById("submit-btn");
    btn.disabled = true;
    btn.innerHTML = `<svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24" style="animation:spin .8s linear infinite"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg> Processing…`;

    try {
        const res  = await fetch("/refunds/api/create", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                invoice_id:    selectedInvoice.id,
                reason,
                refund_method: document.getElementById("refund-method").value,
                items,
            }),
        });
        const data = await res.json();
        if (data.detail) { showToast(data.detail, "error"); return; }
        showToast("✓ " + data.refund_number + " — " + data.amount.toFixed(2) + " EGP refunded", "success");
        window.open("/refunds/print/" + data.id, "_blank");
        await Promise.all([loadRefunds(), selectInvoice(selectedInvoice.id), loadInvoices()]);
    } catch(e) {
        showToast("Failed to create refund", "error");
    } finally {
        btn.disabled = false;
        btn.innerHTML = `<svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg> Issue Refund`;
        recalc();
    }
}

// ── Recent Refunds ────────────────────────────────────────────
async function loadRefunds() {
    const list = document.getElementById("refund-list");
    try {
        const data = await (await fetch("/refunds/api/refunds")).json();
        document.getElementById("refund-count").innerText = data.length || "0";
        if (!data.length) {
            list.innerHTML = `<div class="empty-list">No refunds yet</div>`;
            return;
        }
        list.innerHTML = data.map(r => `
            <div class="refund-card">
                <div class="refund-card-top">
                    <span class="refund-num">${r.refund_number}</span>
                    <a class="refund-print" href="/refunds/print/${r.id}" target="_blank">
                        <svg width="11" height="11" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24">
                            <polyline points="6 9 6 2 18 2 18 9"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/>
                            <rect width="12" height="8" x="6" y="14"/>
                        </svg>
                        Print
                    </a>
                </div>
                <div class="refund-customer">${r.customer}</div>
                <div class="refund-meta">${r.invoice_number}  ·  ${r.created_at.slice(0,10)}</div>
                <div class="refund-bottom">
                    <span class="refund-reason">${r.reason || r.refund_method}</span>
                    <span class="refund-amount">−${r.total.toFixed(2)}</span>
                </div>
            </div>
        `).join("");
    } catch(e) {
        list.innerHTML = `<div class="empty-list">Failed to load</div>`;
    }
}

// ── Spin animation ────────────────────────────────────────────
const style = document.createElement("style");
style.textContent = "@keyframes spin{to{transform:rotate(360deg)}}";
document.head.appendChild(style);

// ── Boot ──────────────────────────────────────────────────────
loadInvoices();
loadRefunds();
</script>
</body>
</html>"""
