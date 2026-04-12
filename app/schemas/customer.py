from pydantic import BaseModel, EmailStr, Field
from typing import Optional


class CustomerCreate(BaseModel):
    name:    str = Field(..., min_length=1, max_length=200)
    phone:   Optional[str] = Field(None, max_length=50)
    email:   Optional[EmailStr] = None
    address: Optional[str] = Field(None, max_length=300)


class CustomerUpdate(BaseModel):
    name:    Optional[str] = Field(None, min_length=1, max_length=200)
    phone:   Optional[str] = Field(None, max_length=50)
    email:   Optional[EmailStr] = None
    address: Optional[str] = Field(None, max_length=300)
