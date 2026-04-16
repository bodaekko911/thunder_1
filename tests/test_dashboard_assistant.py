import asyncio
from collections.abc import AsyncGenerator
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

import app.app_factory as app_factory
import app.routers.dashboard as dashboard_router
from app.app_factory import create_app
from app.core import security
from app.database import get_async_session
from app.services.dashboard_assistant_service import (
    answer_dashboard_question,
    parse_dashboard_question,
)


class FakePermissionSession:
    def __init__(self) -> None:
        self.logged = []

    def add(self, obj) -> None:
        self.logged.append(obj)

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class FakeDashboardScalarResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar(self):
        return self._value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value if isinstance(self._value, list) else []


class FakeDashboardDataSession:
    async def execute(self, statement):
        entity = statement.column_descriptions[0].get("entity")
        if entity is not None:
            return FakeDashboardScalarResult([])
        return FakeDashboardScalarResult(0)


def _make_client(user, fake_db) -> TestClient:
    async def override_session() -> AsyncGenerator[FakePermissionSession, None]:
        yield fake_db

    async def override_user():
        return user

    async def noop() -> None:
        return None

    app_factory.configure_logging = lambda: None
    app_factory.configure_monitoring = lambda: None
    app_factory.verify_migration_status = noop

    app = create_app()
    app.dependency_overrides[get_async_session] = override_session
    app.dependency_overrides[security.get_current_user] = override_user
    return TestClient(app)


def test_parse_dashboard_question_maps_supported_phrases() -> None:
    assert parse_dashboard_question("today's sales").intent == "sales_today"
    assert parse_dashboard_question("top products").intent == "top_products"
    assert parse_dashboard_question("low-stock items").intent == "low_stock"
    assert parse_dashboard_question("expenses this month").intent == "expenses_month"
    assert parse_dashboard_question("unpaid invoices").intent == "unpaid_invoices"


def test_answer_dashboard_question_returns_helpful_fallback_for_unsupported_question() -> None:
    user = SimpleNamespace(id=1, name="Admin", role="admin", permissions="", is_active=True)

    result = asyncio.run(
        answer_dashboard_question(
            FakePermissionSession(),
            question="how many customers visited last quarter?",
            current_user=user,
        )
    )

    assert result["supported"] is False
    assert result["intent"] is None
    assert "today's sales" in result["message"]


def test_answer_dashboard_question_returns_sales_today_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_dashboard_data(*, db):
        return {
            "total_today": 1500.0,
            "pos_today": 900.0,
            "b2b_today": 600.0,
            "ref_today": 50.0,
            "top_products": [],
            "low_stock": [],
            "low_stock_count": 0,
        }

    monkeypatch.setattr(dashboard_router, "dashboard_data", fake_dashboard_data)
    user = SimpleNamespace(id=1, name="Admin", role="admin", permissions="", is_active=True)

    result = asyncio.run(
        answer_dashboard_question(
            FakePermissionSession(),
            question="sales today",
            current_user=user,
        )
    )

    assert result["supported"] is True
    assert result["intent"] == "sales_today"
    assert result["result"]["total_sales"] == 1500.0
    assert result["result"]["pos_sales"] == 900.0
    assert result["result"]["b2b_sales"] == 600.0


def test_answer_dashboard_question_requires_accounting_permission_for_expenses() -> None:
    fake_db = FakePermissionSession()
    user = SimpleNamespace(
        id=4,
        name="Dashboard Viewer",
        role="viewer",
        permissions="page_dashboard",
        is_active=True,
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            answer_dashboard_question(
                fake_db,
                question="expenses this month",
                current_user=user,
            )
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Permission denied: page_accounting"
    assert any(log.action == "PERMISSION_DENIED" and log.ref_id == "page_accounting" for log in fake_db.logged)


def test_dashboard_assistant_endpoint_returns_structured_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_answer(db, *, question, current_user):
        assert question == "top products"
        assert current_user.name == "Admin"
        return {
            "supported": True,
            "intent": "top_products",
            "parameters": {"limit": 10},
            "result": {"items": [{"name": "Olives", "qty": 5, "revenue": 250.0}], "count": 1},
            "message": "Here are the current top products for this month.",
        }

    monkeypatch.setattr(dashboard_router, "answer_dashboard_question", fake_answer)

    user = SimpleNamespace(
        id=1,
        name="Admin",
        role="admin",
        permissions="",
        is_active=True,
    )
    fake_db = FakePermissionSession()

    with _make_client(user, fake_db) as client:
        response = client.post("/dashboard/assistant", json={"question": "top products"})

    assert response.status_code == 200
    assert response.json() == {
        "supported": True,
        "intent": "top_products",
        "parameters": {"limit": 10},
        "result": {"items": [{"name": "Olives", "qty": 5, "revenue": 250.0}], "count": 1},
        "message": "Here are the current top products for this month.",
    }


def test_dashboard_assistant_endpoint_is_csrf_exempt_for_session_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_answer(db, *, question, current_user):
        return {
            "supported": True,
            "intent": "sales_today",
            "parameters": {},
            "result": {"total_sales": 100.0, "pos_sales": 100.0, "b2b_sales": 0.0, "refunds": 0.0},
            "message": "Today's total sales are 100.00.",
        }

    monkeypatch.setattr(dashboard_router, "answer_dashboard_question", fake_answer)

    user = SimpleNamespace(
        id=1,
        name="Admin",
        role="admin",
        permissions="page_dashboard",
        is_active=True,
    )
    fake_db = FakePermissionSession()

    with _make_client(user, fake_db) as client:
        client.cookies.set("access_token", "session-cookie-token")
        response = client.post("/dashboard/assistant", json={"question": "today's sales"})

    assert response.status_code == 200
    assert response.json()["intent"] == "sales_today"


def test_answer_dashboard_question_fallback_includes_not_configured_message() -> None:
    """When ANTHROPIC_API_KEY is absent, unsupported questions surface 'AI assistant is not configured'."""
    user = SimpleNamespace(id=1, name="Admin", role="admin", permissions="", is_active=True)

    result = asyncio.run(
        answer_dashboard_question(
            FakePermissionSession(),
            question="how many deliveries were made this quarter?",
            current_user=user,
        )
    )

    assert result["supported"] is False
    assert "AI assistant is not configured" in result["message"]


def test_answer_dashboard_question_ai_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """When API key is configured, the Claude tool-use loop runs and returns a structured answer."""
    import app.services.dashboard_assistant_service as das

    # Patch settings so the AI path is taken
    fake_settings = SimpleNamespace(
        ANTHROPIC_API_KEY="test-key-abc",
        ANTHROPIC_MODEL="claude-test",
    )
    monkeypatch.setattr(das, "settings", fake_settings)

    # Stub tool execution so no real DB queries run
    async def fake_execute_tool(db, current_user, name, input_data):
        return {"total": 1500.0, "pos_sales": 900.0, "b2b_sales": 600.0, "refunds": 0.0, "net_pos": 900.0, "date_from": "2026-04-15", "date_to": "2026-04-15"}

    monkeypatch.setattr(das, "_execute_tool", fake_execute_tool)

    # Build minimal mock of AsyncAnthropic client
    tool_block = SimpleNamespace(
        type="tool_use",
        id="tu_1",
        name="get_sales_summary",
        input={"date_from": "2026-04-15", "date_to": "2026-04-15"},
    )
    text_block = SimpleNamespace(type="text", text="Today's total sales are 1500.00.")

    call_count = 0

    async def mock_create(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return SimpleNamespace(stop_reason="tool_use", content=[tool_block])
        return SimpleNamespace(stop_reason="end_turn", content=[text_block])

    mock_client = SimpleNamespace(messages=SimpleNamespace(create=mock_create))

    def mock_async_anthropic(api_key):
        return mock_client

    import anthropic
    monkeypatch.setattr(anthropic, "AsyncAnthropic", mock_async_anthropic)

    user = SimpleNamespace(id=1, name="Admin", role="admin", permissions="", is_active=True)

    result = asyncio.run(
        answer_dashboard_question(
            FakePermissionSession(),
            question="today's sales",
            current_user=user,
        )
    )

    assert result["supported"] is True
    assert result["intent"] == "get_sales_summary"
    assert "1500" in result["message"]
    assert call_count == 2


def test_answer_dashboard_question_ai_no_tool_call_returns_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    """When Claude answers directly without calling a tool, supported is False."""
    import app.services.dashboard_assistant_service as das

    fake_settings = SimpleNamespace(
        ANTHROPIC_API_KEY="test-key-abc",
        ANTHROPIC_MODEL="claude-test",
    )
    monkeypatch.setattr(das, "settings", fake_settings)

    text_block = SimpleNamespace(type="text", text="I don't know the answer to that question.")

    async def mock_create(**kwargs):
        return SimpleNamespace(stop_reason="end_turn", content=[text_block])

    mock_client = SimpleNamespace(messages=SimpleNamespace(create=mock_create))

    import anthropic
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda api_key: mock_client)

    user = SimpleNamespace(id=1, name="Admin", role="admin", permissions="", is_active=True)

    result = asyncio.run(
        answer_dashboard_question(
            FakePermissionSession(),
            question="what is the meaning of life?",
            current_user=user,
        )
    )

    assert result["supported"] is False
    assert result["intent"] is None
    assert "don't know" in result["message"]


def test_dashboard_data_includes_monthly_expenses(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_expense_summary(_db):
        return {
            "this_month": 4200.5,
            "last_month": 3150.25,
            "total_all": 10000.75,
            "breakdown": [],
        }

    monkeypatch.setattr(dashboard_router, "get_expense_summary", fake_expense_summary)

    payload = asyncio.run(dashboard_router.dashboard_data(db=FakeDashboardDataSession()))

    assert payload["expenses_month"] == 4200.5
    assert payload["expenses_last_month"] == 3150.25
