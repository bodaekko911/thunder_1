import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.permission_catalog import get_permission_catalog
from app.core.permissions import (
    get_effective_permissions,
    normalize_permissions,
    require_admin,
    serialize_permissions,
)
from app.core.security import (
    create_access_token,
    decode_token,
    get_current_user,
    hash_password,
    password_needs_rehash,
    try_refresh_access_token,
    verify_password,
)
from app.database import get_async_session
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.schemas.user import UserCreate, UserOut, UserLogin
from app.core.rate_limit import limiter

router = APIRouter(tags=["Auth"])


def _redis_client():
    import redis.asyncio as aioredis

    return aioredis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=settings.REDIS_SOCKET_CONNECT_TIMEOUT,
        socket_timeout=settings.REDIS_SOCKET_TIMEOUT,
        retry_on_timeout=False,
    )


@router.get("/", response_class=HTMLResponse)
def login_page():
    return """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>ERP Login</title>
    <style>
        :root {
            --card: rgba(15, 20, 36, 0.88);
            --border: rgba(255,255,255,0.08);
            --text: #ffffff;
            --sub: #8899bb;
            --muted: #445066;
            --accent: #00ff9d;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', sans-serif;
            min-height: 100vh;
            height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            color: var(--text);
            padding: 24px;
            background:
                linear-gradient(rgba(6, 8, 16, 0.68), rgba(6, 8, 16, 0.68)),
                url('/static/home1.jpg.jpeg') center center / cover no-repeat;
        }
        .box {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 40px;
            width: 360px;
            position: relative;
            backdrop-filter: blur(8px);
            box-shadow: 0 24px 60px rgba(0,0,0,0.35);
        }
        h2 {
            color: var(--accent);
            font-size: 24px;
            margin-bottom: 6px;
        }
        p {
            color: var(--muted);
            font-size: 13px;
            margin-bottom: 28px;
        }
        label {
            display: block;
            color: var(--sub);
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 1px;
            text-transform: uppercase;
            margin-bottom: 6px;
        }
        input {
            width: 100%;
            padding: 12px;
            background: rgba(21,28,48,0.9);
            border: 1px solid var(--border);
            border-radius: 10px;
            color: var(--text);
            font-size: 14px;
            margin-bottom: 18px;
            outline: none;
        }
        input:focus {
            border-color: rgba(0,255,157,0.4);
        }
        button {
            width: 100%;
            padding: 14px;
            background: linear-gradient(135deg, #00ff9d, #00d4ff);
            border: none;
            border-radius: 10px;
            color: #021a10;
            font-size: 15px;
            font-weight: 800;
            cursor: pointer;
        }
        button:hover { filter: brightness(1.1); }
        #error {
            color: #ff4d6d;
            font-size: 13px;
            margin-top: 12px;
            text-align: center;
            display: none;
        }
        #session-msg {
            color: var(--sub);
            font-size: 13px;
            margin-bottom: 18px;
            padding: 10px 12px;
            background: rgba(136, 153, 187, 0.1);
            border: 1px solid rgba(136, 153, 187, 0.2);
            border-radius: 8px;
            display: none;
        }
        @media (max-width: 480px) {
            body {
                padding: 16px;
            }
            .box {
                width: 100%;
                padding: 28px 22px;
            }
        }
    </style>
</head>
<body>
    <div class="box">
        <h2>Welcome Back</h2>
        <p>Sign in to your ERP system</p>

        <div id="session-msg"></div>

        <label>Email</label>
        <input id="email" type="email" placeholder="you@example.com">

        <label>Password</label>
        <input id="password" type="password" placeholder="********">

        <button onclick="login()">Sign In</button>
        <div id="error">Wrong email or password</div>
    </div>

    <script>
        // Validate that a ?next= redirect target is a safe internal path.
        // Blocks protocol-relative (//evil.com) and absolute URLs while
        // allowing any same-origin path such as /dashboard or /inventory/?tab=1.
        function _isSafeReturnUrl(url) {
            const backslash = String.fromCharCode(92);
            return typeof url === "string" &&
                   url.startsWith("/") &&
                   !url.startsWith("//") &&
                   url.indexOf(backslash) === -1 &&
                   url.indexOf("\\r") === -1 &&
                   url.indexOf("\\n") === -1;
        }

        // Show a friendly notice when the server redirected here because the
        // session expired (?reason=expired), so users know why they landed here.
        (function () {
            var reason = new URLSearchParams(window.location.search).get('reason');
            if (reason === 'expired') {
                var el = document.getElementById('session-msg');
                el.textContent = 'Your session expired \u2014 please sign in again to continue.';
                el.style.display = 'block';
            }
        })();

        async function login() {
            let res = await fetch("/auth/login", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    email: document.getElementById("email").value,
                    password: document.getElementById("password").value
                })
            });
            let data = await res.json();
            if (!res.ok) {
                const msg = data.detail || "Invalid email or password";
                document.getElementById("error").textContent = msg;
                document.getElementById("error").style.display = "block";
                return;
            }
            // Token is stored in an httpOnly cookie set by the server - no localStorage.
            const permissions = new Set((data.permissions || "").split(",").map(v => v.trim()).filter(Boolean));
            const landingPages = [
                ["/dashboard", "page_dashboard"],
                ["/pos", "page_pos"],
                ["/farm/", "page_farm"],
                ["/production/", "page_production"],
                ["/inventory/", "page_inventory"],
                ["/products/", "page_products"],
                ["/customers-mgmt/", "page_customers"],
                ["/suppliers/", "page_suppliers"],
                ["/receive/", "page_receive_products"],
                ["/import", "page_import"],
                ["/reports/", "page_reports"],
                ["/b2b/", "page_b2b"],
                ["/hr/", "page_hr"],
                ["/accounting/", "page_accounting"],
                ["/expenses/", "page_expenses"]
            ];
            const defaultPage = data.role === "admin"
                ? "/dashboard"
                : (landingPages.find(([, permission]) => permissions.has(permission)) || ["/home"])[0];
            // If the user was redirected here from another page (token expired),
            // send them back there after a successful login instead of the default landing.
            const rawNext = new URLSearchParams(window.location.search).get("next");
            const destination = _isSafeReturnUrl(rawNext) ? rawNext : defaultPage;
            window.location.href = destination;
        }

        // Press Enter to login
        document.addEventListener("keydown", e => {
            if (e.key === "Enter") login();
        });

    </script>
</body>
</html>
"""


@router.post("/auth/login")
@limiter.limit(settings.LOGIN_RATE_LIMIT)
async def login(
    data: UserLogin,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_async_session),
):
    from app.core.log import record

    # Brute-force protection: track failed attempts per IP in Redis
    import logging
    _brute_logger = logging.getLogger("erp")
    _client_ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or "unknown"
    _fail_key = f"login_fail:{_client_ip}"
    try:
        _redis = _redis_client()
        _fails = await _redis.get(_fail_key)
        if _fails and int(_fails) >= 5:
            await _redis.aclose()
            raise HTTPException(
                status_code=429,
                detail="Too many failed attempts. Try again in 15 minutes.",
            )
        await _redis.aclose()
    except HTTPException:
        raise
    except Exception:
        _brute_logger.warning("Redis unavailable for brute-force check — allowing login attempt")

    result = await db.execute(select(User).where(User.email == data.email))
    user = result.scalar_one_or_none()
    if not user or not verify_password(data.password, user.password):
        # Log failed attempt (no user object, store email)
        record(db, "Auth", "login_failed",
               f"Failed login attempt for email: {data.email}")
        await db.commit()
        try:
            _redis = _redis_client()
            await _redis.incr(_fail_key)
            await _redis.expire(_fail_key, 900)  # 15 minutes TTL
            await _redis.aclose()
        except Exception:
            pass
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")
    if password_needs_rehash(user.password):
        user.password = hash_password(data.password)
    permissions = serialize_permissions(
        get_effective_permissions(user.role, user.permissions)
    )
    token = create_access_token(
        {"sub": user.id, "role": user.role, "permissions": permissions}
    )
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
        path="/",
    )
    response.set_cookie(
        key="logged_in",
        value="true",
        httponly=False,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
        path="/",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    # Issue refresh token
    raw_rt = secrets.token_urlsafe(48)
    rt_hash = hashlib.sha256(raw_rt.encode()).hexdigest()
    rt_expires = datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    db.add(RefreshToken(user_id=user.id, token_hash=rt_hash, expires_at=rt_expires))
    response.set_cookie(
        key="refresh_token",
        value=raw_rt,
        httponly=True,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
        path="/",
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
    )

    # Reset brute-force counter on successful login
    try:
        _redis = _redis_client()
        await _redis.delete(_fail_key)
        await _redis.aclose()
    except Exception:
        pass
    record(db, "Auth", "login",
           f"User logged in: {user.name} ({user.role})",
           user=user, ref_type="user", ref_id=user.id)
    await db.commit()
    # access_token is in the httpOnly cookie — not returned in body to prevent XSS
    return {
        "role": user.role,
        "name": user.name,
        "permissions": permissions,
    }


@router.get("/auth/me")
async def me(current_user: User = Depends(get_current_user)):
    permissions = serialize_permissions(
        get_effective_permissions(current_user.role, current_user.permissions)
    )
    return {
        "id": current_user.id,
        "name": current_user.name,
        "email": current_user.email,
        "role": current_user.role,
        "is_active": current_user.is_active,
        "permissions": permissions,
    }


@router.get("/auth/permissions/catalog")
async def permissions_catalog(current_user: User = Depends(get_current_user)):
    return {
        "catalog": get_permission_catalog(),
        "role": current_user.role,
        "permissions": sorted(get_effective_permissions(current_user.role, current_user.permissions)),
    }


@router.post("/auth/register", response_model=UserOut, status_code=201)
async def register(
    data: UserCreate,
    db: AsyncSession = Depends(get_async_session),
    _: User = Depends(require_admin),
):
    result = await db.execute(select(User).where(User.email == data.email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")
    user = User(
        name=data.name,
        email=data.email,
        password=hash_password(data.password),
        role=data.role,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/auth/logout")
async def logout(
    response: Response,
    db: AsyncSession = Depends(get_async_session),
    refresh_token: str | None = Cookie(None, alias="refresh_token"),
):
    """Clear the auth cookie and invalidate the refresh token."""
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/")
    response.delete_cookie("logged_in", path="/")
    if refresh_token:
        token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
        _r = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
        rt = _r.scalar_one_or_none()
        if rt:
            await db.delete(rt)
            await db.commit()
    return {"ok": True}


@router.post("/auth/refresh")
@limiter.limit(settings.REFRESH_RATE_LIMIT)
async def refresh(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_async_session),
    refresh_token: str | None = Cookie(None, alias="refresh_token"),
):
    """Issue a new access token if a valid refresh token cookie is present."""
    if not refresh_token:
        raise HTTPException(status_code=401, detail="No refresh token")

    new_token = await try_refresh_access_token(db, refresh_token)
    if not new_token:
        raise HTTPException(status_code=401, detail="Refresh token expired or invalid")

    response.set_cookie(
        key="access_token",
        value=new_token,
        httponly=True,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
        path="/",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    response.set_cookie(
        key="logged_in",
        value="true",
        httponly=False,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
        path="/",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    return {"ok": True}
