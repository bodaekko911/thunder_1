from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

from app.services.dashboard_briefing_service import build_briefing, build_lead_sentence, detect_overdue_b2b
from app.services.dashboard_summary_service import _utc_range


class FakeDB:
    async def rollback(self):
        return None


def test_empty_db_returns_no_sales_message(monkeypatch: pytest.MonkeyPatch):
    async def fake_sales(*_args, **_kwargs):
        return 0.0, 0

    async def fake_rule(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.services.dashboard_briefing_service._sales_and_transactions", fake_sales)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_overdue_b2b", fake_rule)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_out_of_stock_recent", fake_rule)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_low_stock", fake_rule)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_spoilage_spike", fake_rule)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_big_expense", fake_rule)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_stale_consignment", fake_rule)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_big_b2b_client", fake_rule)

    utc_s, utc_e = _utc_range(date(2026, 4, 20), date(2026, 4, 20))
    result = pytest.run(async_fn=build_briefing(FakeDB(), SimpleNamespace(id=1), "today", utc_s, utc_e)) if hasattr(pytest, "run") else None
    if result is None:
        import asyncio
        result = asyncio.run(build_briefing(FakeDB(), SimpleNamespace(id=1), "today", utc_s, utc_e))

    assert result["lead"] == "You haven't recorded any sales yet for this period."
    assert result["actions"] == []


def test_today_lead_uses_same_weekday_average(monkeypatch: pytest.MonkeyPatch):
    async def fake_sales(_db, _start, _end):
        return 10000.0, 47

    async def fake_average(_db, _day):
        return 8000.0

    monkeypatch.setattr("app.services.dashboard_briefing_service._sales_and_transactions", fake_sales)
    monkeypatch.setattr("app.services.dashboard_briefing_service._today_weekday_average", fake_average)

    utc_s, utc_e = _utc_range(date(2026, 4, 21), date(2026, 4, 21))
    import asyncio
    lead = asyncio.run(build_lead_sentence(FakeDB(), "today", utc_s, utc_e))
    assert "25% above your Tuesday average" in lead


def test_overdue_b2b_action_mentions_client_and_amount():
    class OverdueResult:
        def all(self):
            return [
                SimpleNamespace(
                    id=5,
                    client_id=42,
                    name="Acme Co",
                    total=12400,
                    amount_paid=0,
                    created_at=None,
                    due_date=date(2026, 3, 16),
                )
            ]

    class OverdueDB(FakeDB):
        async def execute(self, _stmt):
            return OverdueResult()

    import asyncio
    action = asyncio.run(detect_overdue_b2b(OverdueDB(), today=date(2026, 4, 20)))
    assert action is not None
    assert "Acme Co" in action["text"]
    assert "EGP 12,400" in action["text"]


def test_low_stock_action_phrase(monkeypatch: pytest.MonkeyPatch):
    async def fake_sales(*_args, **_kwargs):
        return 5000.0, 12

    async def overdue(*_args, **_kwargs):
        return None

    async def low_stock(*_args, **_kwargs):
        return {"priority": 80, "text": "Olive Oil 500ml is down to 3 units - usually sells 5/day.", "link": "/inventory/", "cta": "Reorder"}

    monkeypatch.setattr("app.services.dashboard_briefing_service._sales_and_transactions", fake_sales)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_overdue_b2b", overdue)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_out_of_stock_recent", overdue)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_low_stock", low_stock)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_spoilage_spike", overdue)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_big_expense", overdue)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_stale_consignment", overdue)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_big_b2b_client", overdue)

    utc_s, utc_e = _utc_range(date(2026, 4, 20), date(2026, 4, 20))
    import asyncio
    result = asyncio.run(build_briefing(FakeDB(), SimpleNamespace(id=1), "today", utc_s, utc_e))
    assert any("usually sells 5/day" in action["text"] for action in result["actions"])


def test_rule_failure_keeps_other_actions(monkeypatch: pytest.MonkeyPatch):
    async def fake_sales(*_args, **_kwargs):
        return 7000.0, 18

    async def overdue(*_args, **_kwargs):
        return {"priority": 100, "text": "Acme Co's invoice is 35 days overdue (EGP 12,400).", "link": "/b2b/#client/42", "cta": "Collect"}

    async def broken(*_args, **_kwargs):
        raise RuntimeError("boom")

    logged = []

    monkeypatch.setattr("app.services.dashboard_briefing_service._sales_and_transactions", fake_sales)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_overdue_b2b", overdue)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_out_of_stock_recent", broken)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_low_stock", overdue)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_spoilage_spike", overdue)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_big_expense", overdue)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_stale_consignment", overdue)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_big_b2b_client", overdue)
    monkeypatch.setattr("app.core.log.logger.error", lambda *args, **kwargs: logged.append((args, kwargs)))

    utc_s, utc_e = _utc_range(date(2026, 4, 20), date(2026, 4, 20))
    import asyncio
    result = asyncio.run(build_briefing(FakeDB(), SimpleNamespace(id=1), "today", utc_s, utc_e))
    assert result["actions"]
    assert logged


def test_actions_capped_at_four(monkeypatch: pytest.MonkeyPatch):
    async def fake_sales(*_args, **_kwargs):
        return 7000.0, 18

    async def make_action(*_args, **_kwargs):
        return {"priority": 50, "text": "Action", "link": "/x", "cta": "View"}

    monkeypatch.setattr("app.services.dashboard_briefing_service._sales_and_transactions", fake_sales)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_overdue_b2b", make_action)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_out_of_stock_recent", make_action)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_low_stock", make_action)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_spoilage_spike", make_action)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_big_expense", make_action)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_stale_consignment", make_action)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_big_b2b_client", make_action)

    utc_s, utc_e = _utc_range(date(2026, 4, 20), date(2026, 4, 20))
    import asyncio
    result = asyncio.run(build_briefing(FakeDB(), SimpleNamespace(id=1), "today", utc_s, utc_e))
    assert len(result["actions"]) == 4


def test_no_actions_gets_healthy_message(monkeypatch: pytest.MonkeyPatch):
    async def fake_sales(*_args, **_kwargs):
        return 7000.0, 18

    async def no_action(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.services.dashboard_briefing_service._sales_and_transactions", fake_sales)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_overdue_b2b", no_action)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_out_of_stock_recent", no_action)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_low_stock", no_action)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_spoilage_spike", no_action)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_big_expense", no_action)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_stale_consignment", no_action)
    monkeypatch.setattr("app.services.dashboard_briefing_service.detect_big_b2b_client", no_action)

    utc_s, utc_e = _utc_range(date(2026, 4, 20), date(2026, 4, 20))
    import asyncio
    result = asyncio.run(build_briefing(FakeDB(), SimpleNamespace(id=1), "today", utc_s, utc_e))
    assert "Everything looks healthy" in result["body"]
