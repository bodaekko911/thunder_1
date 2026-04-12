from typing import Optional

from pydantic import BaseModel


class ExpenseCategoryCreate(BaseModel):
    name: str
    account_code: Optional[str] = None
    description: Optional[str] = None


class ExpenseCreate(BaseModel):
    category_id: int
    expense_date: str
    amount: float
    payment_method: str = "cash"
    vendor: Optional[str] = None
    description: Optional[str] = None
    farm_id: Optional[int] = None


class ExpenseUpdate(BaseModel):
    category_id: Optional[int] = None
    expense_date: Optional[str] = None
    amount: Optional[float] = None
    payment_method: Optional[str] = None
    vendor: Optional[str] = None
    description: Optional[str] = None
    farm_id: Optional[int] = None
