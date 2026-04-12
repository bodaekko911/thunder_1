"""align legacy startup mutations

Revision ID: 20260412_0002
Revises: 20260412_0001
Create Date: 2026-04-12 19:10:00
"""

from alembic import context, op
import sqlalchemy as sa
import bcrypt

from app.core.config import settings


# revision identifiers, used by Alembic.
revision = "20260412_0002"
down_revision = "20260412_0001"
branch_labels = None
depends_on = None

_SCHEMA_PATCHES = (
    {
        "table": "b2b_invoices",
        "column": "user_id",
        "type": sa.Integer(),
        "nullable": True,
        "foreign_key": ("fk_b2b_invoices_user_id_users", ["user_id"], "users", ["id"]),
    },
    {
        "table": "consignments",
        "column": "user_id",
        "type": sa.Integer(),
        "nullable": True,
        "foreign_key": ("fk_consignments_user_id_users", ["user_id"], "users", ["id"]),
    },
    {
        "table": "b2b_refunds",
        "column": "user_id",
        "type": sa.Integer(),
        "nullable": True,
        "foreign_key": ("fk_b2b_refunds_user_id_users", ["user_id"], "users", ["id"]),
    },
    {
        "table": "farm_deliveries",
        "column": "user_id",
        "type": sa.Integer(),
        "nullable": True,
        "foreign_key": ("fk_farm_deliveries_user_id_users", ["user_id"], "users", ["id"]),
    },
    {
        "table": "production_batches",
        "column": "user_id",
        "type": sa.Integer(),
        "nullable": True,
        "foreign_key": ("fk_production_batches_user_id_users", ["user_id"], "users", ["id"]),
    },
    {
        "table": "spoilage_records",
        "column": "user_id",
        "type": sa.Integer(),
        "nullable": True,
        "foreign_key": ("fk_spoilage_records_user_id_users", ["user_id"], "users", ["id"]),
    },
    {
        "table": "payroll",
        "column": "days_worked",
        "type": sa.Integer(),
        "nullable": True,
    },
    {
        "table": "payroll",
        "column": "working_days",
        "type": sa.Integer(),
        "nullable": True,
    },
    {
        "table": "payroll",
        "column": "paid_at",
        "type": sa.DateTime(timezone=True),
        "nullable": True,
    },
    {
        "table": "expenses",
        "column": "farm_id",
        "type": sa.Integer(),
        "nullable": True,
        "foreign_key": ("fk_expenses_farm_id_farms", ["farm_id"], "farms", ["id"]),
    },
    {
        "table": "b2b_clients",
        "column": "discount_pct",
        "type": sa.Numeric(precision=6, scale=2),
        "nullable": True,
    },
)

_DEFAULT_EXPENSE_CATEGORIES = (
    ("5001", "Water"),
    ("5002", "Electricity"),
    ("5003", "Gas"),
    ("5004", "Rent"),
    ("5005", "Fuel & Transportation"),
    ("5006", "Salaries & Wages"),
    ("5007", "Packaging Materials"),
    ("5008", "Maintenance & Repairs"),
    ("5009", "Marketing & Advertising"),
    ("5010", "Miscellaneous"),
)


def _column_exists(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def _foreign_key_exists(
    inspector: sa.Inspector,
    table_name: str,
    constrained_columns: list[str],
    referred_table: str,
    referred_columns: list[str],
) -> bool:
    for foreign_key in inspector.get_foreign_keys(table_name):
        if (
            foreign_key["constrained_columns"] == constrained_columns
            and foreign_key["referred_table"] == referred_table
            and foreign_key["referred_columns"] == referred_columns
        ):
            return True
    return False


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _seed_default_expense_categories(inspector: sa.Inspector | None) -> None:
    if inspector is not None and (
        not inspector.has_table("accounts") or not inspector.has_table("expense_categories")
    ):
        return

    for code, name in _DEFAULT_EXPENSE_CATEGORIES:
        op.execute(
            "INSERT INTO accounts (code, name, type, balance) "
            f"VALUES ({_sql_literal(code)}, {_sql_literal(name)}, 'expense', 0) "
            "ON CONFLICT (code) DO NOTHING"
        )
        op.execute(
            "INSERT INTO expense_categories (name, account_code, is_active) "
            f"VALUES ({_sql_literal(name)}, {_sql_literal(code)}, '1') "
            "ON CONFLICT (name) DO NOTHING"
        )


def _seed_default_admin(inspector: sa.Inspector | None) -> None:
    if inspector is not None and not inspector.has_table("users"):
        return

    op.execute(
        "INSERT INTO users (name, email, password, role, is_active) "
        f"VALUES ({_sql_literal(settings.DEFAULT_ADMIN_NAME)}, "
        f"{_sql_literal(settings.DEFAULT_ADMIN_EMAIL)}, "
        f"{_sql_literal(bcrypt.hashpw(settings.ADMIN_PASSWORD.encode('utf-8'), bcrypt.gensalt()).decode('utf-8'))}, "
        "'admin', TRUE) "
        "ON CONFLICT (email) DO NOTHING"
    )


def upgrade() -> None:
    connection = op.get_bind()
    if context.is_offline_mode():
        op.execute("UPDATE b2b_clients SET discount_pct = 0 WHERE discount_pct IS NULL")
        _seed_default_expense_categories(None)
        _seed_default_admin(None)
        return

    inspector = sa.inspect(connection)

    for patch in _SCHEMA_PATCHES:
        table_name = patch["table"]
        if not inspector.has_table(table_name):
            continue
        if _column_exists(inspector, table_name, patch["column"]):
            continue
        op.add_column(
            table_name,
            sa.Column(patch["column"], patch["type"], nullable=patch["nullable"]),
        )

    inspector = sa.inspect(connection)
    for patch in _SCHEMA_PATCHES:
        foreign_key = patch.get("foreign_key")
        if not foreign_key or not inspector.has_table(patch["table"]):
            continue
        name, constrained_columns, referred_table, referred_columns = foreign_key
        if not _column_exists(inspector, patch["table"], patch["column"]):
            continue
        if _foreign_key_exists(
            inspector,
            patch["table"],
            constrained_columns,
            referred_table,
            referred_columns,
        ):
            continue
        op.create_foreign_key(
            name,
            patch["table"],
            referred_table,
            constrained_columns,
            referred_columns,
        )

    inspector = sa.inspect(connection)
    if inspector.has_table("b2b_clients") and _column_exists(inspector, "b2b_clients", "discount_pct"):
        connection.execute(sa.text("UPDATE b2b_clients SET discount_pct = 0 WHERE discount_pct IS NULL"))

    _seed_default_expense_categories(inspector)
    _seed_default_admin(inspector)


def downgrade() -> None:
    # 20260412_0001 now defines the aligned schema. Keep baseline seeded data on downgrade.
    pass
