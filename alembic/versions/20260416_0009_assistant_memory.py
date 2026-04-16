"""add assistant conversation memory tables

Revision ID: 20260416_0009
Revises: 20260414_0008
Create Date: 2026-04-16 22:45:00
"""

from alembic import context, op
import sqlalchemy as sa


revision = "20260416_0009"
down_revision = "20260414_0008"
branch_labels = None
depends_on = None


def _index_exists(inspector: sa.Inspector, table_name: str, index_name: str) -> bool:
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def _create_index_if_missing(
    inspector: sa.Inspector,
    index_name: str,
    table_name: str,
    columns: list[str],
    *,
    unique: bool = False,
) -> None:
    if inspector.has_table(table_name) and not _index_exists(inspector, table_name, index_name):
        op.create_index(index_name, table_name, columns, unique=unique)


def _ensure_assistant_sessions(inspector: sa.Inspector) -> None:
    if inspector.has_table("assistant_sessions"):
        return
    op.create_table(
        "assistant_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=50), nullable=False),
        sa.Column("last_intent", sa.String(length=100), nullable=True),
        sa.Column("last_date_from", sa.Date(), nullable=True),
        sa.Column("last_date_to", sa.Date(), nullable=True),
        sa.Column("last_entity_ids", sa.Text(), nullable=True),
        sa.Column("last_comparison_baseline", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def _ensure_assistant_messages(inspector: sa.Inspector) -> None:
    if inspector.has_table("assistant_messages"):
        return
    op.create_table(
        "assistant_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("message_text", sa.Text(), nullable=False),
        sa.Column("intent", sa.String(length=100), nullable=True),
        sa.Column("parameters_json", sa.Text(), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["assistant_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )


def _ensure_assistant_feedback(inspector: sa.Inspector) -> None:
    if inspector.has_table("assistant_feedback"):
        return
    op.create_table(
        "assistant_feedback",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.ForeignKeyConstraint(["message_id"], ["assistant_messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["assistant_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def upgrade() -> None:
    if context.is_offline_mode():
        return

    inspector = sa.inspect(op.get_bind())
    _ensure_assistant_sessions(inspector)

    inspector = sa.inspect(op.get_bind())
    _ensure_assistant_messages(inspector)

    inspector = sa.inspect(op.get_bind())
    _ensure_assistant_feedback(inspector)

    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_assistant_sessions_id", "assistant_sessions", ["id"])
    _create_index_if_missing(inspector, "ix_assistant_sessions_user_id", "assistant_sessions", ["user_id"])
    _create_index_if_missing(inspector, "ix_assistant_sessions_channel", "assistant_sessions", ["channel"])
    _create_index_if_missing(inspector, "ix_assistant_messages_id", "assistant_messages", ["id"])
    _create_index_if_missing(inspector, "ix_assistant_messages_session_id", "assistant_messages", ["session_id"])
    _create_index_if_missing(inspector, "ix_assistant_feedback_id", "assistant_feedback", ["id"])
    _create_index_if_missing(inspector, "ix_assistant_feedback_session_id", "assistant_feedback", ["session_id"])
    _create_index_if_missing(inspector, "ix_assistant_feedback_message_id", "assistant_feedback", ["message_id"])
    _create_index_if_missing(inspector, "ix_assistant_feedback_user_id", "assistant_feedback", ["user_id"])


def downgrade() -> None:
    # This migration is intentionally non-destructive.
    pass
