import hashlib
import logging
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parents[2]
LOG_DIR = BASE_DIR / "logs"


class Settings(BaseSettings):
    APP_NAME: str = "ERP System"
    APP_ENV: str = "production"
    DEBUG: bool = False

    SECRET_KEY: str = Field(...)
    DATABASE_URL: str = Field(...)
    ADMIN_PASSWORD: str = Field(...)

    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30  # was 480 — reduced for security
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    DEFAULT_ADMIN_NAME: str = "Administrator"
    DEFAULT_ADMIN_EMAIL: str = "admin@example.com"

    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    WORKERS: int = 2
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = str(LOG_DIR / "app.log")

    CORS_ALLOW_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:8000"]
    CORS_ALLOW_CREDENTIALS: bool = True
    CORS_ALLOW_METHODS: list[str] = ["*"]
    CORS_ALLOW_HEADERS: list[str] = ["*"]

    RATE_LIMIT_REQUESTS: int = 120
    RATE_LIMIT_WINDOW_SECONDS: int = 60

    COOKIE_SECURE: bool = False

    # DB connection pool
    POOL_SIZE: int = 10
    POOL_MAX_OVERFLOW: int = 20

    # Redis (optional — used for rate limiting and brute-force protection)
    REDIS_URL: str = "redis://localhost:6379/0"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_settings(cls, data):
        if not isinstance(data, dict):
            return data
        values = dict(data)
        legacy_admin_password = values.get("DEFAULT_ADMIN_PASSWORD") or values.get("default_admin_password")
        if not values.get("ADMIN_PASSWORD") and not values.get("admin_password") and legacy_admin_password:
            values["ADMIN_PASSWORD"] = legacy_admin_password
        return values

    @field_validator("SECRET_KEY", mode="before")
    @classmethod
    def normalize_secret_key(cls, value: str) -> str:
        secret = (value or "").strip()
        if not secret:
            raise ValueError("SECRET_KEY is required")
        if len(secret) < 32:
            return hashlib.sha256(secret.encode("utf-8")).hexdigest()
        return secret

    @field_validator("ADMIN_PASSWORD", mode="before")
    @classmethod
    def normalize_admin_password(cls, value: str) -> str:
        password = (value or "").strip()
        if not password:
            raise ValueError("ADMIN_PASSWORD is required")
        return password

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        database_url = (value or "").strip()
        if not database_url:
            raise ValueError("DATABASE_URL is required")
        if database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql://", 1)
        if database_url.startswith("postgresql://"):
            database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if not database_url.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must use PostgreSQL with asyncpg")
        return database_url

    @field_validator("DEBUG", mode="before")
    @classmethod
    def normalize_debug(cls, value):
        if isinstance(value, bool):
            return value
        normalized = str(value or "").strip().lower()
        if normalized in {"1", "true", "yes", "on", "debug"}:
            return True
        return False

    @field_validator(
        "CORS_ALLOW_ORIGINS",
        "CORS_ALLOW_METHODS",
        "CORS_ALLOW_HEADERS",
        mode="before",
    )
    @classmethod
    def split_csv(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, list):
            return value
        if not value:
            return []
        return [item.strip() for item in str(value).split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    cfg = Settings()
    if cfg.APP_ENV == "production" and not cfg.COOKIE_SECURE:
        logging.getLogger("erp").warning(
            "SECURITY WARNING: COOKIE_SECURE is False in production. "
            "Set COOKIE_SECURE=true to protect the auth cookie."
        )
    return cfg


settings = get_settings()
