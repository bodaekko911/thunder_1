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
    verify_password,
)
from app.database import get_async_session
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.schemas.user import UserCreate, UserOut, UserLogin
from app.core.rate_limit import limiter

router = APIRouter(tags=["Auth"])


@router.get("/", response_class=HTMLResponse)
def login_page():
    return """
<!DOCTYPE html>
<html>
<head>
    <title>ERP Login</title>
    <style>
        :root {
            --bg: #0a0d18;
            --card: #0f1424;
            --border: rgba(255,255,255,0.08);
            --text: #ffffff;
            --sub: #8899bb;
            --muted: #445066;
            --accent: #00ff9d;
        }
        body.light {
            --bg: #f4f5ef;
            --card: #eceee6;
            --border: rgba(0,0,0,0.08);
            --text: #1a1e14;
            --sub: #4a5040;
            --muted: #7b816f;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', sans-serif;
            background: var(--bg);
            height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            color: var(--text);
        }
        .box {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 40px;
            width: 360px;
            position: relative;
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
        body.light input {
            background: rgba(255,255,255,0.55);
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
        .mode-btn {
            position: fixed;
            top: 18px;
            right: 18px;
            width: 40px;
            height: 40px;
            border-radius: 10px;
            border: 1px solid var(--border);
            background: var(--card);
            color: var(--sub);
            font-size: 16px;
            cursor: pointer;
            transition: all .2s;
        }
        .mode-btn:hover {
            transform: scale(1.06);
        }
    </style>
</head>
<body>
    <button class="mode-btn" id="mode-btn" onclick="toggleMode()" title="Toggle color mode">🌙</button>
    <div class="box">
        <h2>Welcome Back</h2>
        <p>Sign in to your ERP system</p>

        <label>Email</label>
        <input id="email" type="email" placeholder="you@example.com">

        <label>Password</label>
        <input id="password" type="password" placeholder="••••••••">

        <button onclick="login()">Sign In</button>
        <div id="error">Wrong email or password</div>
    </div>

    <script>
        function setModeButton(isLight) {
            const btn = document.getElementById("mode-btn");
            if (btn) btn.innerText = isLight ? "☀️" : "🌙";
        }

        function toggleMode() {
            const isLight = document.body.classList.toggle("light");
            localStorage.setItem("colorMode", isLight ? "light" : "dark");
            setModeButton(isLight);
        }

        function initializeColorMode() {
            const isLight = localStorage.getItem("colorMode") === "light";
            document.body.classList.toggle("light", isLight);
            setModeButton(isLight);
        }

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
            // Token is stored in an httpOnly cookie set by the server — no localStorage.
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
                ["/import", "page_import"],
                ["/reports/", "page_reports"],
                ["/b2b/", "page_b2b"],
                ["/hr/", "page_hr"],
                ["/accounting/", "page_accounting"],
                ["/expenses/", "page_accounting"]
            ];
            const nextPage = data.role === "admin"
                ? "/dashboard"
                : (landingPages.find(([, permission]) => permissions.has(permission)) || ["/home"])[0];
            window.location.href = nextPage;
        }

        // Press Enter to login
        document.addEventListener("keydown", e => {
            if (e.key === "Enter") login();
        });

        initializeColorMode();
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
        import redis.asyncio as aioredis
        _redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
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
            import redis.asyncio as aioredis
            _redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
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
        import redis.asyncio as aioredis
        _redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
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
async def refresh(
    response: Response,
    db: AsyncSession = Depends(get_async_session),
    refresh_token: str | None = Cookie(None, alias="refresh_token"),
):
    """Issue a new access token if a valid refresh token cookie is present."""
    if not refresh_token:
        raise HTTPException(status_code=401, detail="No refresh token")

    token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
    _r = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    rt = _r.scalar_one_or_none()

    now = datetime.now(timezone.utc)
    if not rt or rt.expires_at.replace(tzinfo=timezone.utc) < now:
        raise HTTPException(status_code=401, detail="Refresh token expired or invalid")

    _u = await db.execute(select(User).where(User.id == rt.user_id))
    user = _u.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")

    permissions = serialize_permissions(get_effective_permissions(user.role, user.permissions))
    new_token = create_access_token({"sub": user.id, "role": user.role, "permissions": permissions})
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
