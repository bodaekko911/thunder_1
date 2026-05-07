"""add HR loans and payroll deduction ledger

Revision ID: 20260507_0017
Revises: 20260506_0016
Create Date: 2026-05-07 00:17:00
"""

from alembic import context, op
import sqlalchemy as sa


revision = "20260507_0017"
down_revision = "20260506_0016"
branch_labels = None
depends_on = None


def _col_exists(inspector, table_name: str, column_name: str) -> bool:
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def _table_exists(inspector, table_name: str) -> bool:
    return inspector.has_table(table_name)


def upgrade() -> None:
    if context.is_offline_mode():
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("payroll"):
        payroll_columns = [
            ("loan_deductions", sa.Numeric(12, 2)),
            ("day_deduction_days", sa.Numeric(8, 2)),
            ("day_deductions", sa.Numeric(12, 2)),
            ("manual_deductions", sa.Numeric(12, 2)),
        ]
        for column_name, column_type in payroll_columns:
            if not _col_exists(inspector, "payroll", column_name):
                op.add_column(
                    "payroll",
                    sa.Column(column_name, column_type, nullable=False, server_default="0"),
                )
        inspector = sa.inspect(bind)

    if not _table_exists(inspector, "employee_loans"):
        op.create_table(
            "employee_loans",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
            sa.Column("loan_date", sa.Date(), nullable=False),
            sa.Column("amount", sa.Numeric(12, 2), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="open"),
            sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.CheckConstraint("amount > 0", name="ck_employee_loans_amount_positive"),
            sa.CheckConstraint(
                "status IN ('open', 'paid', 'cancelled')",
                name="ck_employee_loans_status",
            ),
        )
        op.create_index("ix_employee_loans_employee_id", "employee_loans", ["employee_id"])

    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "employee_loan_repayments"):
        op.create_table(
            "employee_loan_repayments",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("loan_id", sa.Integer(), sa.ForeignKey("employee_loans.id"), nullable=False),
            sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
            sa.Column("payroll_id", sa.Integer(), sa.ForeignKey("payroll.id"), nullable=True),
            sa.Column("repayment_date", sa.Date(), nullable=False),
            sa.Column("amount", sa.Numeric(12, 2), nullable=False),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.CheckConstraint("amount > 0", name="ck_employee_loan_repayments_amount_positive"),
        )
        op.create_index("ix_employee_loan_repayments_loan_id", "employee_loan_repayments", ["loan_id"])
        op.create_index("ix_employee_loan_repayments_employee_id", "employee_loan_repayments", ["employee_id"])
        op.create_index("ix_employee_loan_repayments_payroll_id", "employee_loan_repayments", ["payroll_id"])

    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "employee_payroll_deductions"):
        op.create_table(
            "employee_payroll_deductions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
            sa.Column("payroll_id", sa.Integer(), sa.ForeignKey("payroll.id"), nullable=True),
            sa.Column("period", sa.String(7), nullable=True),
            sa.Column("deduction_date", sa.Date(), nullable=True),
            sa.Column("type", sa.String(30), nullable=False),
            sa.Column("days", sa.Numeric(8, 2), nullable=True),
            sa.Column("daily_rate", sa.Numeric(12, 2), nullable=True),
            sa.Column("amount", sa.Numeric(12, 2), nullable=False),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.CheckConstraint("amount > 0", name="ck_employee_payroll_deductions_amount_positive"),
            sa.CheckConstraint(
                "type IN ('loan_repayment', 'day_deduction', 'manual')",
                name="ck_employee_payroll_deductions_type",
            ),
        )
        op.create_index("ix_employee_payroll_deductions_employee_id", "employee_payroll_deductions", ["employee_id"])
        op.create_index("ix_employee_payroll_deductions_payroll_id", "employee_payroll_deductions", ["payroll_id"])
        op.create_index("ix_employee_payroll_deductions_period", "employee_payroll_deductions", ["period"])


def downgrade() -> None:
    if context.is_offline_mode():
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table_name in [
        "employee_payroll_deductions",
        "employee_loan_repayments",
        "employee_loans",
    ]:
        if inspector.has_table(table_name):
            op.drop_table(table_name)
            inspector = sa.inspect(bind)

    if inspector.has_table("payroll"):
        for column_name in [
            "manual_deductions",
            "day_deductions",
            "day_deduction_days",
            "loan_deductions",
        ]:
            if _col_exists(inspector, "payroll", column_name):
                op.drop_column("payroll", column_name)
                inspector = sa.inspect(bind)
