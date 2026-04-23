from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import require_action, require_permission
from app.database import get_async_session
from app.models.user import User
from app.routers.expenses import expenses_ui as legacy_expenses_ui
from app.schemas.expense import ExpenseCategoryCreate, ExpenseCreate, ExpenseUpdate
from app.services.expense_service import (
    archive_category,
    create_category,
    create_expense_entry,
    delete_expense_entry,
    get_cost_allocation,
    get_summary,
    list_categories,
    list_expenses,
    update_expense_entry,
)

router = APIRouter(
    prefix="/expenses",
    tags=["Expenses"],
    dependencies=[Depends(require_permission("page_expenses"))],
)


@router.get("/api/categories")
async def get_categories(db: AsyncSession = Depends(get_async_session)):
    return await list_categories(db)


@router.post("/api/categories")
async def create_expense_category(
    data: ExpenseCategoryCreate,
    db: AsyncSession = Depends(get_async_session),
    _: User = Depends(require_action("expenses", "expenses", "create")),
):
    return await create_category(db, data)


@router.delete("/api/categories/{cat_id}")
async def delete_expense_category(
    cat_id: int,
    db: AsyncSession = Depends(get_async_session),
    _: User = Depends(require_action("expenses", "expenses", "delete")),
):
    return await archive_category(db, cat_id)


@router.get("/api/list")
async def get_expenses(
    category_id: Optional[int] = None,
    month: Optional[str] = None,
    db: AsyncSession = Depends(get_async_session),
):
    return await list_expenses(db, category_id=category_id, month=month)


@router.get("/api/summary")
async def get_expense_summary(db: AsyncSession = Depends(get_async_session)):
    return await get_summary(db)


@router.post("/api/add")
async def add_expense(
    data: ExpenseCreate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_action("expenses", "expenses", "create")),
):
    return await create_expense_entry(db, data, current_user)


@router.put("/api/edit/{expense_id}")
async def edit_expense(
    expense_id: int,
    data: ExpenseUpdate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_action("expenses", "expenses", "update")),
):
    return await update_expense_entry(db, expense_id, data, current_user)


@router.delete("/api/delete/{expense_id}")
async def remove_expense(
    expense_id: int,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_action("expenses", "expenses", "delete")),
):
    return await delete_expense_entry(db, expense_id, current_user)


@router.get("/api/cost-allocation")
async def get_expense_cost_allocation(
    farm_id: int,
    date_from: str,
    date_to: str,
    db: AsyncSession = Depends(get_async_session),
):
    return await get_cost_allocation(db, farm_id=farm_id, date_from=date_from, date_to=date_to)


@router.get("/", response_class=HTMLResponse)
def expenses_ui(current_user: User = Depends(require_permission("page_expenses"))):
    return legacy_expenses_ui(current_user)
