from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import hash_password
from app.database import session_scope
from app.models.accounting import Account
from app.models.expense import ExpenseCategory
from app.models.user import User

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
    expense_accounts_created: int = 0
    expense_categories_created: int = 0

    def as_lines(self) -> list[str]:
        return [
            f"admin_created={str(self.admin_created).lower()}",
            f"expense_accounts_created={self.expense_accounts_created}",
            f"expense_categories_created={self.expense_categories_created}",
        ]


async def ensure_default_admin(
    db: AsyncSession,
    *,
    name: str = settings.DEFAULT_ADMIN_NAME,
    email: str = settings.DEFAULT_ADMIN_EMAIL,
    password: str = settings.ADMIN_PASSWORD,
) -> bool:
    existing = await db.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none():
        return False

    db.add(
        User(
            name=name,
            email=email,
            password=hash_password(password),
            role="admin",
            is_active=True,
        )
    )
    return True


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


async def run_bootstrap(*, create_admin: bool, create_expense_categories: bool) -> BootstrapSummary:
    summary = BootstrapSummary()

    async with session_scope() as db:
        if create_admin:
            summary.admin_created = await ensure_default_admin(db)

        if create_expense_categories:
            (
                summary.expense_accounts_created,
                summary.expense_categories_created,
            ) = await ensure_default_expense_categories(db)

    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Initialize optional bootstrap data. This command is never run automatically.",
    )
    parser.add_argument("--admin", action="store_true", help="Create the default admin user if missing")
    parser.add_argument(
        "--expense-categories",
        action="store_true",
        help="Create default expense accounts and categories if missing",
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

    create_admin = args.admin or args.all
    create_expense_categories = args.expense_categories or args.all

    if not create_admin and not create_expense_categories:
        parser.error("select at least one action: --admin, --expense-categories, or --all")

    if settings.APP_ENV == "production" and not args.yes:
        parser.error("--yes is required when APP_ENV=production")

    summary = asyncio.run(
        run_bootstrap(
            create_admin=create_admin,
            create_expense_categories=create_expense_categories,
        )
    )

    print("Bootstrap complete")
    for line in summary.as_lines():
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
