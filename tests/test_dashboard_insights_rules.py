from __future__ import annotations
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest

from app.services.dashboard_summary_service import _insight_margin, _insight_overdue, _insight_stockout, _insight_pace, _insight_weekday
import app.services.dashboard_summary_service as service

def test_insight_margin_fires():
    res = asyncio.run(_insight_margin({"delta_pts": 1.5}))
    assert res is not None
    assert res["kind"] == "margin"
    assert "1.5" in res["text"]

def test_insight_margin_does_not_fire_if_low():
    res = asyncio.run(_insight_margin({"delta_pts": 0.5}))
    assert res is None

class FakeResult:
    def __init__(self, row):
        self.row = row
    def first(self):
        return self.row

class FakeDB:
    def __init__(self, row=None):
        self.row = row
    async def execute(self, stmt):
        return FakeResult(self.row)

def test_insight_overdue_fires():
    class MockClient:
        name = "Big Corp"
    class MockInv:
        invoice_number = "INV-999"
        created_at = datetime(2026, 1, 1, tzinfo=ZoneInfo("UTC"))
    
    db = FakeDB((MockInv(), MockClient()))
    res = asyncio.run(_insight_overdue(db, {"clients_owe": {"overdue_count": 1}}))
    assert res is not None
    assert res["kind"] == "overdue"
    assert "Big Corp" in res["text"]
    assert "INV-999" in res["text"]

def test_insight_stockout_fires():
    class MockRow:
        name = "Olive Oil"
        qty_sold = 50
        
    db = FakeDB(MockRow())
    res = asyncio.run(_insight_stockout(db, {"stock_alerts": {"out_count": 3}}))
    assert res is not None
    assert res["kind"] == "stockout"
    assert "3" in res["text"]
    assert "Olive Oil" in res["text"]

def test_insight_pace_fires(monkeypatch: pytest.MonkeyPatch):
    async def deterministic_sales(db, start, end):
        deterministic_sales.calls = getattr(deterministic_sales, "calls", 0) + 1
        return 100.0 if deterministic_sales.calls == 1 else 120.0
        
    monkeypatch.setattr(service, "_sales_total", deterministic_sales)
    rng = {"days": 14, "end": "2026-04-25"}
    res = asyncio.run(_insight_pace(None, rng))
    assert res is not None
    assert res["kind"] == "pace"
    assert "20.0%" in res["text"]

def test_insight_weekday_fires(monkeypatch: pytest.MonkeyPatch):
    async def mock_daily_sales(db, s, e):
        if s == "cur":
            return [{"date": "2026-04-20", "pos": 100, "b2b": 0, "refunds": 0}] # Monday
        return [{"date": "2026-04-21", "pos": 100, "b2b": 0, "refunds": 0}] # Tuesday
        
    monkeypatch.setattr(service, "_daily_sales_rows", mock_daily_sales)
    rng = {"days": 28, "utc_start": "cur", "utc_end": "cur", "prior_utc_start": "pri", "prior_utc_end": "pri"}
    res = asyncio.run(_insight_weekday(None, rng))
    assert res is not None
    assert res["kind"] == "weekday"
    assert "Monday" in res["text"]
    assert "Tuesday" in res["text"]