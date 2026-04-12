from pydantic import BaseModel, Field
from typing import Optional, List


class SupplierCreate(BaseModel):
    name:    str = Field(..., min_length=1, max_length=200)
    phone:   Optional[str] = Field(None, max_length=50)
    email:   Optional[str] = Field(None, max_length=150)
    address: Optional[str] = Field(None, max_length=300)


class SupplierUpdate(BaseModel):
    name:    Optional[str] = Field(None, min_length=1, max_length=200)
    phone:   Optional[str] = Field(None, max_length=50)
    email:   Optional[str] = Field(None, max_length=150)
    address: Optional[str] = Field(None, max_length=300)


class PurchaseItemCreate(BaseModel):
    product_id: int
    qty:        float = Field(..., gt=0)
    unit_cost:  float = Field(..., ge=0)


class PurchaseCreate(BaseModel):
    supplier_id:    int
    items:          List[PurchaseItemCreate]
    notes:          Optional[str] = Field(None, max_length=500)
    payment_method: str = Field("cash", max_length=50)
