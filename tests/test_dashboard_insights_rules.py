from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.services import dashboard_summary_service as summary_service

UTC = ZoneInfo("UTC")


class FakeResult:
    def __init__(self, *, first=None, all_rows=None):
        self._first = first
        self._all_rows = list(all_rows or [])

    def first(self):
        return self._first

    def all(self):
        return self._all_rows


class QueueDB:
    def __init__(self, *results):
        self._results = list(results)

    async def execute(self, _stmt):
        if not self._results:
            raise AssertionError("Unexpected execute call")
        return self._results.pop(0)


def run(coro):
    return asyncio.run(coro)


def test_insight_overdue_fires(monkeypatch):
    fixed_now = datetime(2026, 4, 25, 9, 0, tzinfo=UTC)
    monkeypatch.setattr(summary_service, "now_local", lambda: fixed_now)

    row = SimpleNamespace(
        client_name="Nile Stores",
        invoice_number="B2B-204",
        created_at=fixed_now - timedelta(days=45),
    )
    db = QueueDB(FakeResult(first=row))

    result = run(summary_service._insight_overdue(db, {"clients_owe": {"overdue_count": 2}}))

    assert result == {
        "kind": "overdue",
        "text": "Nile Stores hasn't paid invoice #B2B-204 for 45 days — your largest overdue receivable.",
    }


def test_insight_stockout_fires(monkeypatch):
    fixed_now = datetime(2026, 4, 25, 9, 0, tzinfo=UTC)
    monkeypatch.setattr(summary_service, "now_local", lambda: fixed_now)

    products = [
        SimpleNamespace(id=1, name="Olive Oil"),
        SimpleNamespace(id=2, name="Tahini"),
    ]
    monkeypatch.setattr(summary_service, "_sales_velocity_lookup", lambda *_args, **_kwargs: asyncio.sleep(0, result={1: 12.0, 2: 31.0}))
    db = QueueDB(FakeResult(all_rows=products))

    result = run(summary_service._insight_stockout(db, {"stock_alerts": {"out_count": 2}}))

    assert result == {
        "kind": "stockout",
        "text": "2 products ran out of stock recently. Tahini has been a top seller — restocking it should be the priority.",
    }


def test_insight_pace_fires(monkeypatch):
    def fake_utc_range(start, end):
        return (
            datetime.combine(start, datetime.min.time(), tzinfo=UTC),
            datetime.combine(end, datetime.min.time(), tzinfo=UTC),
        )

    async def fake_sales_total(_db, utc_s, _utc_e):
        return 1000.0 if utc_s.date().isoformat() == "2026-04-01" else 1180.0

    monkeypatch.setattr(summary_service, "_utc_range", fake_utc_range)
    monkeypatch.setattr(summary_service, "_sales_total", fake_sales_total)
    rng = {"days": 14, "end": "2026-04-14"}

    result = run(summary_service._insight_pace(object(), rng))

    assert result == {
        "kind": "pace",
        "text": "Your last 7 days are pacing 18.0% ahead of the first half of this period.",
    }


def test_insight_margin_fires():
    result = run(summary_service._insight_margin({"delta_pts": 1.5}))

    assert result == {
        "kind": "margin",
        "text": "Margin improved 1.5 points versus the previous period.",
    }


def test_insight_weekday_fires(monkeypatch):
    async def fake_daily_rows(_db, utc_s, _utc_e):
        if utc_s.date().isoformat() == "2026-03-29":
            return [
                {"date": "2026-03-30", "pos": 10, "b2b": 0, "refunds": 0},
                {"date": "2026-04-01", "pos": 40, "b2b": 0, "refunds": 0},
            ]
        return [
            {"date": "2026-03-02", "pos": 35, "b2b": 0, "refunds": 0},
            {"date": "2026-03-05", "pos": 8, "b2b": 0, "refunds": 0},
        ]

    monkeypatch.setattr(summary_service, "_daily_sales_rows", fake_daily_rows)
    rng = {
        "days": 28,
        "utc_start": datetime(2026, 3, 29, tzinfo=UTC),
        "utc_end": datetime(2026, 4, 25, tzinfo=UTC),
        "prior_utc_start": datetime(2026, 3, 1, tzinfo=UTC),
        "prior_utc_end": datetime(2026, 3, 28, tzinfo=UTC),
    }

    result = run(summary_service._insight_weekday(object(), rng))

    assert result == {
        "kind": "weekday",
        "text": "Wednesdays are now your busiest day, overtaking Mondays.",
    }
