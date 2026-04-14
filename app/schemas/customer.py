from pydantic import BaseModel, EmailStr, Field
from typing import Optional


class CustomerCreate(BaseModel):
    name:    str = Field(..., min_length=1, max_length=200)
    phone:   Optional[str] = Field(None, max_length=50)
    email:   Optional[EmailStr] = None
    address: Optional[str] = Field(None, max_length=300)
    discount_pct: float = Field(0, ge=0, le=100)


class CustomerUpdate(BaseModel):
    name:    Optional[str] = Field(None, min_length=1, max_length=200)
    phone:   Optional[str] = Field(None, max_length=50)
    email:   Optional[EmailStr] = None
    address: Optional[str] = Field(None, max_length=300)
    discount_pct: Optional[float] = Field(None, ge=0, le=100)
