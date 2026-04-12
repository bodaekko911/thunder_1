import pytest
from pydantic import ValidationError

from app.core.config import DevelopmentSettings
from app.core.password_policy import PASSWORD_MIN_LENGTH, validate_password_change
from app.schemas.user import (
    AdminResetPassword,
    AdminUserCreate,
    ChangePasswordData,
    UserCreate,
    UserUpdate,
)


def _short_password() -> str:
    return "a" * (PASSWORD_MIN_LENGTH - 1)


def _valid_password() -> str:
    return "a" * PASSWORD_MIN_LENGTH


@pytest.mark.parametrize(
    ("factory", "field_name"),
    [
        (
            lambda password: UserCreate(
                name="Test User",
                email="user@example.com",
                password=password,
            ),
            "password",
        ),
        (
            lambda password: AdminUserCreate(
                name="Admin User",
                email="admin@example.com",
                password=password,
            ),
            "password",
        ),
        (
            lambda password: UserUpdate(password=password),
            "password",
        ),
        (
            lambda password: AdminResetPassword(new_password=password),
            "new_password",
        ),
        (
            lambda password: ChangePasswordData(
                old_password=_valid_password(),
                new_password=password,
            ),
            "new_password",
        ),
    ],
)
def test_password_policy_rejects_short_passwords(factory, field_name: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        factory(_short_password())

    errors = exc_info.value.errors()
    assert any(field_name in ".".join(str(part) for part in error["loc"]) for error in errors)
    assert any(f"at least {PASSWORD_MIN_LENGTH} characters" in error["msg"] for error in errors)


def test_change_password_requires_a_different_new_password() -> None:
    current_password = _valid_password()

    with pytest.raises(ValueError, match="New password must be different"):
        validate_password_change(current_password, current_password)


def test_admin_password_setting_uses_shared_policy() -> None:
    with pytest.raises(ValidationError, match=fr"at least {PASSWORD_MIN_LENGTH} characters"):
        DevelopmentSettings(
            SECRET_KEY="x" * 32,
            DATABASE_URL="postgresql+asyncpg://user:pass@localhost/test_db",
            ADMIN_PASSWORD=_short_password(),
        )
