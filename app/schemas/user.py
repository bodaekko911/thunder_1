from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class UserCreate(BaseModel):
    name:     str
    email:    str
    password: str
    role:     str = "cashier"


class UserOut(BaseModel):
    id:        int
    name:      str
    email:     str
    role:      str
    is_active: bool

    model_config = {"from_attributes": True}


class UserLogin(BaseModel):
    email:    str
    password: str