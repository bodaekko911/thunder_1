from pydantic import BaseModel
from typing import List, Optional


class InvoiceItemCreate(BaseModel):
    sku:   str
    name:  Optional[str] = None
    price: Optional[float] = None
    qty:   float


class InvoiceCreate(BaseModel):
    customer_id:      Optional[int] = None
    items:            List[InvoiceItemCreate]
    discount_percent: float = 0
    notes:            Optional[str] = None
    payment_method:   str = "cash"
    settle_later:     bool = False