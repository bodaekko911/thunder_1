from types import SimpleNamespace

from app.core.permission_catalog import get_permission_catalog, get_permission_key
from app.core.permissions import (
    get_custom_permissions,
    get_effective_permissions,
    has_action_permission,
    has_permission,
    normalize_permissions,
    serialize_permission_overrides,
)


def test_normalize_permissions_filters_unknown_values() -> None:
    permissions = normalize_permissions("page_dashboard, unknown_permission, page_pos")

    assert "page_dashboard" in permissions
    assert "page_pos" in permissions
    assert "unknown_permission" not in permissions


def test_has_permission_respects_effective_permissions() -> None:
    user = SimpleNamespace(role="cashier", permissions="page_dashboard")

    assert has_permission(user, "page_dashboard") is True
    assert has_permission(user, "page_accounting") is False


def test_legacy_page_permissions_expand_to_split_modules() -> None:
    accounting_user = SimpleNamespace(role="viewer", permissions="page_accounting")
    suppliers_user = SimpleNamespace(role="viewer", permissions="page_suppliers")

    assert has_permission(accounting_user, "page_expenses") is True
    assert has_permission(accounting_user, "action_expenses_update") is True
    assert has_permission(suppliers_user, "page_receive_products") is True
    assert has_permission(suppliers_user, "action_receive_products_create") is True


def test_get_custom_permissions_subtracts_expanded_role_permissions() -> None:
    custom_permissions = get_custom_permissions(
        "accountant",
        [
            "page_accounting",
            "page_expenses",
            "action_expenses_create",
            "action_expenses_update",
            "action_expenses_delete",
        ],
    )

    assert custom_permissions == set()


def test_serialize_permission_overrides_supports_removing_role_defaults() -> None:
    selected_permissions = [
        "page_reports",
        "tab_reports_sales",
        "tab_reports_pl",
        "tab_reports_inventory",
        "tab_reports_transactions",
    ]

    stored = serialize_permission_overrides("viewer", selected_permissions)

    assert "-page_dashboard" in stored.split(",")
    assert get_effective_permissions("viewer", stored) == set(selected_permissions)


def test_has_action_permission_uses_central_matrix() -> None:
    user = SimpleNamespace(role="cashier", permissions="page_dashboard")

    assert has_action_permission(user, "pos", "sales", "create") is True
    assert has_action_permission(user, "pos", "sales", "refund") is False


def test_permission_catalog_exposes_matrix_with_role_mappings() -> None:
    catalog = get_permission_catalog()

    refund_key = get_permission_key("pos", "sales", "refund")
    matrix_entry = next(item for item in catalog["matrix"] if item["module"] == "pos" and item["resource"] == "sales")
    refund_action = next(action for action in matrix_entry["actions"] if action["key"] == refund_key)

    assert refund_action["action"] == "refund"
    assert "manager" in refund_action["roles"]
    assert "cashier" not in refund_action["roles"]


def test_permission_catalog_pages_include_grouped_actions_and_aliases() -> None:
    catalog = get_permission_catalog()
    b2b_page = next(page for page in catalog["pages"] if page["key"] == "page_b2b")
    expenses_page = next(page for page in catalog["pages"] if page["key"] == "page_expenses")
    receive_page = next(page for page in catalog["pages"] if page["key"] == "page_receive_products")

    action_keys = {action["key"] for action in b2b_page["actions"]}
    expense_keys = {action["key"] for action in expenses_page["actions"]}
    receive_keys = {action["key"] for action in receive_page["actions"]}

    assert "action_b2b_delete" in action_keys
    assert "action_b2b_invoices_create" in action_keys
    assert expense_keys == {
        "action_expenses_create",
        "action_expenses_update",
        "action_expenses_delete",
    }
    assert receive_keys == {
        "action_receive_products_create",
        "action_receive_products_update",
        "action_receive_products_delete",
        "action_receive_products_export",
    }
    assert expenses_page["aliases"] == []
    assert receive_page["aliases"] == []


def test_permission_catalog_role_mappings_include_split_modules() -> None:
    catalog = get_permission_catalog()
    expenses_entry = next(
        item for item in catalog["matrix"] if item["module"] == "expenses" and item["resource"] == "expenses"
    )
    receive_entry = next(
        item
        for item in catalog["matrix"]
        if item["module"] == "receive_products" and item["resource"] == "receipts"
    )

    expense_edit = next(action for action in expenses_entry["actions"] if action["key"] == "action_expenses_update")
    receive_create = next(
        action for action in receive_entry["actions"] if action["key"] == "action_receive_products_create"
    )
    receive_update = next(
        action for action in receive_entry["actions"] if action["key"] == "action_receive_products_update"
    )
    receive_delete = next(
        action for action in receive_entry["actions"] if action["key"] == "action_receive_products_delete"
    )
    receive_export = next(
        action for action in receive_entry["actions"] if action["key"] == "action_receive_products_export"
    )

    assert "accountant" in expense_edit["roles"]
    assert "manager" in receive_create["roles"]
    assert "manager" in receive_update["roles"]
    assert "manager" in receive_delete["roles"]
    assert "manager" in receive_export["roles"]


def test_hr_clear_data_permission_is_admin_only_by_default() -> None:
    catalog = get_permission_catalog()
    hr_entry = next(item for item in catalog["matrix"] if item["module"] == "hr")
    clear_action = next(action for action in hr_entry["actions"] if action["key"] == "action_hr_clear_data")

    assert clear_action["action"] == "clear_hr_data"
    assert clear_action["label"] == "Clear all HR data"
    assert "admin" in clear_action["roles"]
    assert "hr" not in clear_action["roles"]
    assert "manager" not in clear_action["roles"]
    assert "accountant" not in clear_action["roles"]
    assert "viewer" not in clear_action["roles"]
    assert has_permission(SimpleNamespace(role="admin", permissions=""), "action_hr_clear_data") is True
    assert has_permission(SimpleNamespace(role="hr", permissions=""), "action_hr_clear_data") is False
