import asyncio
from types import SimpleNamespace

import pytest

from app.bootstrap.init_data import (
    DEFAULT_EXPENSE_CATEGORIES,
    ensure_default_admin,
    ensure_default_expense_categories,
    main,
    run_bootstrap,
)
from app.core.security import verify_password
from app.models.accounting import Account
from app.models.expense import ExpenseCategory
from app.models.user import User


class FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeBootstrapSession:
    def __init__(self, *, users=None, accounts=None, categories=None):
        self.users = list(users or [])
        self.accounts = list(accounts or [])
        self.categories = list(categories or [])

    async def execute(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        criteria = {}
        compiled = statement.compile()
        for expr in statement._where_criteria:
            column_name = getattr(getattr(expr, "left", None), "name", None)
            if column_name is None:
                continue
            right = getattr(expr, "right", None)
            value = getattr(right, "value", None)
            if value is None and hasattr(right, "key"):
                value = compiled.params.get(right.key)
            criteria[column_name] = value

        if entity is User:
            pool = self.users
        elif entity is Account:
            pool = self.accounts
        else:
            pool = self.categories

        for item in pool:
            if all(getattr(item, key) == value for key, value in criteria.items()):
                return FakeScalarResult(item)
        return FakeScalarResult(None)

    def add(self, obj):
        if isinstance(obj, User):
            self.users.append(obj)
        elif isinstance(obj, Account):
            self.accounts.append(obj)
        elif isinstance(obj, ExpenseCategory):
            self.categories.append(obj)


def test_ensure_default_admin_creates_missing_user() -> None:
    fake_db = FakeBootstrapSession()

    created, password_reset = asyncio.run(
        ensure_default_admin(
            fake_db,
            name="Admin",
            email="admin@example.com",
            password="change-me-now",
        )
    )

    assert created is True
    assert password_reset is False
    assert len(fake_db.users) == 1
    assert fake_db.users[0].email == "admin@example.com"
    assert fake_db.users[0].password != "change-me-now"


def test_ensure_default_admin_is_idempotent() -> None:
    fake_db = FakeBootstrapSession(
        users=[
            User(
                name="Admin",
                email="admin@example.com",
                password="hashed",
                role="admin",
                is_active=True,
            )
        ]
    )

    created, password_reset = asyncio.run(
        ensure_default_admin(
            fake_db,
            name="Admin",
            email="admin@example.com",
            password="change-me-now",
        )
    )

    assert created is False
    assert password_reset is False
    assert len(fake_db.users) == 1


def test_ensure_default_admin_can_reset_existing_password() -> None:
    fake_db = FakeBootstrapSession(
        users=[
            User(
                name="Admin",
                email="admin@example.com",
                password="hashed",
                role="admin",
                is_active=True,
            )
        ]
    )

    created, password_reset = asyncio.run(
        ensure_default_admin(
            fake_db,
            name="Admin",
            email="admin@example.com",
            password="NewPassword123",
            reset_password=True,
        )
    )

    assert created is False
    assert password_reset is True
    assert verify_password("NewPassword123", fake_db.users[0].password) is True


def test_ensure_default_expense_categories_creates_missing_records() -> None:
    fake_db = FakeBootstrapSession()

    accounts_created, categories_created = asyncio.run(ensure_default_expense_categories(fake_db))

    assert accounts_created == len(DEFAULT_EXPENSE_CATEGORIES)
    assert categories_created == len(DEFAULT_EXPENSE_CATEGORIES)
    assert len(fake_db.accounts) == len(DEFAULT_EXPENSE_CATEGORIES)
    assert len(fake_db.categories) == len(DEFAULT_EXPENSE_CATEGORIES)


def test_ensure_default_expense_categories_is_idempotent() -> None:
    fake_db = FakeBootstrapSession(
        accounts=[
            Account(code=code, name=name, type="expense", balance=0)
            for code, name in DEFAULT_EXPENSE_CATEGORIES
        ],
        categories=[
            ExpenseCategory(name=name, account_code=code, is_active="1")
            for code, name in DEFAULT_EXPENSE_CATEGORIES
        ],
    )

    accounts_created, categories_created = asyncio.run(ensure_default_expense_categories(fake_db))

    assert accounts_created == 0
    assert categories_created == 0


def test_bootstrap_cli_requires_an_action() -> None:
    import sys

    original_argv = sys.argv[:]
    with pytest.raises(SystemExit):
        sys.argv = ["init_data.py"]
        main()
    sys.argv = original_argv


def test_bootstrap_cli_requires_yes_in_production(monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["init_data.py", "--admin"])
    monkeypatch.setattr("app.bootstrap.init_data.settings", SimpleNamespace(APP_ENV="production"))

    with pytest.raises(SystemExit):
        main()


def test_run_bootstrap_reports_password_reset() -> None:
    fake_db = FakeBootstrapSession(
        users=[
            User(
                name="Admin",
                email="admin@example.com",
                password="hashed",
                role="admin",
                is_active=True,
            )
        ]
    )

    class _Scope:
        async def __aenter__(self):
            return fake_db

        async def __aexit__(self, exc_type, exc, tb):
            return None

    def fake_session_scope():
        return _Scope()

    from app.bootstrap import init_data

    original_scope = init_data.session_scope
    init_data.session_scope = fake_session_scope
    try:
        summary = asyncio.run(
            run_bootstrap(
                create_admin=True,
                create_expense_categories=False,
                reset_admin_password=True,
            )
        )
    finally:
        init_data.session_scope = original_scope

    assert summary.admin_created is False
    assert summary.admin_password_reset is True
