from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.database import session_scope
from app.models.customer import Customer
from app.models.expense import Expense, ExpenseCategory
from app.models.invoice import Invoice, InvoiceItem
from app.models.product import Product
from app.models.user import User


SAMPLE_CUSTOMERS = (
    {
        "name": "Nile Fresh Market",
        "phone": "+20-100-000-0001",
        "email": "buyer@nilefresh.example",
        "address": "12 River Road, Cairo",
        "discount_pct": Decimal("0.00"),
        "balance": Decimal("0.00"),
    },
    {
        "name": "Green Basket Grocers",
        "phone": "+20-100-000-0002",
        "email": "orders@greenbasket.example",
        "address": "28 Garden Street, Giza",
        "discount_pct": Decimal("2.50"),
        "balance": Decimal("0.00"),
    },
    {
        "name": "Sunrise Cafe",
        "phone": "+20-100-000-0003",
        "email": "manager@sunrisecafe.example",
        "address": "5 Morning Plaza, Alexandria",
        "discount_pct": Decimal("0.00"),
        "balance": Decimal("0.00"),
    },
)

SAMPLE_PRODUCTS = (
    {
        "sku": "DEMO-APPLE-JUICE-1L",
        "name": "Apple Juice 1L",
        "price": Decimal("65.00"),
        "cost": Decimal("42.00"),
        "stock": Decimal("48.000"),
        "min_stock": Decimal("8.000"),
        "unit": "bottle",
        "category": "Beverages",
        "item_type": "finished",
    },
    {
        "sku": "DEMO-ORANGE-JUICE-1L",
        "name": "Orange Juice 1L",
        "price": Decimal("68.00"),
        "cost": Decimal("44.00"),
        "stock": Decimal("36.000"),
        "min_stock": Decimal("8.000"),
        "unit": "bottle",
        "category": "Beverages",
        "item_type": "finished",
    },
    {
        "sku": "DEMO-MIX-BERRIES-330ML",
        "name": "Mixed Berries 330ml",
        "price": Decimal("32.00"),
        "cost": Decimal("18.00"),
        "stock": Decimal("72.000"),
        "min_stock": Decimal("12.000"),
        "unit": "bottle",
        "category": "Beverages",
        "item_type": "finished",
    },
    {
        "sku": "DEMO-GRANOLA-500G",
        "name": "Honey Granola 500g",
        "price": Decimal("55.00"),
        "cost": Decimal("31.00"),
        "stock": Decimal("24.000"),
        "min_stock": Decimal("6.000"),
        "unit": "pack",
        "category": "Snacks",
        "item_type": "finished",
    },
    {
        "sku": "DEMO-DATES-1KG",
        "name": "Premium Dates 1kg",
        "price": Decimal("95.00"),
        "cost": Decimal("61.00"),
        "stock": Decimal("18.000"),
        "min_stock": Decimal("4.000"),
        "unit": "box",
        "category": "Dry Goods",
        "item_type": "finished",
    },
)

SAMPLE_INVOICES = (
    {
        "invoice_number": "INV-DEMO-0001",
        "customer_email": "buyer@nilefresh.example",
        "status": "paid",
        "payment_method": "cash",
        "notes": "Demo invoice for dashboard and customer pages.",
        "items": (
            {"sku": "DEMO-APPLE-JUICE-1L", "qty": Decimal("4.000")},
            {"sku": "DEMO-GRANOLA-500G", "qty": Decimal("2.000")},
        ),
    },
    {
        "invoice_number": "INV-DEMO-0002",
        "customer_email": "manager@sunrisecafe.example",
        "status": "paid",
        "payment_method": "card",
        "notes": "Second demo invoice with mixed products.",
        "items": (
            {"sku": "DEMO-ORANGE-JUICE-1L", "qty": Decimal("3.000")},
            {"sku": "DEMO-MIX-BERRIES-330ML", "qty": Decimal("6.000")},
            {"sku": "DEMO-DATES-1KG", "qty": Decimal("1.000")},
        ),
    },
)

SAMPLE_EXPENSES = (
    {
        "ref_number": "EXP-DEMO-0001",
        "category_name": "Electricity",
        "expense_date": date(2026, 4, 10),
        "amount": Decimal("480.00"),
        "payment_method": "bank_transfer",
        "vendor": "City Utilities",
        "description": "Demo electricity bill for April.",
    },
    {
        "ref_number": "EXP-DEMO-0002",
        "category_name": "Packaging Materials",
        "expense_date": date(2026, 4, 11),
        "amount": Decimal("275.00"),
        "payment_method": "cash",
        "vendor": "PackRight Supplies",
        "description": "Demo packaging purchase for showroom orders.",
    },
)


@dataclass
class DemoSeedSummary:
    customers_created: int = 0
    products_created: int = 0
    invoices_created: int = 0
    expenses_created: int = 0

    def as_lines(self) -> list[str]:
        return [
            f"customers_created={self.customers_created}",
            f"products_created={self.products_created}",
            f"invoices_created={self.invoices_created}",
            f"expenses_created={self.expenses_created}",
        ]


async def _get_seed_user(db: AsyncSession) -> User:
    result = await db.execute(select(User).where(User.is_active.is_(True)).order_by(User.id))
    user = result.scalars().first()
    if user is None:
        raise RuntimeError("Cannot seed demo data without at least one active user.")
    return user


async def ensure_demo_customers(db: AsyncSession) -> tuple[int, dict[str, Customer]]:
    created = 0
    customers_by_email: dict[str, Customer] = {}

    for payload in SAMPLE_CUSTOMERS:
        result = await db.execute(select(Customer).where(Customer.email == payload["email"]))
        customer = result.scalar_one_or_none()
        if customer is None:
            customer = Customer(**payload)
            db.add(customer)
            created += 1
        customers_by_email[payload["email"]] = customer

    await db.flush()
    return created, customers_by_email


async def ensure_demo_products(db: AsyncSession) -> tuple[int, dict[str, Product]]:
    created = 0
    products_by_sku: dict[str, Product] = {}

    for payload in SAMPLE_PRODUCTS:
        result = await db.execute(select(Product).where(Product.sku == payload["sku"]))
        product = result.scalar_one_or_none()
        if product is None:
            product = Product(is_active=True, **payload)
            db.add(product)
            created += 1
        products_by_sku[payload["sku"]] = product

    await db.flush()
    return created, products_by_sku


async def ensure_demo_invoices(
    db: AsyncSession,
    *,
    user: User,
    customers_by_email: dict[str, Customer],
    products_by_sku: dict[str, Product],
) -> int:
    created = 0

    for payload in SAMPLE_INVOICES:
        result = await db.execute(select(Invoice).where(Invoice.invoice_number == payload["invoice_number"]))
        if result.scalar_one_or_none() is not None:
            continue

        customer = customers_by_email[payload["customer_email"]]
        items: list[InvoiceItem] = []
        subtotal = Decimal("0.00")
        for item_payload in payload["items"]:
            product = products_by_sku[item_payload["sku"]]
            unit_price = Decimal(str(product.price))
            qty = item_payload["qty"]
            line_total = unit_price * qty
            subtotal += line_total
            items.append(
                InvoiceItem(
                    product_id=product.id,
                    sku=product.sku,
                    name=product.name,
                    qty=qty,
                    unit_price=unit_price,
                    total=line_total,
                )
            )

        db.add(
            Invoice(
                invoice_number=payload["invoice_number"],
                customer_id=customer.id,
                user_id=user.id,
                status=payload["status"],
                payment_method=payload["payment_method"],
                subtotal=subtotal,
                discount=Decimal("0.00"),
                total=subtotal,
                notes=payload["notes"],
                items=items,
            )
        )
        created += 1

    return created


async def ensure_demo_expenses(db: AsyncSession, *, user: User) -> int:
    created = 0

    category_names = {payload["category_name"] for payload in SAMPLE_EXPENSES}
    result = await db.execute(select(ExpenseCategory).where(ExpenseCategory.name.in_(category_names)))
    categories = {category.name: category for category in result.scalars().all()}
    missing_categories = sorted(category_names - set(categories))
    if missing_categories:
        missing = ", ".join(missing_categories)
        raise RuntimeError(
            f"Cannot seed demo expenses because these categories are missing: {missing}. "
            "Run `python -m app.bootstrap.init_data --expense-categories` first."
        )

    for payload in SAMPLE_EXPENSES:
        result = await db.execute(select(Expense).where(Expense.ref_number == payload["ref_number"]))
        if result.scalar_one_or_none() is not None:
            continue

        category = categories[payload["category_name"]]
        db.add(
            Expense(
                ref_number=payload["ref_number"],
                category_id=category.id,
                user_id=user.id,
                expense_date=payload["expense_date"],
                amount=payload["amount"],
                payment_method=payload["payment_method"],
                vendor=payload["vendor"],
                description=payload["description"],
            )
        )
        created += 1

    return created


async def run_demo_seed() -> DemoSeedSummary:
    summary = DemoSeedSummary()

    async with session_scope() as db:
        user = await _get_seed_user(db)
        summary.customers_created, customers_by_email = await ensure_demo_customers(db)
        summary.products_created, products_by_sku = await ensure_demo_products(db)
        summary.invoices_created = await ensure_demo_invoices(
            db,
            user=user,
            customers_by_email=customers_by_email,
            products_by_sku=products_by_sku,
        )
        summary.expenses_created = await ensure_demo_expenses(db, user=user)

    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Insert a small idempotent demo dataset. This command is never run automatically.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required in production to confirm this manual demo-data action",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if settings.APP_ENV == "production" and not args.yes:
        parser.error("--yes is required when APP_ENV=production")

    summary = asyncio.run(run_demo_seed())
    print("Demo seed complete")
    for line in summary.as_lines():
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
