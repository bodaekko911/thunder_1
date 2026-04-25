from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

from app.app_factory import create_app
from app.core import security
from app.database import get_async_session
from app.services import dashboard_summary_service as summary_service

UTC = ZoneInfo("UTC")


class FakeResult:
    def __init__(self, value=0):
        self._value = value

    def scalar(self):
        return self._value

    def one(self):
        return self._value

    def one_or_none(self):
        return None

    def all(self):
        return self._value if isinstance(self._value, list) else []

    def __iter__(self):
        return iter(self.all())


class FakeDB:
    async def execute(self, _stmt):
        return FakeResult(0)

    async def rollback(self):
        return None


def fake_user():
    return SimpleNamespace(id=1, role="admin", permissions="page_dashboard,page_b2b,page_inventory,page_expenses,page_pos")


def run(coro):
    return asyncio.run(coro)


def test_summary_endpoint_has_required_top_level_keys(monkeypatch: pytest.MonkeyPatch):
    async def fake_summary(_db, _range, _start, _end, _user):
        return {
            "range": {"label": "Today", "start": "2026-04-20", "end": "2026-04-20", "days": 1, "granularity": "day"},
            "briefing": {"lead": "Lead", "body": "", "actions": []},
            "numbers": {
                "sales": {"value": 0, "prev_value": 0.0, "delta_pct": None, "direction": "flat", "sparkline": []},
                "clients_owe": {"value": 0, "overdue_count": 0},
                "spent": {"value": 0, "delta_pct": None, "direction": "flat", "sparkline": []},
                "stock_alerts": {"value": 0, "out_count": 0, "low_count": 0},
                "margin": {"value_pct": None, "delta_pts": None, "gross_profit": None},
            },
            "chart": {"buckets": []},
            "panels": {"top_products_by_revenue": [], "top_products_by_qty": [], "recent_activity": []},
            "insights": [],
            "viewer": {
                "role": "admin",
                "can_view_b2b": True,
                "can_view_expenses": True,
                "can_view_inventory": True,
                "can_view_pos": True,
                "alt_sales_today": {"value": 0.0},
            },
            "generated_at": "2026-04-20T12:00:00+02:00",
            "timezone": "Africa/Cairo",
        }

    monkeypatch.setattr("app.services.dashboard_summary_service.get_summary", fake_summary)
    monkeypatch.setattr("app.app_factory.configure_logging", lambda: None)
    monkeypatch.setattr("app.app_factory.configure_monitoring", lambda: None)
    async def noop():
        return None

    monkeypatch.setattr("app.app_factory.verify_migration_status", noop)

    async def override_session() -> AsyncGenerator:
        yield FakeDB()

    async def override_user():
        return fake_user()

    app = create_app()
    app.dependency_overrides[get_async_session] = override_session
    app.dependency_overrides[security.get_current_user] = override_user

    with TestClient(app) as client:
      response = client.get("/dashboard/summary?range=today")
      data = response.json()
      assert response.status_code == 200
      assert {"range", "briefing", "numbers", "chart", "panels", "insights", "generated_at", "viewer", "timezone"} <= set(data.keys())


def test_numbers_contains_expected_additive_entries(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.services.dashboard_briefing_service.build_briefing", lambda *args, **kwargs: asyncio.sleep(0, result={"lead": "Lead", "actions": []}))
    result = run(summary_service.get_summary(FakeDB(), "today", None, None, fake_user()))
    assert set(result["numbers"].keys()) == {"sales", "clients_owe", "spent", "stock_alerts", "margin"}
    assert isinstance(result["numbers"]["sales"]["prev_value"], float)
    assert set(result["numbers"]["margin"].keys()) == {"value_pct", "delta_pts", "gross_profit"}
    assert isinstance(result["insights"], list)


@pytest.mark.parametrize(
    ("range_param", "expected_len", "expected_granularity"),
    [("today", 1, "day"), ("7d", 7, "day")],
)
def test_chart_bucket_lengths_for_short_ranges(monkeypatch: pytest.MonkeyPatch, range_param: str, expected_len: int, expected_granularity: str):
    monkeypatch.setattr("app.services.dashboard_briefing_service.build_briefing", lambda *args, **kwargs: asyncio.sleep(0, result={"lead": "Lead", "actions": []}))
    data = run(summary_service.get_summary(FakeDB(), range_param, None, None, fake_user()))
    assert data["range"]["granularity"] == expected_granularity
    assert len(data["chart"]["buckets"]) == expected_len


def test_chart_bucket_lengths_for_90d_and_year(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.services.dashboard_briefing_service.build_briefing", lambda *args, **kwargs: asyncio.sleep(0, result={"lead": "Lead", "actions": []}))
    data_90d = run(summary_service.get_summary(FakeDB(), "custom", "2026-01-01", "2026-03-31", fake_user()))
    data_year = run(summary_service.get_summary(FakeDB(), "year", None, None, fake_user()))
    assert len(data_90d["chart"]["buckets"]) <= 14
    assert len(data_year["chart"]["buckets"]) <= 12


def test_top_products_are_capped(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.services.dashboard_briefing_service.build_briefing", lambda *args, **kwargs: asyncio.sleep(0, result={"lead": "Lead", "actions": []}))
    data = run(summary_service.get_summary(FakeDB(), "today", None, None, fake_user()))
    assert len(data["panels"]["top_products_by_revenue"]) <= 8


def test_recent_activity_sorted_desc(monkeypatch: pytest.MonkeyPatch):
    async def fake_briefing(*_args, **_kwargs):
        return {"lead": "Lead", "actions": []}

    async def fake_panels(_db, _rng):
        return {
            "top_products_by_revenue": [],
            "top_products_by_qty": [],
            "recent_activity": [
                {"timestamp": "2026-04-20T12:00:00+02:00"},
                {"timestamp": "2026-04-20T11:00:00+02:00"},
            ],
        }

    monkeypatch.setattr("app.services.dashboard_briefing_service.build_briefing", fake_briefing)
    monkeypatch.setattr(summary_service, "_build_panels", fake_panels)
    data = run(summary_service.get_summary(FakeDB(), "today", None, None, fake_user()))
    assert len(data["panels"]["recent_activity"]) <= 10
    assert data["panels"]["recent_activity"][0]["timestamp"] > data["panels"]["recent_activity"][1]["timestamp"]


def test_section_failure_returns_partial_response(monkeypatch: pytest.MonkeyPatch):
    async def fake_briefing(*_args, **_kwargs):
        return {"lead": "Lead", "actions": []}

    async def broken_panels(*_args, **_kwargs):
        raise RuntimeError("broken")

    monkeypatch.setattr("app.services.dashboard_briefing_service.build_briefing", fake_briefing)
    monkeypatch.setattr(summary_service, "_build_panels", broken_panels)
    data = run(summary_service.get_summary(FakeDB(), "today", None, None, fake_user()))
    assert data["panels"]["top_products_by_revenue"] == []
    assert any(error["section"] == "top_products" for error in data["_errors"])


def test_cairo_timezone_places_2330_utc_sale_into_april():
    invoice_utc = datetime(2026, 3, 31, 23, 30, 0, tzinfo=UTC)
    april_start, april_end = summary_service._utc_range(date(2026, 4, 1), date(2026, 4, 30))
    march_start, march_end = summary_service._utc_range(date(2026, 3, 1), date(2026, 3, 31))
    assert april_start <= invoice_utc <= april_end
    assert not (march_start <= invoice_utc <= march_end)
