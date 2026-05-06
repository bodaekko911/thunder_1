"""repair employee HR columns

Revision ID: 20260506_0016
Revises: 20260506_0015
Create Date: 2026-05-06 00:16:00
"""

from alembic import context, op
import sqlalchemy as sa


revision = "20260506_0016"
down_revision = "20260506_0015"
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

    if not _col_exists(inspector, "employees", "attendance_auto_status"):
        op.add_column(
            "employees",
            sa.Column(
                "attendance_auto_status",
                sa.String(length=20),
                nullable=False,
                server_default="present",
            ),
        )

    inspector = sa.inspect(bind)
    if _col_exists(inspector, "employees", "attendance_auto_status"):
        bind.execute(
            sa.text(
                """
                UPDATE employees
                SET attendance_auto_status = 'present'
                WHERE attendance_auto_status IS NULL
                   OR attendance_auto_status NOT IN ('present', 'absent')
                """
            )
        )
        if bind.dialect.name != "sqlite":
            columns = {
                column["name"]: column
                for column in sa.inspect(bind).get_columns("employees")
            }
            if columns["attendance_auto_status"].get("nullable"):
                op.alter_column(
                    "employees",
                    "attendance_auto_status",
                    existing_type=sa.String(length=20),
                    nullable=False,
                    server_default="present",
                )

    inspector = sa.inspect(bind)
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
    # This is a repair migration for columns owned by previous revisions.
    # Downgrading to 0015 should leave that schema intact.
    return
