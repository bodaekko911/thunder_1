from types import SimpleNamespace

from app.core.permissions import has_permission, normalize_permissions


def test_normalize_permissions_filters_unknown_values() -> None:
    permissions = normalize_permissions("page_dashboard, unknown_permission, page_pos")

    assert "page_dashboard" in permissions
    assert "page_pos" in permissions
    assert "unknown_permission" not in permissions


def test_has_permission_respects_effective_permissions() -> None:
    user = SimpleNamespace(role="cashier", permissions="page_dashboard")

    assert has_permission(user, "page_dashboard") is True
    assert has_permission(user, "page_accounting") is False
