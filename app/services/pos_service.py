from sqlalchemy.orm import Session
from fastapi import HTTPException
from decimal import Decimal

from app.models.invoice import Invoice, InvoiceItem
from app.models.product import Product
from app.models.customer import Customer
from app.models.inventory import StockMove
from app.models.accounting import Account, Journal, JournalEntry
from app.schemas.invoice import InvoiceCreate


def _next_invoice_number(db: Session) -> str:
    count = db.query(Invoice).count()
    return f"INV-{str(count + 1).zfill(5)}"


def _post_journal(db, description, entries, user_id=None):
    journal = Journal(ref_type="invoice", description=description, user_id=user_id)
    db.add(journal); db.flush()
    for code, debit, credit in entries:
        acc = db.query(Account).filter(Account.code == code).first()
        if acc:
            db.add(JournalEntry(
                journal_id=journal.id, account_id=acc.id,
                debit=debit, credit=credit,
            ))
            acc.balance += Decimal(str(debit)) - Decimal(str(credit))


def _get_walk_in_customer_id(db: Session) -> int:
    customer = (
        db.query(Customer)
        .filter(Customer.name == "Walk-in Customer")
        .first()
    )
    if not customer:
        customer = Customer(name="Walk-in Customer", phone="", email="", address="")
        db.add(customer)
        db.flush()
    return customer.id


def create_invoice(db: Session, data: InvoiceCreate, user_id: int) -> Invoice:
    if not data.items:
        raise HTTPException(status_code=400, detail="Cart is empty")

    subtotal = 0
    line_items = []

    for item in data.items:
        product = db.query(Product).filter(Product.sku == item.sku).first()
        if not product:
            raise HTTPException(status_code=404, detail=f"Product not found: {item.sku}")
        if product.stock < item.qty:
            raise HTTPException(status_code=400,
                detail=f"Not enough stock for {product.name}. Available: {float(product.stock)}")
        line_total = float(product.price) * item.qty
        subtotal  += line_total
        line_items.append((product, item.qty, line_total))

    discount_amount = subtotal * (data.discount_percent / 100)
    total = subtotal - discount_amount

    # settle_later means unpaid
    is_settle_later  = getattr(data, "settle_later", False)
    payment_method   = getattr(data, "payment_method", "cash") or "cash"
    status           = "unpaid" if is_settle_later else "paid"
    customer_id      = data.customer_id or _get_walk_in_customer_id(db)

    invoice = Invoice(
        invoice_number=_next_invoice_number(db),
        customer_id=customer_id,
        user_id=user_id,
        payment_method=payment_method,
        subtotal=round(subtotal, 2),
        discount=round(discount_amount, 2),
        total=round(total, 2),
        notes=data.notes,
        status=status,
    )
    db.add(invoice); db.flush()

    for product, qty, line_total in line_items:
        db.add(InvoiceItem(
            invoice_id=invoice.id,
            product_id=product.id,
            sku=product.sku,
            name=product.name,
            qty=qty,
            unit_price=float(product.price),
            total=round(line_total, 2),
        ))
        before = float(product.stock)
        after  = before - qty
        product.stock = after
        db.add(StockMove(
            product_id=product.id, type="out",
            qty=-qty, qty_before=before, qty_after=after,
            ref_type="invoice", ref_id=invoice.id,
            note=f"Sale - {invoice.invoice_number}",
        ))

    # Journal entry — only for paid invoices
    if not is_settle_later:
        acc_code = "1000"  # Cash for cash, same for visa (both physical receipt)
        _post_journal(db, f"Sale - {invoice.invoice_number}", [
            (acc_code, round(total, 2), 0),
            ("4000",   0, round(total, 2)),
        ], user_id=user_id)
    else:
        # Settle later → Accounts Receivable
        _post_journal(db, f"Unpaid Sale - {invoice.invoice_number}", [
            ("1100", round(total, 2), 0),
            ("4000", 0, round(total, 2)),
        ], user_id=user_id)

    db.commit()
    db.refresh(invoice)
    return invoice
