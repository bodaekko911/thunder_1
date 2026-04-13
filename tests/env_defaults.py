import os


TEST_ENV_DEFAULTS = {
    "APP_ENV": "development",
    "SECRET_KEY": "test-secret-key-12345678901234567890",
    "DATABASE_URL": "postgresql+asyncpg://postgres:postgres@localhost:5432/erp_test",
    "ADMIN_PASSWORD": "change-me-now",
    "DEFAULT_ADMIN_NAME": "Administrator",
    "DEFAULT_ADMIN_EMAIL": "admin@example.com",
    "ALLOWED_HOSTS": "localhost,127.0.0.1,testserver",
    "CORS_ALLOW_ORIGINS": "http://localhost:3000,http://localhost:8000",
    "COOKIE_SECURE": "false",
    "MIGRATION_CHECK_ON_STARTUP": "false",
    "MIGRATION_CHECK_STRICT": "false",
}


def apply_test_environment_defaults() -> None:
    for key, value in TEST_ENV_DEFAULTS.items():
        os.environ.setdefault(key, value)
