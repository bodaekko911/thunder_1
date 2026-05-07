"""
app/core/cache.py
─────────────────
Shared async Redis connection pool + per-section TTL helpers for the
dashboard summary endpoint.

Usage
-----
In app_factory.py lifespan:

    from app.core.cache import init_redis_pool, close_redis_pool
    await init_redis_pool()
    yield
    await close_redis_pool()

In the dashboard router:

    from app.core.cache import get_redis, dash_cache_get, dash_cache_set

    redis = get_redis()
    section = await dash_cache_get(redis, user_id, range_key, "sales")
    await dash_cache_set(redis, user_id, range_key, "sales", data)
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# ── TTL table ────────────────────────────────────────────────────────────────
# 0 = bypass cache entirely (always query the DB).
# Sections that are cheap and change on every transaction stay at 0.
# Sections driven by slower-changing data or expensive queries get longer TTLs.

SECTION_TTLS: dict[str, int] = {
    # Real-time sections — never cached
    "stock_alerts":     0,
    "recent_activity":  0,

    # Fast-changing financial numbers — short cache
    "sales":            10,
    "profit":           10,
    "spent":            10,
    "margin":           10,
    "alt_sales_today":  10,
    "b2b_cash":         10,

    # Changes only on B2B payment events — medium cache
    "clients_owe":      30,
    "top_b2b_clients":  30,

    # Stable within a shift — long cache
    "top_products":     60,
    "chart":            60,

    # LLM-generated, expensive — very long cache
    "briefing":        300,
    "insights":        300,
}

# ── Pool singleton ───────────────────────────────────────────────────────────

_redis_pool = None


async def init_redis_pool() -> None:
    """Create the shared connection pool. Call once at app startup."""
    global _redis_pool
    from app.core.config import settings
    try:
        import redis.asyncio as aioredis
        _redis_pool = aioredis.from_url(
            settings.REDIS_URL,
            socket_connect_timeout=settings.REDIS_SOCKET_CONNECT_TIMEOUT,
            socket_timeout=settings.REDIS_SOCKET_TIMEOUT,
            decode_responses=True,
            max_connections=20,
        )
        # Smoke-test the connection
        await _redis_pool.ping()
        logger.info("cache: Redis pool initialised — %s", settings.REDIS_URL)
    except Exception:
        logger.warning("cache: Redis unavailable at startup — dashboard will run without cache", exc_info=True)
        _redis_pool = None


async def close_redis_pool() -> None:
    """Gracefully close the pool at app shutdown."""
    global _redis_pool
    if _redis_pool is not None:
        try:
            await _redis_pool.aclose()
        except Exception:
            pass
        _redis_pool = None
        logger.info("cache: Redis pool closed")


def get_redis():
    """Return the pool (may be None if Redis is unavailable)."""
    return _redis_pool


# ── Per-section helpers ──────────────────────────────────────────────────────

def _section_key(user_id: int | str, range_key: str, section: str) -> str:
    return f"dash:{user_id}:{range_key}:{section}"


async def dash_cache_get(redis, user_id: int | str, range_key: str, section: str) -> Any | None:
    """
    Return the cached value for *section*, or None on miss / bypass / error.
    Sections with TTL == 0 are always bypassed (returns None immediately).
    """
    if redis is None:
        return None
    if SECTION_TTLS.get(section, 0) == 0:
        return None
    try:
        raw = await redis.get(_section_key(user_id, range_key, section))
        if raw is not None:
            return json.loads(raw)
    except Exception:
        logger.debug("cache: get failed for section=%s", section, exc_info=True)
    return None


async def dash_cache_set(redis, user_id: int | str, range_key: str, section: str, value: Any) -> None:
    """
    Write *value* for *section* with its configured TTL.
    Sections with TTL == 0 are silently skipped.
    Errors are swallowed — a cache write failure must never break a response.
    """
    if redis is None:
        return
    ttl = SECTION_TTLS.get(section, 0)
    if ttl == 0:
        return
    try:
        await redis.set(
            _section_key(user_id, range_key, section),
            json.dumps(value, default=str),
            ex=ttl,
        )
    except Exception:
        logger.debug("cache: set failed for section=%s", section, exc_info=True)


async def dash_cache_get_many(
    redis, user_id: int | str, range_key: str, sections: list[str]
) -> dict[str, Any]:
    """
    Batch-fetch multiple sections in a single Redis pipeline.
    Returns a dict of {section: value} for cache hits only.
    Sections with TTL == 0 are excluded from the pipeline.
    """
    if redis is None:
        return {}

    cacheable = [s for s in sections if SECTION_TTLS.get(s, 0) > 0]
    if not cacheable:
        return {}

    results: dict[str, Any] = {}
    try:
        pipe = redis.pipeline(transaction=False)
        for section in cacheable:
            pipe.get(_section_key(user_id, range_key, section))
        raw_results = await pipe.execute()
        for section, raw in zip(cacheable, raw_results):
            if raw is not None:
                try:
                    results[section] = json.loads(raw)
                except Exception:
                    pass
    except Exception:
        logger.debug("cache: pipeline get failed", exc_info=True)

    return results


async def dash_cache_set_many(
    redis, user_id: int | str, range_key: str, data: dict[str, Any]
) -> None:
    """
    Batch-write multiple sections in a single Redis pipeline.
    Sections with TTL == 0 are silently skipped.
    """
    if redis is None:
        return

    cacheable = {k: v for k, v in data.items() if SECTION_TTLS.get(k, 0) > 0}
    if not cacheable:
        return

    try:
        pipe = redis.pipeline(transaction=False)
        for section, value in cacheable.items():
            ttl = SECTION_TTLS[section]
            pipe.set(
                _section_key(user_id, range_key, section),
                json.dumps(value, default=str),
                ex=ttl,
            )
        await pipe.execute()
    except Exception:
        logger.debug("cache: pipeline set failed", exc_info=True)