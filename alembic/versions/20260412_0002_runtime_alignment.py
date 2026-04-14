"""align legacy startup mutations

Revision ID: 20260412_0002
Revises: 20260412_0001
Create Date: 2026-04-12 19:10:00
"""

from alembic import context, op
import sqlalchemy as sa


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
    {
        "table": "customers",
        "column": "discount_pct",
        "type": sa.Numeric(precision=6, scale=2),
        "nullable": True,
    },
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


def upgrade() -> None:
    connection = op.get_bind()
    if context.is_offline_mode():
        op.execute("UPDATE b2b_clients SET discount_pct = 0 WHERE discount_pct IS NULL")
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
    if inspector.has_table("customers") and _column_exists(inspector, "customers", "discount_pct"):
        connection.execute(sa.text("UPDATE customers SET discount_pct = 0 WHERE discount_pct IS NULL"))


def downgrade() -> None:
    # 20260412_0001 now defines the aligned schema.
    pass
