"""add stock locations and transfers

Revision ID: 20260413_0004
Revises: 20260413_0003
Create Date: 2026-04-13 12:45:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260413_0004"
down_revision = "20260413_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stock_locations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("code", sa.String(length=40), nullable=True),
        sa.Column("location_type", sa.String(length=30), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
        sa.UniqueConstraint("name"),
    )
    op.create_index("ix_stock_locations_id", "stock_locations", ["id"], unique=False)
    op.create_index("ix_stock_locations_name", "stock_locations", ["name"], unique=False)
    op.create_index("ix_stock_locations_code", "stock_locations", ["code"], unique=False)

    op.create_table(
        "location_stocks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("qty", sa.Numeric(precision=12, scale=3), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.ForeignKeyConstraint(["location_id"], ["stock_locations.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("location_id", "product_id", name="uq_location_stocks_location_product"),
    )
    op.create_index("ix_location_stocks_id", "location_stocks", ["id"], unique=False)
    op.create_index("ix_location_stocks_location_id", "location_stocks", ["location_id"], unique=False)
    op.create_index("ix_location_stocks_product_id", "location_stocks", ["product_id"], unique=False)

    op.create_table(
        "stock_transfers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("source_location_id", sa.Integer(), nullable=False),
        sa.Column("destination_location_id", sa.Integer(), nullable=False),
        sa.Column("qty", sa.Numeric(precision=12, scale=3), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.ForeignKeyConstraint(["destination_location_id"], ["stock_locations.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.ForeignKeyConstraint(["source_location_id"], ["stock_locations.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_stock_transfers_id", "stock_transfers", ["id"], unique=False)
    op.create_index("ix_stock_transfers_product_id", "stock_transfers", ["product_id"], unique=False)
    op.create_index("ix_stock_transfers_source_location_id", "stock_transfers", ["source_location_id"], unique=False)
    op.create_index("ix_stock_transfers_destination_location_id", "stock_transfers", ["destination_location_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_stock_transfers_destination_location_id", table_name="stock_transfers")
    op.drop_index("ix_stock_transfers_source_location_id", table_name="stock_transfers")
    op.drop_index("ix_stock_transfers_product_id", table_name="stock_transfers")
    op.drop_index("ix_stock_transfers_id", table_name="stock_transfers")
    op.drop_table("stock_transfers")

    op.drop_index("ix_location_stocks_product_id", table_name="location_stocks")
    op.drop_index("ix_location_stocks_location_id", table_name="location_stocks")
    op.drop_index("ix_location_stocks_id", table_name="location_stocks")
    op.drop_table("location_stocks")

    op.drop_index("ix_stock_locations_code", table_name="stock_locations")
    op.drop_index("ix_stock_locations_name", table_name="stock_locations")
    op.drop_index("ix_stock_locations_id", table_name="stock_locations")
    op.drop_table("stock_locations")
