from pydantic import BaseModel, Field
from typing import Optional, List


class ClientCreate(BaseModel):
    name:           str = Field(..., min_length=1, max_length=200)
    contact_person: Optional[str] = Field(None, max_length=200)
    phone:          Optional[str] = Field(None, max_length=50)
    email:          Optional[str] = Field(None, max_length=150)
    address:        Optional[str] = Field(None, max_length=300)
    payment_terms:  str = Field("cash", min_length=1, max_length=50)
    discount_pct:   float = Field(0, ge=0, le=100)
    credit_limit:   float = Field(0, ge=0)
    notes:          Optional[str] = Field(None, max_length=500)


class ClientUpdate(BaseModel):
    name:           Optional[str] = Field(None, min_length=1, max_length=200)
    contact_person: Optional[str] = Field(None, max_length=200)
    phone:          Optional[str] = Field(None, max_length=50)
    email:          Optional[str] = Field(None, max_length=150)
    address:        Optional[str] = Field(None, max_length=300)
    payment_terms:  Optional[str] = Field(None, max_length=50)
    discount_pct:   Optional[float] = Field(None, ge=0, le=100)
    credit_limit:   Optional[float] = Field(None, ge=0)
    notes:          Optional[str] = Field(None, max_length=500)


class InvoiceItemCreate(BaseModel):
    product_id: int
    qty:        float = Field(..., gt=0)
    unit_price: float = Field(..., ge=0)


class InvoiceCreate(BaseModel):
    client_id:      int
    invoice_type:   Optional[str] = Field(None, max_length=50)
    payment_method: Optional[str] = Field(None, max_length=50)
    discount_pct:   float = Field(0, ge=0, le=100)
    notes:          Optional[str] = Field(None, max_length=500)
    items:          List[InvoiceItemCreate]


class PaymentRecord(BaseModel):
    amount: float = Field(..., gt=0)
    method: str = Field("transfer", min_length=1, max_length=50)


class RefundItemCreate(BaseModel):
    product_id: int
    qty:        float = Field(..., gt=0)
    unit_price: float = Field(..., ge=0)


class ClientRefundCreate(BaseModel):
    client_id: int
    notes:     Optional[str] = Field(None, max_length=500)
    items:     List[RefundItemCreate]
