from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.log import record
from app.models.accounting import Account, Journal, JournalEntry
from app.models.expense import Expense, ExpenseCategory
from app.models.farm import Farm, FarmDelivery, FarmDeliveryItem
from app.models.user import User
from app.schemas.expense import ExpenseCategoryCreate, ExpenseCreate, ExpenseUpdate


def _clean_text(value: Optional[str]) -> Optional[str]:
    return (value or "").strip() or None


def _payment_account_code(payment_method: str) -> str:
    return "1000" if payment_method in {"cash", "card"} else "1200"


async def _next_expense_reference(db: AsyncSession) -> str:
    result = await db.execute(select(func.max(Expense.id)))
    max_id = result.scalar() or 0
    return f"EXP-{str(max_id + 1).zfill(5)}"


async def _get_or_create_account(
    db: AsyncSession,
    account_code: str,
    *,
    account_name: Optional[str] = None,
) -> Account:
    result = await db.execute(select(Account).where(Account.code == account_code))
    account = result.scalar_one_or_none()
    if account:
        return account

    account = Account(
        code=account_code,
        name=account_name or f"Account {account_code}",
        type="expense" if account_code.startswith("5") else "asset",
        balance=0,
    )
    db.add(account)
    await db.flush()
    return account


async def _post_expense_journal(
    db: AsyncSession,
    *,
    description: str,
    amount: float,
    expense_account_code: str,
    payment_method: str,
    user_id: Optional[int],
) -> Journal:
    journal = Journal(ref_type="expense", description=description, user_id=user_id)
    db.add(journal)
    await db.flush()

    entries = [
        (expense_account_code, amount, 0),
        (_payment_account_code(payment_method), 0, amount),
    ]
    for account_code, debit, credit in entries:
        account = await _get_or_create_account(db, account_code)
        db.add(
            JournalEntry(
                journal_id=journal.id,
                account_id=account.id,
                debit=debit,
                credit=credit,
            )
        )
        account.balance += Decimal(str(debit)) - Decimal(str(credit))

    return journal


async def _reverse_expense_journal(db: AsyncSession, expense: Expense) -> None:
    if not expense.category:
        return

    journal = Journal(
        ref_type="expense_reversal",
        description=f"Reversal - {expense.ref_number}",
        user_id=expense.user_id,
    )
    db.add(journal)
    await db.flush()

    entries = [
        (_payment_account_code(expense.payment_method), float(expense.amount), 0),
        (expense.category.account_code, 0, float(expense.amount)),
    ]
    for account_code, debit, credit in entries:
        result = await db.execute(select(Account).where(Account.code == account_code))
        account = result.scalar_one_or_none()
        if not account:
            continue

        db.add(
            JournalEntry(
                journal_id=journal.id,
                account_id=account.id,
                debit=debit,
                credit=credit,
            )
        )
        account.balance += Decimal(str(debit)) - Decimal(str(credit))


async def _get_active_category(db: AsyncSession, category_id: int) -> ExpenseCategory:
    result = await db.execute(
        select(ExpenseCategory).where(
            ExpenseCategory.id == category_id,
            ExpenseCategory.is_active == "1",
        )
    )
    category = result.scalar_one_or_none()
    if not category:
        raise HTTPException(status_code=404, detail="Category not found")
    return category


async def list_categories(db: AsyncSession) -> list[dict]:
    result = await db.execute(
        select(ExpenseCategory)
        .options(selectinload(ExpenseCategory.expenses))
        .where(ExpenseCategory.is_active == "1")
        .order_by(ExpenseCategory.account_code)
    )
    categories = result.scalars().all()
    return [
        {
            "id": category.id,
            "name": category.name,
            "account_code": category.account_code,
            "description": category.description or "",
            "count": len(category.expenses),
            "total": float(sum(expense.amount for expense in category.expenses)),
        }
        for category in categories
    ]


async def create_category(db: AsyncSession, data: ExpenseCategoryCreate) -> dict:
    category_name = data.name.strip()
    result = await db.execute(select(ExpenseCategory).where(ExpenseCategory.name == category_name))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Category name already exists")

    if data.account_code and data.account_code.strip():
        account_code = data.account_code.strip()
    else:
        categories_result = await db.execute(select(ExpenseCategory))
        existing_codes = [
            int(category.account_code)
            for category in categories_result.scalars().all()
            if category.account_code
            and category.account_code.isdigit()
            and 5000 <= int(category.account_code) <= 5999
        ]
        account_code = str(max(existing_codes) + 1) if existing_codes else "5001"

    await _get_or_create_account(db, account_code, account_name=category_name)

    category = ExpenseCategory(
        name=category_name,
        account_code=account_code,
        description=_clean_text(data.description),
    )
    db.add(category)
    await db.commit()
    await db.refresh(category)
    return {"id": category.id, "name": category.name, "account_code": category.account_code}


async def archive_category(db: AsyncSession, category_id: int) -> dict:
    result = await db.execute(
        select(ExpenseCategory)
        .options(selectinload(ExpenseCategory.expenses))
        .where(ExpenseCategory.id == category_id)
    )
    category = result.scalar_one_or_none()
    if not category:
        raise HTTPException(status_code=404, detail="Category not found")
    if category.expenses:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete a category that has expenses. Archive it instead.",
        )

    category.is_active = "0"
    await db.commit()
    return {"ok": True}


async def list_expenses(
    db: AsyncSession,
    *,
    category_id: Optional[int] = None,
    month: Optional[str] = None,
) -> list[dict]:
    statement = select(Expense).options(
        selectinload(Expense.category),
        selectinload(Expense.user),
        selectinload(Expense.farm),
    )
    if category_id:
        statement = statement.where(Expense.category_id == category_id)
    if month:
        try:
            year, month_number = int(month[:4]), int(month[5:7])
            statement = statement.where(
                func.extract("year", Expense.expense_date) == year,
                func.extract("month", Expense.expense_date) == month_number,
            )
        except (ValueError, IndexError):
            pass

    statement = statement.order_by(Expense.expense_date.desc(), Expense.id.desc())
    result = await db.execute(statement)
    expenses = result.scalars().all()
    return [
        {
            "id": expense.id,
            "ref_number": expense.ref_number,
            "category": expense.category.name if expense.category else "—",
            "category_id": expense.category_id,
            "account_code": expense.category.account_code if expense.category else "—",
            "expense_date": str(expense.expense_date),
            "amount": float(expense.amount),
            "payment_method": expense.payment_method,
            "vendor": expense.vendor or "",
            "description": expense.description or "",
            "created_by": expense.user.name if expense.user else "—",
            "farm_id": expense.farm_id,
            "farm_name": expense.farm.name if expense.farm else None,
        }
        for expense in expenses
    ]


async def get_summary(db: AsyncSession) -> dict:
    now = datetime.utcnow()
    this_month_result = await db.execute(
        select(func.coalesce(func.sum(Expense.amount), 0)).where(
            func.extract("year", Expense.expense_date) == now.year,
            func.extract("month", Expense.expense_date) == now.month,
        )
    )
    this_month = this_month_result.scalar() or 0

    last_month_year = now.year if now.month > 1 else now.year - 1
    last_month_number = now.month - 1 if now.month > 1 else 12
    last_month_result = await db.execute(
        select(func.coalesce(func.sum(Expense.amount), 0)).where(
            func.extract("year", Expense.expense_date) == last_month_year,
            func.extract("month", Expense.expense_date) == last_month_number,
        )
    )
    last_month = last_month_result.scalar() or 0

    total_result = await db.execute(select(func.coalesce(func.sum(Expense.amount), 0)))
    total_all = total_result.scalar() or 0

    categories_result = await db.execute(
        select(ExpenseCategory).where(ExpenseCategory.is_active == "1")
    )
    breakdown = []
    for category in categories_result.scalars().all():
        category_total_result = await db.execute(
            select(func.coalesce(func.sum(Expense.amount), 0)).where(
                Expense.category_id == category.id,
                func.extract("year", Expense.expense_date) == now.year,
                func.extract("month", Expense.expense_date) == now.month,
            )
        )
        category_total = category_total_result.scalar() or 0
        if float(category_total) > 0:
            breakdown.append({"name": category.name, "total": float(category_total)})

    breakdown.sort(key=lambda item: item["total"], reverse=True)
    return {
        "this_month": float(this_month),
        "last_month": float(last_month),
        "total_all": float(total_all),
        "breakdown": breakdown,
    }


async def create_expense_entry(db: AsyncSession, data: ExpenseCreate, current_user: User) -> dict:
    category = await _get_active_category(db, data.category_id)
    if data.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than 0")

    try:
        expense_date = date_type.fromisoformat(data.expense_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid date format - use YYYY-MM-DD") from exc

    reference_number = await _next_expense_reference(db)
    amount = round(float(data.amount), 2)
    vendor = _clean_text(data.vendor)
    description = _clean_text(data.description)

    journal = await _post_expense_journal(
        db,
        description=f"{category.name} expense - {reference_number}" + (f" - {vendor}" if vendor else ""),
        amount=amount,
        expense_account_code=category.account_code,
        payment_method=data.payment_method,
        user_id=current_user.id,
    )

    expense = Expense(
        ref_number=reference_number,
        category_id=category.id,
        user_id=current_user.id,
        expense_date=expense_date,
        amount=amount,
        payment_method=data.payment_method,
        vendor=vendor,
        description=description,
        journal_id=journal.id,
        farm_id=data.farm_id or None,
    )
    db.add(expense)
    record(
        db,
        "Expenses",
        "add_expense",
        f"{category.name} - {reference_number} - {amount:.2f} - {data.payment_method}",
        user=current_user,
        ref_type="expense",
        ref_id=0,
    )
    await db.commit()
    await db.refresh(expense)
    return {
        "id": expense.id,
        "ref_number": expense.ref_number,
        "amount": float(expense.amount),
        "category": category.name,
    }


async def update_expense_entry(
    db: AsyncSession,
    expense_id: int,
    data: ExpenseUpdate,
    current_user: User,
) -> dict:
    result = await db.execute(
        select(Expense)
        .options(selectinload(Expense.category))
        .where(Expense.id == expense_id)
    )
    expense = result.scalar_one_or_none()
    if not expense:
        raise HTTPException(status_code=404, detail="Expense not found")

    await _reverse_expense_journal(db, expense)

    if data.category_id is not None:
        expense.category = await _get_active_category(db, data.category_id)
        expense.category_id = expense.category.id
    if data.expense_date is not None:
        try:
            expense.expense_date = date_type.fromisoformat(data.expense_date)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid date format") from exc
    if data.amount is not None:
        if data.amount <= 0:
            raise HTTPException(status_code=400, detail="Amount must be greater than 0")
        expense.amount = round(float(data.amount), 2)
    if data.payment_method is not None:
        expense.payment_method = data.payment_method
    if data.vendor is not None:
        expense.vendor = _clean_text(data.vendor)
    if data.description is not None:
        expense.description = _clean_text(data.description)
    if data.farm_id is not None:
        expense.farm_id = data.farm_id or None

    if expense.category is None:
        category_result = await db.execute(
            select(ExpenseCategory).where(ExpenseCategory.id == expense.category_id)
        )
        expense.category = category_result.scalar_one_or_none()

    journal = await _post_expense_journal(
        db,
        description=f"{expense.category.name} expense (edited) - {expense.ref_number}",
        amount=float(expense.amount),
        expense_account_code=expense.category.account_code,
        payment_method=expense.payment_method,
        user_id=current_user.id,
    )
    expense.journal_id = journal.id

    record(
        db,
        "Expenses",
        "edit_expense",
        f"Edited {expense.ref_number} - {float(expense.amount):.2f}",
        user=current_user,
        ref_type="expense",
        ref_id=expense.id,
    )
    await db.commit()
    return {"ok": True}


async def delete_expense_entry(
    db: AsyncSession,
    expense_id: int,
    current_user: User,
) -> dict:
    result = await db.execute(
        select(Expense)
        .options(selectinload(Expense.category))
        .where(Expense.id == expense_id)
    )
    expense = result.scalar_one_or_none()
    if not expense:
        raise HTTPException(status_code=404, detail="Expense not found")

    reference_number = expense.ref_number
    await _reverse_expense_journal(db, expense)
    await db.delete(expense)
    record(
        db,
        "Expenses",
        "delete_expense",
        f"Deleted {reference_number} - journal reversed",
        user=current_user,
        ref_type="expense",
        ref_id=expense_id,
    )
    await db.commit()
    return {"ok": True}


async def get_cost_allocation(
    db: AsyncSession,
    *,
    farm_id: int,
    date_from: str,
    date_to: str,
) -> dict:
    try:
        start_date = date_type.fromisoformat(date_from)
        end_date = date_type.fromisoformat(date_to)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid date format - use YYYY-MM-DD") from exc

    farm_result = await db.execute(select(Farm).where(Farm.id == farm_id))
    farm = farm_result.scalar_one_or_none()
    if not farm:
        raise HTTPException(status_code=404, detail="Farm not found")

    expenses_result = await db.execute(
        select(Expense)
        .options(selectinload(Expense.category))
        .where(
            Expense.farm_id == farm_id,
            Expense.expense_date >= start_date,
            Expense.expense_date <= end_date,
        )
    )
    expenses = expenses_result.scalars().all()
    total_cost = sum(float(expense.amount) for expense in expenses)

    cost_by_category: dict[str, float] = {}
    for expense in expenses:
        category_name = expense.category.name if expense.category else "Other"
        cost_by_category[category_name] = cost_by_category.get(category_name, 0) + float(expense.amount)

    deliveries_result = await db.execute(
        select(FarmDelivery)
        .options(selectinload(FarmDelivery.items).selectinload(FarmDeliveryItem.product))
        .where(
            FarmDelivery.farm_id == farm_id,
            FarmDelivery.delivery_date >= start_date,
            FarmDelivery.delivery_date <= end_date,
        )
    )
    deliveries = deliveries_result.scalars().all()

    quantity_by_product: dict[int, dict] = {}
    for delivery in deliveries:
        for item in delivery.items:
            product = item.product
            if item.product_id not in quantity_by_product:
                quantity_by_product[item.product_id] = {
                    "product_id": item.product_id,
                    "product_name": product.name if product else f"#{item.product_id}",
                    "unit": item.unit or (product.unit if product else "kg"),
                    "sale_price": float(product.price) if product else 0,
                    "total_qty": 0,
                }
            quantity_by_product[item.product_id]["total_qty"] += float(item.qty)

    total_quantity = sum(item["total_qty"] for item in quantity_by_product.values())
    products = []
    for product_id, info in quantity_by_product.items():
        share = info["total_qty"] / total_quantity if total_quantity > 0 else 0
        allocated_cost = total_cost * share
        cost_per_unit = allocated_cost / info["total_qty"] if info["total_qty"] > 0 else 0
        profit_per_unit = info["sale_price"] - cost_per_unit
        products.append(
            {
                "product_id": product_id,
                "product_name": info["product_name"],
                "unit": info["unit"],
                "total_qty": round(info["total_qty"], 3),
                "share_pct": round(share * 100, 1),
                "allocated_cost": round(allocated_cost, 2),
                "cost_per_unit": round(cost_per_unit, 2),
                "sale_price": round(info["sale_price"], 2),
                "profit_per_unit": round(profit_per_unit, 2),
                "profit_margin_pct": round(
                    (profit_per_unit / info["sale_price"] * 100) if info["sale_price"] > 0 else 0,
                    1,
                ),
            }
        )

    products.sort(key=lambda item: item["allocated_cost"], reverse=True)
    return {
        "farm_id": farm_id,
        "farm_name": farm.name,
        "date_from": date_from,
        "date_to": date_to,
        "total_cost": round(total_cost, 2),
        "total_qty": round(total_quantity, 3),
        "cost_by_category": [
            {"name": name, "amount": round(amount, 2)}
            for name, amount in sorted(cost_by_category.items(), key=lambda item: -item[1])
        ],
        "products": products,
        "expense_count": len(expenses),
        "delivery_count": len(deliveries),
    }
