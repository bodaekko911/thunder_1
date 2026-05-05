"""add nullable payroll link to expenses

Revision ID: 20260419_0013
Revises: 20260419_0012
Create Date: 2026-04-19 00:13:00
"""

from alembic import context, op
import sqlalchemy as sa


revision = "20260419_0013"
down_revision = "20260419_0012"
branch_labels = None
depends_on = None


def _col_exists(inspector, table, col):
    return col in {c["name"] for c in inspector.get_columns(table)}


def _idx_exists(inspector, table, idx):
    return any(i["name"] == idx for i in inspector.get_indexes(table))


def _fk_exists(inspector, table, constrained_columns, referred_table):
    return any(
        fk["constrained_columns"] == constrained_columns
        and fk["referred_table"] == referred_table
        for fk in inspector.get_foreign_keys(table)
    )


def upgrade() -> None:
    if context.is_offline_mode():
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("expenses"):
        return

    if not _col_exists(inspector, "expenses", "payroll_id"):
        op.add_column("expenses", sa.Column("payroll_id", sa.Integer(), nullable=True))

    inspector = sa.inspect(bind)
    if inspector.has_table("payroll") and not _fk_exists(
        inspector, "expenses", ["payroll_id"], "payroll"
    ):
        op.create_foreign_key(
            "fk_expenses_payroll_id_payroll",
            "expenses",
            "payroll",
            ["payroll_id"],
            ["id"],
        )

    inspector = sa.inspect(bind)
    if not _idx_exists(inspector, "expenses", "ix_expenses_payroll_id"):
        op.create_index("ix_expenses_payroll_id", "expenses", ["payroll_id"], unique=True)


def downgrade() -> None:
    if context.is_offline_mode():
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("expenses"):
        return

    if _idx_exists(inspector, "expenses", "ix_expenses_payroll_id"):
        op.drop_index("ix_expenses_payroll_id", table_name="expenses")
    inspector = sa.inspect(bind)
    if _fk_exists(inspector, "expenses", ["payroll_id"], "payroll"):
        op.drop_constraint("fk_expenses_payroll_id_payroll", "expenses", type_="foreignkey")
    inspector = sa.inspect(bind)
    if _col_exists(inspector, "expenses", "payroll_id"):
        op.drop_column("expenses", "payroll_id")
