from pydantic import BaseModel, EmailStr, Field
from typing import Optional


class UserCreate(BaseModel):
    name:     str = Field(..., min_length=1, max_length=150)
    email:    EmailStr
    password: str = Field(..., min_length=8, max_length=200)
    role:     str = Field("cashier", min_length=1, max_length=50)


class UserOut(BaseModel):
    id:        int
    name:      str
    email:     str
    role:      str
    is_active: bool
    model_config = {"from_attributes": True}


class UserLogin(BaseModel):
    email:    EmailStr
    password: str = Field(..., min_length=1, max_length=200)
