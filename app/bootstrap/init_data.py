from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import hash_password, verify_password
from app.database import session_scope
from app.models.accounting import Account
from app.models.expense import ExpenseCategory
from app.models.product import Product
from app.models.user import User
from app.services.location_inventory_service import sync_product_stock_to_default_location

DEFAULT_EXPENSE_CATEGORIES = (
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


@dataclass
class BootstrapSummary:
    admin_created: bool = False
    admin_password_reset: bool = False
    expense_accounts_created: int = 0
    expense_categories_created: int = 0
    stock_location_created_or_reused: bool = False
    location_stock_rows_synced: int = 0

    def as_lines(self) -> list[str]:
        return [
            f"admin_created={str(self.admin_created).lower()}",
            f"admin_password_reset={str(self.admin_password_reset).lower()}",
            f"expense_accounts_created={self.expense_accounts_created}",
            f"expense_categories_created={self.expense_categories_created}",
            f"stock_location_created_or_reused={str(self.stock_location_created_or_reused).lower()}",
            f"location_stock_rows_synced={self.location_stock_rows_synced}",
        ]


async def ensure_default_admin(
    db: AsyncSession,
    *,
    name: str = settings.DEFAULT_ADMIN_NAME,
    email: str = settings.DEFAULT_ADMIN_EMAIL,
    password: str = settings.ADMIN_PASSWORD,
    reset_password: bool = False,
) -> tuple[bool, bool]:
    existing = await db.execute(select(User).where(User.email == email))
    user = existing.scalar_one_or_none()
    if user:
        if reset_password:
            try:
                password_matches = verify_password(password, user.password)
            except Exception:
                password_matches = False
            if not password_matches:
                user.password = hash_password(password)
                return False, True
        return False, False

    db.add(
        User(
            name=name,
            email=email,
            password=hash_password(password),
            role="admin",
            is_active=True,
        )
    )
    return True, False


async def ensure_default_expense_categories(db: AsyncSession) -> tuple[int, int]:
    accounts_created = 0
    categories_created = 0

    for code, name in DEFAULT_EXPENSE_CATEGORIES:
        account_result = await db.execute(select(Account).where(Account.code == code))
        if account_result.scalar_one_or_none() is None:
            db.add(Account(code=code, name=name, type="expense", balance=0))
            accounts_created += 1

        category_result = await db.execute(select(ExpenseCategory).where(ExpenseCategory.name == name))
        if category_result.scalar_one_or_none() is None:
            db.add(
                ExpenseCategory(
                    name=name,
                    account_code=code,
                    is_active="1",
                )
            )
            categories_created += 1

    return accounts_created, categories_created


async def ensure_default_stock_location_data(db: AsyncSession) -> tuple[bool, int]:
    result = await db.execute(select(Product))
    products = result.scalars().all()
    synced_rows = 0
    for product in products:
        await sync_product_stock_to_default_location(db, product=product)
        synced_rows += 1
    return True, synced_rows


async def run_bootstrap(
    *,
    create_admin: bool,
    create_expense_categories: bool,
    create_stock_location: bool,
    reset_admin_password: bool,
) -> BootstrapSummary:
    summary = BootstrapSummary()

    async with session_scope() as db:
        if create_admin:
            (
                summary.admin_created,
                summary.admin_password_reset,
            ) = await ensure_default_admin(db, reset_password=reset_admin_password)

        if create_expense_categories:
            (
                summary.expense_accounts_created,
                summary.expense_categories_created,
            ) = await ensure_default_expense_categories(db)

        if create_stock_location:
            (
                summary.stock_location_created_or_reused,
                summary.location_stock_rows_synced,
            ) = await ensure_default_stock_location_data(db)

    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Initialize optional bootstrap data. This command is never run automatically.",
    )
    parser.add_argument("--admin", action="store_true", help="Create the default admin user if missing")
    parser.add_argument(
        "--reset-admin-password",
        action="store_true",
        help="Update the default admin user's password to ADMIN_PASSWORD if the user already exists",
    )
    parser.add_argument(
        "--expense-categories",
        action="store_true",
        help="Create default expense accounts and categories if missing",
    )
    parser.add_argument(
        "--stock-location",
        action="store_true",
        help="Create or reuse the default stock location and sync product stock into it",
    )
    parser.add_argument("--all", action="store_true", help="Run all optional bootstrap actions")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required in production to confirm this one-time initialization",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    create_admin = args.admin or args.all or args.reset_admin_password
    create_expense_categories = args.expense_categories or args.all
    create_stock_location = args.stock_location or args.all

    if not create_admin and not create_expense_categories and not create_stock_location:
        parser.error(
            "select at least one action: --admin, --reset-admin-password, --expense-categories, --stock-location, or --all"
        )

    if settings.APP_ENV == "production" and not args.yes:
        parser.error("--yes is required when APP_ENV=production")

    summary = asyncio.run(
        run_bootstrap(
            create_admin=create_admin,
            create_expense_categories=create_expense_categories,
            create_stock_location=create_stock_location,
            reset_admin_password=args.reset_admin_password,
        )
    )

    print("Bootstrap complete")
    for line in summary.as_lines():
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
