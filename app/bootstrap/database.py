from sqlalchemy import inspect

import app.models  # noqa: F401
from app.core.config import settings
from app.core.security import hash_password
from app.database import Base, SessionLocal, engine
from app.models.accounting import Account
from app.models.expense import ExpenseCategory
from app.models.user import User

_SAFE_COLUMNS = {
    "b2b_invoices": {
        "user_id": "INTEGER",
    },
    "consignments": {
        "user_id": "INTEGER",
    },
    "b2b_refunds": {
        "user_id": "INTEGER",
    },
    "farm_deliveries": {
        "user_id": "INTEGER",
    },
    "production_batches": {
        "user_id": "INTEGER",
    },
    "spoilage_records": {
        "user_id": "INTEGER",
    },
    "payroll": {
        "days_worked": "INTEGER",
        "working_days": "INTEGER",
        "paid_at": "DATETIME",
    },
    "expenses": {
        "farm_id": "INTEGER",
    },
    "b2b_clients": {
        "discount_pct": "NUMERIC(6,2) DEFAULT 0",
    },
}

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
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table_name, columns in _SAFE_COLUMNS.items():
            if not inspector.has_table(table_name):
                continue
            existing_columns = {
                column["name"] for column in inspector.get_columns(table_name)
            }
            for column_name, definition in columns.items():
                if column_name in existing_columns:
                    continue
                conn.exec_driver_sql(
                    f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"
                )


def _seed_expense_categories() -> None:
    with SessionLocal() as db:
        for code, name in _DEFAULT_EXPENSE_CATEGORIES:
            if not db.query(Account).filter(Account.code == code).first():
                db.add(Account(code=code, name=name, type="expense", balance=0))

            if not db.query(ExpenseCategory).filter(ExpenseCategory.name == name).first():
                db.add(ExpenseCategory(name=name, account_code=code))

        db.commit()


def _seed_default_admin() -> None:
    with SessionLocal() as db:
        admin = db.query(User).filter(User.email == settings.DEFAULT_ADMIN_EMAIL).first()
        if admin:
            return

        db.add(
            User(
                name=settings.DEFAULT_ADMIN_NAME,
                email=settings.DEFAULT_ADMIN_EMAIL,
                password=hash_password(settings.DEFAULT_ADMIN_PASSWORD),
                role="admin",
                is_active=True,
            )
        )
        db.commit()


def initialize_database() -> None:
    _ensure_schema()
    _run_safe_alterations()
    _seed_expense_categories()
    _seed_default_admin()
