from __future__ import annotations

from typing import Iterable

PERMISSION_PAGES = [
    {
        "key": "page_dashboard",
        "label": "Dashboard",
        "icon": "chart",
        "actions": [],
    },
    {
        "key": "page_reports",
        "label": "Reports",
        "icon": "reports",
        "actions": [
            {"key": "tab_reports_sales", "label": "Sales tab"},
            {"key": "tab_reports_pl", "label": "P&L tab"},
            {"key": "tab_reports_inventory", "label": "Inventory tab"},
            {"key": "tab_reports_transactions", "label": "Transactions tab"},
            {"key": "action_export_excel", "label": "Export to Excel"},
        ],
    },
    {
        "key": "page_pos",
        "label": "POS",
        "icon": "pos",
        "actions": [
            {"key": "action_pos_delete_invoice", "label": "Delete invoices"},
            {"key": "action_pos_discount", "label": "Apply discounts"},
            {"key": "action_pos_settle_later", "label": "Settle later"},
        ],
    },
    {
        "key": "page_b2b",
        "label": "B2B",
        "icon": "b2b",
        "actions": [
            {"key": "tab_b2b_clients", "label": "Clients tab"},
            {"key": "tab_b2b_invoices", "label": "Invoices tab"},
            {"key": "tab_b2b_consignment", "label": "Consignment tab"},
            {"key": "action_b2b_delete", "label": "Delete invoices"},
            {"key": "action_b2b_collect", "label": "Collect payments"},
        ],
    },
    {
        "key": "page_inventory",
        "label": "Inventory",
        "icon": "inventory",
        "actions": [
            {"key": "action_inventory_adjust", "label": "Adjust stock"},
        ],
    },
    {
        "key": "page_products",
        "label": "Products",
        "icon": "products",
        "actions": [
            {"key": "action_products_edit", "label": "Edit products"},
            {"key": "action_products_delete", "label": "Delete products"},
        ],
    },
    {
        "key": "page_import",
        "label": "Import Data",
        "icon": "import",
        "actions": [],
    },
    {
        "key": "page_production",
        "label": "Production",
        "icon": "production",
        "actions": [
            {"key": "tab_production_batches", "label": "Batches tab"},
            {"key": "tab_production_packaging", "label": "Packaging tab"},
            {"key": "tab_production_spoilage", "label": "Spoilage tab"},
            {"key": "tab_production_recipes", "label": "Recipes tab"},
        ],
    },
    {
        "key": "page_farm",
        "label": "Farm Intake",
        "icon": "farm",
        "actions": [],
    },
    {
        "key": "page_hr",
        "label": "HR & Payroll",
        "icon": "hr",
        "actions": [
            {"key": "tab_hr_employees", "label": "Employees tab"},
            {"key": "tab_hr_attendance", "label": "Attendance tab"},
            {"key": "tab_hr_payroll", "label": "Payroll tab"},
            {"key": "action_hr_run_payroll", "label": "Run payroll"},
            {"key": "action_hr_mark_paid", "label": "Mark payroll paid"},
        ],
    },
    {
        "key": "page_accounting",
        "label": "Accounting",
        "icon": "accounting",
        "actions": [
            {"key": "tab_accounting_pos", "label": "POS invoices tab"},
            {"key": "tab_accounting_b2b", "label": "B2B invoices tab"},
            {"key": "tab_accounting_journal", "label": "Journal tab"},
            {"key": "tab_accounting_pl", "label": "P&L tab"},
            {"key": "action_accounting_post_journal", "label": "Post journal entries"},
        ],
    },
    {
        "key": "page_customers",
        "label": "Customers",
        "icon": "customers",
        "actions": [],
    },
    {
        "key": "page_suppliers",
        "label": "Suppliers",
        "icon": "suppliers",
        "actions": [],
    },
]

ROLE_DEFINITIONS = {
    "cashier": {
        "label": "Cashier",
        "description": "POS terminal access with checkout actions.",
        "permissions": {
            "page_pos",
            "action_pos_discount",
            "action_pos_settle_later",
        },
    },
    "manager": {
        "label": "Manager",
        "description": "Operations access across sales, inventory, production, farm, and reports.",
        "permissions": {
            "page_dashboard",
            "page_reports",
            "tab_reports_sales",
            "tab_reports_inventory",
            "tab_reports_transactions",
            "action_export_excel",
            "page_pos",
            "action_pos_discount",
            "action_pos_settle_later",
            "page_b2b",
            "tab_b2b_clients",
            "tab_b2b_invoices",
            "tab_b2b_consignment",
            "action_b2b_collect",
            "page_inventory",
            "action_inventory_adjust",
            "page_products",
            "action_products_edit",
            "page_import",
            "page_production",
            "tab_production_batches",
            "tab_production_packaging",
            "tab_production_spoilage",
            "tab_production_recipes",
            "page_farm",
            "page_customers",
            "page_suppliers",
        },
    },
    "accountant": {
        "label": "Accountant",
        "description": "Financial operations, journals, and reporting.",
        "permissions": {
            "page_dashboard",
            "page_reports",
            "tab_reports_sales",
            "tab_reports_pl",
            "tab_reports_inventory",
            "tab_reports_transactions",
            "action_export_excel",
            "page_accounting",
            "tab_accounting_pos",
            "tab_accounting_b2b",
            "tab_accounting_journal",
            "tab_accounting_pl",
            "action_accounting_post_journal",
        },
    },
    "hr": {
        "label": "HR",
        "description": "People operations, attendance, and payroll.",
        "permissions": {
            "page_dashboard",
            "page_hr",
            "tab_hr_employees",
            "tab_hr_attendance",
            "tab_hr_payroll",
            "action_hr_run_payroll",
            "action_hr_mark_paid",
        },
    },
    "viewer": {
        "label": "Viewer",
        "description": "Read-only access to dashboards and selected reports.",
        "permissions": {
            "page_dashboard",
            "page_reports",
            "tab_reports_sales",
            "tab_reports_pl",
            "tab_reports_inventory",
            "tab_reports_transactions",
        },
    },
    "admin": {
        "label": "Admin",
        "description": "Full system access including user management and audit visibility.",
        "permissions": {"*"},
    },
}


def iter_known_permissions() -> Iterable[str]:
    for page in PERMISSION_PAGES:
        yield page["key"]
        for action in page["actions"]:
            yield action["key"]


KNOWN_PERMISSIONS = set(iter_known_permissions())


def get_role_permissions(role: str | None) -> set[str]:
    return set(ROLE_DEFINITIONS.get(role or "", {}).get("permissions", set()))


def is_known_permission(permission: str) -> bool:
    return permission == "*" or permission in KNOWN_PERMISSIONS


def get_permission_catalog() -> dict:
    return {
        "pages": PERMISSION_PAGES,
        "roles": [
            {
                "key": role_key,
                "label": role_data["label"],
                "description": role_data["description"],
                "permissions": sorted(role_data["permissions"]),
            }
            for role_key, role_data in ROLE_DEFINITIONS.items()
        ],
    }
