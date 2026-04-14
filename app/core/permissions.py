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

LEGACY_PERMISSION_EXPANSIONS = {
    "page_accounting": {
        "page_expenses",
        "action_expenses_create",
        "action_expenses_update",
        "action_expenses_delete",
    },
    "page_suppliers": {
        "page_receive_products",
        "action_receive_products_create",
        "action_receive_products_update",
        "action_receive_products_delete",
        "action_receive_products_export",
    },
}


def expand_legacy_permissions(permissions: Iterable[str]) -> Set[str]:
    expanded = {permission for permission in permissions if permission}
    for permission in list(expanded):
        expanded.update(LEGACY_PERMISSION_EXPANSIONS.get(permission, set()))
    return expanded


def normalize_permissions(raw_permissions: str | None) -> Set[str]:
    normalized = {
        permission.strip()
        for permission in (raw_permissions or "").split(",")
        if permission and permission.strip() and is_known_permission(permission.strip())
    }
    return expand_legacy_permissions(normalized)


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


def _split_permission_overrides(raw_permissions: str | None) -> tuple[Set[str], Set[str]]:
    grants: Set[str] = set()
    revokes: Set[str] = set()
    for token in (raw_permissions or "").split(","):
        value = (token or "").strip()
        if not value:
            continue
        is_revoke = value.startswith("-")
        permission = value[1:].strip() if is_revoke else value
        if not permission or not is_known_permission(permission):
            continue
        expanded = expand_legacy_permissions({permission})
        if is_revoke:
            revokes.update(expanded)
        else:
            grants.update(expanded)
    return grants, revokes


def serialize_permission_overrides(role: str | None, selected_permissions: Iterable[str]) -> str:
    cleaned = {
        permission.strip()
        for permission in selected_permissions
        if permission and permission.strip() and is_known_permission(permission.strip())
    }
    role_permissions = expand_legacy_permissions(get_role_permissions(role))
    if "*" in role_permissions:
        return ""
    grants = cleaned - role_permissions
    revokes = role_permissions - cleaned
    return ",".join(sorted([*grants, *[f"-{permission}" for permission in revokes]]))


def get_effective_permissions(role: str | None, raw_permissions: str | None) -> Set[str]:
    role_permissions = expand_legacy_permissions(get_role_permissions(role))
    if "*" in role_permissions:
        return {"*"}
    grants, revokes = _split_permission_overrides(raw_permissions)
    # Backward compatibility: old rows stored additive-only custom permissions.
    if grants or revokes:
        return (role_permissions | grants) - revokes
    return role_permissions | normalize_permissions(raw_permissions)


def get_custom_permissions(role: str | None, selected_permissions: Iterable[str]) -> Set[str]:
    cleaned = {
        permission.strip()
        for permission in selected_permissions
        if permission and permission.strip() and is_known_permission(permission.strip())
    }
    role_permissions = expand_legacy_permissions(get_role_permissions(role))
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
