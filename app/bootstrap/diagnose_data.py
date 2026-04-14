from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import session_scope
from app.models.accounting import Account, Journal
from app.models.b2b import B2BClient, B2BInvoice
from app.models.customer import Customer
from app.models.expense import Expense, ExpenseCategory
from app.models.farm import Farm, FarmDelivery
from app.models.hr import Employee
from app.models.inventory import StockLocation, StockMove
from app.models.invoice import Invoice
from app.models.product import Product
from app.models.receipt import ProductReceipt
from app.models.supplier import Purchase, Supplier
from app.models.user import User


@dataclass
class DataCheckReport:
    counts: dict[str, int]
    likely_primary_issue: str
    likely_secondary_issue: str | None
    impacted_pages: dict[str, dict[str, object]]
    missing_reference_data: list[str]
    notes: list[str]


async def _count(db: AsyncSession, model) -> int:
    result = await db.execute(select(func.count()).select_from(model))
    return int(result.scalar() or 0)


async def build_report(db: AsyncSession) -> DataCheckReport:
    counts = {
        "users": await _count(db, User),
        "products": await _count(db, Product),
        "customers": await _count(db, Customer),
        "suppliers": await _count(db, Supplier),
        "purchases": await _count(db, Purchase),
        "pos_invoices": await _count(db, Invoice),
        "b2b_clients": await _count(db, B2BClient),
        "b2b_invoices": await _count(db, B2BInvoice),
        "expenses": await _count(db, Expense),
        "expense_categories": await _count(db, ExpenseCategory),
        "accounts": await _count(db, Account),
        "journals": await _count(db, Journal),
        "farms": await _count(db, Farm),
        "farm_deliveries": await _count(db, FarmDelivery),
        "employees": await _count(db, Employee),
        "stock_moves": await _count(db, StockMove),
        "product_receipts": await _count(db, ProductReceipt),
        "stock_locations": await _count(db, StockLocation),
    }

    missing_reference_data: list[str] = []
    if counts["expense_categories"] == 0:
        missing_reference_data.append("expense_categories")
    if counts["accounts"] == 0:
        missing_reference_data.append("accounts")
    if counts["farms"] == 0:
        missing_reference_data.append("farms")
    if counts["stock_locations"] == 0:
        missing_reference_data.append("stock_locations")

    transactional_groups = {
        "catalog_and_stock": counts["products"] + counts["stock_moves"] + counts["product_receipts"],
        "sales": counts["customers"] + counts["pos_invoices"],
        "procurement": counts["suppliers"] + counts["purchases"],
        "b2b": counts["b2b_clients"] + counts["b2b_invoices"],
        "operations": counts["farm_deliveries"] + counts["employees"],
        "finance": counts["expenses"] + counts["journals"],
    }

    if sum(transactional_groups.values()) == 0:
        likely_primary_issue = "production DB has empty tables"
    elif missing_reference_data:
        likely_primary_issue = "required seed/reference data is missing"
    else:
        likely_primary_issue = "production DB has partial business data; frontend or filter issues are less likely"

    likely_secondary_issue = None
    if likely_primary_issue != "required seed/reference data is missing" and missing_reference_data:
        likely_secondary_issue = "required seed/reference data is missing"

    impacted_pages = {
        "Dashboard (/dashboard/data)": {
            "tables": [
                "products",
                "customers",
                "pos_invoices",
                "b2b_clients",
                "b2b_invoices",
                "expenses",
                "farm_deliveries",
                "stock_moves",
            ],
            "empty_if": [
                "products == 0",
                "pos_invoices == 0",
                "b2b_invoices == 0",
                "expenses == 0",
            ],
        },
        "Products (/products/api/list)": {
            "tables": ["products"],
            "empty_if": ["products == 0"],
        },
        "Inventory (/inventory/api/stock, /inventory/api/moves)": {
            "tables": ["products", "stock_moves", "stock_locations"],
            "empty_if": ["products == 0"],
        },
        "Receive Products (/receive/api/products, /receive/api/history)": {
            "tables": ["products", "product_receipts"],
            "empty_if": ["products == 0"],
        },
        "Customers (/customers-mgmt/api/list)": {
            "tables": ["customers", "pos_invoices"],
            "empty_if": ["customers == 0"],
        },
        "Suppliers (/suppliers/api/list, /suppliers/api/purchases)": {
            "tables": ["suppliers", "purchases", "products"],
            "empty_if": ["suppliers == 0"],
        },
        "B2B (/b2b/api/clients, /b2b/api/invoices)": {
            "tables": ["b2b_clients", "b2b_invoices", "products", "accounts"],
            "empty_if": ["b2b_clients == 0", "products == 0"],
        },
        "Farm (/farm/api/farms, /farm/api/deliveries)": {
            "tables": ["farms", "farm_deliveries", "products"],
            "empty_if": ["farms == 0"],
        },
        "Expenses (/expenses/api/categories, /expenses/api/list)": {
            "tables": ["expense_categories", "expenses", "accounts"],
            "empty_if": ["expense_categories == 0"],
        },
        "Accounting (/accounting/api/accounts, /accounting/api/journals)": {
            "tables": ["accounts", "journals"],
            "empty_if": ["accounts == 0"],
        },
        "HR (/hr/api/employees, /hr/api/payroll)": {
            "tables": ["employees"],
            "empty_if": ["employees == 0"],
        },
        "Reports (/reports/api/*)": {
            "tables": [
                "products",
                "pos_invoices",
                "b2b_invoices",
                "expenses",
                "farm_deliveries",
                "stock_moves",
            ],
            "empty_if": ["most business tables are 0"],
        },
    }

    notes = [
        "This script is read-only and safe to run in production.",
        "The codebase does not define tenant/company/branch-scoped business tables, so empty pages are more likely caused by empty tables than by tenant filtering.",
        "A missing stock_locations table mostly affects location-transfer features, not the main stock list.",
    ]

    return DataCheckReport(
        counts=counts,
        likely_primary_issue=likely_primary_issue,
        likely_secondary_issue=likely_secondary_issue,
        impacted_pages=impacted_pages,
        missing_reference_data=missing_reference_data,
        notes=notes,
    )


async def run_diagnostics() -> DataCheckReport:
    async with session_scope() as db:
        return await build_report(db)


def _print_text(report: DataCheckReport) -> None:
    print("Data diagnostics")
    print(f"likely_primary_issue={report.likely_primary_issue}")
    print(f"likely_secondary_issue={report.likely_secondary_issue or 'none'}")
    print("")
    print("counts:")
    for key, value in report.counts.items():
        print(f"  {key}={value}")
    print("")
    print("missing_reference_data:")
    if report.missing_reference_data:
        for item in report.missing_reference_data:
            print(f"  {item}")
    else:
        print("  none")
    print("")
    print("impacted_pages:")
    for page, meta in report.impacted_pages.items():
        tables = ", ".join(meta["tables"])
        empty_if = ", ".join(meta["empty_if"])
        print(f"  {page}")
        print(f"    tables: {tables}")
        print(f"    empty_if: {empty_if}")
    print("")
    print("notes:")
    for note in report.notes:
        print(f"  - {note}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only production-safe data diagnostics for key ERP tables.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full report as JSON",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    report = asyncio.run(run_diagnostics())
    if args.json:
        print(json.dumps(asdict(report), indent=2, sort_keys=True))
    else:
        _print_text(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
