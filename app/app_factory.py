from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette_csrf import CSRFMiddleware

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from app.api.routes import ROUTERS
from app.bootstrap.database import initialize_database
from app.core.config import settings
from app.core.log import configure_logging, logger
from app.database import get_async_session

STATIC_DIR = Path(__file__).resolve().parent / "static"

limiter = Limiter(key_func=get_remote_address, default_limits=[f"{settings.RATE_LIMIT_REQUESTS}/{settings.RATE_LIMIT_WINDOW_SECONDS}seconds"])


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    await initialize_database()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        debug=False,
        lifespan=lifespan,
    )

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    (STATIC_DIR / "uploads").mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ALLOW_ORIGINS,
        allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
        allow_methods=settings.CORS_ALLOW_METHODS,
        allow_headers=settings.CORS_ALLOW_HEADERS,
    )
    # CSRF protection: only triggers on requests that carry the access_token cookie.
    # Auth endpoints (/auth/*) are exempt — they use credentials as proof, not a session.
    # Pure JSON API calls (/*/api/*) are also exempt — protected by CORS same-origin policy.
    import re
    app.add_middleware(
        CSRFMiddleware,
        secret=settings.SECRET_KEY,
        sensitive_cookies={"access_token"},
        exempt_urls=[
            re.compile(r"^/auth/.*"),
            re.compile(r".*/api/.*"),
            re.compile(r"^/import/.*"),
            re.compile(r"^/invoice.*"),
            re.compile(r"^/health.*"),
        ],
    )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_: Request, exc: Exception):
        logger.exception("Unhandled application error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "An internal server error occurred."},
        )

    for router in ROUTERS:
        app.include_router(router)

    @app.get("/health/live")
    async def liveness():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def readiness(db: AsyncSession = Depends(get_async_session)):
        try:
            await db.execute(text("SELECT 1"))
            return {"status": "ok", "db": "ok"}
        except Exception:
            return JSONResponse(
                status_code=503,
                content={"status": "error", "db": "unreachable"},
            )

    # Backward-compat alias
    @app.get("/health")
    async def health():
        return {"status": "ok", "app": settings.APP_NAME, "environment": settings.APP_ENV}

    logger.info("Application configured")
    return app
