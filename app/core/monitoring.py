from app.core.config import settings
from app.core.log import logger


def configure_monitoring() -> None:
    if not settings.SENTRY_DSN:
        return

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
    except ImportError:
        logger.warning(
            "Sentry DSN configured but sentry-sdk is not installed",
            extra={"monitoring_backend": "sentry"},
        )
        return

    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.SENTRY_ENVIRONMENT or settings.APP_ENV,
        traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
        send_default_pii=settings.SENTRY_SEND_DEFAULT_PII,
        integrations=[FastApiIntegration()],
    )
    logger.info(
        "Sentry monitoring initialized",
        extra={
            "monitoring_backend": "sentry",
            "environment": settings.SENTRY_ENVIRONMENT or settings.APP_ENV,
        },
    )
