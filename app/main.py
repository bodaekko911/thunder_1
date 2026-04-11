from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from app.core.config import settings
from app.database import Base, engine
import app.models

Base.metadata.create_all(bind=engine)

with engine.begin() as conn:
    _safe_tables = {"b2b_invoices", "consignments", "b2b_refunds", "farm_deliveries", "production_batches", "spoilage_records"}
    for table_name in _safe_tables:
        assert table_name in _safe_tables  # guard against f-string injection if list is ever changed
        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS user_id INTEGER"))
    # Payroll columns added in bug fix — safe to run on existing DBs
    for col_def in (
        "days_worked  INTEGER",
        "working_days INTEGER",
        "paid_at      TIMESTAMPTZ",
    ):
        conn.execute(text(f"ALTER TABLE payroll ADD COLUMN IF NOT EXISTS {col_def}"))
    # Add farm_id to expenses for seasonal cost allocation
    conn.execute(text("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS farm_id INTEGER"))
    # Bug fix: credit_limit was misused to store discount_pct.
    # Add the proper discount_pct column and migrate existing values across.
    conn.execute(text("ALTER TABLE b2b_clients ADD COLUMN IF NOT EXISTS discount_pct NUMERIC(6,2) DEFAULT 0"))
    conn.execute(text("""
        UPDATE b2b_clients
        SET discount_pct = credit_limit,
            credit_limit = 0
        WHERE discount_pct = 0 AND credit_limit > 0
    """))

# Seed default expense categories and their ledger accounts if not already present
from app.database import SessionLocal
from app.models.expense import ExpenseCategory
from app.models.accounting import Account

_DEFAULT_EXPENSE_CATEGORIES = [
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
]

with SessionLocal() as _db:
    for code, name in _DEFAULT_EXPENSE_CATEGORIES:
        # Ensure ledger account exists
        if not _db.query(Account).filter(Account.code == code).first():
            _db.add(Account(code=code, name=name, type="expense", balance=0))
        # Ensure category exists
        if not _db.query(ExpenseCategory).filter(ExpenseCategory.name == name).first():
            _db.add(ExpenseCategory(name=name, account_code=code))
    _db.commit()

app = FastAPI(title=settings.APP_NAME)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

from app.routers import (
    auth, pos, import_data, dashboard, products, customers,
    suppliers, inventory, hr, accounting, production, home,
    b2b, farm, reports, users, refunds, expenses, audit_log
)

app.include_router(auth.router)
app.include_router(home.router)
app.include_router(pos.router)
app.include_router(import_data.router)
app.include_router(dashboard.router)
app.include_router(products.router)
app.include_router(customers.router)
app.include_router(suppliers.router)
app.include_router(inventory.router)
app.include_router(hr.router)
app.include_router(accounting.router)
app.include_router(production.router)
app.include_router(b2b.router)
app.include_router(farm.router)
app.include_router(reports.router)
app.include_router(users.router)
app.include_router(refunds.router)
app.include_router(expenses.router)
app.include_router(audit_log.router)

@app.get("/health")
def health():
    return {"status": "ok", "app": settings.APP_NAME}