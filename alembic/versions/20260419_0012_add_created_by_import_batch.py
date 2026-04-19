"""add created_by_import_batch to products and customers for historical-import traceability

Revision ID: 20260419_0012
Revises: 20260418_0011
Create Date: 2026-04-19 00:00:00
"""

from alembic import context, op
import sqlalchemy as sa

revision = "20260419_0012"
down_revision = "20260418_0011"
branch_labels = None
depends_on = None


def _col_exists(inspector, table, col):
    return col in {c["name"] for c in inspector.get_columns(table)}


def _idx_exists(inspector, table, idx):
    return any(i["name"] == idx for i in inspector.get_indexes(table))


def upgrade() -> None:
    if context.is_offline_mode():
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table, idx_name in [
        ("products",  "ix_products_created_by_import_batch"),
        ("customers", "ix_customers_created_by_import_batch"),
    ]:
        if not inspector.has_table(table):
            continue
        if not _col_exists(inspector, table, "created_by_import_batch"):
            op.add_column(table, sa.Column("created_by_import_batch", sa.String(64), nullable=True))
        inspector = sa.inspect(bind)
        if not _idx_exists(inspector, table, idx_name):
            op.create_index(idx_name, table, ["created_by_import_batch"])


def downgrade() -> None:
    if context.is_offline_mode():
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table, idx_name in [
        ("customers", "ix_customers_created_by_import_batch"),
        ("products",  "ix_products_created_by_import_batch"),
    ]:
        if not inspector.has_table(table):
            continue
        if _idx_exists(inspector, table, idx_name):
            op.drop_index(idx_name, table_name=table)
        inspector = sa.inspect(bind)
        if _col_exists(inspector, table, "created_by_import_batch"):
            op.drop_column(table, "created_by_import_batch")
