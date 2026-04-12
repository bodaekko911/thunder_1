import asyncio
from types import SimpleNamespace

import pytest

import app.core.migrations as migrations
from app.core.config import DevelopmentSettings, ProductionSettings


def test_database_url_normalizes_to_asyncpg() -> None:
    settings = DevelopmentSettings(
        SECRET_KEY="x" * 32,
        DATABASE_URL="postgresql://erp:erp@localhost:5432/erp",
        ADMIN_PASSWORD="change-me-now",
    )

    assert settings.DATABASE_URL == "postgresql+asyncpg://erp:erp@localhost:5432/erp"


def test_production_settings_require_allowed_hosts() -> None:
    with pytest.raises(ValueError, match="ALLOWED_HOSTS must be set in production"):
        ProductionSettings(
            SECRET_KEY="x" * 32,
            DATABASE_URL="postgresql://erp:erp@localhost:5432/erp",
            ADMIN_PASSWORD="strong-password",
            COOKIE_SECURE=True,
            CORS_ALLOW_ORIGINS=["https://erp.example.com"],
        )


def test_production_settings_reject_wildcard_cors_with_credentials() -> None:
    with pytest.raises(ValueError, match="CORS_ALLOW_ORIGINS cannot contain '\\*'"):
        ProductionSettings(
            SECRET_KEY="x" * 32,
            DATABASE_URL="postgresql://erp:erp@localhost:5432/erp",
            ADMIN_PASSWORD="strong-password",
            COOKIE_SECURE=True,
            ALLOWED_HOSTS=["erp.example.com"],
            CORS_ALLOW_ORIGINS=["*"],
            CORS_ALLOW_CREDENTIALS=True,
        )


def test_verify_migration_status_skips_when_disabled(monkeypatch) -> None:
    called = False

    async def fake_check_migration_status():
        nonlocal called
        called = True
        return {"status": "ok", "heads": ["head"], "current_revisions": ["head"]}

    monkeypatch.setattr(
        migrations,
        "settings",
        SimpleNamespace(MIGRATION_CHECK_ON_STARTUP=False, MIGRATION_CHECK_STRICT=False),
    )
    monkeypatch.setattr(migrations, "check_migration_status", fake_check_migration_status)

    asyncio.run(migrations.verify_migration_status())

    assert called is False


def test_verify_migration_status_warns_when_non_strict(monkeypatch) -> None:
    warnings: list[tuple[str, dict[str, object]]] = []

    async def fake_check_migration_status():
        return {"status": "pending", "heads": ["head"], "current_revisions": []}

    monkeypatch.setattr(
        migrations,
        "settings",
        SimpleNamespace(MIGRATION_CHECK_ON_STARTUP=True, MIGRATION_CHECK_STRICT=False),
    )
    monkeypatch.setattr(migrations, "check_migration_status", fake_check_migration_status)
    monkeypatch.setattr(
        migrations.logger,
        "warning",
        lambda message, extra=None: warnings.append((message, extra or {})),
    )

    asyncio.run(migrations.verify_migration_status())

    assert warnings == [
        (
            "Database migrations are pending",
            {"migration_status": {"status": "pending", "heads": ["head"], "current_revisions": []}},
        )
    ]


def test_verify_migration_status_raises_when_strict(monkeypatch) -> None:
    errors: list[tuple[str, dict[str, object]]] = []

    async def fake_check_migration_status():
        return {"status": "pending", "heads": ["head"], "current_revisions": []}

    monkeypatch.setattr(
        migrations,
        "settings",
        SimpleNamespace(MIGRATION_CHECK_ON_STARTUP=True, MIGRATION_CHECK_STRICT=True),
    )
    monkeypatch.setattr(migrations, "check_migration_status", fake_check_migration_status)
    monkeypatch.setattr(
        migrations.logger,
        "error",
        lambda message, extra=None: errors.append((message, extra or {})),
    )

    with pytest.raises(RuntimeError, match="Database migrations are pending"):
        asyncio.run(migrations.verify_migration_status())

    assert errors == [
        (
            "Database migrations are pending",
            {"migration_status": {"status": "pending", "heads": ["head"], "current_revisions": []}},
        )
    ]
