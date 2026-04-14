from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.core.password_policy import PASSWORD_MAX_LENGTH, validate_password_policy


class UserCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    email:    EmailStr
    password: str = Field(..., min_length=1, max_length=PASSWORD_MAX_LENGTH)
    role:     str = Field("cashier", min_length=1, max_length=50)

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        return validate_password_policy(value)


class AdminUserCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=PASSWORD_MAX_LENGTH)
    role: str = Field("cashier", min_length=1, max_length=50)
    is_active: bool = True
    permissions: Optional[str] = None

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        return validate_password_policy(value)


class UserUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=150)
    email: Optional[EmailStr] = None
    password: Optional[str] = Field(None, min_length=1, max_length=PASSWORD_MAX_LENGTH)
    role: Optional[str] = Field(None, min_length=1, max_length=50)
    is_active: Optional[bool] = None
    permissions: Optional[str] = None

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return validate_password_policy(value)


class UserOut(BaseModel):
    id:        int
    name:      str
    email:     str
    role:      str
    is_active: bool
    model_config = {"from_attributes": True}


class UserLogin(BaseModel):
    email:    EmailStr
    password: str = Field(..., min_length=1, max_length=PASSWORD_MAX_LENGTH)


class ChangePasswordData(BaseModel):
    old_password: str = Field(..., min_length=1, max_length=PASSWORD_MAX_LENGTH)
    new_password: str = Field(..., min_length=1, max_length=PASSWORD_MAX_LENGTH)
    confirm_new_password: str = Field(..., min_length=1, max_length=PASSWORD_MAX_LENGTH)

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, value: str) -> str:
        return validate_password_policy(value, subject="New password")


class AdminResetPassword(BaseModel):
    new_password: str = Field(..., min_length=1, max_length=PASSWORD_MAX_LENGTH)

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, value: str) -> str:
        return validate_password_policy(value)
