from collections.abc import AsyncGenerator
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

import app.app_factory as app_factory
from app.app_factory import create_app
from app.core import security
from app.core.permissions import serialize_permission_overrides, serialize_permissions
from app.database import get_async_session
from app.models.user import User
from app.routers.users import delete_user, get_users, update_user
from app.schemas.user import UserUpdate


class FakeUsersSession:
    def __init__(self) -> None:
        self.added = []
        self.commits = 0

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1


class FakeScalarResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value if isinstance(self._value, list) else []


class FakeUsersCrudSession:
    def __init__(self, users: list[User]) -> None:
        self.users = users
        self.added = []
        self.commits = 0

    async def execute(self, statement):
        compiled = statement.compile()
        criteria = {}
        for expr in statement._where_criteria:
            column_name = getattr(getattr(expr, "left", None), "name", None)
            if column_name is None:
                continue
            right = getattr(expr, "right", None)
            value = getattr(right, "value", None)
            if value is None and hasattr(right, "key"):
                value = compiled.params.get(right.key)
            criteria[column_name] = value

        if not criteria:
            return FakeScalarResult(list(self.users))

        for user in self.users:
            matched = True
            for field, expected in criteria.items():
                if field == "id" and statement.column_descriptions[0]["entity"] is User:
                    if getattr(user, field) != expected:
                        matched = False
                elif field == "email":
                    if getattr(user, field) != expected:
                        matched = False
                elif field == "id":
                    if getattr(user, field) == expected:
                        matched = False
            if matched:
                return FakeScalarResult(user)
        return FakeScalarResult(None)

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _obj) -> None:
        return None


def _make_client(*, current_user=None, optional_user=None) -> tuple[TestClient, FakeUsersSession]:
    fake_db = FakeUsersSession()

    async def override_session() -> AsyncGenerator[FakeUsersSession, None]:
        yield fake_db

    async def noop() -> None:
        return None

    app_factory.configure_logging = lambda: None
    app_factory.configure_monitoring = lambda: None
    app_factory.verify_migration_status = noop

    app = create_app()
    app.dependency_overrides[get_async_session] = override_session

    if current_user is not None:
        async def override_current_user():
            return current_user

        app.dependency_overrides[security.get_current_user] = override_current_user

    if optional_user is not None:
        async def override_optional_user():
            return optional_user

        app.dependency_overrides[security.get_optional_current_user] = override_optional_user

    return TestClient(app), fake_db


def test_change_password_uses_shared_current_user_dependency() -> None:
    user = SimpleNamespace(
        id=7,
        name="Cookie User",
        role="manager",
        password=security.hash_password("OldPassword123"),
        is_active=True,
    )
    client, fake_db = _make_client(current_user=user)

    response = client.post(
        "/users/api/change-password",
        json={
            "old_password": "OldPassword123",
            "new_password": "NewPassword123",
            "confirm_new_password": "NewPassword123",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert security.verify_password("NewPassword123", user.password) is True
    assert fake_db.commits == 2


def test_change_password_rejects_wrong_current_password() -> None:
    user = SimpleNamespace(
        id=8,
        name="Cookie User",
        role="manager",
        password=security.hash_password("OldPassword123"),
        is_active=True,
    )
    client, _fake_db = _make_client(current_user=user)

    response = client.post(
        "/users/api/change-password",
        json={
            "old_password": "WrongPassword123",
            "new_password": "NewPassword123",
            "confirm_new_password": "NewPassword123",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Current password is incorrect"


def test_change_password_rejects_confirmation_mismatch() -> None:
    user = SimpleNamespace(
        id=9,
        name="Cookie User",
        role="manager",
        password=security.hash_password("OldPassword123"),
        is_active=True,
    )
    client, _fake_db = _make_client(current_user=user)

    response = client.post(
        "/users/api/change-password",
        json={
            "old_password": "OldPassword123",
            "new_password": "NewPassword123",
            "confirm_new_password": "DifferentPassword123",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "New password and confirmation do not match"


def test_password_page_is_available_to_authenticated_user() -> None:
    user = SimpleNamespace(
        id=10,
        name="Cookie User",
        email="cookie@example.com",
        role="manager",
        password=security.hash_password("OldPassword123"),
        is_active=True,
    )
    client, _fake_db = _make_client(current_user=user)

    response = client.get("/users/password")

    assert response.status_code == 200
    assert "Change Password" in response.text
    assert "Current Password" in response.text


def test_log_action_uses_optional_shared_auth_dependency() -> None:
    user = SimpleNamespace(id=9, name="Cookie Logger", role="admin", is_active=True)
    client, fake_db = _make_client(optional_user=user)

    response = client.post(
        "/users/api/log",
        json={"action": "PING", "module": "TESTS", "description": "shared auth"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert len(fake_db.added) == 1
    assert fake_db.added[0].user_id == user.id


def test_non_admin_cannot_update_user_permissions() -> None:
    user = SimpleNamespace(
        id=11,
        name="Manager User",
        email="manager@example.com",
        role="manager",
        is_active=True,
    )
    client, _fake_db = _make_client(current_user=user)

    response = client.put(
        "/users/api/users/12",
        json={"permissions": "page_dashboard"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Admin access required"


def test_update_user_persists_custom_permissions_for_selected_role() -> None:
    user = User(
        id=12,
        name="Permissions User",
        email="perm@example.com",
        password="hashed",
        role="cashier",
        is_active=True,
        permissions=None,
    )
    fake_db = FakeUsersCrudSession([user])
    admin = SimpleNamespace(id=1, name="Admin", role="admin")

    result = __import__("asyncio").run(
        update_user(
            12,
            UserUpdate(
                role="viewer",
                permissions="page_dashboard,page_reports,tab_reports_sales,page_pos",
            ),
            db=fake_db,
            admin=admin,
        )
    )

    assert result["ok"] is True
    assert user.role == "viewer"
    assert user.permissions == serialize_permission_overrides(
        "viewer",
        ["page_dashboard", "page_reports", "tab_reports_sales", "page_pos"],
    )
    assert result["permissions"] == serialize_permissions(["page_dashboard", "page_reports", "tab_reports_sales", "page_pos"])


def test_update_user_removes_role_default_permission_and_reopen_matches_saved_state() -> None:
    user = User(
        id=13,
        name="Permissions User",
        email="perm2@example.com",
        password="hashed",
        role="viewer",
        is_active=True,
        permissions=None,
    )
    fake_db = FakeUsersCrudSession([user])
    admin = SimpleNamespace(id=1, name="Admin", role="admin")
    final_permissions = "page_reports,tab_reports_sales,tab_reports_pl,tab_reports_inventory,tab_reports_transactions"

    result = __import__("asyncio").run(
        update_user(
            13,
            UserUpdate(
                role="viewer",
                permissions=final_permissions,
            ),
            db=fake_db,
            admin=admin,
        )
    )
    users = __import__("asyncio").run(get_users(db=fake_db, admin=admin))

    assert result["ok"] is True
    assert user.permissions == serialize_permission_overrides("viewer", final_permissions.split(","))
    assert result["permissions"] == serialize_permissions(final_permissions.split(","))
    reopened = next(row for row in users if row["id"] == 13)
    assert reopened["permissions"] == serialize_permissions(final_permissions.split(","))


def test_delete_user_deactivates_user() -> None:
    admin = User(id=1, name="Admin", email="admin@example.com", password="x", role="admin", is_active=True)
    target = User(id=2, name="Cashier", email="cashier@example.com", password="x", role="cashier", is_active=True)
    fake_db = FakeUsersCrudSession([admin, target])

    result = __import__("asyncio").run(delete_user(2, db=fake_db, admin=admin))

    assert result == {"ok": True}
    assert target.is_active is False


def test_get_users_marks_only_admin_as_not_deletable() -> None:
    admin = User(id=1, name="Admin", email="admin@example.com", password="x", role="admin", is_active=True)
    cashier = User(id=2, name="Cashier", email="cashier@example.com", password="x", role="cashier", is_active=True)
    fake_db = FakeUsersCrudSession([admin, cashier])

    result = __import__("asyncio").run(get_users(db=fake_db, admin=admin))

    by_id = {row["id"]: row for row in result}
    assert by_id[1]["can_delete"] is False
    assert by_id[2]["can_delete"] is True
