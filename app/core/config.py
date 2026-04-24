import hashlib
import logging
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.password_policy import validate_password_policy


BASE_DIR = Path(__file__).resolve().parents[2]
LOG_DIR = BASE_DIR / "logs"

CONFIG_MODEL = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    case_sensitive=False,
    extra="ignore",
)


class EnvironmentSelector(BaseSettings):
    APP_ENV: Literal["development", "production"] = "development"

    model_config = CONFIG_MODEL

    @field_validator("APP_ENV", mode="before")
    @classmethod
    def normalize_app_env(cls, value: str) -> str:
        env = str(value or "development").strip().lower()
        if env not in {"development", "production"}:
            raise ValueError("APP_ENV must be either 'development' or 'production'")
        return env


class BaseAppSettings(BaseSettings):
    APP_NAME: str = "ERP System"
    APP_ENV: Literal["development", "production"]
    DEBUG: bool = False

    SECRET_KEY: str = Field(...)
    DATABASE_URL: str = Field(...)
    ADMIN_PASSWORD: str = Field(...)

    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    DEFAULT_ADMIN_NAME: str = "Administrator"
    DEFAULT_ADMIN_EMAIL: str = "admin@example.com"

    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    WORKERS: int = 2
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = str(LOG_DIR / "app.log")
    ALLOWED_HOSTS: Annotated[list[str], NoDecode] = []

    CORS_ALLOW_ORIGINS: Annotated[list[str], NoDecode] = []
    CORS_ALLOW_CREDENTIALS: bool = True
    CORS_ALLOW_METHODS: Annotated[list[str], NoDecode] = ["*"]
    CORS_ALLOW_HEADERS: Annotated[list[str], NoDecode] = ["*"]

    RATE_LIMIT_REQUESTS: int = 120
    RATE_LIMIT_WINDOW_SECONDS: int = 60
    LOGIN_RATE_LIMIT: str = "5/minute"
    REFRESH_RATE_LIMIT: str = "10/minute"
    PASSWORD_RATE_LIMIT: str = "5/minute"

    COOKIE_SECURE: bool = False

    POOL_SIZE: int = 10
    POOL_MAX_OVERFLOW: int = 20

    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_SOCKET_CONNECT_TIMEOUT: float = 0.25
    REDIS_SOCKET_TIMEOUT: float = 0.25

    APP_TIMEZONE: str = "Africa/Cairo"
    APP_LOCALE_DIR: str = "ltr"

    ASSISTANT_MEMORY_CHANNEL: str = "dashboard"

    SENTRY_DSN: str | None = None
    SENTRY_ENVIRONMENT: str | None = None
    SENTRY_TRACES_SAMPLE_RATE: float = 0.0
    SENTRY_SEND_DEFAULT_PII: bool = False
    MIGRATION_CHECK_ON_STARTUP: bool = True
    MIGRATION_CHECK_STRICT: bool = False

    OLLAMA_BASE_URL: str = Field(default="http://localhost:11434")
    AI_API_KEY: str | None = None

    model_config = CONFIG_MODEL

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

    @model_validator(mode="after")
    def validate_environment_rules(self):
        if self.APP_ENV == "production":
            if self.DEBUG:
                raise ValueError("DEBUG must be false in production")
            if not self.COOKIE_SECURE:
                raise ValueError("COOKIE_SECURE must be true in production")
            if self.SECRET_KEY == hashlib.sha256("change-this-to-a-long-random-secret-key".encode("utf-8")).hexdigest():
                raise ValueError("SECRET_KEY must be replaced in production")
            if self.ADMIN_PASSWORD == "change-me-now":
                raise ValueError("ADMIN_PASSWORD must be replaced in production")
            if not self.ALLOWED_HOSTS:
                raise ValueError("ALLOWED_HOSTS must be set in production")
            if self.CORS_ALLOW_CREDENTIALS and "*" in self.CORS_ALLOW_ORIGINS:
                raise ValueError("CORS_ALLOW_ORIGINS cannot contain '*' when credentials are enabled")
        return self

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
        return validate_password_policy(password, subject="ADMIN_PASSWORD")

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

    @field_validator(
        "DEBUG",
        "COOKIE_SECURE",
        "CORS_ALLOW_CREDENTIALS",
        "SENTRY_SEND_DEFAULT_PII",
        "MIGRATION_CHECK_ON_STARTUP",
        "MIGRATION_CHECK_STRICT",
        mode="before",
    )
    @classmethod
    def normalize_bool(cls, value):
        if isinstance(value, bool):
            return value
        normalized = str(value or "").strip().lower()
        return normalized in {"1", "true", "yes", "on", "debug"}

    @field_validator("SENTRY_DSN", "SENTRY_ENVIRONMENT", mode="before")
    @classmethod
    def normalize_optional_string(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @field_validator(
        "CORS_ALLOW_ORIGINS",
        "CORS_ALLOW_METHODS",
        "CORS_ALLOW_HEADERS",
        "ALLOWED_HOSTS",
        mode="before",
    )
    @classmethod
    def split_csv(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, list):
            return value
        if not value:
            return []
        return [item.strip() for item in str(value).split(",") if item.strip()]


class DevelopmentSettings(BaseAppSettings):
    APP_ENV: Literal["development"] = "development"
    DEBUG: bool = True
    WORKERS: int = 1
    LOG_LEVEL: str = "DEBUG"
    COOKIE_SECURE: bool = False
    ALLOWED_HOSTS: Annotated[list[str], NoDecode] = ["localhost", "127.0.0.1", "testserver"]
    CORS_ALLOW_ORIGINS: Annotated[list[str], NoDecode] = ["http://localhost:3000", "http://localhost:8000"]
    OLLAMA_BASE_URL: str = "http://localhost:11434"


class ProductionSettings(BaseAppSettings):
    APP_ENV: Literal["production"] = "production"
    DEBUG: bool = False
    WORKERS: int = 2
    LOG_LEVEL: str = "INFO"
    COOKIE_SECURE: bool = True
    ALLOWED_HOSTS: Annotated[list[str], NoDecode] = []
    CORS_ALLOW_ORIGINS: Annotated[list[str], NoDecode] = []
    OLLAMA_BASE_URL: str = "http://ollama:11434"


@lru_cache
def get_settings() -> BaseAppSettings:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    env = EnvironmentSelector().APP_ENV
    settings_cls = DevelopmentSettings if env == "development" else ProductionSettings
    cfg = settings_cls()
    logging.getLogger("erp").info("Loaded %s configuration", cfg.APP_ENV)
    return cfg


settings = get_settings()
