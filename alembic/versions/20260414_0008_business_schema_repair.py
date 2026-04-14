"""repair missing business schema after incorrect production stamping

Revision ID: 20260414_0008
Revises: 20260414_0007
Create Date: 2026-04-14 20:10:00
"""

from alembic import context, op
import sqlalchemy as sa


revision = "20260414_0008"
down_revision = "20260414_0007"
branch_labels = None
depends_on = None


def _columns(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


def _index_exists(inspector: sa.Inspector, table_name: str, index_name: str) -> bool:
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


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


def _create_index_if_missing(
    inspector: sa.Inspector,
    index_name: str,
    table_name: str,
    columns: list[str],
    *,
    unique: bool = False,
) -> None:
    if inspector.has_table(table_name) and not _index_exists(inspector, table_name, index_name):
        op.create_index(index_name, table_name, columns, unique=unique)


def _create_foreign_key_if_missing(
    inspector: sa.Inspector,
    name: str,
    table_name: str,
    referred_table: str,
    constrained_columns: list[str],
    referred_columns: list[str],
) -> None:
    if not inspector.has_table(table_name) or not inspector.has_table(referred_table):
        return
    if _foreign_key_exists(inspector, table_name, constrained_columns, referred_table, referred_columns):
        return
    op.create_foreign_key(name, table_name, referred_table, constrained_columns, referred_columns)


def _ensure_core_business_tables(inspector: sa.Inspector) -> None:
    if not inspector.has_table("accounts"):
        op.create_table(
            "accounts",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("code", sa.String(length=20), nullable=False),
            sa.Column("name", sa.String(length=150), nullable=False),
            sa.Column("type", sa.String(length=30), nullable=False),
            sa.Column("balance", sa.Numeric(precision=14, scale=2)),
            sa.Column("parent_id", sa.Integer(), sa.ForeignKey("accounts.id")),
            sa.UniqueConstraint("code"),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_accounts_id", "accounts", ["id"])

    if not inspector.has_table("b2b_clients"):
        op.create_table(
            "b2b_clients",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("contact_person", sa.String(length=150)),
            sa.Column("phone", sa.String(length=50)),
            sa.Column("email", sa.String(length=150)),
            sa.Column("address", sa.String(length=300)),
            sa.Column("payment_terms", sa.String(length=50)),
            sa.Column("discount_pct", sa.Numeric(precision=6, scale=2)),
            sa.Column("credit_limit", sa.Numeric(precision=14, scale=2)),
            sa.Column("outstanding", sa.Numeric(precision=14, scale=2)),
            sa.Column("notes", sa.Text()),
            sa.Column("is_active", sa.Boolean()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_b2b_clients_id", "b2b_clients", ["id"])

    if not inspector.has_table("customers"):
        op.create_table(
            "customers",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=150), nullable=False),
            sa.Column("phone", sa.String(length=30)),
            sa.Column("email", sa.String(length=150)),
            sa.Column("address", sa.Text()),
            sa.Column("balance", sa.Numeric(precision=12, scale=2)),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_customers_id", "customers", ["id"])
    _create_index_if_missing(inspector, "ix_customers_name", "customers", ["name"])

    if not inspector.has_table("employees"):
        op.create_table(
            "employees",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=150), nullable=False),
            sa.Column("phone", sa.String(length=30)),
            sa.Column("position", sa.String(length=100)),
            sa.Column("department", sa.String(length=100)),
            sa.Column("hire_date", sa.Date()),
            sa.Column("base_salary", sa.Numeric(precision=12, scale=2)),
            sa.Column("is_active", sa.Boolean()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_employees_id", "employees", ["id"])

    if not inspector.has_table("expense_categories"):
        op.create_table(
            "expense_categories",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("account_code", sa.String(length=20), nullable=False),
            sa.Column("description", sa.String(length=255)),
            sa.Column("is_active", sa.String(length=1)),
            sa.UniqueConstraint("name"),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_expense_categories_id", "expense_categories", ["id"])

    if not inspector.has_table("farms"):
        op.create_table(
            "farms",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=150), nullable=False),
            sa.Column("location", sa.String(length=200)),
            sa.Column("notes", sa.Text()),
            sa.Column("is_active", sa.Integer()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.UniqueConstraint("name"),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_farms_id", "farms", ["id"])

    if not inspector.has_table("products"):
        op.create_table(
            "products",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("sku", sa.String(length=80), nullable=False),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=False),
            sa.Column("cost", sa.Numeric(precision=12, scale=2)),
            sa.Column("stock", sa.Numeric(precision=12, scale=3)),
            sa.Column("min_stock", sa.Numeric(precision=12, scale=3)),
            sa.Column("unit", sa.String(length=30)),
            sa.Column("is_active", sa.Boolean()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.Column("category", sa.String(length=100)),
            sa.Column("item_type", sa.String(length=20)),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_products_id", "products", ["id"])
    _create_index_if_missing(inspector, "ix_products_name", "products", ["name"])
    _create_index_if_missing(inspector, "ix_products_sku", "products", ["sku"], unique=True)

    if not inspector.has_table("recipes"):
        op.create_table(
            "recipes",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("description", sa.Text()),
            sa.Column("is_active", sa.Boolean()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_recipes_id", "recipes", ["id"])

    if not inspector.has_table("suppliers"):
        op.create_table(
            "suppliers",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=150), nullable=False),
            sa.Column("phone", sa.String(length=30)),
            sa.Column("email", sa.String(length=150)),
            sa.Column("address", sa.Text()),
            sa.Column("balance", sa.Numeric(precision=12, scale=2)),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_suppliers_id", "suppliers", ["id"])
    _create_index_if_missing(inspector, "ix_suppliers_name", "suppliers", ["name"])


def _ensure_relational_business_tables(inspector: sa.Inspector) -> None:
    if not inspector.has_table("attendance"):
        op.create_table(
            "attendance",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
            sa.Column("date", sa.Date(), nullable=False),
            sa.Column("status", sa.String(length=20)),
            sa.Column("note", sa.Text()),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_attendance_id", "attendance", ["id"])

    if not inspector.has_table("b2b_client_prices"):
        op.create_table(
            "b2b_client_prices",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("client_id", sa.Integer(), sa.ForeignKey("b2b_clients.id"), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
            sa.Column("price", sa.Numeric(precision=14, scale=2), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("client_id", "product_id", name="uq_client_product_price"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_b2b_client_prices_id", "b2b_client_prices", ["id"])

    if not inspector.has_table("b2b_invoices"):
        op.create_table(
            "b2b_invoices",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("invoice_number", sa.String(length=30)),
            sa.Column("client_id", sa.Integer(), sa.ForeignKey("b2b_clients.id"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id")),
            sa.Column("invoice_type", sa.String(length=20), nullable=False),
            sa.Column("status", sa.String(length=20)),
            sa.Column("payment_method", sa.String(length=30)),
            sa.Column("subtotal", sa.Numeric(precision=14, scale=2)),
            sa.Column("discount", sa.Numeric(precision=14, scale=2)),
            sa.Column("total", sa.Numeric(precision=14, scale=2)),
            sa.Column("amount_paid", sa.Numeric(precision=14, scale=2)),
            sa.Column("due_date", sa.Date()),
            sa.Column("notes", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_b2b_invoices_id", "b2b_invoices", ["id"])
    _create_index_if_missing(
        inspector, "ix_b2b_invoices_invoice_number", "b2b_invoices", ["invoice_number"], unique=True
    )

    if not inspector.has_table("b2b_refunds"):
        op.create_table(
            "b2b_refunds",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("refund_number", sa.String(length=30)),
            sa.Column("client_id", sa.Integer(), sa.ForeignKey("b2b_clients.id"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id")),
            sa.Column("subtotal", sa.Numeric(precision=14, scale=2)),
            sa.Column("discount", sa.Numeric(precision=14, scale=2)),
            sa.Column("total", sa.Numeric(precision=14, scale=2)),
            sa.Column("notes", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_b2b_refunds_id", "b2b_refunds", ["id"])
    _create_index_if_missing(
        inspector, "ix_b2b_refunds_refund_number", "b2b_refunds", ["refund_number"], unique=True
    )

    if not inspector.has_table("farm_deliveries"):
        op.create_table(
            "farm_deliveries",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("delivery_number", sa.String(length=30)),
            sa.Column("farm_id", sa.Integer(), sa.ForeignKey("farms.id"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id")),
            sa.Column("delivery_date", sa.Date(), nullable=False),
            sa.Column("received_by", sa.String(length=150)),
            sa.Column("quality_notes", sa.Text()),
            sa.Column("notes", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(
        inspector, "ix_farm_deliveries_delivery_number", "farm_deliveries", ["delivery_number"], unique=True
    )
    _create_index_if_missing(inspector, "ix_farm_deliveries_id", "farm_deliveries", ["id"])

    if not inspector.has_table("invoices"):
        op.create_table(
            "invoices",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("invoice_number", sa.String(length=30)),
            sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id")),
            sa.Column("status", sa.String(length=20)),
            sa.Column("payment_method", sa.String(length=30)),
            sa.Column("subtotal", sa.Numeric(precision=12, scale=2)),
            sa.Column("discount", sa.Numeric(precision=12, scale=2)),
            sa.Column("total", sa.Numeric(precision=12, scale=2)),
            sa.Column("notes", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_invoices_id", "invoices", ["id"])
    _create_index_if_missing(inspector, "ix_invoices_invoice_number", "invoices", ["invoice_number"], unique=True)

    if not inspector.has_table("journals"):
        op.create_table(
            "journals",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("ref_type", sa.String(length=30)),
            sa.Column("ref_id", sa.Integer()),
            sa.Column("description", sa.Text()),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_journals_id", "journals", ["id"])

    if not inspector.has_table("payroll"):
        op.create_table(
            "payroll",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
            sa.Column("period", sa.String(length=7), nullable=False),
            sa.Column("base_salary", sa.Numeric(precision=12, scale=2)),
            sa.Column("bonuses", sa.Numeric(precision=12, scale=2)),
            sa.Column("deductions", sa.Numeric(precision=12, scale=2)),
            sa.Column("net_salary", sa.Numeric(precision=12, scale=2)),
            sa.Column("paid", sa.Boolean()),
            sa.Column("days_worked", sa.Integer()),
            sa.Column("working_days", sa.Integer()),
            sa.Column("paid_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_payroll_id", "payroll", ["id"])

    if not inspector.has_table("production_batches"):
        op.create_table(
            "production_batches",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("batch_number", sa.String(length=30)),
            sa.Column("recipe_id", sa.Integer(), sa.ForeignKey("recipes.id")),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id")),
            sa.Column("status", sa.String(length=20)),
            sa.Column("waste_pct", sa.Numeric(precision=5, scale=2)),
            sa.Column("notes", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(
        inspector, "ix_production_batches_batch_number", "production_batches", ["batch_number"], unique=True
    )
    _create_index_if_missing(inspector, "ix_production_batches_id", "production_batches", ["id"])

    if not inspector.has_table("purchases"):
        op.create_table(
            "purchases",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("purchase_number", sa.String(length=30)),
            sa.Column("supplier_id", sa.Integer(), sa.ForeignKey("suppliers.id"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id")),
            sa.Column("status", sa.String(length=20)),
            sa.Column("subtotal", sa.Numeric(precision=12, scale=2)),
            sa.Column("discount", sa.Numeric(precision=12, scale=2)),
            sa.Column("total", sa.Numeric(precision=12, scale=2)),
            sa.Column("notes", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_purchases_id", "purchases", ["id"])
    _create_index_if_missing(
        inspector, "ix_purchases_purchase_number", "purchases", ["purchase_number"], unique=True
    )

    if not inspector.has_table("recipe_inputs"):
        op.create_table(
            "recipe_inputs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("recipe_id", sa.Integer(), sa.ForeignKey("recipes.id"), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
            sa.Column("qty", sa.Numeric(precision=12, scale=3), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_recipe_inputs_id", "recipe_inputs", ["id"])

    if not inspector.has_table("recipe_outputs"):
        op.create_table(
            "recipe_outputs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("recipe_id", sa.Integer(), sa.ForeignKey("recipes.id"), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
            sa.Column("qty", sa.Numeric(precision=12, scale=3), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_recipe_outputs_id", "recipe_outputs", ["id"])

    if not inspector.has_table("spoilage_records"):
        op.create_table(
            "spoilage_records",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("ref_number", sa.String(length=30)),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id")),
            sa.Column("qty", sa.Numeric(precision=12, scale=3), nullable=False),
            sa.Column("spoilage_date", sa.Date(), nullable=False),
            sa.Column("reason", sa.String(length=100)),
            sa.Column("farm_id", sa.Integer(), sa.ForeignKey("farms.id")),
            sa.Column("notes", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_spoilage_records_id", "spoilage_records", ["id"])
    _create_index_if_missing(
        inspector, "ix_spoilage_records_ref_number", "spoilage_records", ["ref_number"], unique=True
    )

    if not inspector.has_table("stock_moves"):
        op.create_table(
            "stock_moves",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
            sa.Column("type", sa.String(length=20), nullable=False),
            sa.Column("qty", sa.Numeric(precision=12, scale=3), nullable=False),
            sa.Column("qty_before", sa.Numeric(precision=12, scale=3)),
            sa.Column("qty_after", sa.Numeric(precision=12, scale=3)),
            sa.Column("ref_type", sa.String(length=30)),
            sa.Column("ref_id", sa.Integer()),
            sa.Column("note", sa.Text()),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_stock_moves_id", "stock_moves", ["id"])

    if not inspector.has_table("weather_logs"):
        op.create_table(
            "weather_logs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("farm_id", sa.Integer(), sa.ForeignKey("farms.id"), nullable=False),
            sa.Column("log_date", sa.Date(), nullable=False),
            sa.Column("temp_min", sa.Numeric(precision=5, scale=1)),
            sa.Column("temp_max", sa.Numeric(precision=5, scale=1)),
            sa.Column("rainfall_mm", sa.Numeric(precision=7, scale=2)),
            sa.Column("humidity_pct", sa.Numeric(precision=5, scale=1)),
            sa.Column("notes", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_weather_logs_id", "weather_logs", ["id"])


def _ensure_transaction_tables(inspector: sa.Inspector) -> None:
    if not inspector.has_table("b2b_invoice_items"):
        op.create_table(
            "b2b_invoice_items",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("invoice_id", sa.Integer(), sa.ForeignKey("b2b_invoices.id"), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
            sa.Column("qty", sa.Numeric(precision=12, scale=3), nullable=False),
            sa.Column("unit_price", sa.Numeric(precision=14, scale=2), nullable=False),
            sa.Column("total", sa.Numeric(precision=14, scale=2), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_b2b_invoice_items_id", "b2b_invoice_items", ["id"])

    if not inspector.has_table("b2b_refund_items"):
        op.create_table(
            "b2b_refund_items",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("refund_id", sa.Integer(), sa.ForeignKey("b2b_refunds.id"), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
            sa.Column("qty", sa.Numeric(precision=12, scale=3), nullable=False),
            sa.Column("unit_price", sa.Numeric(precision=14, scale=2), nullable=False),
            sa.Column("total", sa.Numeric(precision=14, scale=2), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_b2b_refund_items_id", "b2b_refund_items", ["id"])

    if not inspector.has_table("batch_inputs"):
        op.create_table(
            "batch_inputs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("batch_id", sa.Integer(), sa.ForeignKey("production_batches.id"), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
            sa.Column("qty", sa.Numeric(precision=12, scale=3), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_batch_inputs_id", "batch_inputs", ["id"])

    if not inspector.has_table("batch_outputs"):
        op.create_table(
            "batch_outputs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("batch_id", sa.Integer(), sa.ForeignKey("production_batches.id"), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
            sa.Column("qty", sa.Numeric(precision=12, scale=3), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_batch_outputs_id", "batch_outputs", ["id"])

    if not inspector.has_table("consignments"):
        op.create_table(
            "consignments",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("ref_number", sa.String(length=30)),
            sa.Column("client_id", sa.Integer(), sa.ForeignKey("b2b_clients.id"), nullable=False),
            sa.Column("invoice_id", sa.Integer(), sa.ForeignKey("b2b_invoices.id")),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id")),
            sa.Column("status", sa.String(length=20)),
            sa.Column("notes", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.Column("settled_at", sa.DateTime(timezone=True)),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_consignments_id", "consignments", ["id"])
    _create_index_if_missing(inspector, "ix_consignments_ref_number", "consignments", ["ref_number"], unique=True)

    if not inspector.has_table("expenses"):
        op.create_table(
            "expenses",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("ref_number", sa.String(length=30)),
            sa.Column("category_id", sa.Integer(), sa.ForeignKey("expense_categories.id"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id")),
            sa.Column("expense_date", sa.Date(), nullable=False),
            sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
            sa.Column("payment_method", sa.String(length=20)),
            sa.Column("vendor", sa.String(length=150)),
            sa.Column("description", sa.Text()),
            sa.Column("journal_id", sa.Integer(), sa.ForeignKey("journals.id")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.Column("farm_id", sa.Integer(), sa.ForeignKey("farms.id")),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_expenses_id", "expenses", ["id"])
    _create_index_if_missing(inspector, "ix_expenses_ref_number", "expenses", ["ref_number"], unique=True)

    if not inspector.has_table("farm_delivery_items"):
        op.create_table(
            "farm_delivery_items",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("delivery_id", sa.Integer(), sa.ForeignKey("farm_deliveries.id"), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
            sa.Column("qty", sa.Numeric(precision=12, scale=3), nullable=False),
            sa.Column("unit", sa.String(length=30)),
            sa.Column("notes", sa.String(length=255)),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_farm_delivery_items_id", "farm_delivery_items", ["id"])

    if not inspector.has_table("invoice_items"):
        op.create_table(
            "invoice_items",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("invoice_id", sa.Integer(), sa.ForeignKey("invoices.id"), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
            sa.Column("sku", sa.String(length=80)),
            sa.Column("name", sa.String(length=200)),
            sa.Column("qty", sa.Numeric(precision=12, scale=3), nullable=False),
            sa.Column("unit_price", sa.Numeric(precision=12, scale=2), nullable=False),
            sa.Column("total", sa.Numeric(precision=12, scale=2), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_invoice_items_id", "invoice_items", ["id"])

    if not inspector.has_table("journal_entries"):
        op.create_table(
            "journal_entries",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("journal_id", sa.Integer(), sa.ForeignKey("journals.id"), nullable=False),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
            sa.Column("debit", sa.Numeric(precision=14, scale=2)),
            sa.Column("credit", sa.Numeric(precision=14, scale=2)),
            sa.Column("note", sa.String(length=255)),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_journal_entries_id", "journal_entries", ["id"])

    if not inspector.has_table("purchase_items"):
        op.create_table(
            "purchase_items",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("purchase_id", sa.Integer(), sa.ForeignKey("purchases.id"), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
            sa.Column("qty", sa.Numeric(precision=12, scale=3), nullable=False),
            sa.Column("unit_cost", sa.Numeric(precision=12, scale=2), nullable=False),
            sa.Column("total", sa.Numeric(precision=12, scale=2), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_purchase_items_id", "purchase_items", ["id"])

    if not inspector.has_table("retail_refunds"):
        op.create_table(
            "retail_refunds",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("refund_number", sa.String(length=30)),
            sa.Column("invoice_id", sa.Integer(), sa.ForeignKey("invoices.id")),
            sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id")),
            sa.Column("reason", sa.String(length=200)),
            sa.Column("refund_method", sa.String(length=30)),
            sa.Column("total", sa.Numeric(precision=12, scale=2)),
            sa.Column("notes", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_retail_refunds_id", "retail_refunds", ["id"])
    _create_index_if_missing(
        inspector, "ix_retail_refunds_refund_number", "retail_refunds", ["refund_number"], unique=True
    )

    if not inspector.has_table("consignment_items"):
        op.create_table(
            "consignment_items",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("consignment_id", sa.Integer(), sa.ForeignKey("consignments.id"), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
            sa.Column("qty_sent", sa.Numeric(precision=12, scale=3)),
            sa.Column("qty_sold", sa.Numeric(precision=12, scale=3)),
            sa.Column("qty_returned", sa.Numeric(precision=12, scale=3)),
            sa.Column("unit_price", sa.Numeric(precision=14, scale=2)),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_consignment_items_id", "consignment_items", ["id"])

    if not inspector.has_table("retail_refund_items"):
        op.create_table(
            "retail_refund_items",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("refund_id", sa.Integer(), sa.ForeignKey("retail_refunds.id"), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
            sa.Column("qty", sa.Numeric(precision=12, scale=3), nullable=False),
            sa.Column("unit_price", sa.Numeric(precision=12, scale=2), nullable=False),
            sa.Column("total", sa.Numeric(precision=12, scale=2), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_retail_refund_items_id", "retail_refund_items", ["id"])


def _ensure_followup_schema(inspector: sa.Inspector) -> None:
    if inspector.has_table("customers") and "discount_pct" not in _columns(inspector, "customers"):
        op.add_column("customers", sa.Column("discount_pct", sa.Numeric(precision=6, scale=2), nullable=True))
        op.execute("UPDATE customers SET discount_pct = 0 WHERE discount_pct IS NULL")

    if inspector.has_table("products"):
        product_columns = _columns(inspector, "products")
        if "reorder_level" not in product_columns:
            op.add_column("products", sa.Column("reorder_level", sa.Numeric(precision=12, scale=3), nullable=True))
        if "reorder_qty" not in product_columns:
            op.add_column("products", sa.Column("reorder_qty", sa.Numeric(precision=12, scale=3), nullable=True))
        if "preferred_supplier_id" not in product_columns:
            op.add_column("products", sa.Column("preferred_supplier_id", sa.Integer(), nullable=True))

    inspector = sa.inspect(op.get_bind())
    _create_foreign_key_if_missing(
        inspector,
        "fk_products_preferred_supplier_id_suppliers",
        "products",
        "suppliers",
        ["preferred_supplier_id"],
        ["id"],
    )

    if not inspector.has_table("stock_locations"):
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
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_stock_locations_id", "stock_locations", ["id"])
    _create_index_if_missing(inspector, "ix_stock_locations_name", "stock_locations", ["name"])
    _create_index_if_missing(inspector, "ix_stock_locations_code", "stock_locations", ["code"])

    if not inspector.has_table("location_stocks"):
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
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_location_stocks_id", "location_stocks", ["id"])
    _create_index_if_missing(inspector, "ix_location_stocks_location_id", "location_stocks", ["location_id"])
    _create_index_if_missing(inspector, "ix_location_stocks_product_id", "location_stocks", ["product_id"])

    if not inspector.has_table("stock_transfers"):
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
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_stock_transfers_id", "stock_transfers", ["id"])
    _create_index_if_missing(inspector, "ix_stock_transfers_product_id", "stock_transfers", ["product_id"])
    _create_index_if_missing(
        inspector, "ix_stock_transfers_source_location_id", "stock_transfers", ["source_location_id"]
    )
    _create_index_if_missing(
        inspector,
        "ix_stock_transfers_destination_location_id",
        "stock_transfers",
        ["destination_location_id"],
    )

    if not inspector.has_table("product_receipts"):
        op.create_table(
            "product_receipts",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("ref_number", sa.String(length=30), nullable=False),
            sa.Column("product_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("receive_date", sa.Date(), nullable=False),
            sa.Column("qty", sa.Numeric(precision=12, scale=3), nullable=False),
            sa.Column("unit_cost", sa.Numeric(precision=12, scale=2), nullable=True),
            sa.Column("total_cost", sa.Numeric(precision=12, scale=2), nullable=True),
            sa.Column("supplier_ref", sa.String(length=150), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("expense_id", sa.Integer(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=True,
            ),
            sa.ForeignKeyConstraint(["expense_id"], ["expenses.id"]),
            sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("ref_number"),
        )
    inspector = sa.inspect(op.get_bind())
    _create_index_if_missing(inspector, "ix_product_receipts_id", "product_receipts", ["id"])
    _create_index_if_missing(inspector, "ix_product_receipts_ref_number", "product_receipts", ["ref_number"])
    _create_index_if_missing(inspector, "ix_product_receipts_product_id", "product_receipts", ["product_id"])


def upgrade() -> None:
    if context.is_offline_mode():
        return

    inspector = sa.inspect(op.get_bind())
    _ensure_core_business_tables(inspector)

    inspector = sa.inspect(op.get_bind())
    _ensure_relational_business_tables(inspector)

    inspector = sa.inspect(op.get_bind())
    _ensure_transaction_tables(inspector)

    inspector = sa.inspect(op.get_bind())
    _ensure_followup_schema(inspector)


def downgrade() -> None:
    # This repair migration is intentionally non-destructive.
    pass
