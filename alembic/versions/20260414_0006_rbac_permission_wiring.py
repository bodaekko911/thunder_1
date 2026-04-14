"""backfill split expense and receive permissions

Revision ID: 20260414_0006
Revises: 20260413_0005
Create Date: 2026-04-14 11:00:00
"""

from alembic import context, op
import sqlalchemy as sa


revision = "20260414_0006"
down_revision = "20260413_0005"
branch_labels = None
depends_on = None

LEGACY_GRANTS = {
    "page_accounting": {
        "page_expenses",
        "action_expenses_create",
        "action_expenses_update",
        "action_expenses_delete",
    },
    "page_suppliers": {
        "page_receive_products",
        "action_receive_products_create",
    },
}


def _expanded_permissions(raw_permissions: str | None) -> str | None:
    if raw_permissions is None:
        return None

    permissions = {
        permission.strip()
        for permission in raw_permissions.split(",")
        if permission and permission.strip()
    }
    expanded = set(permissions)
    for permission in list(permissions):
        expanded.update(LEGACY_GRANTS.get(permission, set()))
    return ",".join(sorted(expanded))


def upgrade() -> None:
    if context.is_offline_mode():
        return

    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if not inspector.has_table("users"):
        return

    columns = {column["name"] for column in inspector.get_columns("users")}
    if "permissions" not in columns:
        return

    users = connection.execute(sa.text("SELECT id, permissions FROM users")).mappings().all()
    for row in users:
        expanded = _expanded_permissions(row["permissions"])
        if expanded == row["permissions"]:
            continue
        connection.execute(
            sa.text("UPDATE users SET permissions = :permissions WHERE id = :id"),
            {"id": row["id"], "permissions": expanded},
        )


def downgrade() -> None:
    pass
