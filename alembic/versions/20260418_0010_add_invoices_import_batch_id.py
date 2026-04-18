"""add invoices.import_batch_id for historical-sales-import idempotency

Revision ID: 20260418_0010
Revises: 20260416_0009
Create Date: 2026-04-18 00:00:00
"""

from alembic import context, op
import sqlalchemy as sa

revision = "20260418_0010"
down_revision = "20260416_0009"
branch_labels = None
depends_on = None


def _index_exists(inspector: sa.Inspector, table_name: str, index_name: str) -> bool:
    return any(idx["name"] == index_name for idx in inspector.get_indexes(table_name))


def upgrade() -> None:
    if context.is_offline_mode():
        return

    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("invoices"):
        return

    existing_cols = {c["name"] for c in inspector.get_columns("invoices")}
    if "import_batch_id" not in existing_cols:
        op.add_column(
            "invoices",
            sa.Column("import_batch_id", sa.String(64), nullable=True),
        )

    inspector = sa.inspect(op.get_bind())
    if not _index_exists(inspector, "invoices", "ix_invoices_import_batch_id"):
        op.create_index("ix_invoices_import_batch_id", "invoices", ["import_batch_id"])


def downgrade() -> None:
    if context.is_offline_mode():
        return

    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("invoices"):
        return

    if _index_exists(inspector, "invoices", "ix_invoices_import_batch_id"):
        op.drop_index("ix_invoices_import_batch_id", table_name="invoices")

    existing_cols = {c["name"] for c in inspector.get_columns("invoices")}
    if "import_batch_id" in existing_cols:
        op.drop_column("invoices", "import_batch_id")
