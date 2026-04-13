"""Receive Products service.

Handles stock intake: updates inventory, creates a StockMove, and — when a
cost is provided — posts a linked Expense + double-entry Journal.

The internal `_create_receipt_core` does all work without committing so that
`create_receipt_batch` can process multiple products in one transaction.
"""
from __future__ import annotations

from datetime import date as date_type
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.log import record
from app.models.accounting import Account, Journal, JournalEntry
from app.models.expense import Expense, ExpenseCategory
from app.models.inventory import StockMove
from app.models.product import Product
from app.models.receipt import ProductReceipt
from app.models.user import User

_MONEY = Decimal("0.01")
_QTY   = Decimal("0.001")

STOCK_PURCHASE_ACCOUNT_CODE  = "5011"
STOCK_PURCHASE_CATEGORY_NAME = "Stock Purchase"


# ── Input schemas ─────────────────────────────────────────────────────────────

class ReceiptCreate(BaseModel):
    """Single-product receipt (used by the batch assembler and the legacy endpoint)."""
    product_id:   int             = Field(..., ge=1)
    qty:          float           = Field(..., gt=0)
    unit_cost:    Optional[float] = Field(None, ge=0)
    receive_date: date_type
    supplier_ref: Optional[str]   = Field(None, max_length=150)
    notes:        Optional[str]   = None


class BatchReceiptItem(BaseModel):
    """One line inside a batch receive."""
    product_id: int             = Field(..., ge=1)
    qty:        float           = Field(..., gt=0)
    unit_cost:  Optional[float] = Field(None, ge=0)


class BatchReceiptCreate(BaseModel):
    """Multi-product receive submitted from the form."""
    receive_date: date_type
    supplier_ref: Optional[str] = Field(None, max_length=150)
    notes:        Optional[str] = None
    items:        list[BatchReceiptItem] = Field(..., min_length=1)


# ── Private helpers ───────────────────────────────────────────────────────────

async def _next_receipt_ref(db: AsyncSession) -> str:
    result = await db.execute(select(func.max(ProductReceipt.id)))
    max_id = result.scalar() or 0
    return f"RCV-{str(max_id + 1).zfill(5)}"


async def _next_expense_ref(db: AsyncSession) -> str:
    result = await db.execute(select(func.max(Expense.id)))
    max_id = result.scalar() or 0
    return f"EXP-{str(max_id + 1).zfill(5)}"


async def _ensure_account(
    db: AsyncSession,
    code: str,
    name: str,
    account_type: str,
) -> Account:
    result  = await db.execute(select(Account).where(Account.code == code))
    account = result.scalar_one_or_none()
    if account is None:
        account = Account(code=code, name=name, type=account_type, balance=0)
        db.add(account)
        await db.flush()
    return account


async def _get_or_create_stock_purchase_category(db: AsyncSession) -> ExpenseCategory:
    result   = await db.execute(
        select(ExpenseCategory).where(
            ExpenseCategory.account_code == STOCK_PURCHASE_ACCOUNT_CODE
        )
    )
    category = result.scalar_one_or_none()
    if category is not None:
        return category

    await _ensure_account(
        db, STOCK_PURCHASE_ACCOUNT_CODE, STOCK_PURCHASE_CATEGORY_NAME, "expense"
    )
    category = ExpenseCategory(
        name=STOCK_PURCHASE_CATEGORY_NAME,
        account_code=STOCK_PURCHASE_ACCOUNT_CODE,
        is_active="1",
    )
    db.add(category)
    await db.flush()
    return category


async def _post_receipt_expense(
    db: AsyncSession,
    *,
    category: ExpenseCategory,
    product_name: str,
    receipt_ref: str,
    qty: Decimal,
    total_cost: Decimal,
    receive_date: date_type,
    supplier_ref: Optional[str],
    user_id: Optional[int],
) -> Expense:
    """Create Expense + double-entry Journal. No commit — caller owns transaction."""
    exp_ref = await _next_expense_ref(db)

    journal = Journal(
        ref_type="expense",
        description=f"{STOCK_PURCHASE_CATEGORY_NAME} — {receipt_ref}",
        user_id=user_id,
    )
    db.add(journal)
    await db.flush()

    amount      = float(total_cost)
    expense_acc = await _ensure_account(db, category.account_code, category.name, "expense")
    cash_acc    = await _ensure_account(db, "1000", "Cash", "asset")

    db.add(JournalEntry(journal_id=journal.id, account_id=expense_acc.id, debit=amount, credit=0))
    db.add(JournalEntry(journal_id=journal.id, account_id=cash_acc.id,    debit=0,      credit=amount))
    expense_acc.balance += Decimal(str(amount))
    cash_acc.balance    -= Decimal(str(amount))

    expense = Expense(
        ref_number=exp_ref,
        category_id=category.id,
        user_id=user_id,
        expense_date=receive_date,
        amount=amount,
        payment_method="cash",
        vendor=supplier_ref,
        description=(
            f"Stock receipt {receipt_ref} — "
            f"{float(qty):.3f} \u00d7 {product_name}"
        ),
        journal_id=journal.id,
    )
    db.add(expense)
    return expense


async def _create_receipt_core(
    db: AsyncSession,
    data: ReceiptCreate,
    current_user: User,
) -> dict[str, Any]:
    """
    All receipt logic for one product — no commit.
    Caller must call db.commit() after (possibly after processing more items).
    """
    result  = await db.execute(select(Product).where(Product.id == data.product_id))
    product = result.scalar_one_or_none()
    if product is None:
        raise HTTPException(status_code=404, detail=f"Product {data.product_id} not found")

    qty = Decimal(str(data.qty)).quantize(_QTY, rounding=ROUND_HALF_UP)

    unit_cost:  Optional[Decimal] = None
    total_cost: Optional[Decimal] = None
    if data.unit_cost is not None and data.unit_cost > 0:
        unit_cost  = Decimal(str(data.unit_cost)).quantize(_MONEY, rounding=ROUND_HALF_UP)
        total_cost = (qty * unit_cost).quantize(_MONEY, rounding=ROUND_HALF_UP)

    ref_number = await _next_receipt_ref(db)

    qty_before    = Decimal(str(product.stock or 0))
    qty_after     = qty_before + qty
    product.stock = qty_after
    if unit_cost is not None:
        product.cost = unit_cost

    supplier_ref = (data.supplier_ref or "").strip() or None
    notes        = (data.notes or "").strip() or None

    receipt = ProductReceipt(
        ref_number=ref_number,
        product_id=product.id,
        user_id=current_user.id,
        receive_date=data.receive_date,
        qty=qty,
        unit_cost=unit_cost,
        total_cost=total_cost,
        supplier_ref=supplier_ref,
        notes=notes,
    )
    db.add(receipt)

    move = StockMove(
        product_id=product.id,
        type="in",
        qty=qty,
        qty_before=qty_before,
        qty_after=qty_after,
        ref_type="receipt",
        ref_id=0,
        note=f"Receipt {ref_number}",
        user_id=current_user.id,
    )
    db.add(move)
    await db.flush()
    move.ref_id = receipt.id

    expense_ref: Optional[str] = None
    if total_cost and total_cost > 0:
        category = await _get_or_create_stock_purchase_category(db)
        expense  = await _post_receipt_expense(
            db,
            category=category,
            product_name=product.name,
            receipt_ref=ref_number,
            qty=qty,
            total_cost=total_cost,
            receive_date=data.receive_date,
            supplier_ref=supplier_ref,
            user_id=current_user.id,
        )
        await db.flush()
        receipt.expense_id = expense.id
        expense_ref = expense.ref_number

    record(
        db,
        "Receive",
        "receive_products",
        f"{product.name} — {float(qty):.3f} — {ref_number}",
        user=current_user,
        ref_type="receipt",
        ref_id=receipt.id,
    )

    return {
        "id":           receipt.id,
        "ref_number":   ref_number,
        "product_id":   product.id,
        "product_name": product.name,
        "product_sku":  product.sku,
        "receive_date": data.receive_date.isoformat(),
        "qty":          float(qty),
        "unit_cost":    float(unit_cost)  if unit_cost  else None,
        "total_cost":   float(total_cost) if total_cost else None,
        "supplier_ref": supplier_ref,
        "notes":        notes,
        "expense_id":   receipt.expense_id,
        "expense_ref":  expense_ref,
    }


# ── Public API ────────────────────────────────────────────────────────────────

async def create_receipt(
    db: AsyncSession,
    data: ReceiptCreate,
    current_user: User,
) -> dict[str, Any]:
    """Single-product receive, one transaction."""
    result = await _create_receipt_core(db, data, current_user)
    await db.commit()
    return result


async def create_receipt_batch(
    db: AsyncSession,
    data: BatchReceiptCreate,
    current_user: User,
) -> dict[str, Any]:
    """
    Multi-product receive submitted in one form.

    All items are processed inside a single transaction:
    one commit at the end — if any product is invalid the whole batch rolls back.
    """
    receipts: list[dict[str, Any]] = []
    for item in data.items:
        line = ReceiptCreate(
            product_id=item.product_id,
            qty=item.qty,
            unit_cost=item.unit_cost,
            receive_date=data.receive_date,
            supplier_ref=data.supplier_ref,
            notes=data.notes,
        )
        receipts.append(await _create_receipt_core(db, line, current_user))

    await db.commit()

    total_cost = sum(r["total_cost"] or 0 for r in receipts)
    return {
        "count":      len(receipts),
        "total_cost": round(total_cost, 2),
        "receipts":   receipts,
    }


async def list_receipts(
    db: AsyncSession,
    *,
    skip: int = 0,
    limit: int = 50,
    product_id: Optional[int] = None,
) -> dict[str, Any]:
    """Paginated receipt history with product, user, and expense refs."""
    base = select(ProductReceipt)
    if product_id is not None:
        base = base.where(ProductReceipt.product_id == product_id)

    count_result = await db.execute(
        select(func.count()).select_from(base.subquery())
    )
    total = count_result.scalar() or 0

    rows_stmt = (
        base.options(
            selectinload(ProductReceipt.product),
            selectinload(ProductReceipt.user),
            selectinload(ProductReceipt.expense),
        )
        .order_by(ProductReceipt.receive_date.desc(), ProductReceipt.id.desc())
        .offset(skip)
        .limit(limit)
    )
    result   = await db.execute(rows_stmt)
    receipts = result.scalars().all()

    return {
        "total": total,
        "items": [
            {
                "id":           r.id,
                "ref_number":   r.ref_number,
                "product_id":   r.product_id,
                "product_name": r.product.name if r.product else None,
                "product_sku":  r.product.sku  if r.product else None,
                "receive_date": r.receive_date.isoformat() if r.receive_date else None,
                "qty":          float(r.qty),
                "unit_cost":    float(r.unit_cost)  if r.unit_cost  is not None else None,
                "total_cost":   float(r.total_cost) if r.total_cost is not None else None,
                "supplier_ref": r.supplier_ref,
                "notes":        r.notes,
                "expense_id":   r.expense_id,
                "expense_ref":  r.expense.ref_number if r.expense else None,
                "received_by":  r.user.name if r.user else None,
                "created_at":   r.created_at.isoformat() if r.created_at else None,
            }
            for r in receipts
        ],
    }
