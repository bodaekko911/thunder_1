from sqlalchemy import inspect, select

import app.models  # noqa: F401
from app.core.config import settings
from app.core.security import hash_password
from app.db.base import Base
from app.db.session import AsyncSessionLocal, engine
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
        "paid_at": "TIMESTAMP",
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


async def _ensure_schema() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _run_safe_alterations() -> None:
    async with engine.begin() as conn:
        def run(sync_conn):
            inspector = inspect(sync_conn)
            for table_name, columns in _SAFE_COLUMNS.items():
                if not inspector.has_table(table_name):
                    continue
                existing_columns = {
                    column["name"] for column in inspector.get_columns(table_name)
                }
                for column_name, definition in columns.items():
                    if column_name in existing_columns:
                        continue
                    sync_conn.exec_driver_sql(
                        f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"
                    )

        await conn.run_sync(run)


async def _seed_expense_categories() -> None:
    async with AsyncSessionLocal() as session:
        for code, name in _DEFAULT_EXPENSE_CATEGORIES:
            account = await session.scalar(select(Account).where(Account.code == code))
            if not account:
                session.add(Account(code=code, name=name, type="expense", balance=0))

            category = await session.scalar(
                select(ExpenseCategory).where(ExpenseCategory.name == name)
            )
            if not category:
                session.add(ExpenseCategory(name=name, account_code=code))

        await session.commit()


async def _seed_default_admin() -> None:
    async with AsyncSessionLocal() as session:
        admin = await session.scalar(
            select(User).where(User.email == settings.DEFAULT_ADMIN_EMAIL)
        )
        if admin:
            return

        session.add(
            User(
                name=settings.DEFAULT_ADMIN_NAME,
                email=settings.DEFAULT_ADMIN_EMAIL,
                password=hash_password(settings.ADMIN_PASSWORD),
                role="admin",
                is_active=True,
            )
        )
        await session.commit()


async def initialize_database() -> None:
    await _ensure_schema()
    await _run_safe_alterations()
    await _seed_expense_categories()
    await _seed_default_admin()
