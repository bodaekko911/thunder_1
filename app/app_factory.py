from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from time import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import ROUTERS
from app.bootstrap.database import initialize_database
from app.core.config import settings
from app.core.log import configure_logging, logger

STATIC_DIR = Path(__file__).resolve().parent / "static"
_rate_limit_store: dict[str, deque[float]] = defaultdict(deque)


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    await initialize_database()
    yield


def _client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        debug=False,
        lifespan=lifespan,
    )

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

    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        if request.url.path in {"/health"}:
            return await call_next(request)

        now = time()
        key = _client_ip(request)
        bucket = _rate_limit_store[key]
        window = settings.RATE_LIMIT_WINDOW_SECONDS
        while bucket and now - bucket[0] > window:
            bucket.popleft()
        if len(bucket) >= settings.RATE_LIMIT_REQUESTS:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Please try again later."},
            )
        bucket.append(now)
        return await call_next(request)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_: Request, exc: Exception):
        logger.exception("Unhandled application error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "An internal server error occurred."},
        )

    for router in ROUTERS:
        app.include_router(router)

    @app.get("/health")
    async def health():
        return {"status": "ok", "app": settings.APP_NAME, "environment": settings.APP_ENV}

    logger.info("Application configured")
    return app
