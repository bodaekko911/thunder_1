"""add low stock reorder settings

Revision ID: 20260413_0003
Revises: 20260412_0002
Create Date: 2026-04-13 12:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260413_0003"
down_revision = "20260412_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("reorder_level", sa.Numeric(precision=12, scale=3), nullable=True))
    op.add_column("products", sa.Column("reorder_qty", sa.Numeric(precision=12, scale=3), nullable=True))
    op.add_column("products", sa.Column("preferred_supplier_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_products_preferred_supplier_id_suppliers",
        "products",
        "suppliers",
        ["preferred_supplier_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_products_preferred_supplier_id_suppliers", "products", type_="foreignkey")
    op.drop_column("products", "preferred_supplier_id")
    op.drop_column("products", "reorder_qty")
    op.drop_column("products", "reorder_level")
