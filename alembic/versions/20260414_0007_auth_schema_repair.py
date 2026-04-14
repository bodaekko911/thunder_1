"""repair auth schema when production is stamped without auth tables

Revision ID: 20260414_0007
Revises: eadb1eb64495
Create Date: 2026-04-14 18:05:00
"""

from alembic import context, op
import sqlalchemy as sa


revision = "20260414_0007"
down_revision = "eadb1eb64495"
branch_labels = None
depends_on = None

_LEGACY_USER_TABLE_CANDIDATES = ("user", "app_users")
_USER_REQUIRED_COLUMNS = {
    "id",
    "name",
    "email",
    "password",
    "role",
    "is_active",
    "created_at",
    "permissions",
}


def _columns(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


def _index_exists(inspector: sa.Inspector, table_name: str, index_name: str) -> bool:
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def _ensure_users_table(inspector: sa.Inspector) -> None:
    if inspector.has_table("users"):
        users_columns = _columns(inspector, "users")
        if "permissions" not in users_columns:
            op.add_column("users", sa.Column("permissions", sa.Text(), nullable=True))
        return

    for candidate in _LEGACY_USER_TABLE_CANDIDATES:
        if not inspector.has_table(candidate):
            continue
        candidate_columns = _columns(inspector, candidate)
        if _USER_REQUIRED_COLUMNS.issubset(candidate_columns):
            op.rename_table(candidate, "users")
            return

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("email", sa.String(length=150), nullable=False),
        sa.Column("password", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=30), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("permissions", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def _ensure_users_indexes(inspector: sa.Inspector) -> None:
    if not inspector.has_table("users"):
        return
    if not _index_exists(inspector, "users", "ix_users_id"):
        op.create_index("ix_users_id", "users", ["id"], unique=False)
    if not _index_exists(inspector, "users", "ix_users_email"):
        op.create_index("ix_users_email", "users", ["email"], unique=True)


def _ensure_refresh_tokens_table(inspector: sa.Inspector) -> None:
    if inspector.has_table("refresh_tokens"):
        return
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )


def _ensure_refresh_tokens_indexes(inspector: sa.Inspector) -> None:
    if not inspector.has_table("refresh_tokens"):
        return
    if not _index_exists(inspector, "refresh_tokens", "ix_refresh_tokens_id"):
        op.create_index("ix_refresh_tokens_id", "refresh_tokens", ["id"], unique=False)
    if not _index_exists(inspector, "refresh_tokens", "ix_refresh_tokens_token_hash"):
        op.create_index("ix_refresh_tokens_token_hash", "refresh_tokens", ["token_hash"], unique=True)


def _ensure_activity_logs_table(inspector: sa.Inspector) -> None:
    if inspector.has_table("activity_logs"):
        return
    op.create_table(
        "activity_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("user_name", sa.String(length=150), nullable=True),
        sa.Column("user_role", sa.String(length=50), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=True),
        sa.Column("module", sa.String(length=50), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("ref_type", sa.String(length=50), nullable=True),
        sa.Column("ref_id", sa.String(length=50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def _ensure_activity_logs_indexes(inspector: sa.Inspector) -> None:
    if not inspector.has_table("activity_logs"):
        return
    if not _index_exists(inspector, "activity_logs", "ix_activity_logs_id"):
        op.create_index("ix_activity_logs_id", "activity_logs", ["id"], unique=False)


def upgrade() -> None:
    if context.is_offline_mode():
        return

    inspector = sa.inspect(op.get_bind())
    _ensure_users_table(inspector)

    inspector = sa.inspect(op.get_bind())
    _ensure_users_indexes(inspector)

    inspector = sa.inspect(op.get_bind())
    _ensure_refresh_tokens_table(inspector)

    inspector = sa.inspect(op.get_bind())
    _ensure_refresh_tokens_indexes(inspector)

    inspector = sa.inspect(op.get_bind())
    _ensure_activity_logs_table(inspector)

    inspector = sa.inspect(op.get_bind())
    _ensure_activity_logs_indexes(inspector)


def downgrade() -> None:
    # This repair migration is intentionally non-destructive.
    pass
