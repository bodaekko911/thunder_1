import asyncio
from collections.abc import AsyncGenerator
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

import app.app_factory as app_factory
import app.routers.dashboard as dashboard_router
import app.services.copilot.engine as copilot_engine
from app.app_factory import create_app
from app.core import security
from app.database import get_async_session
from app.models.assistant import AssistantMessage, AssistantSession
from app.services.dashboard_assistant_service import (
    answer_dashboard_question,
    parse_dashboard_question,
)


class FakePermissionSession:
    def __init__(self) -> None:
        self.logged = []
        self.commits = 0

    def add(self, obj) -> None:
        self.logged.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1
        return None

    async def rollback(self) -> None:
        return None

    async def execute(self, _statement):
        return FakeDashboardScalarResult(None)


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


class FakeAssistantSession:
    def __init__(
        self,
        *,
        last_intent: str | None = None,
        last_date_from=None,
        last_date_to=None,
        last_entity_ids=None,
        last_comparison_baseline: str | None = None,
    ) -> None:
        self.last_intent = last_intent
        self.last_date_from = last_date_from
        self.last_date_to = last_date_to
        self.last_comparison_baseline = last_comparison_baseline
        self._last_entity_ids = last_entity_ids or []

    def get_last_entity_ids(self):
        return list(self._last_entity_ids)


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
    assert parse_dashboard_question("sales by period").intent == "sales_by_period"
    assert parse_dashboard_question("overdue customers").intent == "overdue_customers"
    assert parse_dashboard_question("customer balance for acme").intent == "customer_balance"
    assert parse_dashboard_question("product details for olive oil").intent == "product_details"
    assert parse_dashboard_question("stock levels").intent == "stock_levels"
    assert parse_dashboard_question("expense breakdown").intent == "expense_breakdown"
    assert parse_dashboard_question("profit/loss summary").intent == "profit_loss_summary"
    assert parse_dashboard_question("top products").intent == "top_products"
    assert parse_dashboard_question("low-stock items").intent == "low_stock"
    assert parse_dashboard_question("expenses this month").intent == "expenses_month"
    assert parse_dashboard_question("unpaid invoices").intent == "unpaid_invoices"


def test_copilot_engine_uses_internal_provider() -> None:
    provider = copilot_engine._build_provider()

    assert provider.__class__.__name__ == "InternalCopilotProvider"


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
    assert result["parameters"] == {}
    assert result["result"] is None
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


def test_answer_dashboard_question_fallback_includes_supported_scope_message() -> None:
    user = SimpleNamespace(id=1, name="Admin", role="admin", permissions="", is_active=True)

    result = asyncio.run(
        answer_dashboard_question(
            FakePermissionSession(),
            question="how many deliveries were made this quarter?",
            current_user=user,
        )
    )

    assert result["supported"] is False
    assert "deterministic ERP questions" in result["message"]


def test_answer_dashboard_question_records_minimal_memory(monkeypatch: pytest.MonkeyPatch) -> None:
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
    fake_db = FakePermissionSession()
    user = SimpleNamespace(id=7, name="Admin", role="admin", permissions="", is_active=True)

    result = asyncio.run(
        answer_dashboard_question(
            fake_db,
            question="today's sales",
            current_user=user,
        )
    )

    assert result["supported"] is True
    sessions = [entry for entry in fake_db.logged if isinstance(entry, AssistantSession)]
    messages = [entry for entry in fake_db.logged if isinstance(entry, AssistantMessage)]
    assert sessions
    assert len(messages) == 2
    assert fake_db.commits >= 1


@pytest.mark.parametrize(
    ("question", "tool_result", "expected_intent", "message_fragment"),
    [
        (
            "sales by period",
            {"period": "daily", "date_from": "2026-04-01", "date_to": "2026-04-16", "data": []},
            "sales_by_period",
            "daily",
        ),
        (
            "overdue customers",
            {"count": 2, "total_overdue_amount": 4200.0, "customers": []},
            "overdue_customers",
            "2 overdue customers",
        ),
        (
            "customer balance for acme",
            {"query": "acme", "selected": {"name": "Acme", "outstanding": 3200.0, "open_invoice_count": 3}},
            "customer_balance",
            "3200.00 outstanding",
        ),
        (
            "product details for olive oil",
            {"query": "olive oil", "selected": {"name": "Olive Oil", "sku": "OOL-1", "price": 150.0, "stock": 12.0}},
            "product_details",
            "Olive Oil (OOL-1)",
        ),
        (
            "stock levels",
            {"count": 4, "items": []},
            "stock_levels",
            "4 stock records",
        ),
        (
            "expense breakdown",
            {"month": "2026-04", "total": 8000.0, "breakdown": []},
            "expense_breakdown",
            "2026-04 totals 8000.00",
        ),
        (
            "profit/loss summary",
            {"revenue": 10000.0, "expenses": 7500.0, "gross_profit": 2500.0},
            "profit_loss_summary",
            "gross profit is 2500.00",
        ),
    ],
)
def test_answer_dashboard_question_supports_phase3_toolset(
    monkeypatch: pytest.MonkeyPatch,
    question: str,
    tool_result: dict,
    expected_intent: str,
    message_fragment: str,
) -> None:
    import app.services.copilot.providers.internal as internal_provider

    async def fake_execute_tool(db, *, current_user, name, input_data):
        return tool_result

    monkeypatch.setattr(internal_provider, "execute_tool", fake_execute_tool)
    fake_db = FakePermissionSession()
    user = SimpleNamespace(
        id=1,
        name="Admin",
        role="admin",
        permissions="page_dashboard,page_b2b,page_products,page_inventory,page_accounting",
        is_active=True,
    )

    result = asyncio.run(
        answer_dashboard_question(
            fake_db,
            question=question,
            current_user=user,
        )
    )

    assert result["supported"] is True
    assert result["intent"] == expected_intent
    assert message_fragment in result["message"]
    assert result["result"] == tool_result


def test_answer_dashboard_question_customer_balance_respects_permission_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.copilot.providers.internal as internal_provider

    async def fake_execute_tool(db, *, current_user, name, input_data):
        return {"error": "Permission denied: page_b2b is required to view customer balances."}

    monkeypatch.setattr(internal_provider, "execute_tool", fake_execute_tool)
    user = SimpleNamespace(id=3, name="Viewer", role="viewer", permissions="page_dashboard", is_active=True)

    result = asyncio.run(
        answer_dashboard_question(
            FakePermissionSession(),
            question="customer balance for acme",
            current_user=user,
        )
    )

    assert result["supported"] is True
    assert "Permission denied" in result["message"]


def test_followup_last_month_carries_date_range(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.copilot.providers.internal as internal_provider

    captured = {}

    async def fake_get_latest_session(db, *, user_id: int, channel: str = "dashboard"):
        return FakeAssistantSession(
            last_intent="profit_loss_summary",
            last_date_from=date(2026, 4, 1),
            last_date_to=date(2026, 4, 16),
        )

    async def fake_execute_tool(db, *, current_user, name, input_data):
        captured["name"] = name
        captured["input_data"] = dict(input_data)
        return {
            "date_from": input_data["date_from"],
            "date_to": input_data["date_to"],
            "revenue": 1000.0,
            "expenses": 700.0,
            "gross_profit": 300.0,
        }

    monkeypatch.setattr(internal_provider, "get_latest_session", fake_get_latest_session)
    monkeypatch.setattr(internal_provider, "execute_tool", fake_execute_tool)

    result = asyncio.run(
        answer_dashboard_question(
            FakePermissionSession(),
            question="what about last month?",
            current_user=SimpleNamespace(id=1, name="Admin", role="admin", permissions="page_accounting", is_active=True),
        )
    )

    assert result["supported"] is True
    assert result["intent"] == "profit_loss_summary"
    assert captured["name"] == "get_profit_loss_summary"
    assert captured["input_data"]["date_from"] == "2026-03-01"
    assert captured["input_data"]["date_to"] == "2026-03-16"


def test_followup_compare_previous_week_uses_comparison_ranges(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.copilot.providers.internal as internal_provider

    calls = []

    async def fake_get_latest_session(db, *, user_id: int, channel: str = "dashboard"):
        return FakeAssistantSession(
            last_intent="sales_by_period",
            last_date_from=date(2026, 4, 10),
            last_date_to=date(2026, 4, 16),
            last_comparison_baseline="daily",
        )

    async def fake_execute_tool(db, *, current_user, name, input_data):
        calls.append((name, dict(input_data)))
        return {
            "period": input_data.get("period", "daily"),
            "date_from": input_data["date_from"],
            "date_to": input_data["date_to"],
            "data": [],
        }

    monkeypatch.setattr(internal_provider, "get_latest_session", fake_get_latest_session)
    monkeypatch.setattr(internal_provider, "execute_tool", fake_execute_tool)

    result = asyncio.run(
        answer_dashboard_question(
            FakePermissionSession(),
            question="compare that to previous week",
            current_user=SimpleNamespace(id=1, name="Admin", role="admin", permissions="page_dashboard", is_active=True),
        )
    )

    assert result["supported"] is True
    assert result["intent"] == "sales_by_period"
    assert len(calls) == 2
    assert calls[0][1]["date_from"] == "2026-04-10"
    assert calls[0][1]["date_to"] == "2026-04-16"
    assert calls[1][1]["date_from"] == "2026-04-03"
    assert calls[1][1]["date_to"] == "2026-04-09"
    assert "previous week" in result["message"]


def test_followup_entity_carryover_for_that_item(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.copilot.providers.internal as internal_provider

    captured = {}

    async def fake_get_latest_session(db, *, user_id: int, channel: str = "dashboard"):
        return FakeAssistantSession(
            last_intent="stock_levels",
            last_entity_ids=[42],
        )

    async def fake_execute_tool(db, *, current_user, name, input_data):
        captured["name"] = name
        captured["input_data"] = dict(input_data)
        return {
            "selected": {"product_id": 42, "name": "Olive Oil", "sku": "OOL-1", "price": 150.0, "stock": 12.0},
            "matches": [],
            "count": 1,
            "query": "",
        }

    monkeypatch.setattr(internal_provider, "get_latest_session", fake_get_latest_session)
    monkeypatch.setattr(internal_provider, "execute_tool", fake_execute_tool)

    result = asyncio.run(
        answer_dashboard_question(
            FakePermissionSession(),
            question="show me the product details for that item",
            current_user=SimpleNamespace(id=1, name="Admin", role="admin", permissions="page_products", is_active=True),
        )
    )

    assert result["supported"] is True
    assert result["intent"] == "product_details"
    assert captured["name"] == "get_product_details"
    assert captured["input_data"]["product_id"] == 42


def test_followup_customers_caused_most_of_it_uses_context(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.copilot.providers.internal as internal_provider

    async def fake_get_latest_session(db, *, user_id: int, channel: str = "dashboard"):
        return FakeAssistantSession(last_intent="unpaid_invoices")

    async def fake_execute_tool(db, *, current_user, name, input_data):
        assert name == "get_overdue_customers"
        return {"count": 1, "total_overdue_amount": 900.0, "customers": [{"client_id": 5, "name": "Acme", "overdue_amount": 900.0}]}

    monkeypatch.setattr(internal_provider, "get_latest_session", fake_get_latest_session)
    monkeypatch.setattr(internal_provider, "execute_tool", fake_execute_tool)

    result = asyncio.run(
        answer_dashboard_question(
            FakePermissionSession(),
            question="which customers caused most of it?",
            current_user=SimpleNamespace(id=1, name="Admin", role="admin", permissions="page_b2b", is_active=True),
        )
    )

    assert result["supported"] is True
    assert result["intent"] == "overdue_customers"


def test_followup_without_context_falls_back_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.copilot.providers.internal as internal_provider

    async def fake_get_latest_session(db, *, user_id: int, channel: str = "dashboard"):
        return None

    monkeypatch.setattr(internal_provider, "get_latest_session", fake_get_latest_session)

    result = asyncio.run(
        answer_dashboard_question(
            FakePermissionSession(),
            question="what about last month?",
            current_user=SimpleNamespace(id=1, name="Admin", role="admin", permissions="page_dashboard", is_active=True),
        )
    )

    assert result["supported"] is False
    assert "do not have enough recent assistant context" in result["message"].lower()


def test_followup_with_ambiguous_context_falls_back_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.copilot.providers.internal as internal_provider

    async def fake_get_latest_session(db, *, user_id: int, channel: str = "dashboard"):
        return FakeAssistantSession(last_intent="stock_levels")

    monkeypatch.setattr(internal_provider, "get_latest_session", fake_get_latest_session)

    result = asyncio.run(
        answer_dashboard_question(
            FakePermissionSession(),
            question="compare that to previous week",
            current_user=SimpleNamespace(id=1, name="Admin", role="admin", permissions="page_dashboard", is_active=True),
        )
    )

    assert result["supported"] is False
    assert "do not have enough recent assistant context" in result["message"].lower()


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
