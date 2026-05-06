"""add optional employee farm assignment

Revision ID: 20260506_0015
Revises: 20260505_0014
Create Date: 2026-05-06 00:15:00
"""

from alembic import context, op
import sqlalchemy as sa


revision = "20260506_0015"
down_revision = "20260505_0014"
branch_labels = None
depends_on = None


def _col_exists(inspector, table_name: str, column_name: str) -> bool:
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def _idx_exists(inspector, table_name: str, index_name: str) -> bool:
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def _fk_exists(inspector, table_name: str, constrained_columns: list[str], referred_table: str) -> bool:
    return any(
        fk["constrained_columns"] == constrained_columns
        and fk["referred_table"] == referred_table
        for fk in inspector.get_foreign_keys(table_name)
    )


def upgrade() -> None:
    if context.is_offline_mode():
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("employees"):
        return

    if not _col_exists(inspector, "employees", "farm_id"):
        op.add_column("employees", sa.Column("farm_id", sa.Integer(), nullable=True))

    inspector = sa.inspect(bind)
    if not _idx_exists(inspector, "employees", "ix_employees_farm_id"):
        op.create_index("ix_employees_farm_id", "employees", ["farm_id"], unique=False)

    inspector = sa.inspect(bind)
    if (
        bind.dialect.name != "sqlite"
        and inspector.has_table("farms")
        and not _fk_exists(inspector, "employees", ["farm_id"], "farms")
    ):
        op.create_foreign_key(
            "fk_employees_farm_id_farms",
            "employees",
            "farms",
            ["farm_id"],
            ["id"],
        )


def downgrade() -> None:
    if context.is_offline_mode():
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("employees"):
        return

    if (
        bind.dialect.name != "sqlite"
        and _fk_exists(inspector, "employees", ["farm_id"], "farms")
    ):
        op.drop_constraint("fk_employees_farm_id_farms", "employees", type_="foreignkey")

    inspector = sa.inspect(bind)
    if _idx_exists(inspector, "employees", "ix_employees_farm_id"):
        op.drop_index("ix_employees_farm_id", table_name="employees")

    inspector = sa.inspect(bind)
    if _col_exists(inspector, "employees", "farm_id"):
        op.drop_column("employees", "farm_id")
