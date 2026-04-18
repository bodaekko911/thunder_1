from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Cookie, Depends, Header, HTTPException, status
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.database import get_async_session

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    return pwd_context.verify(plain, hashed)


def password_needs_rehash(hashed: str) -> bool:
    if not hashed:
        return True
    return pwd_context.needs_update(hashed)


def create_access_token(data: dict, expires_minutes: Optional[int] = None) -> str:
    payload = data.copy()
    now = datetime.now(timezone.utc)
    payload["sub"] = str(payload["sub"])
    payload["iat"] = now
    payload["exp"] = now + timedelta(
        minutes=expires_minutes or settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def extract_bearer_token(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    scheme, _, token = authorization.strip().partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


def resolve_auth_token(
    authorization: Optional[str],
    access_token: Optional[str],
    *,
    required: bool = True,
) -> Optional[str]:
    if access_token:
        return access_token
    if authorization:
        return extract_bearer_token(authorization)
    if required:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return None


async def _load_current_user(
    token: Optional[str],
    db: AsyncSession,
    *,
    required: bool,
):
    if not token:
        return None

    from app.models.user import User

    payload = decode_token(token)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
            headers={"WWW-Authenticate": "Bearer"},
        )

    result = await db.execute(select(User).where(User.id == int(user_id)))
    user = result.scalar_one_or_none()
    if not user:
        if required:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return None
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled",
        )
    return user


async def get_current_user(
    authorization: Optional[str] = Header(None),
    access_token: Optional[str] = Cookie(None),
    db: AsyncSession = Depends(get_async_session),
):
    token = resolve_auth_token(authorization, access_token, required=True)
    return await _load_current_user(token, db, required=True)


async def get_optional_current_user(
    authorization: Optional[str] = Header(None),
    access_token: Optional[str] = Cookie(None),
    db: AsyncSession = Depends(get_async_session),
):
    token = resolve_auth_token(authorization, access_token, required=False)
    return await _load_current_user(token, db, required=False)


def require_role(*roles: str):
    async def checker(current_user=Depends(get_current_user)):
        if current_user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Required role: {', '.join(roles)}",
            )
        return current_user

    return checker


async def try_refresh_access_token(db: AsyncSession, refresh_token_value: str) -> Optional[str]:
    """
    Validate a raw refresh-token value and, if valid, mint a new access token.
    Returns the new token string, or None on any failure (bad/expired token,
    user not found, user inactive).

    Uses deferred imports to avoid a circular dependency with app.core.permissions,
    which imports get_current_user from this module.

    Called by both ``POST /auth/refresh`` and the session-expiry middleware in
    app_factory.py so the DB + token logic lives in exactly one place.
    """
    import hashlib
    from app.models.refresh_token import RefreshToken

    token_hash = hashlib.sha256(refresh_token_value.encode()).hexdigest()
    result = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    rt = result.scalar_one_or_none()

    now = datetime.now(timezone.utc)
    if not rt or rt.expires_at.replace(tzinfo=timezone.utc) < now:
        return None

    from app.models.user import User as _User
    u_result = await db.execute(select(_User).where(_User.id == rt.user_id))
    user = u_result.scalar_one_or_none()
    if not user or not user.is_active:
        return None

    from app.core.permissions import get_effective_permissions, serialize_permissions
    permissions = serialize_permissions(get_effective_permissions(user.role, user.permissions))
    return create_access_token({"sub": user.id, "role": user.role, "permissions": permissions})
