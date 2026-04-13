from __future__ import annotations

from collections import defaultdict
from typing import Iterable


PERMISSION_MATRIX = [
    {
        "module": "dashboard",
        "resource": "overview",
        "label": "Dashboard",
        "icon": "chart",
        "actions": [
            {"action": "view", "key": "page_dashboard", "label": "View dashboard"},
        ],
    },
    {
        "module": "reports",
        "resource": "reports",
        "label": "Reports",
        "icon": "reports",
        "actions": [
            {"action": "view", "key": "page_reports", "label": "Open reports"},
            {"action": "view_sales", "key": "tab_reports_sales", "label": "Sales tab"},
            {"action": "view_pl", "key": "tab_reports_pl", "label": "P&L tab"},
            {"action": "view_inventory", "key": "tab_reports_inventory", "label": "Inventory tab"},
            {"action": "view_transactions", "key": "tab_reports_transactions", "label": "Transactions tab"},
            {"action": "export", "key": "action_export_excel", "label": "Export to Excel"},
        ],
    },
    {
        "module": "pos",
        "resource": "sales",
        "label": "POS",
        "icon": "pos",
        "actions": [
            {"action": "view", "key": "page_pos", "label": "Open POS"},
            {"action": "create", "key": "action_pos_create_sale", "label": "Create sales"},
            {"action": "delete", "key": "action_pos_delete_invoice", "label": "Delete invoices"},
            {"action": "discount_override", "key": "action_pos_discount", "label": "Discount override"},
            {"action": "approve", "key": "action_pos_settle_later", "label": "Approve settle later"},
            {"action": "refund", "key": "action_pos_refund", "label": "Create retail refunds"},
        ],
    },
    {
        "module": "b2b",
        "resource": "clients",
        "label": "B2B",
        "icon": "b2b",
        "actions": [
            {"action": "view", "key": "page_b2b", "label": "Open B2B"},
            {"action": "view_clients", "key": "tab_b2b_clients", "label": "Clients tab"},
            {"action": "view_invoices", "key": "tab_b2b_invoices", "label": "Invoices tab"},
            {"action": "view_consignment", "key": "tab_b2b_consignment", "label": "Consignment tab"},
            {"action": "create_client", "key": "action_b2b_clients_create", "label": "Create clients"},
            {"action": "update_client", "key": "action_b2b_clients_update", "label": "Update clients"},
            {"action": "delete_client", "key": "action_b2b_clients_delete", "label": "Delete clients"},
        ],
    },
    {
        "module": "b2b",
        "resource": "invoices",
        "label": "B2B Invoices",
        "icon": "b2b",
        "actions": [
            {"action": "create", "key": "action_b2b_invoices_create", "label": "Create invoices"},
            {"action": "update", "key": "action_b2b_invoices_update", "label": "Update invoices"},
            {"action": "delete", "key": "action_b2b_delete", "label": "Delete invoices"},
            {"action": "approve", "key": "action_b2b_collect", "label": "Collect payments"},
            {"action": "refund", "key": "action_b2b_refund", "label": "Create client refunds"},
            {"action": "settle", "key": "action_b2b_consignment_settle", "label": "Settle consignments"},
        ],
    },
    {
        "module": "inventory",
        "resource": "stock",
        "label": "Inventory",
        "icon": "inventory",
        "actions": [
            {"action": "view", "key": "page_inventory", "label": "Open inventory"},
            {"action": "update", "key": "action_inventory_adjust", "label": "Adjust stock"},
        ],
    },
    {
        "module": "products",
        "resource": "products",
        "label": "Products",
        "icon": "products",
        "actions": [
            {"action": "view", "key": "page_products", "label": "Open products"},
            {"action": "update", "key": "action_products_edit", "label": "Edit products"},
            {"action": "delete", "key": "action_products_delete", "label": "Delete products"},
        ],
    },
    {
        "module": "import",
        "resource": "imports",
        "label": "Import Data",
        "icon": "import",
        "actions": [
            {"action": "view", "key": "page_import", "label": "Open import data"},
        ],
    },
    {
        "module": "production",
        "resource": "production",
        "label": "Production",
        "icon": "production",
        "actions": [
            {"action": "view", "key": "page_production", "label": "Open production"},
            {"action": "view_batches", "key": "tab_production_batches", "label": "Batches tab"},
            {"action": "view_packaging", "key": "tab_production_packaging", "label": "Packaging tab"},
            {"action": "view_spoilage", "key": "tab_production_spoilage", "label": "Spoilage tab"},
            {"action": "view_recipes", "key": "tab_production_recipes", "label": "Recipes tab"},
        ],
    },
    {
        "module": "farm",
        "resource": "intake",
        "label": "Farm Intake",
        "icon": "farm",
        "actions": [
            {"action": "view", "key": "page_farm", "label": "Open farm intake"},
        ],
    },
    {
        "module": "hr",
        "resource": "payroll",
        "label": "HR & Payroll",
        "icon": "hr",
        "actions": [
            {"action": "view", "key": "page_hr", "label": "Open HR"},
            {"action": "view_employees", "key": "tab_hr_employees", "label": "Employees tab"},
            {"action": "view_attendance", "key": "tab_hr_attendance", "label": "Attendance tab"},
            {"action": "view_payroll", "key": "tab_hr_payroll", "label": "Payroll tab"},
            {"action": "approve", "key": "action_hr_run_payroll", "label": "Run payroll"},
            {"action": "mark_paid", "key": "action_hr_mark_paid", "label": "Mark payroll paid"},
        ],
    },
    {
        "module": "accounting",
        "resource": "journals",
        "label": "Accounting",
        "icon": "accounting",
        "actions": [
            {"action": "view", "key": "page_accounting", "label": "Open accounting"},
            {"action": "view_pos", "key": "tab_accounting_pos", "label": "POS invoices tab"},
            {"action": "view_b2b", "key": "tab_accounting_b2b", "label": "B2B invoices tab"},
            {"action": "view_journal", "key": "tab_accounting_journal", "label": "Journal tab"},
            {"action": "view_pl", "key": "tab_accounting_pl", "label": "P&L tab"},
            {"action": "create", "key": "action_accounting_post_journal", "label": "Post journal entries"},
        ],
    },
    {
        "module": "customers",
        "resource": "customers",
        "label": "Customers",
        "icon": "customers",
        "actions": [
            {"action": "view", "key": "page_customers", "label": "Open customers"},
        ],
    },
    {
        "module": "suppliers",
        "resource": "suppliers",
        "label": "Suppliers",
        "icon": "suppliers",
        "actions": [
            {"action": "view", "key": "page_suppliers", "label": "Open suppliers"},
            {"action": "view_directory", "key": "tab_suppliers_directory", "label": "Suppliers tab"},
            {"action": "view_purchases", "key": "tab_suppliers_purchases", "label": "Purchase orders tab"},
        ],
    },
]


ROLE_DEFINITIONS = {
    "cashier": {
        "label": "Cashier",
        "description": "POS terminal access with checkout actions.",
        "permissions": {
            "page_pos",
            "action_pos_create_sale",
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
            "action_pos_create_sale",
            "action_pos_discount",
            "action_pos_settle_later",
            "action_pos_refund",
            "page_b2b",
            "tab_b2b_clients",
            "tab_b2b_invoices",
            "tab_b2b_consignment",
            "action_b2b_clients_create",
            "action_b2b_clients_update",
            "action_b2b_clients_delete",
            "action_b2b_invoices_create",
            "action_b2b_invoices_update",
            "action_b2b_delete",
            "action_b2b_collect",
            "action_b2b_refund",
            "action_b2b_consignment_settle",
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
            "tab_suppliers_directory",
            "tab_suppliers_purchases",
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
    for entry in PERMISSION_MATRIX:
        for action in entry["actions"]:
            yield action["key"]


KNOWN_PERMISSIONS = set(iter_known_permissions())


def get_role_permissions(role: str | None) -> set[str]:
    return set(ROLE_DEFINITIONS.get(role or "", {}).get("permissions", set()))


def is_known_permission(permission: str) -> bool:
    return permission == "*" or permission in KNOWN_PERMISSIONS


def get_permission_key(module: str, resource: str, action: str) -> str:
    for entry in PERMISSION_MATRIX:
        if entry["module"] != module or entry["resource"] != resource:
            continue
        for action_entry in entry["actions"]:
            if action_entry["action"] == action:
                return action_entry["key"]
    raise KeyError(f"Unknown permission action: {module}.{resource}.{action}")


def _build_page_catalog() -> list[dict]:
    pages: dict[str, dict] = {}
    for entry in PERMISSION_MATRIX:
        page_key = None
        page_label = entry["label"]
        icon = entry["icon"]
        children = []
        for action in entry["actions"]:
            if action["action"] == "view" and action["key"].startswith("page_"):
                page_key = action["key"]
                page_label = entry["label"]
            else:
                children.append({"key": action["key"], "label": action["label"]})
        if page_key is None:
            continue
        page_entry = pages.setdefault(
            page_key,
            {"key": page_key, "label": page_label, "icon": icon, "actions": []},
        )
        page_entry["actions"].extend(children)

    for page_entry in pages.values():
        deduped = {action["key"]: action for action in page_entry["actions"]}
        page_entry["actions"] = list(deduped.values())
    return list(pages.values())


PERMISSION_PAGES = _build_page_catalog()


def get_permission_catalog() -> dict:
    role_permissions = {
        role_key: set(role_data["permissions"])
        for role_key, role_data in ROLE_DEFINITIONS.items()
    }
    matrix = []
    for entry in PERMISSION_MATRIX:
        actions = []
        for action in entry["actions"]:
            allowed_roles = [
                role_key
                for role_key, permissions in role_permissions.items()
                if "*" in permissions or action["key"] in permissions
            ]
            actions.append(
                {
                    "action": action["action"],
                    "key": action["key"],
                    "label": action["label"],
                    "roles": allowed_roles,
                }
            )
        matrix.append(
            {
                "module": entry["module"],
                "resource": entry["resource"],
                "label": entry["label"],
                "icon": entry["icon"],
                "actions": actions,
            }
        )

    grouped_roles: dict[str, list[str]] = defaultdict(list)
    for entry in matrix:
        for action in entry["actions"]:
            grouped_roles[action["key"]] = list(action["roles"])

    return {
        "pages": PERMISSION_PAGES,
        "matrix": matrix,
        "roles": [
            {
                "key": role_key,
                "label": role_data["label"],
                "description": role_data["description"],
                "permissions": sorted(role_data["permissions"]),
            }
            for role_key, role_data in ROLE_DEFINITIONS.items()
        ],
        "role_access": grouped_roles,
    }
