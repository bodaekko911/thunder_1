from __future__ import annotations
import asyncio
import pytest

from app.services.dashboard_summary_service import _insight_margin

def test_insight_margin_fires():
    res = asyncio.run(_insight_margin({"delta_pts": 1.5}))
    assert res is not None
    assert res["kind"] == "margin"
    assert "1.5" in res["text"]

def test_insight_margin_does_not_fire_if_low():
    res = asyncio.run(_insight_margin({"delta_pts": 0.5}))
    assert res is None