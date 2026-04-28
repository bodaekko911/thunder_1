from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import func, select
from typing import Optional, List
from pydantic import BaseModel
from decimal import Decimal
from datetime import date, datetime, time, timedelta, timezone
import re

from app.database import get_async_session
from app.core.permissions import get_current_user, require_action, require_admin, require_permission
from app.core.log import record
from app.core.navigation import render_app_header
from app.core.templates import templates
from app.models.b2b import B2BClient, B2BInvoice, B2BInvoiceItem, Consignment, ConsignmentItem, B2BRefund, B2BRefundItem, B2BClientPrice
from app.models.product import Product
from app.models.inventory import StockMove
from app.models.accounting import Journal, JournalEntry
from app.models.user import User

router = APIRouter(
    prefix="/b2b",
    tags=["B2B"],
    dependencies=[Depends(require_permission("page_b2b"))],
)


# ── Schemas ────────────────────────────────────────────
class ClientCreate(BaseModel):
    name:           str
    contact_person: Optional[str] = None
    phone:          Optional[str] = None
    email:          Optional[str] = None
    address:        Optional[str] = None
    payment_terms:  str = "cash"
    discount_pct:   float = 0
    credit_limit:   float = 0
    notes:          Optional[str] = None

class ClientUpdate(BaseModel):
    name:           Optional[str] = None
    contact_person: Optional[str] = None
    phone:          Optional[str] = None
    email:          Optional[str] = None
    address:        Optional[str] = None
    payment_terms:  Optional[str] = None
    discount_pct:   Optional[float] = None
    credit_limit:   Optional[float] = None
    notes:          Optional[str] = None

class InvoiceItemIn(BaseModel):
    product_id: int
    qty:        float
    unit_price: float

class InvoiceCreate(BaseModel):
    client_id:      int
    invoice_type:   Optional[str] = None
    payment_method: Optional[str] = None
    discount_pct:   float = 0
    notes:          Optional[str] = None
    items:          List[InvoiceItemIn]

class PaymentRecord(BaseModel):
    amount: float
    method: str = "transfer"

class ConsignmentSettle(BaseModel):
    items: List[dict]

class RefundItemIn(BaseModel):
    product_id: int
    qty:        float
    unit_price: float

class ClientRefundCreate(BaseModel):
    client_id: int
    notes:     Optional[str] = None
    items:     List[RefundItemIn]


# ── HELPERS ────────────────────────────────────────────
from app.services.b2b_shared import (
    post_journal      as _post_journal,
    seed_deferred_revenue as _seed_deferred_revenue,
    next_b2b_number   as _next_b2b_number,
    next_cons_number  as _next_cons_number,
)

async def _next_refund_number(db: AsyncSession) -> str:
    _r = await db.execute(select(func.max(B2BRefund.id)))
    max_id = _r.scalar() or 0
    return f"RFD-{str(max_id + 1).zfill(5)}"

def _normalized_client_terms(client: B2BClient) -> str:
    terms = (client.payment_terms or "cash").strip().lower()
    if terms in ("cash", "full_payment", "consignment"):
        return terms
    if terms in ("immediate", "pay_now", "cod"):
        return "cash"
    if terms in ("credit", "net15", "net30", "net60"):
        return "full_payment"
    return "cash"

def _client_discount_pct(client: B2BClient) -> float:
    return float(client.discount_pct or 0)


async def _load_client_payment_activity(
    db: AsyncSession,
    *,
    client_id: int,
    as_of: Optional[date] = None,
):
    payment_ref_types = ("consignment_client_payment", "consignment_payment", "b2b_payment", "b2b_collection")
    stmt = (
        select(Journal)
        .where(Journal.ref_type.in_(payment_ref_types))
        .options(selectinload(Journal.entries).selectinload(JournalEntry.account), selectinload(Journal.user))
        .order_by(Journal.created_at)
    )
    if as_of:
        stmt = stmt.where(Journal.created_at < datetime.combine(as_of + timedelta(days=1), time.min, tzinfo=timezone.utc))
    payment_result = await db.execute(stmt)
    journals = payment_result.scalars().all()

    invoice_result = await db.execute(
        select(B2BInvoice)
        .where(B2BInvoice.client_id == client_id)
        .options(selectinload(B2BInvoice.client))
    )
    invoices = invoice_result.scalars().all()
    invoice_by_id = {invoice.id: invoice for invoice in invoices}
    invoice_by_number = {str(invoice.invoice_number or "").upper(): invoice for invoice in invoices}
    invoice_pattern = re.compile(r"(B2B-\d{5,})", re.IGNORECASE)

    records = []
    for journal in journals:
        matched_invoice = None
        if journal.ref_type == "consignment_client_payment":
            if journal.ref_id != client_id:
                continue
        else:
            if journal.ref_id and journal.ref_id in invoice_by_id:
                matched_invoice = invoice_by_id[journal.ref_id]
            else:
                match = invoice_pattern.search(journal.description or "")
                if match:
                    matched_invoice = invoice_by_number.get(match.group(1).upper())
            if not matched_invoice or matched_invoice.client_id != client_id:
                continue

        amount = 0.0
        for entry in journal.entries:
            if entry.account and entry.account.code == "1000" and float(entry.debit or 0) > 0:
                amount = float(entry.debit or 0)
                break
        if amount <= 0:
            amount = max((float(entry.debit or 0) for entry in journal.entries), default=0.0)
        if amount <= 0:
            continue

        reference = f"PAY-{journal.id}"
        if matched_invoice and matched_invoice.invoice_number:
            reference = matched_invoice.invoice_number

        records.append({
            "date": journal.created_at,
            "date_str": journal.created_at.strftime("%d-%b-%Y") if journal.created_at else "—",
            "ref": reference,
            "type": "payment",
            "desc": journal.description or "Client payment",
            "amount": round(amount, 2),
            "ref_type": journal.ref_type or "payment",
            "user_name": journal.user.name if journal.user else "—",
        })
    return records

async def _reverse_invoice_stock(invoice, db: AsyncSession):
    for item in invoice.items:
        _r = await db.execute(select(Product).where(Product.id == item.product_id))
        product = _r.scalar_one_or_none()
        if product:
            before = float(product.stock)
            after  = before + float(item.qty)
            product.stock = after
            db.add(StockMove(
                product_id=product.id, type="in",
                qty=float(item.qty), qty_before=before, qty_after=after,
                ref_type="b2b_reversal", ref_id=invoice.id,
                note=f"Edit/Delete reversal — {invoice.invoice_number}",
            ))

async def _reverse_invoice_journal(invoice, db: AsyncSession):
    total = float(invoice.total)
    if invoice.invoice_type == "cash":
        # Reverse: debit Revenue, credit Cash
        await _post_journal(db, f"Reversal — {invoice.invoice_number}", "b2b_reversal", [
            ("1000", 0, total),
            ("4000", total, 0),
        ])
    elif invoice.invoice_type in ("full_payment", "consignment"):
        # Reverse: debit Deferred Revenue, credit AR
        await _post_journal(db, f"Reversal — {invoice.invoice_number}", "b2b_reversal", [
            ("2200", total, 0),   # Debit Deferred Revenue (reversal)
            ("1100", 0, total),   # Credit AR
        ])
        client = invoice.client
        # Only subtract the UNPAID portion — the paid portion was already removed
        # from client.outstanding when payment was collected, so subtracting total
        # again would double-reverse it.
        unpaid = max(0.0, total - float(invoice.amount_paid))
        if unpaid > 0:
            client.outstanding = Decimal(str(max(0, float(client.outstanding) - unpaid)))


async def _reverse_refund_effects(refund, db: AsyncSession, current_user: User):
    for item in refund.items:
        product = item.product
        if not product:
            continue
        before = float(product.stock)
        after = before - float(item.qty)
        if after < -0.001:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot delete refund {refund.refund_number}: stock for {product.name} would become negative",
            )
        after = max(0.0, after)
        product.stock = after
        db.add(StockMove(
            product_id=product.id,
            type="out",
            qty=float(item.qty),
            user_id=current_user.id,
            qty_before=before,
            qty_after=after,
            ref_type="b2b_refund_delete",
            ref_id=refund.id,
            note=f"Delete refund â€” {refund.refund_number}",
        ))

    if refund.client:
        refund.client.outstanding = Decimal(str(float(refund.client.outstanding) + float(refund.total)))

    await _post_journal(
        db,
        f"Delete refund â€” {refund.refund_number}",
        "b2b_refund_delete",
        [
            ("2200", 0, float(refund.total)),
            ("1100", float(refund.total), 0),
        ],
        user_id=current_user.id,
    )


# ── SEED DEFERRED REVENUE ──────────────────────────────
@router.post("/api/seed-accounts")
async def seed_accounts(db: AsyncSession = Depends(get_async_session)):
    await _seed_deferred_revenue(db)
    return {"ok": True}


# ── CLIENT API ─────────────────────────────────────────
@router.get("/api/clients")
async def get_clients(q: str = "", db: AsyncSession = Depends(get_async_session)):
    # Compute outstanding live from invoice data so it always matches the invoices tab
    outstanding_sub = (
        select(
            B2BInvoice.client_id,
            func.coalesce(
                func.sum(B2BInvoice.total - B2BInvoice.amount_paid), 0
            ).label("outstanding"),
        )
        .where(B2BInvoice.status.in_(["unpaid", "partial"]))
        .group_by(B2BInvoice.client_id)
        .subquery()
    )
    stmt = (
        select(B2BClient, func.coalesce(outstanding_sub.c.outstanding, 0).label("computed_outstanding"))
        .outerjoin(outstanding_sub, outstanding_sub.c.client_id == B2BClient.id)
        .where(B2BClient.is_active == True)
        .options(selectinload(B2BClient.invoices))
        .order_by(B2BClient.name)
    )
    if q:
        stmt = stmt.where(
            B2BClient.name.ilike(f"%{q}%") |
            B2BClient.phone.ilike(f"%{q}%")
        )
    _r = await db.execute(stmt)
    rows = _r.all()
    return [
        {
            "id":             c.id,
            "name":           c.name,
            "contact_person": c.contact_person or "—",
            "phone":          c.phone or "—",
            "email":          c.email or "—",
            "address":        c.address or "—",
            "payment_terms":  c.payment_terms,
            "discount_pct":   float(c.discount_pct or 0),
            "credit_limit":   float(c.credit_limit or 0),
            "outstanding":    float(computed_outstanding or 0),
            "notes":          c.notes or "",
            "invoice_count":  len(c.invoices),
        }
        for c, computed_outstanding in rows
    ]

@router.post("/api/clients", dependencies=[Depends(require_action("b2b", "clients", "create_client"))])
async def create_client(data: ClientCreate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    c = B2BClient(
        name=data.name, contact_person=data.contact_person,
        phone=data.phone, email=data.email, address=data.address,
        payment_terms=data.payment_terms,
        discount_pct=data.discount_pct,
        credit_limit=data.credit_limit,
        notes=data.notes,
    )
    db.add(c); await db.commit(); await db.refresh(c)
    return {"id": c.id, "name": c.name}

@router.put("/api/clients/{client_id}", dependencies=[Depends(require_action("b2b", "clients", "update_client"))])
async def update_client(client_id: int, data: ClientUpdate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    _r = await db.execute(select(B2BClient).where(B2BClient.id == client_id))
    c = _r.scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="Client not found")
    if data.name is not None:           c.name           = data.name
    if data.contact_person is not None: c.contact_person = data.contact_person
    if data.phone is not None:          c.phone          = data.phone
    if data.email is not None:          c.email          = data.email
    if data.address is not None:        c.address        = data.address
    if data.payment_terms is not None:  c.payment_terms  = data.payment_terms
    if data.discount_pct is not None:   c.discount_pct   = data.discount_pct
    if data.credit_limit is not None:   c.credit_limit   = data.credit_limit
    if data.notes is not None:          c.notes          = data.notes
    await db.commit()
    return {"ok": True}

@router.delete("/api/clients/{client_id}", dependencies=[Depends(require_action("b2b", "clients", "delete_client"))])
async def delete_client(client_id: int, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    _r = await db.execute(select(B2BClient).where(B2BClient.id == client_id))
    c = _r.scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="Client not found")
    c.is_active = False
    await db.commit()
    return {"ok": True}


# ── INVOICE API ────────────────────────────────────────
@router.get("/api/invoices")
async def get_invoices(client_id: int = None, skip: int = 0, limit: int = 100, db: AsyncSession = Depends(get_async_session)):
    where = []
    if client_id:
        where.append(B2BInvoice.client_id == client_id)
    cnt_r = await db.execute(select(func.count()).select_from(B2BInvoice).where(*where))
    total = cnt_r.scalar()
    inv_r = await db.execute(
        select(B2BInvoice)
        .where(*where)
        .options(
            selectinload(B2BInvoice.client),
            selectinload(B2BInvoice.items).selectinload(B2BInvoiceItem.product),
        )
        .order_by(B2BInvoice.created_at.desc()).offset(skip).limit(limit)
    )
    invoices = inv_r.scalars().all()
    return {
        "total": total,
        "invoices": [
            {
                "id":             i.id,
                "invoice_number": i.invoice_number,
                "client":         i.client.name if i.client else "—",
                "client_id":      i.client_id,
                "invoice_type":   i.invoice_type,
                "status":         i.status,
                "payment_method": i.payment_method or "—",
                "subtotal":       float(i.subtotal),
                "discount":       float(i.discount),
                "total":          float(i.total),
                "amount_paid":    float(i.amount_paid),
                "balance_due":    float(i.total) - float(i.amount_paid),
                "discount_pct":   round(float(i.discount) / float(i.subtotal) * 100, 1) if float(i.subtotal) > 0 else 0,
                "notes":          i.notes or "",
                "created_at":     i.created_at.strftime("%Y-%m-%d %H:%M") if i.created_at else "—",
                "items": [
                    {
                        "product":    item.product.name if item.product else "—",
                        "product_id": item.product_id,
                        "qty":        float(item.qty),
                        "unit_price": float(item.unit_price),
                        "total":      float(item.total),
                    }
                    for item in i.items
                ],
            }
            for i in invoices
        ],
    }

@router.post("/api/invoices", dependencies=[Depends(require_action("b2b", "invoices", "create"))])
async def create_invoice(data: InvoiceCreate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    await _seed_deferred_revenue(db)

    _r = await db.execute(select(B2BClient).where(B2BClient.id == data.client_id))
    client = _r.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    if not data.items:
        raise HTTPException(status_code=400, detail="Invoice must have at least one item")

    for item in data.items:
        _r = await db.execute(select(Product).where(Product.id == item.product_id))
        product = _r.scalar_one_or_none()
        if not product:
            raise HTTPException(status_code=404, detail=f"Product not found: {item.product_id}")
        if float(product.stock) < item.qty:
            raise HTTPException(status_code=400,
                detail=f"Not enough stock for '{product.name}'. Available: {float(product.stock)}")

    invoice_type    = _normalized_client_terms(client)
    discount_pct    = _client_discount_pct(client)
    subtotal        = sum(i.qty * i.unit_price for i in data.items)
    discount_amount = round(subtotal * (discount_pct / 100), 2)
    total           = round(subtotal - discount_amount, 2)
    invoice_number  = await _next_b2b_number(db)
    status = "paid" if invoice_type == "cash" else "unpaid"

    invoice = B2BInvoice(
        invoice_number=invoice_number, client_id=data.client_id,
        user_id=current_user.id,
        invoice_type=invoice_type,
        status=status,
        payment_method=invoice_type,
        subtotal=round(subtotal, 2), discount=discount_amount,
        total=total,
        amount_paid=total if invoice_type == "cash" else 0,
        notes=data.notes,
    )
    db.add(invoice); await db.flush()

    for item in data.items:
        _r = await db.execute(select(Product).where(Product.id == item.product_id))
        product = _r.scalar_one_or_none()
        db.add(B2BInvoiceItem(
            invoice_id=invoice.id, product_id=product.id,
            qty=item.qty, unit_price=item.unit_price,
            total=round(item.qty * item.unit_price, 2),
        ))
        before = float(product.stock); after = before - item.qty
        product.stock = after
        db.add(StockMove(
            product_id=product.id, type="out", qty=-item.qty,
            user_id=current_user.id,
            qty_before=before, qty_after=after,
            ref_type="b2b", ref_id=invoice.id,
            note=f"B2B {invoice_number} ({invoice_type})",
        ))

    # ── ACCOUNTING ──────────────────────────────────────
    if invoice_type == "cash":
        await _post_journal(db, f"B2B Cash Sale - {invoice_number}", "b2b", [
            ("1000", total, 0),
            ("4000", 0, total),
        ], user_id=current_user.id)

    elif invoice_type == "full_payment":
        await _post_journal(db, f"B2B Full Payment Invoice - {invoice_number}", "b2b", [
            ("1100", total, 0),
            ("2200", 0, total),
        ], user_id=current_user.id)
        client.outstanding = Decimal(str(float(client.outstanding) + total))

    elif invoice_type == "consignment":
        await _post_journal(db, f"B2B Consignment Invoice - {invoice_number}", "b2b", [
            ("1100", total, 0),
            ("2200", 0, total),
        ], user_id=current_user.id)
        client.outstanding = Decimal(str(float(client.outstanding) + total))

        cons_ref = await _next_cons_number(db)
        consignment = Consignment(
            ref_number=cons_ref, client_id=data.client_id,
            invoice_id=invoice.id, user_id=current_user.id, status="active", notes=data.notes,
        )
        db.add(consignment); await db.flush()
        for item in data.items:
            db.add(ConsignmentItem(
                consignment_id=consignment.id, product_id=item.product_id,
                qty_sent=item.qty, qty_sold=0, qty_returned=0,
                unit_price=item.unit_price,
            ))

    record(db, "B2B", "create_invoice",
           f"B2B invoice {invoice_number} — {client.name} — {total:.2f} — {invoice_type}",
           user=current_user, ref_type="b2b_invoice", ref_id=invoice.id)
    await db.commit(); await db.refresh(invoice)
    return {"id": invoice.id, "invoice_number": invoice_number, "total": total}


@router.put("/api/invoices/{invoice_id}", dependencies=[Depends(require_action("b2b", "invoices", "update"))])
async def edit_invoice(invoice_id: int, data: InvoiceCreate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    _r = await db.execute(
        select(B2BInvoice)
        .where(B2BInvoice.id == invoice_id)
        .options(
            selectinload(B2BInvoice.items),
            selectinload(B2BInvoice.client),
        )
    )
    invoice = _r.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.status == "paid" and float(invoice.amount_paid) > 0 and invoice.invoice_type == "cash":
        raise HTTPException(status_code=400, detail="Cannot edit a paid cash invoice.")
    if invoice.invoice_type == "consignment":
        cons_r = await db.execute(
            select(Consignment).where(Consignment.invoice_id == invoice_id)
            .options(selectinload(Consignment.items))
        )
        cons_chk = cons_r.scalar_one_or_none()
        if cons_chk and any(float(ci.qty_sold) > 0 for ci in cons_chk.items):
            raise HTTPException(status_code=400, detail="Cannot edit a consignment that has sales recorded.")

    await _reverse_invoice_stock(invoice, db)
    await _reverse_invoice_journal(invoice, db)

    for item in invoice.items:
        await db.delete(item)
    old_cons_r = await db.execute(
        select(Consignment).where(Consignment.invoice_id == invoice_id)
        .options(selectinload(Consignment.items))
    )
    old_cons = old_cons_r.scalar_one_or_none()
    if old_cons:
        for ci in old_cons.items:
            await db.delete(ci)
        await db.delete(old_cons)

    for item in data.items:
        _r = await db.execute(select(Product).where(Product.id == item.product_id))
        product = _r.scalar_one_or_none()
        if not product:
            raise HTTPException(status_code=404, detail=f"Product not found: {item.product_id}")
        if float(product.stock) < item.qty:
            raise HTTPException(status_code=400,
                detail=f"Not enough stock for '{product.name}'. Available: {float(product.stock)}")

    _r = await db.execute(select(B2BClient).where(B2BClient.id == data.client_id))
    client = _r.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    invoice_type    = _normalized_client_terms(client)
    discount_pct    = _client_discount_pct(client)
    subtotal        = sum(i.qty * i.unit_price for i in data.items)
    discount_amount = round(subtotal * (discount_pct / 100), 2)
    total           = round(subtotal - discount_amount, 2)

    invoice.client_id      = data.client_id
    invoice.user_id        = current_user.id
    invoice.invoice_type   = invoice_type
    invoice.payment_method = invoice_type
    invoice.subtotal       = round(subtotal, 2)
    invoice.discount       = discount_amount
    invoice.total          = total
    invoice.amount_paid    = total if invoice_type == "cash" else 0
    invoice.status         = "paid" if invoice_type == "cash" else "unpaid"
    invoice.notes          = data.notes

    for item in data.items:
        _r = await db.execute(select(Product).where(Product.id == item.product_id))
        product = _r.scalar_one_or_none()
        db.add(B2BInvoiceItem(
            invoice_id=invoice.id, product_id=product.id,
            qty=item.qty, unit_price=item.unit_price,
            total=round(item.qty * item.unit_price, 2),
        ))
        before = float(product.stock); after = before - item.qty
        product.stock = after
        db.add(StockMove(
            product_id=product.id, type="out", qty=-item.qty,
            user_id=current_user.id,
            qty_before=before, qty_after=after,
            ref_type="b2b", ref_id=invoice.id,
            note=f"B2B {invoice.invoice_number} (edited)",
        ))

    if invoice_type == "cash":
        await _post_journal(db, f"B2B Cash Sale (edited) - {invoice.invoice_number}", "b2b", [
            ("1000", total, 0),
            ("4000", 0, total),
        ], user_id=current_user.id)
    elif invoice_type in ("full_payment", "consignment"):
        await _post_journal(db, f"B2B {invoice_type} Invoice (edited) - {invoice.invoice_number}", "b2b", [
            ("1100", total, 0),
            ("2200", 0, total),
        ], user_id=current_user.id)
        if client:
            client.outstanding = Decimal(str(float(client.outstanding) + total))
        if invoice_type == "consignment":
            cons_ref = await _next_cons_number(db)
            consignment = Consignment(
                ref_number=cons_ref, client_id=data.client_id,
                invoice_id=invoice.id, user_id=current_user.id, status="active", notes=data.notes,
            )
            db.add(consignment); await db.flush()
            for item in data.items:
                db.add(ConsignmentItem(
                    consignment_id=consignment.id, product_id=item.product_id,
                    qty_sent=item.qty, qty_sold=0, qty_returned=0,
                    unit_price=item.unit_price,
                ))

    record(db, "B2B", "edit_invoice",
           f"Edited B2B invoice {invoice.invoice_number} — {total:.2f}",
           user=current_user, ref_type="b2b_invoice", ref_id=invoice_id)
    await db.commit()
    return {"ok": True, "invoice_number": invoice.invoice_number, "total": total}


@router.delete("/api/invoices/{invoice_id}", dependencies=[Depends(require_action("b2b", "invoices", "delete"))])
async def delete_invoice(invoice_id: int, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    _r = await db.execute(
        select(B2BInvoice)
        .where(B2BInvoice.id == invoice_id)
        .options(
            selectinload(B2BInvoice.items),
            selectinload(B2BInvoice.client),
        )
    )
    invoice = _r.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    inv_num = invoice.invoice_number
    await _reverse_invoice_stock(invoice, db)
    await _reverse_invoice_journal(invoice, db)
    cons_r = await db.execute(
        select(Consignment).where(Consignment.invoice_id == invoice_id)
        .options(selectinload(Consignment.items))
    )
    cons = cons_r.scalar_one_or_none()
    if cons:
        for ci in cons.items:
            await db.delete(ci)
        await db.delete(cons)
    await db.delete(invoice)
    record(db, "B2B", "delete_invoice",
           f"Deleted B2B invoice {inv_num} — stock and journal reversed",
           ref_type="b2b_invoice", ref_id=invoice_id)
    await db.commit()
    return {"ok": True}


@router.post("/api/invoices/{invoice_id}/pay", dependencies=[Depends(require_action("b2b", "invoices", "approve"))])
async def record_payment(invoice_id: int, data: PaymentRecord, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    """
    Collect payment on a full_payment invoice.
    Moves amount from Deferred Revenue → Sales Revenue, and Cash ← AR.
    """
    _r = await db.execute(
        select(B2BInvoice)
        .where(B2BInvoice.id == invoice_id)
        .options(selectinload(B2BInvoice.client))
    )
    invoice = _r.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    balance = float(invoice.total) - float(invoice.amount_paid)
    if data.amount > balance + 0.01:
        raise HTTPException(status_code=400, detail=f"Amount exceeds balance: {balance:.2f}")

    amount = round(data.amount, 2)
    invoice.amount_paid = Decimal(str(float(invoice.amount_paid) + amount))
    invoice.status = "paid" if float(invoice.amount_paid) >= float(invoice.total) else "partial"

    client = invoice.client
    client.outstanding = Decimal(str(max(0, float(client.outstanding) - amount)))

    await _post_journal(db, f"Payment received - {invoice.invoice_number}", "b2b_payment", [
        ("1000", amount, 0),
        ("1100", 0, amount),
        ("2200", amount, 0),
        ("4000", 0, amount),
    ], user_id=current_user.id)

    await db.commit()
    return {"ok": True, "status": invoice.status}


# ── CONSIGNMENT API ────────────────────────────────────
@router.get("/api/consignments")
async def get_consignments(db: AsyncSession = Depends(get_async_session)):
    _r = await db.execute(
        select(Consignment)
        .options(
            selectinload(Consignment.client),
            selectinload(Consignment.items).selectinload(ConsignmentItem.product),
        )
        .order_by(Consignment.created_at.desc())
    )
    conses = _r.scalars().all()
    return [
        {
            "id":         c.id,
            "ref_number": c.ref_number,
            "client":     c.client.name if c.client else "—",
            "client_id":  c.client_id,
            "status":     c.status,
            "created_at": c.created_at.strftime("%Y-%m-%d") if c.created_at else "—",
            "notes":      c.notes or "",
            "items": [
                {
                    "id":           ci.id,
                    "product":      ci.product.name if ci.product else "—",
                    "product_id":   ci.product_id,
                    "unit_price":   float(ci.unit_price),
                    "qty_sent":     float(ci.qty_sent),
                    "qty_sold":     float(ci.qty_sold),
                    "qty_returned": float(ci.qty_returned),
                    "qty_pending":  float(ci.qty_sent) - float(ci.qty_sold) - float(ci.qty_returned),
                    "revenue":      float(ci.qty_sold) * float(ci.unit_price),
                }
                for ci in c.items
            ],
            "total_sent":     sum(float(ci.qty_sent)     for ci in c.items),
            "total_sold":     sum(float(ci.qty_sold)     for ci in c.items),
            "total_returned": sum(float(ci.qty_returned) for ci in c.items),
            "total_revenue":  sum(float(ci.qty_sold) * float(ci.unit_price) for ci in c.items),
        }
        for c in conses
    ]

@router.post("/api/consignments/{cons_id}/settle", dependencies=[Depends(require_action("b2b", "invoices", "settle"))])
async def settle_consignment(cons_id: int, data: ConsignmentSettle, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    """
    Settle consignment — for each qty sold, move from Deferred Revenue → Sales Revenue.
    Returned items restore stock.
    """
    _r = await db.execute(
        select(Consignment)
        .where(Consignment.id == cons_id)
        .options(
            selectinload(Consignment.items).selectinload(ConsignmentItem.product),
            selectinload(Consignment.client),
        )
    )
    cons = _r.scalar_one_or_none()
    if not cons:
        raise HTTPException(status_code=404, detail="Consignment not found")
    if cons.status == "closed":
        raise HTTPException(status_code=400, detail="Consignment already closed")

    total_revenue = 0
    for entry in data.items:
        ci_r = await db.execute(
            select(ConsignmentItem)
            .where(ConsignmentItem.id == entry["consignment_item_id"])
            .options(selectinload(ConsignmentItem.product))
        )
        ci = ci_r.scalar_one_or_none()
        if not ci: continue
        qty_sold     = float(entry.get("qty_sold", 0))
        qty_returned = float(entry.get("qty_returned", 0))
        pending      = float(ci.qty_sent) - float(ci.qty_sold) - float(ci.qty_returned)
        if qty_sold + qty_returned > pending + 0.001:
            raise HTTPException(status_code=400,
                detail=f"Total exceeds pending for {ci.product.name}. Pending: {pending:.2f}")
        ci.qty_sold     = Decimal(str(float(ci.qty_sold)     + qty_sold))
        ci.qty_returned = Decimal(str(float(ci.qty_returned) + qty_returned))
        if qty_returned > 0:
            product = ci.product
            before  = float(product.stock); after = before + qty_returned
            product.stock = after
            db.add(StockMove(
                product_id=product.id, type="in",
                user_id=current_user.id,
                qty=qty_returned, qty_before=before, qty_after=after,
                ref_type="consignment_return", ref_id=cons.id,
                note=f"Returned from {cons.ref_number}",
            ))
        total_revenue += qty_sold * float(ci.unit_price)

    if total_revenue > 0:
        amount = round(total_revenue, 2)
        # Deferred Revenue → Sales Revenue (earned on settlement)
        # Cash ← AR (client paid for what they sold)
        await _post_journal(db, f"Consignment settlement - {cons.ref_number}", "consignment_settlement", [
            ("1000", amount, 0),
            ("1100", 0, amount),
            ("2200", amount, 0),
            ("4000", 0, amount),
        ], user_id=current_user.id)
        cons.client.outstanding = Decimal(str(max(0, float(cons.client.outstanding) - amount)))

    all_done = all(
        float(ci.qty_sold) + float(ci.qty_returned) >= float(ci.qty_sent)
        for ci in cons.items
    )
    cons.status = "closed" if all_done else "active"
    if all_done:
        cons.settled_at = datetime.utcnow()

    await db.commit()
    return {"ok": True, "total_revenue": round(total_revenue, 2), "status": cons.status}


class ConsignmentPayment(BaseModel):
    amount:      float
    month_label: Optional[str] = None
    notes:       Optional[str] = None

@router.post("/api/invoices/{invoice_id}/consignment-payment", dependencies=[Depends(require_action("b2b", "invoices", "approve"))])
async def consignment_payment(invoice_id: int, data: ConsignmentPayment, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    """
    Record a cash payment from a consignment client.
    Moves amount: Deferred Revenue → Sales Revenue, Cash ← AR.
    """
    _r = await db.execute(
        select(B2BInvoice)
        .where(B2BInvoice.id == invoice_id)
        .options(selectinload(B2BInvoice.client))
    )
    invoice = _r.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.invoice_type != "consignment":
        raise HTTPException(status_code=400, detail="This endpoint is for consignment invoices only")

    amount = round(data.amount, 2)
    balance = float(invoice.total) - float(invoice.amount_paid)
    if amount > balance + 0.01:
        raise HTTPException(status_code=400, detail=f"Amount exceeds remaining balance: {balance:.2f}")

    invoice.amount_paid = Decimal(str(float(invoice.amount_paid) + amount))
    if float(invoice.amount_paid) >= float(invoice.total):
        invoice.status = "paid"

    client = invoice.client
    client.outstanding = Decimal(str(max(0, float(client.outstanding) - amount)))

    note = f"Consignment payment - {invoice.invoice_number}"
    if data.month_label:
        note += f" - {data.month_label}"

    await _post_journal(db, note, "consignment_payment", [
        ("1000", amount, 0),
        ("1100", 0, amount),
        ("2200", amount, 0),
        ("4000", 0, amount),
    ], user_id=current_user.id)

    await db.commit()
    return {"ok": True, "invoice_number": invoice.invoice_number, "amount": amount, "status": invoice.status}

@router.post("/api/refunds", dependencies=[Depends(require_action("b2b", "invoices", "refund"))])
async def create_client_refund(data: ClientRefundCreate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    _r = await db.execute(select(B2BClient).where(B2BClient.id == data.client_id, B2BClient.is_active == True))
    client = _r.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    if not data.items:
        raise HTTPException(status_code=400, detail="Refund must have at least one item")

    refund_number = await _next_refund_number(db)
    subtotal = 0.0
    for item in data.items:
        _r = await db.execute(select(Product).where(Product.id == item.product_id))
        product = _r.scalar_one_or_none()
        if not product:
            raise HTTPException(status_code=404, detail=f"Product not found: {item.product_id}")
        if item.qty <= 0:
            raise HTTPException(status_code=400, detail="Refund quantities must be greater than 0")
        if item.unit_price < 0:
            raise HTTPException(status_code=400, detail="Unit price cannot be negative")
        subtotal += round(item.qty * item.unit_price, 2)

    subtotal = round(subtotal, 2)
    discount_pct = _client_discount_pct(client)
    discount = round(subtotal * (discount_pct / 100), 2)
    total = round(subtotal - discount, 2)
    if total <= 0:
        raise HTTPException(status_code=400, detail="Refund total must be greater than 0")
    if total > float(client.outstanding) + 0.01:
        raise HTTPException(status_code=400, detail=f"Refund exceeds client outstanding: {float(client.outstanding):.2f}")

    refund = B2BRefund(
        refund_number=refund_number,
        client_id=client.id,
        user_id=current_user.id,
        subtotal=subtotal,
        discount=discount,
        total=total,
        notes=(data.notes or "").strip() or None,
    )
    db.add(refund); await db.flush()

    for item in data.items:
        _r = await db.execute(select(Product).where(Product.id == item.product_id))
        product = _r.scalar_one_or_none()
        line_total = round(item.qty * item.unit_price, 2)
        db.add(B2BRefundItem(
            refund_id=refund.id,
            product_id=product.id,
            qty=item.qty,
            unit_price=item.unit_price,
            total=line_total,
        ))
        before = float(product.stock)
        after  = before + item.qty
        product.stock = after
        db.add(StockMove(
            product_id=product.id, type="in", qty=float(item.qty),
            user_id=current_user.id,
            qty_before=before, qty_after=after,
            ref_type="b2b_refund", ref_id=refund.id,
            note=f"B2B refund {refund_number} - {client.name}",
        ))

    client.outstanding = Decimal(str(max(0, float(client.outstanding) - total)))

    note = (data.notes or "").strip()
    desc = f"B2B client refund - {refund_number} - {client.name}"
    if note:
        desc += f" - {note}"
    await _post_journal(db, desc, "b2b_refund", [
        ("2200", total, 0),
        ("1100", 0, total),
    ], user_id=current_user.id)

    await db.commit()
    return {
        "ok": True,
        "refund_id": refund.id,
        "refund_number": refund_number,
        "client": client.name,
        "subtotal": subtotal,
        "discount": discount,
        "discount_pct": discount_pct,
        "amount": total,
        "outstanding": float(client.outstanding),
    }


# ── STATS ──────────────────────────────────────────────
@router.get("/api/stats")
async def get_stats(db: AsyncSession = Depends(get_async_session)):
    r1 = await db.execute(select(func.count(B2BClient.id)).where(B2BClient.is_active == True))
    r2 = await db.execute(
        select(func.sum(B2BInvoice.total - B2BInvoice.amount_paid))
        .where(B2BInvoice.status.in_(["unpaid", "partial"]))
    )
    r3 = await db.execute(
        select(func.count(B2BInvoice.id))
        .where(B2BInvoice.status.in_(["unpaid", "partial"]))
    )
    r4 = await db.execute(select(func.count(Consignment.id)).where(Consignment.status == "active"))
    return {
        "total_clients":     r1.scalar() or 0,
        "total_outstanding": float(r2.scalar() or 0),
        "unpaid_invoices":   r3.scalar() or 0,
        "active_consign":    r4.scalar() or 0,
    }

@router.get("/api/products-list")
async def products_list(client_id: int = None, db: AsyncSession = Depends(get_async_session)):
    _r = await db.execute(select(Product).where(Product.is_active == True).order_by(Product.name))
    products = _r.scalars().all()
    custom = {}
    if client_id:
        cp_r = await db.execute(select(B2BClientPrice).where(B2BClientPrice.client_id == client_id))
        for cp in cp_r.scalars().all():
            custom[cp.product_id] = float(cp.price)
    return [
        {
            "id":            p.id,
            "name":          p.name,
            "sku":           p.sku,
            "price":         custom.get(p.id, float(p.price)),
            "default_price": float(p.price),
            "has_custom":    p.id in custom,
            "stock":         float(p.stock),
            "unit":          p.unit,
        }
        for p in products
    ]


# ── CLIENT PRICE LIST API ──────────────────────────────
@router.get("/api/clients/{client_id}/prices")
async def get_client_prices(client_id: int, db: AsyncSession = Depends(get_async_session)):
    _r = await db.execute(select(B2BClient).where(B2BClient.id == client_id))
    client = _r.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    cp_r = await db.execute(
        select(B2BClientPrice)
        .where(B2BClientPrice.client_id == client_id)
        .options(selectinload(B2BClientPrice.product))
    )
    prices = cp_r.scalars().all()
    return [
        {
            "id":            cp.id,
            "product_id":    cp.product_id,
            "product_name":  cp.product.name if cp.product else "—",
            "sku":           cp.product.sku  if cp.product else "—",
            "custom_price":  float(cp.price),
            "default_price": float(cp.product.price) if cp.product else 0,
        }
        for cp in prices
    ]


class ClientPriceUpsert(BaseModel):
    product_id: int
    price:      float

@router.put("/api/clients/{client_id}/prices")
async def upsert_client_price(client_id: int, data: ClientPriceUpsert,
                               db: AsyncSession = Depends(get_async_session),
                               current_user: User = Depends(get_current_user)):
    _r = await db.execute(select(B2BClient).where(B2BClient.id == client_id))
    client = _r.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    if data.price < 0:
        raise HTTPException(status_code=400, detail="Price must be >= 0")
    cp_r = await db.execute(select(B2BClientPrice).where(
        B2BClientPrice.client_id == client_id,
        B2BClientPrice.product_id == data.product_id,
    ))
    cp = cp_r.scalar_one_or_none()
    if cp:
        cp.price = data.price
    else:
        db.add(B2BClientPrice(client_id=client_id, product_id=data.product_id, price=data.price))
    await db.commit()
    return {"ok": True}


@router.delete("/api/clients/{client_id}/prices/{product_id}")
async def delete_client_price(client_id: int, product_id: int,
                               db: AsyncSession = Depends(get_async_session),
                               current_user: User = Depends(get_current_user)):
    cp_r = await db.execute(select(B2BClientPrice).where(
        B2BClientPrice.client_id == client_id,
        B2BClientPrice.product_id == product_id,
    ))
    cp = cp_r.scalar_one_or_none()
    if cp:
        await db.delete(cp)
        await db.commit()
    return {"ok": True}

@router.get("/api/refund-products/{client_id}")
async def refund_products(client_id: int, db: AsyncSession = Depends(get_async_session)):
    _r = await db.execute(select(B2BClient).where(B2BClient.id == client_id, B2BClient.is_active == True))
    client = _r.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    latest_prices = {}
    items_r = await db.execute(
        select(B2BInvoiceItem, B2BInvoice)
        .join(B2BInvoice, B2BInvoice.id == B2BInvoiceItem.invoice_id)
        .where(B2BInvoice.client_id == client_id)
        .order_by(B2BInvoice.created_at.desc(), B2BInvoice.id.desc(), B2BInvoiceItem.id.desc())
    )
    for item, _inv in items_r.all():
        if item.product_id not in latest_prices:
            latest_prices[item.product_id] = float(item.unit_price)

    prod_r = await db.execute(select(Product).where(Product.is_active == True).order_by(Product.name))
    products = prod_r.scalars().all()
    return [
        {
            "id":    p.id,
            "name":  p.name,
            "sku":   p.sku,
            "price": latest_prices.get(p.id, float(p.price)),
            "stock": float(p.stock),
            "unit":  p.unit,
        }
        for p in products
    ]

@router.get("/api/refunds")
async def get_refunds(client_id: int = None, db: AsyncSession = Depends(get_async_session)):
    stmt = (
        select(B2BRefund)
        .options(
            selectinload(B2BRefund.client),
            selectinload(B2BRefund.items).selectinload(B2BRefundItem.product),
        )
        .order_by(B2BRefund.created_at.desc(), B2BRefund.id.desc())
    )
    if client_id:
        stmt = stmt.where(B2BRefund.client_id == client_id)
    _r = await db.execute(stmt)
    refunds = _r.scalars().all()
    return [
        {
            "id":           r.id,
            "refund_number": r.refund_number,
            "client":       r.client.name if r.client else "—",
            "client_id":    r.client_id,
            "subtotal":     float(r.subtotal),
            "discount":     float(r.discount),
            "discount_pct": round(float(r.discount) / float(r.subtotal) * 100, 1) if float(r.subtotal) > 0 else 0,
            "total":        float(r.total),
            "notes":        r.notes or "",
            "created_at":   r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "—",
            "items": [
                {
                    "product":    item.product.name if item.product else "—",
                    "sku":        item.product.sku if item.product else "—",
                    "qty":        float(item.qty),
                    "unit_price": float(item.unit_price),
                    "total":      float(item.total),
                }
                for item in r.items
            ],
        }
        for r in refunds
    ]


@router.delete("/api/refunds/{refund_id}", dependencies=[Depends(require_admin)])
async def delete_refund(
    refund_id: int,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    _r = await db.execute(
        select(B2BRefund)
        .where(B2BRefund.id == refund_id)
        .options(
            selectinload(B2BRefund.client),
            selectinload(B2BRefund.items).selectinload(B2BRefundItem.product),
        )
    )
    refund = _r.scalar_one_or_none()
    if not refund:
        raise HTTPException(status_code=404, detail="Refund not found")

    await _reverse_refund_effects(refund, db, current_user)

    record(
        db,
        "B2B",
        "delete_refund",
        f"Deleted refund {refund.refund_number} for {refund.client.name if refund.client else 'Unknown client'}",
        current_user,
        "b2b_refund",
        refund.id,
    )
    await db.delete(refund)
    await db.commit()
    return {"ok": True, "refund_number": refund.refund_number}


@router.get("/invoice/{invoice_id}/print", response_class=HTMLResponse)
async def print_invoice(
    invoice_id: int,
    request: Request,
    db: AsyncSession = Depends(get_async_session),
):
    _r = await db.execute(
        select(B2BInvoice)
        .where(B2BInvoice.id == invoice_id)
        .options(
            selectinload(B2BInvoice.client),
            selectinload(B2BInvoice.items).selectinload(B2BInvoiceItem.product),
        )
    )
    inv = _r.scalar_one_or_none()
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    client = inv.client
    subtotal = float(inv.subtotal or 0)
    discount = float(inv.discount or 0)
    total = float(inv.total or 0)
    discount_pct = round(discount / subtotal * 100, 1) if subtotal > 0 else 0.0

    return templates.TemplateResponse(
        request,
        "b2b_invoice_print.html",
        {
            "invoice": inv,
            "client_name": client.name if client else "—",
            "client_code": f"C{str(client.id).zfill(4)}" if client else "—",
            "subtotal": subtotal,
            "discount": discount,
            "total": total,
            "discount_pct": discount_pct,
        },
    )

@router.get("/refund/{refund_id}/print", response_class=HTMLResponse)
async def print_refund(
    refund_id: int,
    request: Request,
    db: AsyncSession = Depends(get_async_session),
):
    _r = await db.execute(
        select(B2BRefund)
        .where(B2BRefund.id == refund_id)
        .options(
            selectinload(B2BRefund.client),
            selectinload(B2BRefund.items).selectinload(B2BRefundItem.product),
        )
    )
    refund = _r.scalar_one_or_none()
    if not refund:
        raise HTTPException(status_code=404, detail="Refund not found")

    client = refund.client
    subtotal = float(refund.subtotal or 0)
    discount = float(refund.discount or 0)
    total = float(refund.total or 0)
    discount_pct = round(discount / subtotal * 100, 1) if subtotal > 0 else 0.0

    return templates.TemplateResponse(
        request,
        "b2b_refund_print.html",
        {
            "refund": refund,
            "client_name": client.name if client else "—",
            "client_code": f"C{str(client.id).zfill(4)}" if client else "—",
            "subtotal": subtotal,
            "discount": discount,
            "total": total,
            "discount_pct": discount_pct,
        },
    )

# â”€â”€ CLIENT STATEMENT â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
async def _build_client_statement_payload(
    client_id: int,
    db: AsyncSession,
    *,
    as_of: Optional[date] = None,
):
    _r = await db.execute(select(B2BClient).where(B2BClient.id == client_id))
    client = _r.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    invoice_stmt = (
        select(B2BInvoice)
        .where(B2BInvoice.client_id == client_id)
        .order_by(B2BInvoice.created_at)
    )
    refund_stmt = (
        select(B2BRefund)
        .where(B2BRefund.client_id == client_id)
        .order_by(B2BRefund.created_at)
    )
    if as_of:
        cutoff = datetime.combine(as_of + timedelta(days=1), time.min, tzinfo=timezone.utc)
        invoice_stmt = invoice_stmt.where(B2BInvoice.created_at < cutoff)
        refund_stmt = refund_stmt.where(B2BRefund.created_at < cutoff)

    invoices = (await db.execute(invoice_stmt)).scalars().all()
    refunds = (await db.execute(refund_stmt)).scalars().all()
    payments = await _load_client_payment_activity(db, client_id=client_id, as_of=as_of)

    txns = []
    for inv in invoices:
        txns.append({
            "date": inv.created_at,
            "ref": inv.invoice_number,
            "type": "invoice",
            "desc": f"{(inv.invoice_type or 'b2b').replace('_', ' ').title()} Invoice",
            "debit": float(inv.total or 0),
            "credit": float(inv.amount_paid or 0),
            "status": inv.status,
        })
    for rfnd in refunds:
        txns.append({
            "date": rfnd.created_at,
            "ref": rfnd.refund_number,
            "type": "refund",
            "desc": "Credit / Refund",
            "debit": 0.0,
            "credit": float(rfnd.total or 0),
            "status": "refund",
        })

    txns.sort(key=lambda x: x["date"] or datetime.min.replace(tzinfo=timezone.utc))

    running = 0.0
    rows = []
    for t in txns:
        running += t["debit"] - t["credit"]
        rows.append({
            "date": t["date"].strftime("%d-%b-%Y") if t["date"] else "-",
            "ref": t["ref"],
            "type": t["type"],
            "desc": t["desc"],
            "debit": round(float(t["debit"] or 0), 2),
            "credit": round(float(t["credit"] or 0), 2),
            "balance": round(running, 2),
            "status": t["status"],
        })

    statement_date = as_of or date.today()
    return {
        "client": {
            "id": client.id,
            "code": f"C{str(client.id).zfill(4)}",
            "name": client.name,
            "contact_person": client.contact_person or "",
            "phone": client.phone or "",
            "email": client.email or "",
            "address": client.address or "",
            "payment_terms": client.payment_terms or "",
            "credit_limit": float(client.credit_limit or 0),
            "outstanding": round(running, 2),
        },
        "statement_date": statement_date.strftime("%d-%b-%Y"),
        "statement_period_label": f"As of {statement_date.strftime('%d-%b-%Y')}",
        "transactions": rows,
        "payment_activity": payments,
        "total_invoiced": round(sum(t["debit"] for t in rows), 2),
        "total_paid": round(sum(t["credit"] for t in rows), 2),
        "balance_due": round(running, 2),
        "as_of": as_of.isoformat() if as_of else None,
    }


@router.get("/api/clients/{client_id}/statement")
async def client_statement_data(
    client_id: int,
    as_of: Optional[date] = None,
    db: AsyncSession = Depends(get_async_session),
):
    return await _build_client_statement_payload(client_id, db, as_of=as_of)


@router.get("/client/{client_id}/statement", response_class=HTMLResponse)
async def client_statement_print(
    client_id: int,
    request: Request,
    as_of: Optional[date] = None,
    db: AsyncSession = Depends(get_async_session),
):
    payload = await _build_client_statement_payload(client_id, db, as_of=as_of)
    return templates.TemplateResponse(
        request,
        "b2b_client_statement_print.html",
        {
            "client": payload["client"],
            "transactions": payload["transactions"],
            "payment_activity": payload["payment_activity"],
            "statement_date": payload["statement_date"],
            "statement_period_label": payload["statement_period_label"],
            "total_invoiced": payload["total_invoiced"],
            "total_paid": payload["total_paid"],
            "balance_due": payload["balance_due"],
        },
    )

@router.get("/", response_class=HTMLResponse)
def b2b_ui(current_user: User = Depends(require_permission("page_b2b"))):
    return """<!DOCTYPE html>
<html>
<head>
<script src="/static/theme-init.js"></script>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>B2B — Thunder ERP</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root{
    --bg:#060810;--surface:#0a0d18;--card:#0f1424;--card2:#151c30;
    --border:rgba(255,255,255,0.06);--border2:rgba(255,255,255,0.11);
    --green:#00ff9d;--blue:#4d9fff;--purple:#a855f7;--orange:#fb923c;
    --danger:#ff4d6d;--warn:#ffb547;--teal:#2dd4bf;
    --text:#f0f4ff;--sub:#8899bb;--muted:#445066;
    --sans:'Outfit',sans-serif;--mono:'JetBrains Mono',monospace;--r:12px;
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
nav{position:sticky;top:0;z-index:100;display:flex;align-items:center;gap:8px;padding:0 24px;height:58px;background:rgba(10,13,24,.92);backdrop-filter:blur(20px);border-bottom:1px solid var(--border);flex-wrap:wrap;}
.logo{font-size:17px;font-weight:900;background:linear-gradient(135deg,var(--green),var(--blue));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;margin-right:10px;text-decoration:none;display:flex;align-items:center;gap:8px;}
.nav-link{padding:7px 12px;border-radius:8px;color:var(--sub);font-size:12px;font-weight:600;text-decoration:none;transition:all .2s;white-space:nowrap;}
.nav-link:hover{background:rgba(255,255,255,.05);color:var(--text);}
.nav-link.active{background:rgba(77,159,255,.1);color:var(--blue);}
.nav-spacer{flex:1;}
.content{max-width:1300px;margin:0 auto;padding:28px 24px;display:flex;flex-direction:column;gap:20px;}
.page-title{font-size:24px;font-weight:800;letter-spacing:-.5px;}
.page-sub{color:var(--muted);font-size:13px;margin-top:3px;}
.info-banner{background:rgba(77,159,255,.07);border:1px solid rgba(77,159,255,.2);border-radius:var(--r);padding:12px 16px;font-size:13px;color:var(--blue);display:flex;align-items:center;gap:10px;}
.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;}
.stat-card{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:18px 20px;display:flex;flex-direction:column;gap:8px;position:relative;overflow:hidden;}
.stat-card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;}
.stat-card.blue::before  {background:linear-gradient(90deg,var(--blue),transparent);}
.stat-card.warn::before  {background:linear-gradient(90deg,var(--warn),transparent);}
.stat-card.danger::before{background:linear-gradient(90deg,var(--danger),transparent);}
.stat-card.teal::before  {background:linear-gradient(90deg,var(--teal),transparent);}
.stat-label{font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);}
.stat-value{font-family:var(--mono);font-size:26px;font-weight:700;}
.stat-value.blue  {color:var(--blue);}
.stat-value.warn  {color:var(--warn);}
.stat-value.danger{color:var(--danger);}
.stat-value.teal  {color:var(--teal);}
.tabs{display:flex;gap:4px;background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:4px;flex-wrap:wrap;}
.tab{padding:8px 18px;border-radius:9px;font-size:13px;font-weight:700;cursor:pointer;border:none;background:transparent;color:var(--muted);transition:all .2s;font-family:var(--sans);}
.tab.active{background:var(--card2);color:var(--text);}
.btn{display:flex;align-items:center;gap:7px;padding:10px 16px;border-radius:var(--r);font-family:var(--sans);font-size:13px;font-weight:700;cursor:pointer;border:none;transition:all .2s;white-space:nowrap;}
.btn-blue {background:linear-gradient(135deg,var(--blue),var(--purple));color:white;}
.btn-blue:hover{filter:brightness(1.1);transform:translateY(-1px);}
.btn-green{background:linear-gradient(135deg,var(--green),#00d4ff);color:#021a10;}
.btn-green:hover{filter:brightness(1.1);transform:translateY(-1px);}
.btn-warn {background:linear-gradient(135deg,var(--warn),var(--orange));color:#1a0800;}
.btn-warn:hover{filter:brightness(1.1);transform:translateY(-1px);}
.btn-teal {background:linear-gradient(135deg,var(--teal),var(--blue));color:#001a18;}
.btn-teal:hover{filter:brightness(1.1);transform:translateY(-1px);}
.toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;}
.search-box{display:flex;align-items:center;gap:9px;background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:0 14px;flex:1;min-width:200px;}
.search-box input{background:transparent;border:none;outline:none;color:var(--text);font-family:var(--sans);font-size:14px;padding:11px 0;width:100%;}
.search-box input::placeholder{color:var(--muted);}
.table-wrap{background:var(--card);border:1px solid var(--border);border-radius:var(--r);overflow:hidden;}
table{width:100%;border-collapse:collapse;}
thead{background:var(--card2);}
th{text-align:left;font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);padding:12px 16px;}
td{padding:12px 16px;border-top:1px solid var(--border);color:var(--sub);font-size:13px;}
tr:hover td{background:rgba(255,255,255,.02);}
td.name{color:var(--text);font-weight:600;}
.action-btn{background:transparent;border:1px solid var(--border2);color:var(--sub);font-size:12px;font-weight:600;padding:5px 10px;border-radius:7px;cursor:pointer;transition:all .15s;font-family:var(--sans);}
.action-btn:hover      {border-color:var(--blue);  color:var(--blue);}
.action-btn.danger:hover{border-color:var(--danger);color:var(--danger);}
.action-btn.green:hover {border-color:var(--green); color:var(--green);}
.action-btn.warn:hover  {border-color:var(--warn);  color:var(--warn);}
.action-btn.teal:hover  {border-color:var(--teal);  color:var(--teal);}
.badge{display:inline-flex;padding:2px 9px;border-radius:20px;font-size:11px;font-weight:700;}
.badge-cash        {background:rgba(0,255,157,.1); color:var(--green);}
.badge-full_payment{background:rgba(77,159,255,.1);color:var(--blue);}
.badge-consignment {background:rgba(45,212,191,.1);color:var(--teal);}
.badge-paid        {background:rgba(0,255,157,.1); color:var(--green);}
.badge-unpaid      {background:rgba(255,181,71,.1);color:var(--warn);}
.badge-partial     {background:rgba(77,159,255,.1);color:var(--blue);}
.badge-active      {background:rgba(45,212,191,.1);color:var(--teal);}
.badge-closed      {background:rgba(0,255,157,.1); color:var(--green);}
.modal-bg{position:fixed;inset:0;z-index:500;background:rgba(0,0,0,.75);backdrop-filter:blur(4px);display:none;align-items:center;justify-content:center;}
.modal-bg.open{display:flex;}
.modal{background:var(--card);border:1px solid var(--border2);border-radius:16px;padding:28px;width:680px;max-width:95vw;max-height:90vh;overflow-y:auto;animation:modalIn .2s ease;}
@keyframes modalIn{from{opacity:0;transform:scale(.95)}to{opacity:1;transform:scale(1)}}
.modal-title{font-size:18px;font-weight:800;margin-bottom:4px;}
.modal-sub{font-size:13px;color:var(--muted);margin-bottom:20px;}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:12px;}
.fld{display:flex;flex-direction:column;gap:6px;margin-bottom:14px;}
.fld.span2{grid-column:span 2;}
.fld label{font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);}
.fld input,.fld select,.fld textarea{background:var(--card2);border:1px solid var(--border2);border-radius:10px;padding:10px 12px;color:var(--text);font-family:var(--sans);font-size:14px;outline:none;transition:border-color .2s;width:100%;}
.fld input:focus,.fld select:focus{border-color:rgba(77,159,255,.4);}
.modal-actions{display:flex;gap:10px;margin-top:8px;justify-content:flex-end;}
.btn-cancel{background:transparent;border:1px solid var(--border2);color:var(--sub);padding:10px 18px;border-radius:var(--r);font-family:var(--sans);font-size:13px;font-weight:700;cursor:pointer;}
.btn-cancel:hover{border-color:var(--danger);color:var(--danger);}
.type-selector{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:16px;}
.type-opt{background:var(--card2);border:2px solid var(--border2);border-radius:12px;padding:14px 10px;cursor:pointer;text-align:center;transition:all .2s;}
.type-opt:hover{border-color:var(--blue);}
.type-opt.selected.cash        {border-color:var(--green);background:rgba(0,255,157,.08);}
.type-opt.selected.full_payment{border-color:var(--blue); background:rgba(77,159,255,.08);}
.type-opt.selected.consignment {border-color:var(--teal); background:rgba(45,212,191,.08);}
.type-icon{font-size:24px;margin-bottom:6px;}
.type-label{font-size:13px;font-weight:700;color:var(--text);}
.type-desc{font-size:10px;color:var(--muted);margin-top:3px;}
.type-accounting{font-size:10px;color:var(--warn);margin-top:4px;font-style:italic;}
.item-row{display:grid;grid-template-columns:2fr 80px 100px 30px;gap:8px;align-items:center;margin-bottom:8px;}
.item-row select,.item-row input{background:var(--card2);border:1px solid var(--border2);border-radius:8px;padding:8px 10px;color:var(--text);font-family:var(--sans);font-size:13px;outline:none;width:100%;}
.item-row select:focus,.item-row input:focus{border-color:rgba(77,159,255,.4);}
.rm-btn{background:none;border:none;color:var(--muted);font-size:18px;cursor:pointer;padding:0;transition:color .15s;}
.rm-btn:hover{color:var(--danger);}
.add-item-btn{border:1px dashed rgba(77,159,255,.3);color:var(--blue);font-family:var(--sans);font-size:13px;font-weight:600;padding:8px;border-radius:8px;cursor:pointer;width:100%;transition:all .2s;margin-bottom:14px;background:transparent;}
.add-item-btn:hover{background:rgba(77,159,255,.08);}
.invoice-summary{background:var(--card2);border:1px solid var(--border2);border-radius:10px;padding:14px;margin-bottom:14px;}
.inv-row{display:flex;justify-content:space-between;font-size:13px;padding:4px 0;}
.inv-row.total{font-size:18px;font-weight:800;border-top:1px solid var(--border2);margin-top:8px;padding-top:10px;}
.side-bg{position:fixed;inset:0;z-index:400;background:rgba(0,0,0,.5);display:none;}
.side-bg.open{display:block;}
.side-panel{position:fixed;right:0;top:0;bottom:0;width:500px;max-width:95vw;background:var(--card);border-left:1px solid var(--border2);display:flex;flex-direction:column;transform:translateX(100%);transition:transform .3s ease;z-index:401;}
.side-panel.open{transform:translateX(0);}
.side-header{padding:20px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;}
.side-header h3{font-size:16px;font-weight:800;}
.close-btn{background:none;border:none;color:var(--muted);font-size:22px;cursor:pointer;padding:0;}
.close-btn:hover{color:var(--danger);}
.side-body{flex:1;overflow-y:auto;padding:16px 20px;}
.cons-item-card{background:var(--card2);border:1px solid var(--border);border-radius:10px;padding:14px;margin-bottom:10px;}
.cons-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px;}
.cons-input{background:var(--card);border:1px solid var(--border2);border-radius:7px;padding:7px 10px;color:var(--text);font-family:var(--mono);font-size:13px;outline:none;width:100%;}
.cons-input:focus{border-color:rgba(45,212,191,.4);}
.toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%) translateY(16px);background:var(--card2);border:1px solid var(--border2);border-radius:var(--r);padding:12px 20px;font-size:13px;font-weight:600;color:var(--text);box-shadow:0 20px 50px rgba(0,0,0,.5);opacity:0;pointer-events:none;transition:opacity .25s,transform .25s;z-index:999;}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0);}
::-webkit-scrollbar{width:4px;}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:4px;}
</style>
    <script src="/static/auth-guard.js"></script>
</head>
<body>
""" + render_app_header(current_user, "page_b2b") + """

<div class="content">
    <div>
        <div class="page-title">B2B Sales</div>
        <div class="page-sub">Business clients — cash, full payment and consignment deals</div>
    </div>

    <div class="info-banner">
        <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
        <span><b>Accounting:</b> Cash invoices post directly to Revenue. Full Payment &amp; Consignment go to <b>Deferred Revenue</b> — revenue is only recognized when payment is collected or consignment is settled.</span>
    </div>

    <div class="stats-grid">
        <div class="stat-card blue"><div class="stat-label">B2B Clients</div><div class="stat-value blue" id="stat-clients">—</div></div>
        <div class="stat-card warn"><div class="stat-label">Outstanding</div><div class="stat-value warn" id="stat-outstanding">—</div></div>
        <div class="stat-card danger"><div class="stat-label">Unpaid Invoices</div><div class="stat-value danger" id="stat-unpaid">—</div></div>
        <div class="stat-card teal"><div class="stat-label">Active Consignments</div><div class="stat-value teal" id="stat-consign">—</div></div>
    </div>

    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;">
        <div class="tabs">
            <button class="tab active" id="tab-clients"    onclick="switchTab('clients')">Clients</button>
            <button class="tab"        id="tab-invoices"  onclick="switchTab('invoices')">Invoices</button>
            <button class="tab"        id="tab-refunds"   onclick="switchTab('refunds')">Client Refund</button>
            <button class="tab"        id="tab-pricelists" onclick="switchTab('pricelists')">&#127991; Price Lists</button>
        </div>
        <div style="display:flex;gap:10px;">
            <button class="btn btn-blue"  id="btn-add-client"  onclick="openClientModal()">+ Add Client</button>
            <button class="btn btn-green" id="btn-new-invoice" onclick="openInvoiceModal()" style="display:none">+ New Invoice</button>
        </div>
    </div>

    <!-- CLIENTS -->
    <div id="section-clients">
        <div class="toolbar">
            <div class="search-box">
                <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
                <input id="client-search" placeholder="Search clients..." oninput="onClientSearch()">
            </div>
        </div>
        <div class="table-wrap">
            <table>
                <thead><tr><th>Business</th><th>Contact</th><th>Phone</th><th>Default Terms</th><th>Discount %</th><th>Outstanding</th><th>Actions</th></tr></thead>
                <tbody id="clients-body"><tr><td colspan="7" style="text-align:center;color:var(--muted);padding:40px">Loading...</td></tr></tbody>
            </table>
        </div>
    </div>

    <!-- INVOICES -->
    <div id="section-invoices" style="display:none">
        <div class="toolbar">
            <div class="search-box">
                <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
                <input id="invoice-search" placeholder="Search by client, invoice number..." oninput="filterInvoices()">
            </div>
            <select class="filter-sel" id="type-filter" onchange="filterInvoices()" style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:10px 14px;color:var(--text);font-family:var(--sans);font-size:13px;outline:none;">
                <option value="">All Types</option>
                <option value="cash">💵 Cash</option>
                <option value="full_payment">📋 Full Payment</option>
                <option value="consignment">🔄 Consignment</option>
            </select>
            <select class="filter-sel" id="status-filter" onchange="filterInvoices()" style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:10px 14px;color:var(--text);font-family:var(--sans);font-size:13px;outline:none;">
                <option value="">All Statuses</option>
                <option value="paid">Paid</option>
                <option value="unpaid">Unpaid</option>
                <option value="partial">Partial</option>
                <option value="consignment">Consignment</option>
            </select>
        </div>
        <div class="table-wrap">
            <table>
                <thead><tr><th>Invoice #</th><th>Client</th><th>Type</th><th>Total</th><th>Paid</th><th>Balance</th><th>Status</th><th>Date</th><th>Actions</th></tr></thead>
                <tbody id="invoices-body"><tr><td colspan="9" style="text-align:center;color:var(--muted);padding:40px">Loading...</td></tr></tbody>
            </table>
        </div>
    </div>

    <!-- REFUNDS -->
    <div id="section-refunds" style="display:none">
        <div class="table-wrap" style="padding:18px">
            <div class="modal-title" style="margin-bottom:4px">Client Refund</div>
            <div class="modal-sub" style="margin-bottom:16px">Select a client, add returned products, and the total will be calculated automatically.</div>

            <div class="form-row">
                <div class="fld">
                    <label>Client *</label>
                    <select id="refund-client" onchange="onRefundClientChange()"></select>
                </div>
                <div class="fld">
                    <label>Current Outstanding</label>
                    <input id="refund-outstanding" readonly value="0.00">
                </div>
            </div>

            <div style="font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-bottom:8px">Returned Products</div>
            <div style="display:grid;grid-template-columns:2fr 80px 100px 30px;gap:8px;margin-bottom:6px;">
                <span style="font-size:10px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:1px">Product</span>
                <span style="font-size:10px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:1px">Qty</span>
                <span style="font-size:10px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:1px">Unit Price</span>
                <span></span>
            </div>
            <div id="refund-items"></div>
            <button class="add-item-btn" onclick="addRefundItem()">+ Add Product</button>

            <div class="invoice-summary">
                <div class="inv-row"><span style="color:var(--muted)">Subtotal</span><span style="font-family:var(--mono)" id="refund-subtotal">0.00</span></div>
                <div class="inv-row"><span style="color:var(--muted)">Discount (<span id="refund-pct">0</span>%)</span><span style="font-family:var(--mono);color:var(--danger)" id="refund-discount">-0.00</span></div>
                <div class="inv-row total"><span>Refund Total</span><span style="font-family:var(--mono);color:var(--warn)" id="refund-total">0.00</span></div>
                <div class="inv-row"><span style="color:var(--muted)">Outstanding After Refund</span><span style="font-family:var(--mono);color:var(--green)" id="refund-after">0.00</span></div>
            </div>

            <div class="fld"><label>Notes</label><input id="refund-notes" placeholder="Optional return notes"></div>
            <div class="modal-actions" style="padding:0;margin-top:8px">
                <button class="btn-cancel" onclick="resetRefundForm()">Reset</button>
                <button class="btn btn-warn" onclick="saveRefund()">Record Refund</button>
            </div>
        </div>

        <div class="table-wrap" style="margin-top:18px">
            <div style="padding:16px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap">
                <div>
                    <div class="modal-title" style="margin-bottom:2px">Refund Records</div>
                    <div class="modal-sub">Recent client refunds with discount, notes, and print access.</div>
                </div>
                <button class="btn btn-outline" onclick="loadRefundRecords()">Refresh Records</button>
            </div>
            <table>
                <thead><tr><th>Refund #</th><th>Client</th><th>Subtotal</th><th>Discount</th><th>Total</th><th>Date</th><th>Actions</th></tr></thead>
                <tbody id="refund-records-body"><tr><td colspan="7" style="text-align:center;color:var(--muted);padding:28px">Loading refunds...</td></tr></tbody>
            </table>
        </div>
    </div>

    <!-- CONSIGNMENT SETTLE (inline cards, no separate tab) -->
    <div id="section-consignments" style="display:none"></div>

    <!-- PRICE LISTS -->
    <div id="section-pricelists" style="display:none">
        <div class="table-wrap">
            <div style="padding:16px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap">
                <div>
                    <div class="modal-title" style="margin-bottom:2px">Client Price Lists</div>
                    <div class="modal-sub">Set custom prices per client. These override the default product price on new invoices.</div>
                </div>
                <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
                    <select id="pl-client-select" onchange="loadPriceList()" style="background:var(--card2);border:1px solid var(--border2);border-radius:8px;padding:8px 12px;color:var(--text);font-family:var(--sans);font-size:13px;outline:none;min-width:200px">
                        <option value="">— Select a client —</option>
                    </select>
                    <button class="btn btn-blue" onclick="openAddPriceModal()" id="btn-add-price" style="display:none">+ Add / Edit Price</button>
                </div>
            </div>
            <table>
                <thead><tr><th>Product</th><th>SKU</th><th>Default Price</th><th>Client Price</th><th>Difference</th><th>Actions</th></tr></thead>
                <tbody id="pl-body"><tr><td colspan="6" style="text-align:center;color:var(--muted);padding:40px">Select a client to view their price list.</td></tr></tbody>
            </table>
        </div>
    </div>
</div>

<!-- PRICE LIST MODAL -->
<div class="modal-bg" id="pl-modal">
    <div class="modal" style="width:460px">
        <div class="modal-title">Set Custom Price</div>
        <div class="modal-sub" id="pl-modal-sub">Override the default product price for this client.</div>
        <div class="fld" style="margin-top:14px">
            <label>Product *</label>
            <select id="pl-product" onchange="onPlProductChange()" style="background:var(--card2);border:1px solid var(--border2);border-radius:8px;padding:9px 12px;color:var(--text);font-family:var(--sans);font-size:13px;outline:none;width:100%"></select>
        </div>
        <div style="background:var(--card2);border:1px solid var(--border);border-radius:8px;padding:10px 14px;font-size:12px;color:var(--muted);margin-bottom:12px" id="pl-default-hint"></div>
        <div class="fld">
            <label>Custom Price (ج.م.) *</label>
            <input id="pl-price" type="number" min="0" step="any" placeholder="0.00"
                style="background:var(--card2);border:1px solid var(--border2);border-radius:8px;padding:9px 12px;color:var(--text);font-family:var(--mono);font-size:14px;outline:none;width:100%">
        </div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="document.getElementById('pl-modal').classList.remove('open')">Cancel</button>
            <button class="btn btn-blue" onclick="savePriceEntry()">Save Price</button>
        </div>
    </div>
</div>

<!-- CLIENT MODAL -->
<div class="modal-bg" id="client-modal">
    <div class="modal">
        <div class="modal-title" id="client-modal-title">Add B2B Client</div>
        <div class="modal-sub">Cafes, restaurants, retail stores, distributors</div>
        <div class="form-row">
            <div class="fld span2"><label>Business Name *</label><input id="c-name" placeholder="e.g. Green Cafe"></div>
            <div class="fld"><label>Contact Person</label><input id="c-contact" placeholder="Name"></div>
            <div class="fld"><label>Phone</label><input id="c-phone" placeholder="+20 100 000 0000"></div>
            <div class="fld"><label>Email</label><input id="c-email" placeholder="contact@business.com"></div>
            <div class="fld"><label>Address</label><input id="c-address" placeholder="City / Area"></div>
            <div class="fld"><label>Default Payment Terms</label>
                <select id="c-terms">
                    <option value="cash">Cash — Pay on delivery</option>
                    <option value="full_payment">Full Payment — Invoice then pay</option>
                    <option value="consignment">Consignment — Pay what you sell</option>
                </select>
            </div>
            <div class="fld"><label>Default Discount %</label>
                <input id="c-discount" type="number" placeholder="0" min="0" max="100" step="0.5" value="0">
            </div>
            <div class="fld span2"><label>Notes</label><input id="c-notes" placeholder="Internal notes"></div>
        </div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeClientModal()">Cancel</button>
            <button class="btn btn-blue" onclick="saveClient()">Save Client</button>
        </div>
    </div>
</div>

<!-- INVOICE MODAL -->
<div class="modal-bg" id="invoice-modal">
    <div class="modal">
        <div class="modal-title" id="inv-modal-title">New B2B Invoice</div>
        <div class="modal-sub">Select client and products. Deal type and discount come from the client profile.</div>
        <div class="fld"><label>Client *</label>
            <select id="inv-client" onchange="onClientChange()"></select>
        </div>
        <div class="form-row" style="margin-bottom:16px">
            <div class="fld">
                <label>Deal Type</label>
                <input id="inv-deal-type" readonly>
            </div>
            <div class="fld">
                <label>Discount %</label>
                <input id="inv-discount-pct" type="number" readonly value="0">
            </div>
        </div>
        <div style="font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-bottom:8px">Products</div>
        <div style="display:grid;grid-template-columns:2fr 80px 100px 30px;gap:8px;margin-bottom:6px;">
            <span style="font-size:10px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:1px">Product</span>
            <span style="font-size:10px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:1px">Qty</span>
            <span style="font-size:10px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:1px">Unit Price</span>
            <span></span>
        </div>
        <div id="inv-items"></div>
        <button class="add-item-btn" onclick="addInvItem()">+ Add Product</button>
        <div class="invoice-summary">
            <div class="inv-row"><span style="color:var(--muted)">Subtotal</span><span style="font-family:var(--mono)" id="s-subtotal">0.00</span></div>
            <div class="inv-row"><span style="color:var(--muted)">Discount (<span id="s-pct">0</span>%)</span><span style="font-family:var(--mono);color:var(--danger)" id="s-discount">-0.00</span></div>
            <div class="inv-row total"><span>Total</span><span style="font-family:var(--mono);color:var(--green)" id="s-total">0.00</span></div>
        </div>
        <div class="fld"><label>Notes</label><input id="inv-notes" placeholder="Optional notes"></div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeInvoiceModal()">Cancel</button>
            <button class="btn btn-green" id="inv-save-btn" onclick="saveInvoice()">Create Invoice</button>
        </div>
    </div>
</div>

<!-- PAYMENT MODAL -->
<div class="modal-bg" id="pay-modal">
    <div class="modal" style="width:420px">
        <div class="modal-title">Record Payment</div>
        <div class="modal-sub" id="pay-modal-sub">Invoice</div>
        <div style="background:rgba(0,255,157,.06);border:1px solid rgba(0,255,157,.15);border-radius:10px;padding:12px 14px;margin-bottom:14px;font-size:12px;color:var(--green);">
            Recording this payment will move the amount from <b>Deferred Revenue → Sales Revenue</b>
        </div>
        <div class="fld"><label>Amount *</label><input id="pay-amount" type="number" placeholder="0.00" min="0.01" step="any"></div>
        <div class="fld"><label>Method</label>
            <select id="pay-method">
                <option value="cash">Cash</option>
                <option value="transfer">Bank Transfer</option>
                <option value="check">Check</option>
            </select>
        </div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="document.getElementById('pay-modal').classList.remove('open')">Cancel</button>
            <button class="btn btn-warn" onclick="savePayment()">Record Payment</button>
        </div>
    </div>
</div>

<!-- CONSIGNMENT SETTLE PANEL -->
<div class="side-bg" id="side-bg" onclick="closeSide()"></div>
<div class="side-panel" id="side-panel">
    <div class="side-header">
        <h3 id="side-title">Settle Consignment</h3>
        <button class="close-btn" onclick="closeSide()">×</button>
    </div>
    <div class="side-body" id="side-body"></div>
</div>

<!-- CONSIGNMENT PAYMENT MODAL -->
<div class="modal-bg" id="cons-pay-modal">
    <div class="modal" style="width:440px">
        <div class="modal-title">💰 Record Consignment Payment</div>
        <div class="modal-sub" id="cons-pay-sub">Invoice</div>
        <div style="background:rgba(45,212,191,.06);border:1px solid rgba(45,212,191,.15);border-radius:10px;padding:12px 14px;margin-bottom:14px;font-size:12px;color:var(--teal);">
            This records cash received from the consignment client.<br>
            Amount moves from <b>Deferred Revenue → Sales Revenue</b>.
        </div>
        <div class="fld">
            <label>Amount Paid *</label>
            <input id="cons-pay-amount" type="number" placeholder="0.00" min="0.01" step="any">
        </div>
        <div class="fld">
            <label>For which month's sales?</label>
            <select id="cons-pay-month">
                <option value="">General payment (no specific month)</option>
            </select>
        </div>
        <div class="fld">
            <label>Notes</label>
            <input id="cons-pay-notes" placeholder="Optional notes...">
        </div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="document.getElementById('cons-pay-modal').classList.remove('open')">Cancel</button>
            <button class="btn btn-teal" onclick="saveConsPayment()">Record Payment & Recognize Revenue</button>
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
  let currentUser = null;
  function hasPermission(permission, u = currentUser){
      const role = u ? (u.role || "") : "";
      const perms = new Set(u ? (u.permissions || []) : []);
      return role === "admin" || perms.has(permission);
  }
  function configureB2BPermissions(u){
      currentUser = u;
      isAdmin = u.role === "admin";
      if(!hasPermission("tab_b2b_clients", u)){
          let el = document.getElementById("tab-clients");
          if(el) el.style.display = "none";
      }
    if(!hasPermission("tab_b2b_invoices", u)){
          let el = document.getElementById("tab-invoices");
          if(el) el.style.display = "none";
          let refundEl = document.getElementById("tab-refunds");
          if(refundEl) refundEl.style.display = "none";
      }
      if(!hasPermission("tab_b2b_clients", u) && hasPermission("tab_b2b_invoices", u)){
          setTimeout(() => switchTab("invoices"), 0);
      }
      renderRefundRecords(allRefunds || []);
  }
  initializeColorMode();
  initUser().then(u => { if(u) configureB2BPermissions(u); });
let allProducts   = [];
let refundProducts = [];
let allClients    = [];
let allInvoices   = [];
let allRefunds    = [];
let selectedType  = "cash";
let editingClientId  = null;
let editingInvoiceId = null;
let payingInvoiceId  = null;
let settlingConsId   = null;
let searchTimer      = null;
let isAdmin = false; // set by initUser() via configureB2BPermissions(u)

async function init(){
    // Run seeding and data loading independently
    fetch("/b2b/api/seed-accounts", {method:"POST"}).catch(e => console.warn("Seeding failed", e));
    
    try {
        const res = await fetch("/b2b/api/products-list");
        if(res.ok) {
            allProducts = await res.json();
            buildB2BProductDatalist();
        }
    } catch(e) { console.error("Products load failed", e); }

    loadStats().catch(e => console.error("Stats load failed", e));
    loadClients().catch(e => {
        console.error("Clients load failed", e);
        document.getElementById("clients-body").innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--danger);padding:40px">Error loading clients. Check permissions or database.</td></tr>`;
    });
}

async function loadStats(){
    let d = await (await fetch("/b2b/api/stats")).json();
    document.getElementById("stat-clients").innerText     = d.total_clients;
    document.getElementById("stat-outstanding").innerText = d.total_outstanding.toFixed(2);
    document.getElementById("stat-unpaid").innerText      = d.unpaid_invoices;
    document.getElementById("stat-consign").innerText     = d.active_consign;
}

/* ── TABS ── */
function switchTab(tab){
    const required = {
        clients: "tab_b2b_clients",
        invoices: "tab_b2b_invoices",
        refunds: "tab_b2b_invoices",
        consignments: "tab_b2b_consignment",
    };
    if(required[tab] && !hasPermission(required[tab])) return;
    ["clients","invoices","refunds","consignments","pricelists"].forEach(t=>{
        let el = document.getElementById("section-"+t);
        if(el) el.style.display = t===tab?"":"none";
        let tb = document.getElementById("tab-"+t);
        if(tb) tb.classList.toggle("active", t===tab);
    });
    document.getElementById("btn-add-client").style.display  = tab==="clients"    ?"":"none";
    document.getElementById("btn-new-invoice").style.display = tab==="invoices"   ?"":"none";
    if(tab==="invoices")   loadInvoices();
    if(tab==="refunds")    prepareRefundTab();
    if(tab==="pricelists") initPriceListTab();
}

/* ── CLIENTS ── */
function onClientSearch(){ clearTimeout(searchTimer); searchTimer=setTimeout(loadClients,300); }

async function loadClients(){
    try {
        let q = document.getElementById("client-search").value.trim();
        const res = await fetch(`/b2b/api/clients${q?"?q="+encodeURIComponent(q):""}`);
        if (!res.ok) throw new Error(`API Error: ${res.status}`);
        allClients = await res.json();
        
        if(!allClients.length){
            document.getElementById("clients-body").innerHTML=`<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:40px">No clients yet.</td></tr>`;
            return;
        }
        const termsLabel={cash:"💵 Cash",full_payment:"📋 Full Payment",consignment:"🔄 Consignment"};
        document.getElementById("clients-body").innerHTML = allClients.map(c=>`
            <tr>
                <td class="name">${c.name}</td>
                <td style="font-size:12px">${c.contact_person}</td>
            <td style="font-family:var(--mono);font-size:12px">${c.phone}</td>
            <td><span class="badge badge-${c.payment_terms}">${termsLabel[c.payment_terms]||c.payment_terms}</span></td>
            <td style="font-family:var(--mono);color:var(--blue)">${c.credit_limit>0?c.credit_limit.toFixed(1)+"%":"—"}</td>
            <td style="font-family:var(--mono);color:${c.outstanding>0?"var(--warn)":"var(--muted)"}">
                ${c.outstanding>0?c.outstanding.toFixed(2):"—"}
            </td>
            <td style="display:flex;gap:6px;flex-wrap:wrap">
                ${hasPermission("tab_b2b_invoices")?`<button class="action-btn green" onclick="quickInvoice(${c.id})">+ Invoice</button>`:""}
                <button class="action-btn" onclick="window.open('/b2b/client/${c.id}/statement','_blank')" title="Account Statement">&#128196; Statement</button>
                <button class="action-btn" onclick="openEditClient(${c.id})">Edit</button>
                ${hasPermission("action_b2b_delete")?`<button class="action-btn danger" onclick="deleteClient(${c.id},'${c.name.replace(/'/g,"\\'")}')">Remove</button>`:""}
            </td>
        </tr>`).join("");
    } catch (err) {
        console.error(err);
        document.getElementById("clients-body").innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--danger);padding:40px">Error loading clients.</td></tr>`;
    }
}

function openClientModal(){
    editingClientId = null;
    document.getElementById("client-modal-title").innerText = "Add B2B Client";
    ["c-name","c-contact","c-phone","c-email","c-address","c-notes"].forEach(id=>document.getElementById(id).value="");
    document.getElementById("c-terms").value    = "cash";
    document.getElementById("c-discount").value = "0";
    document.getElementById("client-modal").classList.add("open");
}

function openEditClient(id){
    let c = allClients.find(x=>x.id===id); if(!c) return;
    editingClientId = id;
    document.getElementById("client-modal-title").innerText = "Edit Client";
    document.getElementById("c-name").value    = c.name;
    document.getElementById("c-contact").value = c.contact_person==="—"?"":c.contact_person;
    document.getElementById("c-phone").value   = c.phone==="—"?"":c.phone;
    document.getElementById("c-email").value   = c.email==="—"?"":c.email;
    document.getElementById("c-address").value = c.address==="—"?"":c.address;
    document.getElementById("c-terms").value   = c.payment_terms;
    document.getElementById("c-discount").value= c.credit_limit;
    document.getElementById("c-notes").value   = c.notes;
    document.getElementById("client-modal").classList.add("open");
}

function closeClientModal(){ document.getElementById("client-modal").classList.remove("open"); }

async function saveClient(){
    let name = document.getElementById("c-name").value.trim();
    if(!name){ showToast("Business name is required"); return; }
    let body = {
        name,
        contact_person: document.getElementById("c-contact").value.trim()||null,
        phone:          document.getElementById("c-phone").value.trim()||null,
        email:          document.getElementById("c-email").value.trim()||null,
        address:        document.getElementById("c-address").value.trim()||null,
        payment_terms:  document.getElementById("c-terms").value,
        discount_pct:   parseFloat(document.getElementById("c-discount").value)||0,
        notes:          document.getElementById("c-notes").value.trim()||null,
    };
    let url=editingClientId?`/b2b/api/clients/${editingClientId}`:"/b2b/api/clients";
    let method=editingClientId?"PUT":"POST";
    let res=await fetch(url,{method,headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    let data=await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    closeClientModal();
    showToast(editingClientId?"Client updated ✓":"Client added ✓");
    loadClients(); loadStats();
}

async function deleteClient(id,name){
    if(!confirm(`Remove "${name}"?`)) return;
    await fetch(`/b2b/api/clients/${id}`,{method:"DELETE"});
    showToast("Client removed ✓");
    loadClients(); loadStats();
}

/* ── INVOICE MODAL ── */
async function openInvoiceModal(preClientId=null){
    editingInvoiceId = null;
    document.getElementById("inv-modal-title").innerText = "New B2B Invoice";
    document.getElementById("inv-save-btn").innerText    = "Create Invoice";
    let sel = document.getElementById("inv-client");
    sel.innerHTML = allClients.map(c=>
        `<option value="${c.id}" data-terms="${c.payment_terms}" data-discount="${c.discount_pct}" ${c.id===preClientId?"selected":""}>${c.name}</option>`
    ).join("");
    document.getElementById("inv-items").innerHTML = "";
    document.getElementById("inv-notes").value = "";
    // Await so allProducts is loaded with client prices before items are added
    await onClientChange();
    addInvItem();
    document.getElementById("invoice-modal").classList.add("open");
}

function quickInvoice(clientId){ switchTab("invoices"); setTimeout(()=>openInvoiceModal(clientId),50); }

function closeInvoiceModal(){
    editingInvoiceId = null;
    document.getElementById("inv-modal-title").innerText = "New B2B Invoice";
    document.getElementById("inv-save-btn").innerText    = "Create Invoice";
    document.getElementById("invoice-modal").classList.remove("open");
}

async function onClientChange(){
    let sel = document.getElementById("inv-client");
    let opt = sel.options[sel.selectedIndex];
    if(!opt || !opt.value) return;
    let terms    = opt.dataset.terms    || "cash";
    let discount = parseFloat(opt.dataset.discount) || 0;
    selectType(terms);
    document.getElementById("inv-deal-type").value = formatDealType(terms);
    document.getElementById("inv-discount-pct").value = discount;
    // Reload product list with client-specific prices
    let clientId = parseInt(opt.value);
    allProducts = await (await fetch(`/b2b/api/products-list?client_id=${clientId}`)).json();
    buildB2BProductDatalist();
    updateSummary();
}

function formatDealType(type){
    const labels = {
        cash: "Cash",
        full_payment: "Full Payment",
        consignment: "Consignment",
    };
    return labels[type] || type;
}

function selectType(type){
    selectedType = type;
}

function buildB2BProductDatalist(){
    let dl = document.getElementById("b2b-product-datalist");
    if(!dl){
        dl = document.createElement("datalist");
        dl.id = "b2b-product-datalist";
        document.body.appendChild(dl);
    }
    dl.innerHTML = allProducts.map(p=>
        `<option data-id="${p.id}" value="${p.sku} — ${p.name}" data-price="${p.price}" data-unit="${p.unit}" data-stock="${p.stock}">`
    ).join("");
}

function resolveB2BProduct(inputEl){
    let val = inputEl.value.trim().toLowerCase();
    let match = allProducts.find(p=>
        (p.sku+" — "+p.name).toLowerCase()===val ||
        p.sku.toLowerCase()===val ||
        p.name.toLowerCase()===val
    );
    if(!match) match = allProducts.find(p=>
        p.sku.toLowerCase().startsWith(val) ||
        p.name.toLowerCase().includes(val)
    );
    return match||null;
}

function productLabel(product){
    return `${product.sku} — ${product.name}`;
}

function productMatches(products, query){
    let q = (query || "").trim().toLowerCase();
    if(!q) return products.slice(0, 8);
    let starts = [];
    let contains = [];
    products.forEach(p=>{
        let sku  = (p.sku || "").toLowerCase();
        let name = (p.name || "").toLowerCase();
        if(sku.startsWith(q) || name.startsWith(q)) starts.push(p);
        else if(sku.includes(q) || name.includes(q)) contains.push(p);
    });
    return starts.concat(contains).slice(0, 8);
}

// getProducts can be an array OR a function returning an array
function attachProductDropdown(inputEl, hiddenEl, hintEl, getProducts, accent, onPick){
    function resolveList(){ return typeof getProducts === "function" ? getProducts() : getProducts; }

    let box = document.createElement("div");
    box.style.cssText = "position:absolute;left:0;right:0;top:calc(100% + 4px);background:var(--card);border:1px solid var(--border2);border-radius:10px;box-shadow:0 18px 40px rgba(0,0,0,.4);max-height:280px;overflow-y:auto;z-index:9999;display:none;";
    inputEl.parentElement.appendChild(box);

    let activeIdx = -1;

    function hideBox(){ box.style.display = "none"; activeIdx = -1; }

    function draw(q){
        let items = productMatches(resolveList(), q);
        activeIdx = -1;
        if(!items.length){
            box.innerHTML = `<div style="padding:10px 14px;color:var(--muted);font-size:12px">No matching products</div>`;
            box.style.display = "block";
            return;
        }
        box.innerHTML = items.map((p, i)=>{
            let priceColor = (p.has_custom) ? "var(--blue)" : accent;
            let customTag  = p.has_custom ? `<span style="font-size:9px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;background:rgba(77,159,255,.15);color:var(--blue);padding:1px 5px;border-radius:4px;margin-left:4px">custom</span>` : "";
            return `<button type="button" data-idx="${i}" style="width:100%;text-align:left;background:transparent;border:none;padding:10px 14px;cursor:pointer;${i>0?"border-top:1px solid var(--border)":""};font-family:var(--sans);transition:background .1s;">
                <div style="display:flex;justify-content:space-between;gap:10px;align-items:center">
                    <div style="min-width:0;flex:1">
                        <div style="font-size:13px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${p.name}${customTag}</div>
                        <div style="font-family:var(--mono);font-size:11px;color:var(--muted);margin-top:1px">${p.sku || "—"}</div>
                    </div>
                    <div style="text-align:right;flex-shrink:0">
                        <div style="font-family:var(--mono);font-size:13px;font-weight:700;color:${priceColor}">${p.price.toFixed(2)}</div>
                        <div style="font-size:10px;color:var(--muted);margin-top:1px">stk ${p.stock.toFixed(0)} ${p.unit}</div>
                    </div>
                </div>
            </button>`;
        }).join("");
        box.querySelectorAll("button[data-idx]").forEach(btn=>{
            btn.addEventListener("mousedown", function(e){
                e.preventDefault();
                let p = items[parseInt(this.dataset.idx)];
                pick(p);
            });
            btn.addEventListener("mouseenter", function(){
                setActive(parseInt(this.dataset.idx));
            });
        });
        box.style.display = "block";
    }

    function setActive(idx){
        activeIdx = idx;
        box.querySelectorAll("button[data-idx]").forEach(btn=>{
            let active = parseInt(btn.dataset.idx) === idx;
            btn.style.background = active ? "var(--card2)" : "transparent";
        });
    }

    function pick(p){
        inputEl.value = productLabel(p);
        hiddenEl.value = p.id;
        hintEl.innerText = `stock: ${p.stock.toFixed(0)} ${p.unit}`;
        inputEl.style.borderColor = accent;
        onPick(p);
        hideBox();
    }

    inputEl.addEventListener("focus", function(){ draw(this.value); });
    inputEl.addEventListener("input", function(){ draw(this.value); });
    inputEl.addEventListener("keydown", function(e){
        let btns = Array.from(box.querySelectorAll("button[data-idx]"));
        if(!btns.length) return;
        if(e.key === "ArrowDown"){
            e.preventDefault();
            setActive(Math.min(activeIdx + 1, btns.length - 1));
        } else if(e.key === "ArrowUp"){
            e.preventDefault();
            setActive(Math.max(activeIdx - 1, 0));
        } else if(e.key === "Enter" && activeIdx >= 0){
            e.preventDefault();
            btns[activeIdx].dispatchEvent(new MouseEvent("mousedown", {bubbles:true}));
        } else if(e.key === "Escape"){
            hideBox();
        }
    });
    inputEl.addEventListener("blur", function(){ setTimeout(hideBox, 150); });
}

function addInvItem(selectedId=null, qty=1, price=null){
    let div = document.createElement("div");
    div.className = "item-row";
    div.innerHTML = `
        <div style="position:relative;flex:1;">
            <input type="text"
                class="b2b-prod-search"
                placeholder="Search by name or SKU…"
                autocomplete="off"
                style="width:100%;background:var(--card2);border:1px solid var(--border2);border-radius:8px;padding:8px 10px;color:var(--text);font-family:var(--sans);font-size:13px;outline:none;transition:border-color .2s;">
            <input type="hidden" class="b2b-prod-id">
            <span class="b2b-stock-hint" style="position:absolute;right:8px;top:50%;transform:translateY(-50%);font-size:10px;color:var(--muted);pointer-events:none;"></span>
        </div>
        <input type="number" placeholder="1" min="0.001" step="any" value="${qty}" oninput="updateSummary()"
            style="background:var(--card2);border:1px solid var(--border2);border-radius:8px;padding:8px 10px;color:var(--text);font-family:var(--mono);font-size:13px;outline:none;width:80px;">
        <input type="number" placeholder="0.00" min="0" step="any" value="${price!=null?price:""}" oninput="updateSummary()"
            style="background:var(--card2);border:1px solid var(--border2);border-radius:8px;padding:8px 10px;color:var(--text);font-family:var(--mono);font-size:13px;outline:none;width:100px;">
        <button class="rm-btn" onclick="this.closest('.item-row').remove();updateSummary()">×</button>
    `;
    let searchInp = div.querySelector(".b2b-prod-search");
    let hiddenId  = div.querySelector(".b2b-prod-id");
    let stockHint = div.querySelector(".b2b-stock-hint");
    let priceInp  = div.querySelectorAll("input[type=number]")[1];

    // Pre-fill when editing an existing invoice
    if(selectedId){
        let p = allProducts.find(x=>x.id===selectedId);
        if(p){
            searchInp.value = productLabel(p);
            hiddenId.value  = p.id;
            stockHint.innerText = `stock: ${p.stock.toFixed(0)} ${p.unit}`;
            searchInp.style.borderColor = "rgba(0,255,157,.4)";
        }
    }

    // Rich dropdown — always reads current allProducts via getter
    attachProductDropdown(
        searchInp, hiddenId, stockHint,
        () => allProducts,
        "rgba(0,255,157,.45)",
        function(p){
            priceInp.value = p.price.toFixed(2);
            if(p.has_custom){
                priceInp.style.borderColor = "rgba(77,159,255,.6)";
                priceInp.title = `Custom price (default: ${(p.default_price||p.price).toFixed(2)})`;
            } else {
                priceInp.style.borderColor = "";
                priceInp.title = "";
            }
            updateSummary();
        }
    );

    document.getElementById("inv-items").appendChild(div);
    if(price != null) updateSummary();
}

function updateSummary(){
    let rows = document.querySelectorAll("#inv-items .item-row");
    let subtotal = 0;
    rows.forEach(row=>{
        let qty   = parseFloat(row.querySelectorAll("input[type=number]")[0].value)||0;
        let price = parseFloat(row.querySelectorAll("input[type=number]")[1].value)||0;
        subtotal += qty * price;
    });
    let pct=parseFloat(document.getElementById("inv-discount-pct").value)||0;
    let discount=subtotal*pct/100;
    let total=subtotal-discount;
    document.getElementById("s-subtotal").innerText = subtotal.toFixed(2);
    document.getElementById("s-pct").innerText      = pct.toFixed(1);
    document.getElementById("s-discount").innerText = "-"+discount.toFixed(2);
    document.getElementById("s-total").innerText    = total.toFixed(2);
}

/* ── REFUNDS ── */
async function prepareRefundTab(){
    let refundClients = await (await fetch("/b2b/api/clients")).json();
    let sel = document.getElementById("refund-client");
    if(!sel) return;
    sel.innerHTML = refundClients.map(c=>
        `<option value="${c.id}" data-outstanding="${c.outstanding}">${c.name}</option>`
    ).join("");
    if(!refundClients.length){
        document.getElementById("refund-outstanding").value = "0.00";
        document.getElementById("refund-pct").innerText = "0";
        document.getElementById("refund-subtotal").innerText = "0.00";
        document.getElementById("refund-discount").innerText = "-0.00";
        document.getElementById("refund-total").innerText = "0.00";
        document.getElementById("refund-after").innerText = "0.00";
        document.getElementById("refund-items").innerHTML = "";
        return;
    }
    await loadRefundProducts(parseInt(sel.value));
    if(!document.getElementById("refund-items").children.length){
        addRefundItem();
    }
    await onRefundClientChange();
    loadRefundRecords();
}

async function onRefundClientChange(){
    let sel = document.getElementById("refund-client");
    let opt = sel.options[sel.selectedIndex];
    let outstanding = opt ? (parseFloat(opt.dataset.outstanding) || 0) : 0;
    let client = allClients.find(c => c.id === parseInt(opt?.value || "0"));
    let discountPct = client ? (parseFloat(client.discount_pct) || 0) : 0;
    document.getElementById("refund-outstanding").value = outstanding.toFixed(2);
    document.getElementById("refund-pct").innerText = discountPct.toFixed(1);
    if(opt && opt.value){
        await loadRefundProducts(parseInt(opt.value));
    }
    updateRefundSummary();
    loadRefundRecords();
}

async function loadRefundProducts(clientId){
    let res = await fetch(`/b2b/api/refund-products/${clientId}`);
    let data = await res.json();
    refundProducts = Array.isArray(data) ? data : [];
}

function addRefundItem(selectedId=null, qty=1, price=null){
    let div = document.createElement("div");
    div.className = "item-row";
    div.innerHTML = `
        <div style="position:relative;flex:1;">
            <input type="text"
                class="b2b-prod-search"
                placeholder="Search by name or SKU…"
                autocomplete="off"
                style="width:100%;background:var(--card2);border:1px solid var(--border2);border-radius:8px;padding:8px 10px;color:var(--text);font-family:var(--sans);font-size:13px;outline:none;transition:border-color .2s;">
            <input type="hidden" class="b2b-prod-id">
            <span class="b2b-stock-hint" style="position:absolute;right:8px;top:50%;transform:translateY(-50%);font-size:10px;color:var(--muted);pointer-events:none;"></span>
        </div>
        <input type="number" placeholder="1" min="0.001" step="any" value="${qty}" oninput="updateRefundSummary()"
            style="background:var(--card2);border:1px solid var(--border2);border-radius:8px;padding:8px 10px;color:var(--text);font-family:var(--mono);font-size:13px;outline:none;width:80px;">
        <input type="number" placeholder="0.00" min="0" step="any" value="${price!=null?price:""}" oninput="updateRefundSummary()"
            style="background:var(--card2);border:1px solid var(--border2);border-radius:8px;padding:8px 10px;color:var(--text);font-family:var(--mono);font-size:13px;outline:none;width:100px;">
        <button class="rm-btn" onclick="this.closest('.item-row').remove();updateRefundSummary()">×</button>
    `;
    let searchInp = div.querySelector(".b2b-prod-search");
    let hiddenId  = div.querySelector(".b2b-prod-id");
    let stockHint = div.querySelector(".b2b-stock-hint");

    if(selectedId){
        let p = allProducts.find(x=>x.id===selectedId);
        if(p){
            searchInp.value = productLabel(p);
            hiddenId.value  = p.id;
            stockHint.innerText = `stock: ${p.stock.toFixed(0)} ${p.unit}`;
            searchInp.style.borderColor = "rgba(255,181,71,.45)";
        }
    }

    let priceInp = div.querySelectorAll("input[type=number]")[1];
    attachProductDropdown(
        searchInp,
        hiddenId,
        stockHint,
        () => refundProducts,
        "rgba(255,181,71,.45)",
        function(p){
            priceInp.value = p.price.toFixed(2);
            updateRefundSummary();
        }
    );

    document.getElementById("refund-items").appendChild(div);
    if(price != null) updateRefundSummary();
}

function updateRefundSummary(){
    let rows = document.querySelectorAll("#refund-items .item-row");
    let subtotal = 0;
    rows.forEach(row=>{
        let qty   = parseFloat(row.querySelectorAll("input[type=number]")[0].value)||0;
        let price = parseFloat(row.querySelectorAll("input[type=number]")[1].value)||0;
        subtotal += qty * price;
    });
    let pct = parseFloat(document.getElementById("refund-pct").innerText)||0;
    let discount = subtotal * pct / 100;
    let total = Math.max(0, subtotal - discount);
    let outstanding = parseFloat(document.getElementById("refund-outstanding").value)||0;
    let after = Math.max(0, outstanding - total);
    document.getElementById("refund-subtotal").innerText = subtotal.toFixed(2);
    document.getElementById("refund-discount").innerText = "-" + discount.toFixed(2);
    document.getElementById("refund-total").innerText = total.toFixed(2);
    document.getElementById("refund-after").innerText = after.toFixed(2);
}

function resetRefundForm(){
    document.getElementById("refund-notes").value = "";
    document.getElementById("refund-items").innerHTML = "";
    addRefundItem();
    onRefundClientChange();
}

async function saveRefund(){
    let client_id = parseInt(document.getElementById("refund-client").value);
    if(!client_id){ showToast("Select a client"); return; }
    let rows = document.querySelectorAll("#refund-items .item-row");
    let items = [];
    for(let row of rows){
        let product_id = parseInt(row.querySelector(".b2b-prod-id").value)||0;
        let qty        = parseFloat(row.querySelectorAll("input[type=number]")[0].value)||0;
        let unit_price = parseFloat(row.querySelectorAll("input[type=number]")[1].value)||0;
        if(!product_id){ showToast("Select a product for all rows"); return; }
        if(qty<=0){ showToast("Refund quantity must be greater than 0"); return; }
        items.push({product_id, qty, unit_price});
    }
    if(!items.length){ showToast("Add at least one returned product"); return; }
    let res = await fetch("/b2b/api/refunds", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({
            client_id,
            notes: document.getElementById("refund-notes").value.trim() || null,
            items,
        }),
    });
    let data = await res.json();
    if(data.detail){ showToast("Error: " + data.detail); return; }
    showToast(`${data.refund_number} recorded for ${data.client} - ${data.amount.toFixed(2)} EGP`);
    resetRefundForm();
    await loadClients();
    await loadStats();
    await prepareRefundTab();
}

async function loadRefundRecords(){
    let sel = document.getElementById("refund-client");
    let clientId = parseInt(sel?.value || "0");
    let url = `/b2b/api/refunds${clientId ? "?client_id="+clientId : ""}`;
    allRefunds = await (await fetch(url)).json();
    renderRefundRecords(allRefunds);
}

function renderRefundRecords(refunds){
    let body = document.getElementById("refund-records-body");
    if(!body) return;
    if(!refunds.length){
        body.innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:28px">No refund records yet.</td></tr>`;
        return;
    }
    body.innerHTML = refunds.map(r=>`
        <tr>
            <td style="font-family:var(--mono);font-size:12px;color:var(--warn)">${r.refund_number}</td>
            <td class="name">${r.client}</td>
            <td style="font-family:var(--mono)">${r.subtotal.toFixed(2)}</td>
            <td style="font-family:var(--mono);color:${r.discount>0?"var(--danger)":"var(--muted)"}">${r.discount>0?`${r.discount.toFixed(2)} (${r.discount_pct.toFixed(1)}%)`:"—"}</td>
            <td style="font-family:var(--mono);font-weight:700;color:var(--warn)">${r.total.toFixed(2)}</td>
            <td style="font-size:12px;color:var(--muted)">${r.created_at}</td>
            <td><div style="display:flex;gap:6px;flex-wrap:wrap">
                <button class="action-btn" onclick="window.open('/b2b/refund/${r.id}/print','_blank')">Print</button>
                ${isAdmin ? `<button class="action-btn danger js-delete-refund" data-refund-id="${r.id}" data-refund-number="${String(r.refund_number).replace(/"/g, "&quot;")}">Delete</button>` : ""}
            </div></td>
        </tr>
    `).join("");
}

document.addEventListener("click", function(event){
    const btn = event.target.closest(".js-delete-refund");
    if(!btn) return;
    deleteRefund(parseInt(btn.dataset.refundId || "0"), btn.dataset.refundNumber || "");
});

async function deleteRefund(id, refundNumber){
    if(!confirm(`Are you sure you want to delete refund ${refundNumber}?`)) return;
    let res = await fetch(`/b2b/api/refunds/${id}`, {method:"DELETE"});
    let data = await res.json().catch(()=>({detail:"Unable to delete refund"}));
    if(!res.ok || data.detail){
        showToast("Error: " + (data.detail || "Unable to delete refund"));
        return;
    }
    showToast(`${refundNumber} deleted`);
    await loadClients();
    await loadStats();
    await prepareRefundTab();
}

async function openEditInvoice(id){
    let data=await (await fetch("/b2b/api/invoices?limit=500")).json();
    let invoice=data.invoices.find(i=>i.id===id);
    if(!invoice){ showToast("Could not load invoice"); return; }
    editingInvoiceId = id;
    document.getElementById("inv-modal-title").innerText = `Edit Invoice — ${invoice.invoice_number}`;
    document.getElementById("inv-save-btn").innerText    = "Save Changes";
    let sel = document.getElementById("inv-client");
    sel.innerHTML = allClients.map(c=>
        `<option value="${c.id}" data-terms="${c.payment_terms}" data-discount="${c.discount_pct}" ${c.id===invoice.client_id?"selected":""}>${c.name}</option>`
    ).join("");
    await onClientChange();
    document.getElementById("inv-notes").value        = invoice.notes;
    document.getElementById("inv-items").innerHTML = "";
    invoice.items.forEach(item=>{ addInvItem(item.product_id, item.qty, item.unit_price); });
    updateSummary();
    document.getElementById("invoice-modal").classList.add("open");
}

async function saveInvoice(){
    let client_id = parseInt(document.getElementById("inv-client").value);
    if(!client_id){ showToast("Select a client"); return; }
    let rows = document.querySelectorAll("#inv-items .item-row");
    let items = [];
    for(let row of rows){
        let product_id = parseInt(row.querySelector(".b2b-prod-id").value)||0;
        let qty        = parseFloat(row.querySelectorAll("input[type=number]")[0].value)||0;
        let unit_price = parseFloat(row.querySelectorAll("input[type=number]")[1].value)||0;
        if(!product_id){ showToast("Select a product for all rows"); return; }
        if(qty<=0)      { showToast("Quantity must be greater than 0"); return; }
        items.push({product_id, qty, unit_price});
    }
    if(!items.length){ showToast("Add at least one product"); return; }
    let body={
        client_id,
        invoice_type:selectedType,
        payment_method:selectedType,
        discount_pct:parseFloat(document.getElementById("inv-discount-pct").value)||0,
        notes:document.getElementById("inv-notes").value.trim()||null,
        items,
    };
    let url=editingInvoiceId?`/b2b/api/invoices/${editingInvoiceId}`:"/b2b/api/invoices";
    let method=editingInvoiceId?"PUT":"POST";
    let res=await fetch(url,{method,headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    let data=await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    closeInvoiceModal();
    let action=editingInvoiceId?"updated":"created";
    showToast(`${data.invoice_number} ${action} ✓  Total: ${data.total.toFixed(2)} EGP`);
    loadInvoices(); loadClients(); loadStats();
}

async function deleteInvoice(id,number){
    if(!confirm(`Delete invoice ${number}? This will reverse all stock and accounting changes.`)) return;
    let res=await fetch(`/b2b/api/invoices/${id}`,{method:"DELETE"});
    let data=await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    showToast(`${number} deleted — reversed ✓`);
    loadInvoices(); loadClients(); loadStats();
}

/* ── INVOICES TABLE ── */
async function loadInvoices(){
    try {
        const res = await fetch("/b2b/api/invoices?limit=200");
        if (!res.ok) throw new Error(`API Error: ${res.status}`);
        let data = await res.json();
        allInvoices = data.invoices;
        renderInvoices(allInvoices);
    } catch (err) {
        document.getElementById("invoices-body").innerHTML = `<tr><td colspan="9" style="text-align:center;color:var(--danger);padding:40px">Error loading invoices.</td></tr>`;
    }
}

function filterInvoices(){
    let q      = document.getElementById("invoice-search").value.toLowerCase();
    let type   = document.getElementById("type-filter").value;
    let status = document.getElementById("status-filter").value;
    renderInvoices(allInvoices.filter(i=>{
        let matchQ = !q || i.client.toLowerCase().includes(q) || i.invoice_number.toLowerCase().includes(q);
        let matchT = !type   || i.invoice_type === type;
        let matchS = !status || i.status === status;
        return matchQ && matchT && matchS;
    }));
}

function renderInvoices(invoices){
    if(!invoices.length){
        document.getElementById("invoices-body").innerHTML=`<tr><td colspan="9" style="text-align:center;color:var(--muted);padding:40px">No invoices yet.</td></tr>`;
        return;
    }
    const typeLabel={cash:"💵 Cash",full_payment:"📋 Full Payment",consignment:"🔄 Consignment"};
    document.getElementById("invoices-body").innerHTML = invoices.map(i=>{
        let actionBtns=`<div style="display:flex;gap:5px;flex-wrap:wrap">
            <button class="action-btn" onclick="window.open('/b2b/invoice/${i.id}/print','_blank')">🖨 Print</button>
            ${(isAdmin || hasPermission("action_b2b_delete"))?`<button class="action-btn danger" onclick="deleteInvoice(${i.id},'${i.invoice_number}')">Delete</button>`:""}
        </div>`;
        return `<tr>
            <td style="font-family:var(--mono);font-size:12px;color:var(--blue)">${i.invoice_number}</td>
            <td class="name">${i.client}</td>
            <td><span class="badge badge-${i.invoice_type}">${typeLabel[i.invoice_type]||i.invoice_type}</span></td>
            <td style="font-family:var(--mono);font-weight:700">${i.total.toFixed(2)}</td>
            <td style="font-family:var(--mono);color:var(--green)">${i.amount_paid.toFixed(2)}</td>
            <td style="font-family:var(--mono);color:${i.balance_due>0?"var(--warn)":"var(--muted)"}">${i.balance_due>0?i.balance_due.toFixed(2):"—"}</td>
            <td><span class="badge badge-${i.status}">${i.status}</span></td>
            <td style="font-size:12px;color:var(--muted)">${i.created_at}</td>
            <td>${actionBtns}</td>
        </tr>`;
    }).join("");
}

/* ── PAYMENT ── */
function openPayModal(id,num,balance){
    payingInvoiceId=id;
    document.getElementById("pay-modal-sub").innerText=`${num} — Balance: ${balance.toFixed(2)} EGP`;
    document.getElementById("pay-amount").value=balance.toFixed(2);
    document.getElementById("pay-modal").classList.add("open");
}

async function savePayment(){
    let amount=parseFloat(document.getElementById("pay-amount").value)||0;
    if(amount<=0){ showToast("Enter a valid amount"); return; }
    let res=await fetch(`/b2b/api/invoices/${payingInvoiceId}/pay`,{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({amount,method:document.getElementById("pay-method").value}),
    });
    let data=await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    document.getElementById("pay-modal").classList.remove("open");
    showToast(`Payment recorded ✓ — Revenue recognized! Status: ${data.status}`);
    loadInvoices(); loadClients(); loadStats();
}

/* ── CONSIGNMENT PAYMENT ── */
let consPayingInvoiceId = null;

function openConsPayModal(id, num, balance){
    consPayingInvoiceId = id;
    document.getElementById("cons-pay-sub").innerText = `${num} — Balance: ${balance.toFixed(2)} EGP`;
    document.getElementById("cons-pay-amount").value  = balance.toFixed(2);
    document.getElementById("cons-pay-notes").value   = "";

    // Fill month selector with last 12 months
    let sel = document.getElementById("cons-pay-month");
    sel.innerHTML = '<option value="">General payment (no specific month)</option>';
    let d = new Date();
    for(let i=0; i<12; i++){
        let label = d.toLocaleDateString("en-GB",{month:"long",year:"numeric"});
        sel.innerHTML += `<option value="${label}">${label}</option>`;
        d.setMonth(d.getMonth()-1);
    }

    document.getElementById("cons-pay-modal").classList.add("open");
}

async function saveConsPayment(){
    let amount = parseFloat(document.getElementById("cons-pay-amount").value)||0;
    if(amount<=0){ showToast("Enter a valid amount"); return; }
    let month  = document.getElementById("cons-pay-month").value;
    let notes  = document.getElementById("cons-pay-notes").value.trim()||null;
    let res    = await fetch(`/b2b/api/invoices/${consPayingInvoiceId}/consignment-payment`,{
        method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({amount, month_label:month||null, notes}),
    });
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    document.getElementById("cons-pay-modal").classList.remove("open");
    showToast(`✓ ${data.amount.toFixed(2)} EGP recorded — Revenue recognized! ${month?"("+month+")":""}`);
    loadInvoices(); loadClients(); loadStats();
}

/* ── CONSIGNMENTS ── */
async function loadConsignments(){
    let conses=await (await fetch("/b2b/api/consignments")).json();
    if(!conses.length){
        document.getElementById("cons-body").innerHTML=`<tr><td colspan="9" style="text-align:center;color:var(--muted);padding:40px">No consignments yet.</td></tr>`;
        return;
    }
    document.getElementById("cons-body").innerHTML = conses.map(c=>`
        <tr>
            <td style="font-family:var(--mono);font-size:12px;color:var(--teal)">${c.ref_number}</td>
            <td class="name">${c.client}</td>
            <td style="font-family:var(--mono)">${c.total_sent.toFixed(0)}</td>
            <td style="font-family:var(--mono);color:var(--green)">${c.total_sold.toFixed(0)}</td>
            <td style="font-family:var(--mono);color:var(--blue)">${c.total_returned.toFixed(0)}</td>
            <td style="font-family:var(--mono);color:var(--warn);font-weight:700">${c.total_revenue.toFixed(2)}</td>
            <td><span class="badge badge-${c.status}">${c.status}</span></td>
            <td style="font-size:12px;color:var(--muted)">${c.created_at}</td>
            <td>${c.status!=="closed"?`<button class="action-btn teal" onclick="openSettle(${c.id})">Settle</button>`:"✓"}</td>
        </tr>`).join("");
}

async function openSettleByInvoice(invoiceId){
    let conses = await (await fetch("/b2b/api/consignments")).json();
    let cons   = conses.find(c => c.items.length > 0 || c.ref_number);
    // Find the consignment linked to this invoice
    let allC   = await (await fetch("/b2b/api/consignments")).json();
    let found  = allC.find(c => {
        // The consignment is linked via invoice_id on backend, load all and match by client+date proximity
        return true; // we'll filter properly below
    });
    // Fetch full list and find by invoice association
    let res  = await fetch("/b2b/api/consignments");
    let data = await res.json();
    // Find the consignment for this invoice — match via the invoice items
    let invoice = allInvoices.find(i => i.id === invoiceId);
    if(!invoice){ showToast("Invoice not found"); return; }
    // Get consignments for this client and find active one matching invoice total
    let match = data.find(c =>
        c.client_id === invoice.client_id &&
        c.status !== "closed" &&
        Math.abs(c.items.reduce((s,ci)=>s+ci.qty_sent*ci.unit_price,0) - invoice.total) < 0.01
    );
    if(!match){
        // fallback: just get latest active consignment for this client
        match = data.find(c => c.client_id === invoice.client_id && c.status !== "closed");
    }
    if(!match){ showToast("No active consignment found for this invoice"); return; }
    openSettle(match.id);
}

async function openSettle(id){
    settlingConsId=id;
    let conses=await (await fetch("/b2b/api/consignments")).json();
    let cons=conses.find(c=>c.id===id);
    if(!cons) return;
    document.getElementById("side-title").innerText=`Settle — ${cons.ref_number} (${cons.client})`;
    document.getElementById("side-body").innerHTML=`
        <p style="color:var(--muted);font-size:13px;margin-bottom:6px">Enter qty sold and returned.</p>
        <div style="background:rgba(0,255,157,.06);border:1px solid rgba(0,255,157,.15);border-radius:8px;padding:10px 12px;margin-bottom:16px;font-size:12px;color:var(--green);">
            Revenue will be recognized only for qty sold — moving from Deferred Revenue to Sales Revenue.
        </div>
        ${cons.items.map(item=>`
            <div class="cons-item-card" data-item-id="${item.id}">
                <div style="font-weight:700;margin-bottom:4px">${item.product}</div>
                <div style="font-size:12px;color:var(--muted);margin-bottom:10px">
                    Sent: ${item.qty_sent.toFixed(0)} &nbsp;|&nbsp;
                    Sold so far: ${item.qty_sold.toFixed(0)} &nbsp;|&nbsp;
                    Pending: <b style="color:var(--warn)">${item.qty_pending.toFixed(0)}</b> &nbsp;|&nbsp;
                    Price: ${item.unit_price.toFixed(2)} EGP
                </div>
                <div class="cons-grid">
                    <div>
                        <div style="font-size:10px;color:var(--green);font-weight:700;text-transform:uppercase;letter-spacing:1px;margin-bottom:5px">Qty Sold</div>
                        <input class="cons-input" type="number" placeholder="0" min="0" step="any" value="0" data-field="sold">
                    </div>
                    <div>
                        <div style="font-size:10px;color:var(--blue);font-weight:700;text-transform:uppercase;letter-spacing:1px;margin-bottom:5px">Qty Returned</div>
                        <input class="cons-input" type="number" placeholder="0" min="0" step="any" value="0" data-field="returned">
                    </div>
                </div>
            </div>`).join("")}
        <button class="btn btn-teal" style="width:100%;margin-top:8px;justify-content:center" onclick="saveSettle()">Confirm Settlement & Recognize Revenue</button>
    `;
    document.getElementById("side-bg").classList.add("open");
    document.getElementById("side-panel").classList.add("open");
}

function closeSide(){
    document.getElementById("side-bg").classList.remove("open");
    document.getElementById("side-panel").classList.remove("open");
}

async function saveSettle(){
    let rows=document.querySelectorAll(".cons-item-card");
    let items=[];
    rows.forEach(row=>{
        items.push({
            consignment_item_id: parseInt(row.dataset.itemId),
            qty_sold:     parseFloat(row.querySelector('[data-field="sold"]').value)||0,
            qty_returned: parseFloat(row.querySelector('[data-field="returned"]').value)||0,
        });
    });
    let res=await fetch(`/b2b/api/consignments/${settlingConsId}/settle`,{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({items}),
    });
    let data=await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    closeSide();
    showToast(`Settlement done ✓  Revenue recognized: ${data.total_revenue.toFixed(2)} EGP`);
    loadConsignments(); loadClients(); loadStats();
}

["client-modal","invoice-modal","pay-modal","cons-pay-modal","pl-modal"].forEach(id=>{
    document.getElementById(id).addEventListener("click",function(e){ if(e.target===this) this.classList.remove("open"); });
});

let toastTimer=null;
function showToast(msg){
    let t=document.getElementById("toast");
    t.innerText=msg; t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer=setTimeout(()=>t.classList.remove("show"),4500);
}

/* ── PRICE LISTS ── */
let plClientPrices = [];   // current client's price entries from API
let plInitDone = false;

function initPriceListTab(){
    if(plInitDone) return;
    plInitDone = true;
    // Populate client dropdown
    let sel = document.getElementById("pl-client-select");
    sel.innerHTML = '<option value="">— Select a client —</option>';
    allClients.forEach(c=>{
        let opt = document.createElement("option");
        opt.value = c.id; opt.textContent = c.name;
        sel.appendChild(opt);
    });
}

async function loadPriceList(){
    let clientId = document.getElementById("pl-client-select").value;
    let addBtn   = document.getElementById("btn-add-price");
    let tbody    = document.getElementById("pl-body");
    if(!clientId){
        addBtn.style.display = "none";
        tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:40px">Select a client to view their price list.</td></tr>`;
        return;
    }
    addBtn.style.display = "";
    tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:28px">Loading...</td></tr>`;
    plClientPrices = await (await fetch(`/b2b/api/clients/${clientId}/prices`)).json();
    renderPriceList();
}

function renderPriceList(){
    let tbody = document.getElementById("pl-body");
    if(!plClientPrices.length){
        tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:40px">No custom prices set. All products use default pricing.</td></tr>`;
        return;
    }
    tbody.innerHTML = plClientPrices.map(cp=>{
        let diff = cp.custom_price - cp.default_price;
        let diffStr = diff === 0 ? "—" : (diff > 0 ? `<span style="color:var(--warn)">+${diff.toFixed(2)}</span>` : `<span style="color:var(--green)">${diff.toFixed(2)}</span>`);
        return `<tr>
            <td class="name">${cp.product_name}</td>
            <td style="font-family:var(--mono);font-size:12px;color:var(--muted)">${cp.sku}</td>
            <td style="font-family:var(--mono);color:var(--muted)">${cp.default_price.toFixed(2)}</td>
            <td style="font-family:var(--mono);font-weight:700;color:var(--blue)">${cp.custom_price.toFixed(2)}</td>
            <td style="font-family:var(--mono)">${diffStr}</td>
            <td style="display:flex;gap:6px">
                <button class="action-btn" onclick="editPriceEntry(${cp.product_id},${cp.custom_price})">Edit</button>
                <button class="action-btn danger" onclick="deletePriceEntry(${cp.product_id},'${cp.product_name.replace(/'/g,"\\'")}')">Remove</button>
            </td>
        </tr>`;
    }).join("");
}

function openAddPriceModal(){
    let clientId = document.getElementById("pl-client-select").value;
    if(!clientId){ showToast("Select a client first"); return; }
    let client = allClients.find(c=>c.id==clientId);
    document.getElementById("pl-modal-sub").innerText = `Client: ${client ? client.name : ""}`;
    // Build product dropdown — skip products already in the list
    let existing = new Set(plClientPrices.map(p=>p.product_id));
    let prodSel = document.getElementById("pl-product");
    prodSel.innerHTML = allProducts.map(p=>{
        let label = p.sku ? `${p.sku} — ${p.name}` : p.name;
        let note  = existing.has(p.id) ? " ★" : "";
        return `<option value="${p.id}" data-default="${p.default_price || p.price}">${label}${note}</option>`;
    }).join("");
    document.getElementById("pl-price").value = "";
    onPlProductChange();
    document.getElementById("pl-modal").classList.add("open");
}

function editPriceEntry(productId, currentPrice){
    openAddPriceModal();
    // Select the right product
    let sel = document.getElementById("pl-product");
    for(let opt of sel.options){ if(parseInt(opt.value)===productId){ sel.value=productId; break; } }
    onPlProductChange();
    document.getElementById("pl-price").value = currentPrice.toFixed(2);
}

function onPlProductChange(){
    let sel  = document.getElementById("pl-product");
    let opt  = sel.options[sel.selectedIndex];
    let hint = document.getElementById("pl-default-hint");
    if(!opt || !opt.value){ hint.innerText = ""; return; }
    let def = parseFloat(opt.dataset.default) || 0;
    // Check if client already has a custom price for this product
    let existing = plClientPrices.find(cp=>cp.product_id===parseInt(opt.value));
    hint.innerHTML = `Default price: <b style="font-family:var(--mono)">${def.toFixed(2)} ج.م.</b>` +
        (existing ? `&nbsp;&nbsp;|&nbsp;&nbsp;Current custom price: <b style="font-family:var(--mono);color:var(--blue)">${existing.custom_price.toFixed(2)} ج.م.</b>` : "");
    if(!document.getElementById("pl-price").value && existing)
        document.getElementById("pl-price").value = existing.custom_price.toFixed(2);
}

async function savePriceEntry(){
    let clientId  = document.getElementById("pl-client-select").value;
    let productId = parseInt(document.getElementById("pl-product").value);
    let price     = parseFloat(document.getElementById("pl-price").value);
    if(!clientId || !productId){ showToast("Select a product"); return; }
    if(isNaN(price) || price < 0){ showToast("Enter a valid price"); return; }
    let res  = await fetch(`/b2b/api/clients/${clientId}/prices`, {
        method:"PUT", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({product_id: productId, price}),
    });
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    document.getElementById("pl-modal").classList.remove("open");
    showToast("Price saved ✓");
    plClientPrices = await (await fetch(`/b2b/api/clients/${clientId}/prices`)).json();
    renderPriceList();
}

async function deletePriceEntry(productId, productName){
    let clientId = document.getElementById("pl-client-select").value;
    if(!confirm(`Remove custom price for "${productName}"? The default product price will apply.`)) return;
    await fetch(`/b2b/api/clients/${clientId}/prices/${productId}`, {method:"DELETE"});
    showToast("Custom price removed ✓");
    plClientPrices = await (await fetch(`/b2b/api/clients/${clientId}/prices`)).json();
    renderPriceList();
}

init();
</script>
</body>
</html>"""
