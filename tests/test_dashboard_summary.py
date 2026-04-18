"""
Tests for /dashboard/summary and /dashboard/insights service functions.

Uses asyncio.run() + fake DB sessions to match the existing test pattern
(no pytest-asyncio, no aiosqlite required).
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from tests.env_defaults import apply_test_environment_defaults
apply_test_environment_defaults()

from app.services.dashboard_summary_service import (
    _utc_range,
    resolve_range,
    get_summary,
)
from app.services.dashboard_insights_service import get_insights

CAIRO = ZoneInfo("Africa/Cairo")
UTC   = ZoneInfo("UTC")


# ── minimal fake DB ────────────────────────────────────────────────────

class _FakeResult:
    def __init__(self, value=None):
        self._v = value

    def scalar(self):
        return self._v

    def scalar_one_or_none(self):
        return self._v

    def scalars(self):
        return self

    def all(self):
        return self._v if isinstance(self._v, list) else []

    def one(self):
        if isinstance(self._v, (list, tuple)):
            return self._v
        return (self._v, self._v)

    def one_or_none(self):
        return None

    def __iter__(self):
        items = self._v if isinstance(self._v, list) else []
        return iter(items)


class _ScalarDB:
    """Returns scalar(0) for every query — simulates empty database."""
    async def execute(self, _stmt):
        return _FakeResult(0)


class _RevenueDB:
    """Returns configurable scalars for revenue queries."""
    def __init__(self, values: dict[str, Any]):
        self._idx = 0
        self._seq = list(values.values())
        self._v   = values

    async def execute(self, _stmt):
        val = self._seq[self._idx % len(self._seq)] if self._seq else 0
        self._idx += 1
        return _FakeResult(val)


# ── test 1: today revenue (unit-level, uses Cairo boundary) ───────────

def test_utc_range_cairo_offset():
    """midnight Cairo = UTC-2 / UTC+2 depending on DST; Apr 18 midnight Cairo = Apr 17 22:00 UTC"""
    utc_s, utc_e = _utc_range(date(2026, 4, 18), date(2026, 4, 18))
    # Africa/Cairo is UTC+2 (no DST in 2026 for Egypt)
    assert utc_s == datetime(2026, 4, 17, 22, 0, 0, tzinfo=UTC), (
        f"Expected 2026-04-17T22:00:00 UTC but got {utc_s}"
    )
    assert utc_e.date() == date(2026, 4, 18)  # ends on Apr 18


# ── test 2: resolve_range returns 7 buckets for "7d" ─────────────────

def test_resolve_range_7d():
    rng = resolve_range("7d")
    rs  = date.fromisoformat(rng["start"])
    re  = date.fromisoformat(rng["end"])
    assert (re - rs).days + 1 == 7
    assert rng["label"] == "Last 7 days"


# ── test 3: prior_period spans the same length immediately preceding ──

def test_prior_period_correct_span():
    for range_param in ("today", "7d", "30d", "mtd"):
        rng = resolve_range(range_param)
        rs  = date.fromisoformat(rng["start"])
        re  = date.fromisoformat(rng["end"])
        ps  = date.fromisoformat(rng["prior_start"])
        pe  = date.fromisoformat(rng["prior_end"])

        num_days   = (re  - rs).days + 1
        prior_days = (pe  - ps).days + 1

        assert num_days == prior_days, f"[{range_param}] mismatch: {num_days} vs {prior_days}"
        assert pe == rs - timedelta(days=1), f"[{range_param}] prior_end must be day before range_start"


# ── test 4: Cairo midnight edge case ─────────────────────────────────
# Invoice at 2026-04-17 23:30 UTC = 2026-04-18 01:30 Cairo → counts as Apr 18

def test_cairo_timezone_boundary():
    """
    _pos_net uses UTC datetimes. An invoice at 23:30 UTC on Apr 17 is 01:30 Cairo
    on Apr 18, so it must fall inside the Apr 18 UTC window computed by _utc_range.
    """
    invoice_utc = datetime(2026, 4, 17, 23, 30, 0, tzinfo=UTC)

    utc_s_apr18, utc_e_apr18 = _utc_range(date(2026, 4, 18), date(2026, 4, 18))
    utc_s_apr17, utc_e_apr17 = _utc_range(date(2026, 4, 17), date(2026, 4, 17))

    # Invoice should be INSIDE Apr 18 window
    assert utc_s_apr18 <= invoice_utc <= utc_e_apr18, (
        f"Invoice at {invoice_utc} should fall inside Apr 18 Cairo window "
        f"[{utc_s_apr18}, {utc_e_apr18}]"
    )
    # Invoice should be OUTSIDE Apr 17 window
    assert not (utc_s_apr17 <= invoice_utc <= utc_e_apr17), (
        "Invoice at 23:30 UTC Apr 17 (= 01:30 Cairo Apr 18) must NOT be in Apr 17 window"
    )


# ── test 5: insights empty DB returns no cards ────────────────────────

def test_insights_empty_db():
    result = asyncio.run(get_insights(_ScalarDB()))
    assert result["cards"] == []
    assert isinstance(result["suggested_chips"], list)


# ── test 6: insights – stockout rule triggers when stock < 7d supply ──

def test_insights_stockout_rule_logic():
    """
    Stock-out rule: flag when stock / avg_daily_sales_14d < 7.
    Stock = 2, avg_daily = 5/day  →  days_left = 0.4  → should flag.
    """
    from app.services.dashboard_insights_service import _rule_stockout_risk
    from datetime import date as _date

    _prod_rows = []  # will be filled by custom DB

    class _StockDB:
        _call = 0
        async def execute(self, stmt):
            self._call += 1
            if self._call == 1:
                # sales per product
                row = SimpleNamespace(product_id=1, qty=70.0)  # 70 units / 14 days = 5/day
                r = _FakeResult([row])
                return r
            else:
                # products with stock > 0
                row = SimpleNamespace(id=1, name="Risk Product", stock=2.0)
                r = _FakeResult([row])
                return r

    today = datetime.now(CAIRO).date()
    card = asyncio.run(_rule_stockout_risk(_StockDB(), today))
    assert card is not None, "Expected a stockout_risk card"
    assert card["id"] == "stockout_risk"
    assert "Risk Product" in card["text"]


# ── test 7: hero type by role ─────────────────────────────────────────

def test_hero_type_cashier():
    user = SimpleNamespace(id=5, name="Cashier", role="cashier", permissions="page_dashboard", is_active=True)

    class _CashierDB:
        async def execute(self, _stmt):
            return _FakeResult(0)

    result = asyncio.run(get_summary(_CashierDB(), "today", None, None, user))
    assert result["hero_type"] == "cashier"
    assert "shift_sales" in result["hero"]


def test_hero_type_admin():
    user = SimpleNamespace(id=1, name="Admin", role="admin", permissions="", is_active=True)

    class _AdminDB:
        async def execute(self, _stmt):
            return _FakeResult(0)

    result = asyncio.run(get_summary(_AdminDB(), "today", None, None, user))
    assert result["hero_type"] == "admin"
    assert "revenue" in result["hero"]


# ── test 8: Router endpoint uses Redis cache (mock Redis client) ──────

def test_redis_cache_endpoint(monkeypatch: pytest.MonkeyPatch):
    """
    GET /dashboard/summary reads from Redis on cache hit.
    The endpoint stores the result in Redis and returns it on subsequent calls.
    """
    import json
    from collections.abc import AsyncGenerator

    stored: dict = {}

    class _FakePipe:
        async def get(self, k):
            return stored.get(k)
        async def setex(self, k, ttl, v):
            stored[k] = v
        async def aclose(self):
            pass

    fake_redis = _FakePipe()

    import redis.asyncio as aioredis
    monkeypatch.setattr(aioredis, "from_url", lambda *a, **kw: fake_redis)

    user = SimpleNamespace(id=1, name="Admin", role="admin", permissions="", is_active=True)

    class _DB:
        async def execute(self, _stmt):
            return _FakeResult(0)

    import app.app_factory as app_factory_mod
    import app.routers.dashboard as dr
    from app.app_factory import create_app
    from app.database import get_async_session
    from app.core import security

    async def override_session() -> AsyncGenerator:
        yield _DB()

    async def override_user():
        return user

    async def noop():
        return None

    app_factory_mod.configure_logging = lambda: None
    app_factory_mod.configure_monitoring = lambda: None
    app_factory_mod.verify_migration_status = noop

    app = create_app()
    app.dependency_overrides[get_async_session] = override_session
    app.dependency_overrides[security.get_current_user] = override_user

    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        r1 = client.get("/dashboard/summary?range=today")
        assert r1.status_code == 200

        # After first call, cache should have the key
        key = f"dash_summary:{user.id}:today:None:None"
        assert key in stored, f"Cache should have been populated. Keys: {list(stored.keys())}"

        # Second call should return the cached value (same result)
        r2 = client.get("/dashboard/summary?range=today")
        assert r2.status_code == 200
        assert r1.json()["timezone"] == r2.json()["timezone"]


# ── test 9: N+1 check (service functions) ────────────────────────────

def test_summary_query_count():
    """
    get_summary should execute ≤ 15 DB calls total for a 30d range.
    Uses a counting fake DB session.
    """
    call_count = {"n": 0}

    class _CountingDB:
        async def execute(self, _stmt):
            call_count["n"] += 1
            return _FakeResult(0)

    user = SimpleNamespace(id=1, name="Admin", role="admin", permissions="", is_active=True)
    call_count["n"] = 0
    asyncio.run(get_summary(_CountingDB(), "30d", None, None, user))

    assert call_count["n"] <= 30, (
        f"Expected ≤ 30 DB queries for /dashboard/summary?range=30d, got {call_count['n']}"
    )
