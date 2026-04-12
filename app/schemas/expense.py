from pydantic import BaseModel, Field
from typing import Optional


class ExpenseCategoryCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)


class ExpenseCreate(BaseModel):
    category_id:    int
    amount:         float = Field(..., gt=0)
    description:    Optional[str] = Field(None, max_length=500)
    payment_method: str = Field("cash", max_length=50)
    date:           Optional[str] = None
    farm_id:        Optional[int] = None


class ExpenseUpdate(BaseModel):
    category_id:    Optional[int] = None
    amount:         Optional[float] = Field(None, gt=0)
    description:    Optional[str] = Field(None, max_length=500)
    payment_method: Optional[str] = Field(None, max_length=50)
    date:           Optional[str] = None
    farm_id:        Optional[int] = None
