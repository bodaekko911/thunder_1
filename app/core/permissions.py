from typing import Iterable, Set

from fastapi import Depends, HTTPException, status

from app.core.permission_catalog import get_role_permissions, is_known_permission
from app.core.security import get_current_user
from app.models.user import User


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


def require_permission(permission: str):
    async def checker(user: User = Depends(get_current_user)):
        if not has_permission(user, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied: {permission}",
            )
        return user

    return checker


async def require_admin(user: User = Depends(get_current_user)):
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return user
