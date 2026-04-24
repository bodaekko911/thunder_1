from contextlib import asynccontextmanager
from pathlib import Path

from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette_csrf import CSRFMiddleware

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api.routes import ROUTERS
from app.core.config import settings
from app.core.log import configure_logging, logger
from app.core.middleware import RequestLoggingMiddleware, SecurityHeadersMiddleware
from app.core.migrations import verify_migration_status
from app.core.monitoring import configure_monitoring
from app.core.rate_limit import limiter
from app.database import get_async_session

STATIC_DIR = Path(__file__).resolve().parent / "static"

# ── HTML shown when an unhandled 500 occurs during an HTML page navigation ─
_ERROR_HTML_PAGE = """<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<title>Something went wrong</title>
<style>
  :root{--card:rgba(15,20,36,0.88);--border:rgba(255,255,255,0.08);--text:#fff;--sub:#8899bb}
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:'Segoe UI',sans-serif;min-height:100vh;display:flex;align-items:center;
       justify-content:center;color:var(--text);padding:24px;
       background:linear-gradient(rgba(6,8,16,.68),rgba(6,8,16,.68)),
                 url('/static/home1.jpg.jpeg') center/cover no-repeat}
  .box{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:40px;
       width:360px;backdrop-filter:blur(8px);box-shadow:0 24px 60px rgba(0,0,0,.35);text-align:center}
  h2{color:#ff4d6d;font-size:22px;margin-bottom:12px}
  p{color:var(--sub);font-size:14px;margin-bottom:28px}
  a{display:inline-block;padding:12px 28px;background:linear-gradient(135deg,#00ff9d,#00d4ff);
    border-radius:10px;color:#021a10;font-weight:800;font-size:14px;text-decoration:none}
  a:hover{filter:brightness(1.1)}
</style>
</head><body>
<div class="box">
  <h2>Something went wrong</h2>
  <p>An unexpected error occurred. Please try again or go back.</p>
  <a href="javascript:history.back()">Go back</a>
</div>
</body></html>"""


async def _try_silent_refresh(refresh_token_value: str):
    """
    Open a fresh DB session and attempt to mint a new access token from the
    given raw refresh-token value.  Returns the new token string on success,
    or None on any failure, so callers can safely fall through to a login
    redirect without raising.

    Defined at module level (not inside create_app) so tests can monkeypatch
    ``app.app_factory._try_silent_refresh`` without a real DB connection.
    """
    from app.db.session import AsyncSessionLocal
    from app.core import security
    try:
        async with AsyncSessionLocal() as db:
            return await security.try_refresh_access_token(db, refresh_token_value)
    except Exception:
        return None


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    configure_monitoring()
    await verify_migration_status()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        debug=settings.DEBUG,
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
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.ALLOWED_HOSTS)
    app.add_middleware(SecurityHeadersMiddleware)
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
            re.compile(r"^/dashboard/assistant/.*"),
        ],
    )
    app.add_middleware(RequestLoggingMiddleware)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception(
            "Unhandled application error",
            exc_info=exc,
            extra={
                "method": request.method,
                "path": request.url.path,
                "query": str(request.url.query) or None,
            },
        )
        # Return a styled HTML page for browser navigations so users never see
        # raw JSON from an unhandled 500.  API callers (JSON Accept) still get
        # the machine-readable JSON body.
        if (
            request.method == "GET"
            and "text/html" in request.headers.get("accept", "")
        ):
            return HTMLResponse(content=_ERROR_HTML_PAGE, status_code=500)
        return JSONResponse(
            status_code=500,
            content={"detail": "An internal server error occurred."},
        )

    # ── Session-expiry middleware ────────────────────────────────────────────
    # Intercepts 401 responses on HTML GET navigations (e.g. the browser
    # requests /dashboard after the access token has expired):
    #
    #  • If a refresh_token cookie is present → attempt a silent refresh and
    #    307-redirect back to the same URL with the new access_token cookie so
    #    the browser retries with a fresh token.
    #  • Otherwise → 307-redirect to /?next=<path>&reason=expired so the login
    #    page can show a friendly "session expired" message and bounce the user
    #    back after sign-in.
    #
    # Only HTML GETs are rewritten.  JSON/API callers and POST/PUT/DELETE
    # requests keep receiving the plain 401 JSON so auth-guard.js keeps
    # working and API clients are unaffected.
    # /auth/* and /health* are explicitly excluded.
    @app.middleware("http")
    async def _session_expiry(request: Request, call_next):
        response = await call_next(request)

        if (
            response.status_code == 401
            and request.method == "GET"
            and "text/html" in request.headers.get("accept", "")
            and not request.url.path.startswith("/auth/")
            and not request.url.path.startswith("/health")
        ):
            # Reconstruct the original path + query so ?next= roundtrips.
            path = request.url.path
            if request.url.query:
                path += "?" + request.url.query

            refresh_token_value = request.cookies.get("refresh_token")
            if refresh_token_value:
                new_token = await _try_silent_refresh(refresh_token_value)
                if new_token:
                    # Redirect to the same URL; the new cookie rides along so
                    # the retry succeeds without another round-trip.
                    redirect = RedirectResponse(url=path, status_code=307)
                    redirect.set_cookie(
                        key="access_token",
                        value=new_token,
                        httponly=True,
                        samesite="lax",
                        secure=settings.COOKIE_SECURE,
                        path="/",
                        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
                    )
                    redirect.set_cookie(
                        key="logged_in",
                        value="true",
                        httponly=False,
                        samesite="lax",
                        secure=settings.COOKIE_SECURE,
                        path="/",
                        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
                    )
                    return redirect

            # No valid refresh token — send to login with context.
            login_url = "/?next=" + quote(path, safe="") + "&reason=expired"
            return RedirectResponse(url=login_url, status_code=307)

        return response

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
