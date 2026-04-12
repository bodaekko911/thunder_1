"""initial schema

Revision ID: 20260412_0001
Revises:
Create Date: 2026-04-12 14:30:00
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260412_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
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
    op.create_index("ix_accounts_id", "accounts", ["id"], unique=False)

    op.create_table(
        "activity_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer()),
        sa.Column("user_name", sa.String(length=150)),
        sa.Column("user_role", sa.String(length=50)),
        sa.Column("action", sa.String(length=100)),
        sa.Column("module", sa.String(length=50)),
        sa.Column("description", sa.Text()),
        sa.Column("ref_type", sa.String(length=50)),
        sa.Column("ref_id", sa.String(length=50)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_activity_logs_id", "activity_logs", ["id"], unique=False)

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
    op.create_index("ix_b2b_clients_id", "b2b_clients", ["id"], unique=False)

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
    op.create_index("ix_customers_id", "customers", ["id"], unique=False)
    op.create_index("ix_customers_name", "customers", ["name"], unique=False)

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
    op.create_index("ix_employees_id", "employees", ["id"], unique=False)

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
    op.create_index("ix_expense_categories_id", "expense_categories", ["id"], unique=False)

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
    op.create_index("ix_farms_id", "farms", ["id"], unique=False)

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
    op.create_index("ix_products_id", "products", ["id"], unique=False)
    op.create_index("ix_products_name", "products", ["name"], unique=False)
    op.create_index("ix_products_sku", "products", ["sku"], unique=True)

    op.create_table(
        "recipes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("is_active", sa.Boolean()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_recipes_id", "recipes", ["id"], unique=False)

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
    op.create_index("ix_suppliers_id", "suppliers", ["id"], unique=False)
    op.create_index("ix_suppliers_name", "suppliers", ["name"], unique=False)

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("email", sa.String(length=150), nullable=False),
        sa.Column("password", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=30)),
        sa.Column("is_active", sa.Boolean()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("permissions", sa.Text()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_id", "users", ["id"], unique=False)

    op.create_table(
        "attendance",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20)),
        sa.Column("note", sa.Text()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_attendance_id", "attendance", ["id"], unique=False)

    op.create_table(
        "b2b_client_prices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("b2b_clients.id"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("price", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id", "product_id", name="uq_client_product_price"),
    )
    op.create_index("ix_b2b_client_prices_id", "b2b_client_prices", ["id"], unique=False)

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
    op.create_index("ix_b2b_invoices_id", "b2b_invoices", ["id"], unique=False)
    op.create_index("ix_b2b_invoices_invoice_number", "b2b_invoices", ["invoice_number"], unique=True)

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
    op.create_index("ix_b2b_refunds_id", "b2b_refunds", ["id"], unique=False)
    op.create_index("ix_b2b_refunds_refund_number", "b2b_refunds", ["refund_number"], unique=True)

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
    op.create_index("ix_farm_deliveries_delivery_number", "farm_deliveries", ["delivery_number"], unique=True)
    op.create_index("ix_farm_deliveries_id", "farm_deliveries", ["id"], unique=False)

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
    op.create_index("ix_invoices_id", "invoices", ["id"], unique=False)
    op.create_index("ix_invoices_invoice_number", "invoices", ["invoice_number"], unique=True)

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
    op.create_index("ix_journals_id", "journals", ["id"], unique=False)

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
    op.create_index("ix_payroll_id", "payroll", ["id"], unique=False)

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
    op.create_index("ix_production_batches_batch_number", "production_batches", ["batch_number"], unique=True)
    op.create_index("ix_production_batches_id", "production_batches", ["id"], unique=False)

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
    op.create_index("ix_purchases_id", "purchases", ["id"], unique=False)
    op.create_index("ix_purchases_purchase_number", "purchases", ["purchase_number"], unique=True)

    op.create_table(
        "recipe_inputs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("recipe_id", sa.Integer(), sa.ForeignKey("recipes.id"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("qty", sa.Numeric(precision=12, scale=3), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_recipe_inputs_id", "recipe_inputs", ["id"], unique=False)

    op.create_table(
        "recipe_outputs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("recipe_id", sa.Integer(), sa.ForeignKey("recipes.id"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("qty", sa.Numeric(precision=12, scale=3), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_recipe_outputs_id", "recipe_outputs", ["id"], unique=False)

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_refresh_tokens_id", "refresh_tokens", ["id"], unique=False)
    op.create_index("ix_refresh_tokens_token_hash", "refresh_tokens", ["token_hash"], unique=True)

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
    op.create_index("ix_spoilage_records_id", "spoilage_records", ["id"], unique=False)
    op.create_index("ix_spoilage_records_ref_number", "spoilage_records", ["ref_number"], unique=True)

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
    op.create_index("ix_stock_moves_id", "stock_moves", ["id"], unique=False)

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
    op.create_index("ix_weather_logs_id", "weather_logs", ["id"], unique=False)

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
    op.create_index("ix_b2b_invoice_items_id", "b2b_invoice_items", ["id"], unique=False)

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
    op.create_index("ix_b2b_refund_items_id", "b2b_refund_items", ["id"], unique=False)

    op.create_table(
        "batch_inputs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.Integer(), sa.ForeignKey("production_batches.id"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("qty", sa.Numeric(precision=12, scale=3), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_batch_inputs_id", "batch_inputs", ["id"], unique=False)

    op.create_table(
        "batch_outputs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.Integer(), sa.ForeignKey("production_batches.id"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("qty", sa.Numeric(precision=12, scale=3), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_batch_outputs_id", "batch_outputs", ["id"], unique=False)

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
    op.create_index("ix_consignments_id", "consignments", ["id"], unique=False)
    op.create_index("ix_consignments_ref_number", "consignments", ["ref_number"], unique=True)

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
    op.create_index("ix_expenses_id", "expenses", ["id"], unique=False)
    op.create_index("ix_expenses_ref_number", "expenses", ["ref_number"], unique=True)

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
    op.create_index("ix_farm_delivery_items_id", "farm_delivery_items", ["id"], unique=False)

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
    op.create_index("ix_invoice_items_id", "invoice_items", ["id"], unique=False)

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
    op.create_index("ix_journal_entries_id", "journal_entries", ["id"], unique=False)

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
    op.create_index("ix_purchase_items_id", "purchase_items", ["id"], unique=False)

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
    op.create_index("ix_retail_refunds_id", "retail_refunds", ["id"], unique=False)
    op.create_index("ix_retail_refunds_refund_number", "retail_refunds", ["refund_number"], unique=True)

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
    op.create_index("ix_consignment_items_id", "consignment_items", ["id"], unique=False)

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
    op.create_index("ix_retail_refund_items_id", "retail_refund_items", ["id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_retail_refund_items_id", table_name="retail_refund_items")
    op.drop_table("retail_refund_items")
    op.drop_index("ix_consignment_items_id", table_name="consignment_items")
    op.drop_table("consignment_items")
    op.drop_index("ix_retail_refunds_refund_number", table_name="retail_refunds")
    op.drop_index("ix_retail_refunds_id", table_name="retail_refunds")
    op.drop_table("retail_refunds")
    op.drop_index("ix_purchase_items_id", table_name="purchase_items")
    op.drop_table("purchase_items")
    op.drop_index("ix_journal_entries_id", table_name="journal_entries")
    op.drop_table("journal_entries")
    op.drop_index("ix_invoice_items_id", table_name="invoice_items")
    op.drop_table("invoice_items")
    op.drop_index("ix_farm_delivery_items_id", table_name="farm_delivery_items")
    op.drop_table("farm_delivery_items")
    op.drop_index("ix_expenses_ref_number", table_name="expenses")
    op.drop_index("ix_expenses_id", table_name="expenses")
    op.drop_table("expenses")
    op.drop_index("ix_consignments_ref_number", table_name="consignments")
    op.drop_index("ix_consignments_id", table_name="consignments")
    op.drop_table("consignments")
    op.drop_index("ix_batch_outputs_id", table_name="batch_outputs")
    op.drop_table("batch_outputs")
    op.drop_index("ix_batch_inputs_id", table_name="batch_inputs")
    op.drop_table("batch_inputs")
    op.drop_index("ix_b2b_refund_items_id", table_name="b2b_refund_items")
    op.drop_table("b2b_refund_items")
    op.drop_index("ix_b2b_invoice_items_id", table_name="b2b_invoice_items")
    op.drop_table("b2b_invoice_items")
    op.drop_index("ix_weather_logs_id", table_name="weather_logs")
    op.drop_table("weather_logs")
    op.drop_index("ix_stock_moves_id", table_name="stock_moves")
    op.drop_table("stock_moves")
    op.drop_index("ix_spoilage_records_ref_number", table_name="spoilage_records")
    op.drop_index("ix_spoilage_records_id", table_name="spoilage_records")
    op.drop_table("spoilage_records")
    op.drop_index("ix_refresh_tokens_token_hash", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_index("ix_recipe_outputs_id", table_name="recipe_outputs")
    op.drop_table("recipe_outputs")
    op.drop_index("ix_recipe_inputs_id", table_name="recipe_inputs")
    op.drop_table("recipe_inputs")
    op.drop_index("ix_purchases_purchase_number", table_name="purchases")
    op.drop_index("ix_purchases_id", table_name="purchases")
    op.drop_table("purchases")
    op.drop_index("ix_production_batches_id", table_name="production_batches")
    op.drop_index("ix_production_batches_batch_number", table_name="production_batches")
    op.drop_table("production_batches")
    op.drop_index("ix_payroll_id", table_name="payroll")
    op.drop_table("payroll")
    op.drop_index("ix_journals_id", table_name="journals")
    op.drop_table("journals")
    op.drop_index("ix_invoices_invoice_number", table_name="invoices")
    op.drop_index("ix_invoices_id", table_name="invoices")
    op.drop_table("invoices")
    op.drop_index("ix_farm_deliveries_id", table_name="farm_deliveries")
    op.drop_index("ix_farm_deliveries_delivery_number", table_name="farm_deliveries")
    op.drop_table("farm_deliveries")
    op.drop_index("ix_b2b_refunds_refund_number", table_name="b2b_refunds")
    op.drop_index("ix_b2b_refunds_id", table_name="b2b_refunds")
    op.drop_table("b2b_refunds")
    op.drop_index("ix_b2b_invoices_invoice_number", table_name="b2b_invoices")
    op.drop_index("ix_b2b_invoices_id", table_name="b2b_invoices")
    op.drop_table("b2b_invoices")
    op.drop_index("ix_b2b_client_prices_id", table_name="b2b_client_prices")
    op.drop_table("b2b_client_prices")
    op.drop_index("ix_attendance_id", table_name="attendance")
    op.drop_table("attendance")
    op.drop_index("ix_users_id", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
    op.drop_index("ix_suppliers_name", table_name="suppliers")
    op.drop_index("ix_suppliers_id", table_name="suppliers")
    op.drop_table("suppliers")
    op.drop_index("ix_recipes_id", table_name="recipes")
    op.drop_table("recipes")
    op.drop_index("ix_products_sku", table_name="products")
    op.drop_index("ix_products_name", table_name="products")
    op.drop_index("ix_products_id", table_name="products")
    op.drop_table("products")
    op.drop_index("ix_farms_id", table_name="farms")
    op.drop_table("farms")
    op.drop_index("ix_expense_categories_id", table_name="expense_categories")
    op.drop_table("expense_categories")
    op.drop_index("ix_employees_id", table_name="employees")
    op.drop_table("employees")
    op.drop_index("ix_customers_name", table_name="customers")
    op.drop_index("ix_customers_id", table_name="customers")
    op.drop_table("customers")
    op.drop_index("ix_b2b_clients_id", table_name="b2b_clients")
    op.drop_table("b2b_clients")
    op.drop_index("ix_activity_logs_id", table_name="activity_logs")
    op.drop_table("activity_logs")
    op.drop_index("ix_accounts_id", table_name="accounts")
    op.drop_table("accounts")
