import json

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import or_, select
from fastapi import HTTPException
from decimal import Decimal

from app.models.invoice import Invoice, InvoiceItem
from app.models.product import Product
from app.models.customer import Customer
from app.models.inventory import StockMove
from app.models.accounting import Account, Journal, JournalEntry
from app.schemas.invoice import InvoiceCreate
from app.services.barcode_service import normalize_barcode_value
from app.services.location_inventory_service import sync_product_stock_to_default_location
from app.core.log import record
from app.core.permissions import has_permission


async def post_journal(
    db: AsyncSession,
    description: str,
    entries: list,
    user_id=None,
    created_at=None,
):
    """Post a double-entry journal.

    ``created_at`` is optional; when supplied (e.g. for historical imports) the
    journal is stamped with that datetime rather than the DB server default.
    When omitted the server default (now()) applies.
    """
    kwargs = dict(ref_type="invoice", description=description, user_id=user_id)
    if created_at is not None:
        kwargs["created_at"] = created_at
    journal = Journal(**kwargs)
    db.add(journal)
    await db.flush()
    for code, debit, credit in entries:
        _r = await db.execute(select(Account).where(Account.code == code))
        acc = _r.scalar_one_or_none()
        if acc:
            db.add(JournalEntry(
                journal_id=journal.id, account_id=acc.id,
                debit=debit, credit=credit,
            ))
            acc.balance += Decimal(str(debit)) - Decimal(str(credit))


async def get_walk_in_customer_id(db: AsyncSession) -> int:
    _r = await db.execute(select(Customer).where(Customer.name == "Walk-in Customer"))
    customer = _r.scalar_one_or_none()
    if not customer:
        customer = Customer(name="Walk-in Customer", phone="", email="", address="")
        db.add(customer)
        await db.flush()
    return customer.id


async def create_invoice(db: AsyncSession, data: InvoiceCreate, user_id: int, user=None) -> dict:
    try:
        if not data.items:
            raise HTTPException(status_code=400, detail="Cart is empty")

        subtotal = 0
        line_items = []  # (product, qty, line_total, sell_price, catalog_price)
        price_edits = []

        for item in data.items:
            normalized_sku = normalize_barcode_value(item.sku)
            _r = await db.execute(
                select(Product).where(or_(Product.is_active.is_(True), Product.is_active.is_(None)))
            )
            products = _r.scalars().all()
            product = next(
                (candidate for candidate in products if normalize_barcode_value(candidate.sku) == normalized_sku),
                None,
            )
            if not product:
                raise HTTPException(status_code=404, detail=f"Product not found: {item.sku}")
            if product.stock < item.qty:
                raise HTTPException(
                    status_code=400,
                    detail=f"Not enough stock for {product.name}. Available: {float(product.stock)}",
                )

            catalog_price = float(product.price)
            sell_price = item.unit_price if item.unit_price is not None else catalog_price

            # Defense: named customers may not receive price edits
            if data.customer_id is not None and sell_price != catalog_price:
                raise HTTPException(
                    status_code=400,
                    detail="Price edits only allowed for general customer.",
                )

            # Enforce permission for discounts greater than 50% off catalog
            if sell_price < catalog_price * 0.5:
                if user is None or not has_permission(user, "action_pos_edit_price"):
                    raise HTTPException(
                        status_code=403,
                        detail="Permission denied: action_pos_edit_price",
                    )

            line_total = sell_price * item.qty
            subtotal += line_total
            line_items.append((product, item.qty, line_total, sell_price, catalog_price))

            if sell_price != catalog_price:
                price_edits.append({
                    "sku": product.sku,
                    "catalog_price": catalog_price,
                    "sold_at": sell_price,
                    "qty": item.qty,
                })

        discount_amount = subtotal * (data.discount_percent / 100)
        total = subtotal - discount_amount

        is_settle_later = getattr(data, "settle_later", False)
        payment_method = getattr(data, "payment_method", "cash") or "cash"
        status = "unpaid" if is_settle_later else "paid"
        customer_id = data.customer_id or await get_walk_in_customer_id(db)

        invoice = Invoice(
            customer_id=customer_id,
            user_id=user_id,
            payment_method=payment_method,
            subtotal=round(subtotal, 2),
            discount=round(discount_amount, 2),
            total=round(total, 2),
            notes=data.notes,
            status=status,
        )
        db.add(invoice)
        await db.flush()
        invoice.invoice_number = f"INV-{str(invoice.id).zfill(5)}"

        for product, qty, line_total, sell_price, _catalog_price in line_items:
            db.add(InvoiceItem(
                invoice_id=invoice.id,
                product_id=product.id,
                sku=product.sku,
                name=product.name,
                qty=qty,
                unit_price=sell_price,
                total=round(line_total, 2),
            ))
            before = float(product.stock)
            after = before - qty
            _, location_stock = await sync_product_stock_to_default_location(db, product=product)
            location_before = float(location_stock.qty)
            location_after = location_before - qty
            if location_after < 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"Not enough stock for {product.name}. Available: {location_before}",
                )
            location_stock.qty = location_after
            product.stock = after
            db.add(StockMove(
                product_id=product.id,
                type="out",
                qty=-qty,
                qty_before=before,
                qty_after=after,
                ref_type="invoice",
                ref_id=invoice.id,
                note=f"Sale - {invoice.invoice_number}",
            ))

        if not is_settle_later:
            acc_code = "1000"
            await post_journal(db, f"Sale - {invoice.invoice_number}", [
                (acc_code, round(total, 2), 0),
                ("4000", 0, round(total, 2)),
            ], user_id=user_id)
        else:
            await post_journal(db, f"Unpaid Sale - {invoice.invoice_number}", [
                ("1100", round(total, 2), 0),
                ("4000", 0, round(total, 2)),
            ], user_id=user_id)

        from app.models.user import User as UserModel
        _ur = await db.execute(select(UserModel).where(UserModel.id == user_id))
        user_obj = _ur.scalar_one_or_none()
        _cr = await db.execute(select(Customer).where(Customer.id == invoice.customer_id))
        cust_obj = _cr.scalar_one_or_none()
        cust_name = cust_obj.name if cust_obj else "Walk-in"
        record(
            db,
            "POS",
            "sale",
            f"Invoice {invoice.invoice_number} - {cust_name} - {float(invoice.total):.2f} - {payment_method}",
            user=user_obj,
            ref_type="invoice",
            ref_id=invoice.id,
        )

        if price_edits:
            total_discount_vs_catalog = sum(
                (e["catalog_price"] - e["sold_at"]) * e["qty"] for e in price_edits
            )
            record(
                db,
                "POS",
                "pos_sale_with_price_edits",
                json.dumps({
                    "invoice_id": invoice.id,
                    "customer_id": None,
                    "edits": price_edits,
                    "total_discount_vs_catalog": round(total_discount_vs_catalog, 2),
                }),
                user=user_obj,
                ref_type="invoice",
                ref_id=invoice.id,
            )

        await db.commit()
        await db.refresh(invoice)
        return {
            "id": invoice.id,
            "invoice_number": invoice.invoice_number,
            "status": invoice.status,
            "payment_method": invoice.payment_method,
            "total": float(invoice.total),
        }
    except Exception:
        await db.rollback()
        raise
