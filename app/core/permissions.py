from typing import Iterable, Set

from fastapi import Depends, HTTPException, Request, status

from app.core.permission_catalog import (
    get_permission_key,
    get_role_permissions,
    is_known_permission,
)
from app.core.security import get_current_user
from app.database import get_async_session
from app.models.user import User
from app.core.log import ActivityLog


def normalize_permissions(raw_permissions: str | None) -> Set[str]:
    return {
        permission.strip()
        for permission in (raw_permissions or "").split(",")
        if permission and permission.strip() and is_known_permission(permission.strip())
    }


def serialize_permissions(permissions: Iterable[str]) -> str:
    return ",".join(
        sorted(
            {
                permission.strip()
                for permission in permissions
                if permission and permission.strip() and is_known_permission(permission.strip())
            }
        )
    )


def get_effective_permissions(role: str | None, raw_permissions: str | None) -> Set[str]:
    role_permissions = get_role_permissions(role)
    if "*" in role_permissions:
        return {"*"}
    return role_permissions | normalize_permissions(raw_permissions)


def get_custom_permissions(role: str | None, selected_permissions: Iterable[str]) -> Set[str]:
    cleaned = {
        permission.strip()
        for permission in selected_permissions
        if permission and permission.strip() and is_known_permission(permission.strip())
    }
    role_permissions = get_role_permissions(role)
    if "*" in role_permissions:
        return set()
    return cleaned - role_permissions


def has_permission(user: User, permission: str) -> bool:
    effective_permissions = get_effective_permissions(user.role, user.permissions)
    return "*" in effective_permissions or permission in effective_permissions


def has_action_permission(user: User, module: str, resource: str, action: str) -> bool:
    return has_permission(user, get_permission_key(module, resource, action))


async def _record_permission_denial(db, user: User, permission: str, path: str | None) -> None:
    try:
        db.add(
            ActivityLog(
                user_id=user.id,
                user_name=user.name,
                user_role=user.role,
                action="PERMISSION_DENIED",
                module="AUTHZ",
                description=f"Denied {permission}" + (f" on {path}" if path else ""),
                ref_type="permission",
                ref_id=permission,
            )
        )
        await db.commit()
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass


async def ensure_permission(
    db,
    user: User,
    permission: str,
    *,
    path: str | None = None,
) -> None:
    if has_permission(user, permission):
        return
    await _record_permission_denial(db, user, permission, path)
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Permission denied: {permission}",
    )


async def ensure_action_permission(
    db,
    user: User,
    module: str,
    resource: str,
    action: str,
    *,
    path: str | None = None,
) -> None:
    await ensure_permission(
        db,
        user,
        get_permission_key(module, resource, action),
        path=path,
    )


def require_permission(permission: str):
    async def checker(
        user: User = Depends(get_current_user),
        db=Depends(get_async_session),
        request: Request = None,
    ):
        await ensure_permission(
            db,
            user,
            permission,
            path=str(request.url.path) if request is not None else None,
        )
        return user

    return checker


def require_action(module: str, resource: str, action: str):
    return require_permission(get_permission_key(module, resource, action))


async def require_admin(user: User = Depends(get_current_user)):
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return user
