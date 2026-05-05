"""Receive Products service.

Handles stock intake: updates inventory, creates a StockMove, and — when a
cost is provided — posts a linked Expense + double-entry Journal.

The internal `_create_receipt_core` does all work without committing so that
`create_receipt_batch` can process multiple products in one transaction.
"""
from __future__ import annotations

from datetime import date as date_type
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Optional

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
STOCK_PURCHASE_CATEGORY_NAME = "Products"
PACKAGING_CATEGORY_NAME = "Packaging Materials"
PACKAGING_CATEGORY_ACCOUNT_CODE = "5007"
PRODUCT_TYPE_PRODUCTS = "products"
PRODUCT_TYPE_PACKAGING = "packaging_materials"
RECEIPT_PRODUCT_TYPE_TO_CATEGORY = {
    PRODUCT_TYPE_PRODUCTS: (STOCK_PURCHASE_CATEGORY_NAME, STOCK_PURCHASE_ACCOUNT_CODE),
    PRODUCT_TYPE_PACKAGING: (PACKAGING_CATEGORY_NAME, PACKAGING_CATEGORY_ACCOUNT_CODE),
}


# ── Input schemas ─────────────────────────────────────────────────────────────

class ReceiptCreate(BaseModel):
    """Single-product receipt (used by the batch assembler and the legacy endpoint)."""
    product_id:   int             = Field(..., ge=1)
    qty:          float           = Field(..., gt=0)
    unit_cost:    Optional[float] = Field(None, ge=0)
    product_type: Literal["products", "packaging_materials"]
    receive_date: date_type
    supplier_ref: Optional[str]   = Field(None, max_length=150)
    notes:        Optional[str]   = None
    affect_stock: bool            = True


class BatchReceiptItem(BaseModel):
    """One line inside a batch receive."""
    product_id: int             = Field(..., ge=1)
    qty:        float           = Field(..., gt=0)
    unit_cost:  Optional[float] = Field(None, ge=0)


class BatchReceiptCreate(BaseModel):
    """Multi-product receive submitted from the form."""
    product_type: Literal["products", "packaging_materials"]
    receive_date: date_type
    supplier_ref: Optional[str] = Field(None, max_length=150)
    notes:        Optional[str] = None
    items:        list[BatchReceiptItem] = Field(..., min_length=1)


class ReceiptUpdate(BaseModel):
    """Editable fields for an existing receipt."""
    qty:          float           = Field(..., gt=0)
    unit_cost:    Optional[float] = Field(None, ge=0)
    product_type: Optional[Literal["products", "packaging_materials"]] = None
    receive_date: date_type
    supplier_ref: Optional[str]   = Field(None, max_length=150)
    notes:        Optional[str]   = None


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


async def _get_or_create_receipt_category(
    db: AsyncSession,
    *,
    product_type: str,
) -> ExpenseCategory:
    category_config = RECEIPT_PRODUCT_TYPE_TO_CATEGORY.get(product_type)
    if category_config is None:
        raise HTTPException(status_code=422, detail="Product Type is required")

    category_name, account_code = category_config
    result   = await db.execute(
        select(ExpenseCategory).where(
            ExpenseCategory.name == category_name,
            ExpenseCategory.is_active == "1",
        )
    )
    category = result.scalar_one_or_none()
    if category is not None:
        return category

    result = await db.execute(
        select(ExpenseCategory).where(
            ExpenseCategory.account_code == account_code,
            ExpenseCategory.is_active == "1",
        )
    )
    category = result.scalar_one_or_none()
    if category is not None:
        return category

    await _ensure_account(
        db, account_code, category_name, "expense"
    )
    category = ExpenseCategory(
        name=category_name,
        account_code=account_code,
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
        description=f"{category.name} — {receipt_ref}",
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


async def _get_receipt_or_404(db: AsyncSession, receipt_id: int) -> ProductReceipt:
    result = await db.execute(
        select(ProductReceipt)
        .options(
            selectinload(ProductReceipt.product),
            selectinload(ProductReceipt.user),
            selectinload(ProductReceipt.expense),
        )
        .where(ProductReceipt.id == receipt_id)
    )
    receipt = result.scalar_one_or_none()
    if receipt is None:
        raise HTTPException(status_code=404, detail=f"Receipt {receipt_id} not found")
    return receipt


def _quantize_receipt_values(
    qty_value: float,
    unit_cost_value: Optional[float],
) -> tuple[Decimal, Optional[Decimal], Optional[Decimal]]:
    qty = Decimal(str(qty_value)).quantize(_QTY, rounding=ROUND_HALF_UP)
    unit_cost: Optional[Decimal] = None
    total_cost: Optional[Decimal] = None
    if unit_cost_value is not None and unit_cost_value > 0:
        unit_cost = Decimal(str(unit_cost_value)).quantize(_MONEY, rounding=ROUND_HALF_UP)
        total_cost = (qty * unit_cost).quantize(_MONEY, rounding=ROUND_HALF_UP)
    return qty, unit_cost, total_cost


async def _get_receipt_move(db: AsyncSession, receipt_id: int) -> StockMove | None:
    result = await db.execute(
        select(StockMove)
        .where(StockMove.ref_type == "receipt", StockMove.ref_id == receipt_id)
        .order_by(StockMove.id.desc())
    )
    return result.scalar_one_or_none()


async def _delete_expense_bundle(db: AsyncSession, expense: Expense | None) -> None:
    if expense is None:
        return

    journal = None
    if expense.journal_id:
        result = await db.execute(
            select(Journal)
            .options(selectinload(Journal.entries).selectinload(JournalEntry.account))
            .where(Journal.id == expense.journal_id)
        )
        journal = result.scalar_one_or_none()

    if journal is not None:
        for entry in journal.entries:
            if entry.account is not None and entry.account.balance is not None:
                entry.account.balance = Decimal(str(entry.account.balance)) - Decimal(str(entry.debit or 0)) + Decimal(str(entry.credit or 0))
        await db.delete(journal)

    await db.delete(expense)


async def _sync_receipt_expense(
    db: AsyncSession,
    *,
    receipt: ProductReceipt,
    product_name: str,
    qty: Decimal,
    total_cost: Optional[Decimal],
    receive_date: date_type,
    supplier_ref: Optional[str],
    product_type: Optional[str] = None,
) -> Optional[str]:
    if total_cost is None or total_cost <= 0:
        if receipt.expense_id:
            expense_result = await db.execute(
                select(Expense)
                .options(selectinload(Expense.category))
                .where(Expense.id == receipt.expense_id)
            )
            expense = expense_result.scalar_one_or_none()
            await _delete_expense_bundle(db, expense)
            receipt.expense_id = None
        return None

    if receipt.expense_id is None:
        if not product_type:
            raise HTTPException(status_code=422, detail="Product Type is required")
        category = await _get_or_create_receipt_category(db, product_type=product_type)
        expense = await _post_receipt_expense(
            db,
            category=category,
            product_name=product_name,
            receipt_ref=receipt.ref_number,
            qty=qty,
            total_cost=total_cost,
            receive_date=receive_date,
            supplier_ref=supplier_ref,
            user_id=receipt.user_id,
        )
        await db.flush()
        receipt.expense_id = expense.id
        return expense.ref_number

    expense_result = await db.execute(
        select(Expense)
        .options(selectinload(Expense.category))
        .where(Expense.id == receipt.expense_id)
    )
    expense = expense_result.scalar_one_or_none()
    if expense is None:
        receipt.expense_id = None
        return await _sync_receipt_expense(
            db,
            receipt=receipt,
            product_name=product_name,
            qty=qty,
            total_cost=total_cost,
            receive_date=receive_date,
            supplier_ref=supplier_ref,
            product_type=product_type,
        )

    old_amount = Decimal(str(expense.amount or 0)).quantize(_MONEY, rounding=ROUND_HALF_UP)
    delta = total_cost - old_amount

    expense_category = expense.category
    if expense_category is None:
        category_result = await db.execute(
            select(ExpenseCategory).where(ExpenseCategory.id == expense.category_id)
        )
        expense_category = category_result.scalar_one_or_none()
    if expense_category is None:
        if not product_type:
            raise HTTPException(status_code=422, detail="Product Type is required")
        expense_category = await _get_or_create_receipt_category(db, product_type=product_type)
        expense.category_id = expense_category.id

    expense_account = await _ensure_account(db, expense_category.account_code, expense_category.name, "expense")
    cash_account = await _ensure_account(db, "1000", "Cash", "asset")
    if delta:
        expense_account.balance = Decimal(str(expense_account.balance or 0)) + delta
        cash_account.balance = Decimal(str(cash_account.balance or 0)) - delta

    if expense.journal_id:
        journal_result = await db.execute(
            select(Journal)
            .options(selectinload(Journal.entries))
            .where(Journal.id == expense.journal_id)
        )
        journal = journal_result.scalar_one_or_none()
    else:
        journal = None

    if journal is None:
        journal = Journal(
            ref_type="expense",
            description=f"{expense_category.name} — {receipt.ref_number}",
            user_id=receipt.user_id,
        )
        db.add(journal)
        await db.flush()
        expense.journal_id = journal.id

    debit_entry = next((entry for entry in journal.entries if Decimal(str(entry.debit or 0)) > 0), None)
    credit_entry = next((entry for entry in journal.entries if Decimal(str(entry.credit or 0)) > 0), None)
    if debit_entry is None:
        debit_entry = JournalEntry(journal_id=journal.id, account_id=expense_account.id, debit=0, credit=0)
        db.add(debit_entry)
    debit_entry.account_id = expense_account.id
    debit_entry.debit = float(total_cost)
    debit_entry.credit = 0
    if credit_entry is None:
        credit_entry = JournalEntry(journal_id=journal.id, account_id=cash_account.id, debit=0, credit=0)
        db.add(credit_entry)
    credit_entry.account_id = cash_account.id
    credit_entry.debit = 0
    credit_entry.credit = float(total_cost)
    journal.description = f"{expense_category.name} — {receipt.ref_number}"

    expense.expense_date = receive_date
    expense.amount = float(total_cost)
    expense.vendor = supplier_ref
    expense.description = (
        f"Stock receipt {receipt.ref_number} — "
        f"{float(qty):.3f} × {product_name}"
    )
    return expense.ref_number


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
    if data.affect_stock:
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

    if data.affect_stock:
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
    else:
        await db.flush()

    expense_ref: Optional[str] = None
    if total_cost and total_cost > 0:
        category = await _get_or_create_receipt_category(db, product_type=data.product_type)
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
        "product_type": data.product_type,
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


async def update_receipt(
    db: AsyncSession,
    receipt_id: int,
    data: ReceiptUpdate,
    current_user: User,
) -> dict[str, Any]:
    receipt = await _get_receipt_or_404(db, receipt_id)
    product = receipt.product
    if product is None:
        raise HTTPException(status_code=404, detail=f"Product {receipt.product_id} not found")

    old_qty = Decimal(str(receipt.qty or 0)).quantize(_QTY, rounding=ROUND_HALF_UP)
    new_qty, unit_cost, total_cost = _quantize_receipt_values(data.qty, data.unit_cost)
    qty_delta = new_qty - old_qty

    current_stock = Decimal(str(product.stock or 0)).quantize(_QTY, rounding=ROUND_HALF_UP)
    stock_before_receipt = current_stock - old_qty
    new_stock = stock_before_receipt + new_qty
    if new_stock < 0:
        raise HTTPException(status_code=400, detail="Cannot reduce receipt below current available stock")

    product.stock = new_stock
    if unit_cost is not None:
        product.cost = unit_cost

    supplier_ref = (data.supplier_ref or "").strip() or None
    notes = (data.notes or "").strip() or None

    receipt.qty = new_qty
    receipt.unit_cost = unit_cost
    receipt.total_cost = total_cost
    receipt.receive_date = data.receive_date
    receipt.supplier_ref = supplier_ref
    receipt.notes = notes

    move = await _get_receipt_move(db, receipt.id)
    if move is not None:
        move.qty_before = stock_before_receipt
        move.qty = new_qty
        move.qty_after = new_stock
        move.note = f"Receipt {receipt.ref_number}"

    expense_ref = await _sync_receipt_expense(
        db,
        receipt=receipt,
        product_name=product.name,
        qty=new_qty,
        total_cost=total_cost,
        receive_date=data.receive_date,
        supplier_ref=supplier_ref,
        product_type=data.product_type,
    )

    record(
        db,
        "Receive",
        "update_receipt",
        f"{receipt.ref_number} updated",
        user=current_user,
        ref_type="receipt",
        ref_id=receipt.id,
    )
    await db.commit()

    return {
        "id": receipt.id,
        "ref_number": receipt.ref_number,
        "product_id": product.id,
        "product_name": product.name,
        "product_sku": product.sku,
        "receive_date": receipt.receive_date.isoformat() if receipt.receive_date else None,
        "qty": float(new_qty),
        "unit_cost": float(unit_cost) if unit_cost is not None else None,
        "total_cost": float(total_cost) if total_cost is not None else None,
        "supplier_ref": supplier_ref,
        "notes": notes,
        "product_type": data.product_type,
        "expense_id": receipt.expense_id,
        "expense_ref": expense_ref,
        "received_by": receipt.user.name if receipt.user else None,
    }


async def delete_receipt(
    db: AsyncSession,
    receipt_id: int,
    current_user: User,
) -> dict[str, Any]:
    receipt = await _get_receipt_or_404(db, receipt_id)
    product = receipt.product
    if product is None:
        raise HTTPException(status_code=404, detail=f"Product {receipt.product_id} not found")

    receipt_qty = Decimal(str(receipt.qty or 0)).quantize(_QTY, rounding=ROUND_HALF_UP)
    current_stock = Decimal(str(product.stock or 0)).quantize(_QTY, rounding=ROUND_HALF_UP)
    new_stock = current_stock - receipt_qty
    if new_stock < 0:
        raise HTTPException(status_code=400, detail="Cannot delete receipt because stock has already been consumed")

    product.stock = new_stock

    move = await _get_receipt_move(db, receipt.id)
    if move is not None:
        await db.delete(move)

    if receipt.expense_id:
        expense_result = await db.execute(
            select(Expense)
            .options(selectinload(Expense.category))
            .where(Expense.id == receipt.expense_id)
        )
        expense = expense_result.scalar_one_or_none()
        await _delete_expense_bundle(db, expense)

    deleted_ref = receipt.ref_number
    await db.delete(receipt)
    record(
        db,
        "Receive",
        "delete_receipt",
        f"{deleted_ref} deleted",
        user=current_user,
        ref_type="receipt",
        ref_id=receipt_id,
    )
    await db.commit()
    return {"ok": True, "id": receipt_id, "ref_number": deleted_ref}


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
            product_type=data.product_type,
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
            selectinload(ProductReceipt.expense).selectinload(Expense.category),
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
                "expense_category_name": r.expense.category.name if r.expense and r.expense.category else None,
                "expense_id":   r.expense_id,
                "expense_ref":  r.expense.ref_number if r.expense else None,
                "received_by":  r.user.name if r.user else None,
                "created_at":   r.created_at.isoformat() if r.created_at else None,
            }
            for r in receipts
        ],
    }