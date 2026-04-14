"""add customer discount percentage

Revision ID: 20260413_0006
Revises: 20260413_0005
Create Date: 2026-04-13 20:40:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260413_0006"
down_revision = "20260413_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "customers",
        sa.Column("discount_pct", sa.Numeric(precision=6, scale=2), nullable=True, server_default="0"),
    )
    op.execute("UPDATE customers SET discount_pct = 0 WHERE discount_pct IS NULL")
    op.alter_column("customers", "discount_pct", server_default=None)


def downgrade() -> None:
    op.drop_column("customers", "discount_pct")
