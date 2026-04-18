"""add import_batch_id to b2b_invoices and consignments for historical-import idempotency

Revision ID: 20260418_0011
Revises: 20260418_0010
Create Date: 2026-04-18 00:10:00
"""

from alembic import context, op
import sqlalchemy as sa

revision = "20260418_0011"
down_revision = "20260418_0010"
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
        ("b2b_invoices",  "ix_b2b_invoices_import_batch_id"),
        ("consignments",  "ix_consignments_import_batch_id"),
    ]:
        if not inspector.has_table(table):
            continue
        if not _col_exists(inspector, table, "import_batch_id"):
            op.add_column(table, sa.Column("import_batch_id", sa.String(64), nullable=True))
        inspector = sa.inspect(bind)  # refresh after DDL
        if not _idx_exists(inspector, table, idx_name):
            op.create_index(idx_name, table, ["import_batch_id"])


def downgrade() -> None:
    if context.is_offline_mode():
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table, idx_name in [
        ("consignments",  "ix_consignments_import_batch_id"),
        ("b2b_invoices",  "ix_b2b_invoices_import_batch_id"),
    ]:
        if not inspector.has_table(table):
            continue
        if _idx_exists(inspector, table, idx_name):
            op.drop_index(idx_name, table_name=table)
        inspector = sa.inspect(bind)
        if _col_exists(inspector, table, "import_batch_id"):
            op.drop_column(table, "import_batch_id")
