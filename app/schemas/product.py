from pydantic import BaseModel, Field
from typing import Optional


class ProductCreate(BaseModel):
    sku:       str = Field(..., min_length=1, max_length=80)
    name:      str = Field(..., min_length=1, max_length=200)
    price:     float = Field(..., ge=0)
    cost:      float = Field(0, ge=0)
    stock:     float = Field(0, ge=0)
    min_stock: float = Field(5, ge=0)
    reorder_level: Optional[float] = Field(None, ge=0)
    reorder_qty: Optional[float] = Field(None, ge=0)
    preferred_supplier_id: Optional[int] = Field(None, ge=1)
    unit:      str = Field("pcs", min_length=1, max_length=50)
    category:  Optional[str] = Field(None, max_length=100)
    item_type: str = Field("finished", min_length=1, max_length=50)


class ProductUpdate(BaseModel):
    name:      Optional[str] = Field(None, min_length=1, max_length=200)
    price:     Optional[float] = Field(None, ge=0)
    cost:      Optional[float] = Field(None, ge=0)
    min_stock: Optional[float] = Field(None, ge=0)
    reorder_level: Optional[float] = Field(None, ge=0)
    reorder_qty: Optional[float] = Field(None, ge=0)
    preferred_supplier_id: Optional[int] = Field(None, ge=1)
    unit:      Optional[str] = Field(None, min_length=1, max_length=50)
    category:  Optional[str] = Field(None, max_length=100)
    item_type: Optional[str] = Field(None, max_length=50)
    is_active: Optional[bool] = None
