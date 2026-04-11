from sqlalchemy import text

import app.models  # noqa: F401
from app.database import Base, SessionLocal, engine
from app.models.accounting import Account
from app.models.expense import ExpenseCategory

_SAFE_TABLES_WITH_USER_ID = (
    "b2b_invoices",
    "consignments",
    "b2b_refunds",
    "farm_deliveries",
    "production_batches",
    "spoilage_records",
)

_PAYROLL_COLUMNS = (
    "days_worked  INTEGER",
    "working_days INTEGER",
    "paid_at      TIMESTAMPTZ",
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


def _ensure_schema() -> None:
    Base.metadata.create_all(bind=engine)


def _run_safe_alterations() -> None:
    with engine.begin() as conn:
        for table_name in _SAFE_TABLES_WITH_USER_ID:
            conn.execute(
                text(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS user_id INTEGER")
            )

        for col_def in _PAYROLL_COLUMNS:
            conn.execute(text(f"ALTER TABLE payroll ADD COLUMN IF NOT EXISTS {col_def}"))

        conn.execute(text("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS farm_id INTEGER"))
        conn.execute(
            text(
                "ALTER TABLE b2b_clients "
                "ADD COLUMN IF NOT EXISTS discount_pct NUMERIC(6,2) DEFAULT 0"
            )
        )
        conn.execute(
            text(
                """
                UPDATE b2b_clients
                SET discount_pct = credit_limit,
                    credit_limit = 0
                WHERE discount_pct = 0 AND credit_limit > 0
                """
            )
        )


def _seed_expense_categories() -> None:
    with SessionLocal() as db:
        for code, name in _DEFAULT_EXPENSE_CATEGORIES:
            if not db.query(Account).filter(Account.code == code).first():
                db.add(Account(code=code, name=name, type="expense", balance=0))

            if not db.query(ExpenseCategory).filter(ExpenseCategory.name == name).first():
                db.add(ExpenseCategory(name=name, account_code=code))

        db.commit()


def initialize_database() -> None:
    _ensure_schema()
    _run_safe_alterations()
    _seed_expense_categories()
