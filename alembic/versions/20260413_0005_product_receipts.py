"""add product_receipts table

Revision ID: 20260413_0005
Revises: 20260413_0004
Create Date: 2026-04-13 15:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260413_0005"
down_revision = "20260413_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "product_receipts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ref_number", sa.String(length=30), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("receive_date", sa.Date(), nullable=False),
        sa.Column("qty", sa.Numeric(precision=12, scale=3), nullable=False),
        sa.Column("unit_cost", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("total_cost", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("supplier_ref", sa.String(length=150), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("expense_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(["expense_id"], ["expenses.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_number"),
    )
    op.create_index("ix_product_receipts_id", "product_receipts", ["id"], unique=False)
    op.create_index(
        "ix_product_receipts_ref_number", "product_receipts", ["ref_number"], unique=False
    )
    op.create_index(
        "ix_product_receipts_product_id", "product_receipts", ["product_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_product_receipts_product_id", table_name="product_receipts")
    op.drop_index("ix_product_receipts_ref_number", table_name="product_receipts")
    op.drop_index("ix_product_receipts_id", table_name="product_receipts")
    op.drop_table("product_receipts")
