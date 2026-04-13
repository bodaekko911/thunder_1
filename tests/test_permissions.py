from types import SimpleNamespace

from app.core.permission_catalog import get_permission_catalog, get_permission_key
from app.core.permissions import has_action_permission, has_permission, normalize_permissions


def test_normalize_permissions_filters_unknown_values() -> None:
    permissions = normalize_permissions("page_dashboard, unknown_permission, page_pos")

    assert "page_dashboard" in permissions
    assert "page_pos" in permissions
    assert "unknown_permission" not in permissions


def test_has_permission_respects_effective_permissions() -> None:
    user = SimpleNamespace(role="cashier", permissions="page_dashboard")

    assert has_permission(user, "page_dashboard") is True
    assert has_permission(user, "page_accounting") is False


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
