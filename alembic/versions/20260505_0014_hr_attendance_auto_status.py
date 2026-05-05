"""add persistent HR attendance auto status

Revision ID: 20260505_0014
Revises: 20260419_0013
Create Date: 2026-05-05 00:14:00
"""

from alembic import context, op
import sqlalchemy as sa


revision = "20260505_0014"
down_revision = "20260419_0013"
branch_labels = None
depends_on = None


def _col_exists(inspector, table_name: str, column_name: str) -> bool:
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def _unique_exists(inspector, table_name: str, name: str) -> bool:
    if any(constraint["name"] == name for constraint in inspector.get_unique_constraints(table_name)):
        return True
    return any(index["name"] == name and index.get("unique") for index in inspector.get_indexes(table_name))


def _dedupe_attendance(bind) -> None:
    duplicates = bind.execute(
        sa.text(
            """
            SELECT employee_id, date
            FROM attendance
            GROUP BY employee_id, date
            HAVING COUNT(*) > 1
            """
        )
    ).mappings().all()
    if not duplicates:
        return

    delete_stmt = sa.text("DELETE FROM attendance WHERE id IN :ids").bindparams(
        sa.bindparam("ids", expanding=True)
    )
    for duplicate in duplicates:
        rows = bind.execute(
            sa.text(
                """
                SELECT id
                FROM attendance
                WHERE employee_id = :employee_id AND date = :attendance_date
                ORDER BY id DESC
                """
            ),
            {
                "employee_id": duplicate["employee_id"],
                "attendance_date": duplicate["date"],
            },
        ).mappings().all()
        stale_ids = [row["id"] for row in rows[1:]]
        if stale_ids:
            bind.execute(delete_stmt, {"ids": stale_ids})


def upgrade() -> None:
    if context.is_offline_mode():
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("employees") and not _col_exists(
        inspector, "employees", "attendance_auto_status"
    ):
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
    if inspector.has_table("employees") and _col_exists(
        inspector, "employees", "attendance_auto_status"
    ):
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

    inspector = sa.inspect(bind)
    if inspector.has_table("attendance") and not _unique_exists(
        inspector, "attendance", "uq_attendance_employee_date"
    ):
        _dedupe_attendance(bind)
        with op.batch_alter_table("attendance") as batch_op:
            batch_op.create_unique_constraint(
                "uq_attendance_employee_date",
                ["employee_id", "date"],
            )


def downgrade() -> None:
    if context.is_offline_mode():
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("attendance") and _unique_exists(
        inspector, "attendance", "uq_attendance_employee_date"
    ):
        with op.batch_alter_table("attendance") as batch_op:
            batch_op.drop_constraint("uq_attendance_employee_date", type_="unique")

    inspector = sa.inspect(bind)
    if inspector.has_table("employees") and _col_exists(
        inspector, "employees", "attendance_auto_status"
    ):
        op.drop_column("employees", "attendance_auto_status")
