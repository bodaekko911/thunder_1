import asyncio
import json
from collections.abc import AsyncGenerator
from types import SimpleNamespace

from fastapi.testclient import TestClient

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

import app.app_factory as app_factory
import app.routers.dashboard as dashboard_router
import app.services.copilot.providers.cloud_llm as cloud_provider
from app.app_factory import create_app
from app.core.config import settings
from app.core import security
from app.database import get_async_session


class FakeDashboardSession:
    async def execute(self, _statement):
        raise AssertionError("Route-level validation tests should not hit the database")


def _make_client() -> TestClient:
    fake_db = FakeDashboardSession()
    user = SimpleNamespace(
        id=99,
        name="Admin",
        role="admin",
        permissions="page_dashboard",
        is_active=True,
    )

    async def override_session() -> AsyncGenerator[FakeDashboardSession, None]:
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


def test_assistant_question_length_validation() -> None:
    with _make_client() as client:
        response = client.post(
            "/dashboard/assistant/ask",
            json={"question": "x" * (settings.ASSISTANT_MAX_QUESTION_CHARS + 1)},
        )

    assert response.status_code == 422
    assert "question" in response.text.lower()


def test_dashboard_context_is_trimmed_before_answer(monkeypatch) -> None:
    captured = {}

    async def fake_answer(db, *, question, current_user, dashboard_context=None):
        captured["question"] = question
        captured["dashboard_context"] = dashboard_context
        return {"type": "text", "content": "ok"}

    monkeypatch.setattr("app.services.copilot.engine.answer_question", fake_answer)
    monkeypatch.setattr(dashboard_router, "_get_assistant_redis_client", lambda: (_ for _ in ()).throw(RuntimeError("no redis")))
    dashboard_router._reset_assistant_rate_limit_state()

    large_context = {
        "range": "mtd",
        "start": "2026-04-01",
        "end": "2026-04-25",
        "numbers": {
            "sales": {"value": 100, "delta_pct": 5.2, "ignored": "x" * 1000},
            "clients_owe": {"value": 50, "overdue_count": 2, "ignored": "y" * 1000},
        },
        "panels": {
            "top_products_by_revenue": [{"name": f"P{i}", "qty": i, "revenue": i * 10, "ignored": "z"} for i in range(12)],
            "recent_activity": [{"invoice_number": str(i), "customer": "Acme", "total": i, "type": "sale", "time_relative": "now", "ignored": "z"} for i in range(12)],
        },
        "_private": {"secret": "ignore me"},
    }

    with _make_client() as client:
        response = client.post(
            "/dashboard/assistant/ask",
            json={"question": "top products", "dashboard_context": large_context},
        )

    assert response.status_code == 200
    assert captured["question"] == "top products"
    trimmed = captured["dashboard_context"]
    assert "_private" not in trimmed
    assert len(trimmed["panels"]["top_products_by_revenue"]) == settings.ASSISTANT_CONTEXT_LIST_LIMIT
    assert len(trimmed["panels"]["recent_activity"]) == settings.ASSISTANT_CONTEXT_LIST_LIMIT
    assert "ignored" not in trimmed["numbers"]["sales"]
    assert len(json.dumps(trimmed, default=str)) <= settings.ASSISTANT_MAX_CONTEXT_CHARS


def test_assistant_rate_limiting_returns_429(monkeypatch) -> None:
    async def fake_answer(db, *, question, current_user, dashboard_context=None):
        return {"type": "text", "content": "ok"}

    monkeypatch.setattr("app.services.copilot.engine.answer_question", fake_answer)
    monkeypatch.setattr(dashboard_router, "_get_assistant_redis_client", lambda: (_ for _ in ()).throw(RuntimeError("no redis")))
    monkeypatch.setattr(dashboard_router.settings, "ASSISTANT_RATE_LIMIT_REQUESTS", 1, raising=False)
    monkeypatch.setattr(dashboard_router.settings, "ASSISTANT_RATE_LIMIT_WINDOW_SECONDS", 60, raising=False)
    dashboard_router._reset_assistant_rate_limit_state()

    with _make_client() as client:
        first = client.post("/dashboard/assistant/ask", json={"question": "hello"})
        second = client.post("/dashboard/assistant/ask", json={"question": "hello again"})

    assert first.status_code == 200
    assert second.status_code == 429
    assert "rate limit" in second.text.lower()


def test_low_stock_helper_uses_sql_filtered_limited_query() -> None:
    class FakeResult:
        def all(self):
            return [SimpleNamespace(name="Olive Oil", stock=2)]

    class FakeDB:
        async def execute(self, statement):
            sql = str(statement).lower()
            assert "from products" in sql
            assert "coalesce" in sql
            assert "products.stock <=" in sql
            assert "limit" in sql
            return FakeResult()

    result = asyncio.run(cloud_provider._fetch_low_stock_inventory(FakeDB()))

    assert result == [{"name": "Olive Oil", "stock": 2.0}]
