"""Natural-language date range parser for the dashboard assistant (stdlib only)."""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

from app.services.copilot import fuzzy


def parse_time_expression(text: str, today: date | None = None) -> tuple[date, date] | None:
    """Search `text` for a recognisable time expression and return (date_from, date_to).

    Input is expected to already be normalised (lowercase, digits converted).
    Returns None if no pattern is found.
    """
    if today is None:
        today = date.today()

    t = fuzzy.normalize(text or "")

    # ── variable-length windows ────────────────────────────────────────────────
    # "last N days"  (1-365)
    m = re.search(r"\blast\s+(\d+)\s+days?\b", t)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 365:
            return today - timedelta(days=n - 1), today

    # "last N weeks"  (1-52)
    m = re.search(r"\blast\s+(\d+)\s+weeks?\b", t)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 52:
            return today - timedelta(weeks=n), today

    # "last N months"  (1-24)
    m = re.search(r"\blast\s+(\d+)\s+months?\b", t)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 24:
            year, month = today.year, today.month - n
            while month <= 0:
                month += 12
                year -= 1
            return date(year, month, 1), today

    # ── named relative windows ─────────────────────────────────────────────────
    if re.search(r"\blast\s+week\b", t):
        last_monday = today - timedelta(days=today.weekday() + 7)
        last_sunday = last_monday + timedelta(days=6)
        return last_monday, last_sunday

    if re.search(r"\blast\s+month\b", t):
        first_of_this = today.replace(day=1)
        last_of_prev = first_of_this - timedelta(days=1)
        return last_of_prev.replace(day=1), last_of_prev

    if re.search(r"\blast\s+year\b|\bprior\s+year\b", t):
        prev = today.year - 1
        return date(prev, 1, 1), date(prev, 12, 31)

    if re.search(r"\bprevious\s+week\b|\bprev\s+week\b", t):
        last_monday = today - timedelta(days=today.weekday() + 7)
        last_sunday = last_monday + timedelta(days=6)
        return last_monday, last_sunday

    if re.search(r"\bprevious\s+month\b|\bprior\s+month\b|\bprev\s+month\b", t):
        first_of_this = today.replace(day=1)
        last_of_prev = first_of_this - timedelta(days=1)
        return last_of_prev.replace(day=1), last_of_prev

    # ── current-period windows ─────────────────────────────────────────────────
    if re.search(r"\bthis\s+week\b", t):
        monday = today - timedelta(days=today.weekday())
        return monday, today

    if re.search(r"\bthis\s+month\b|\bmtd\b", t):
        return today.replace(day=1), today

    if re.search(r"\bthis\s+quarter\b", t):
        q_start_month = ((today.month - 1) // 3) * 3 + 1
        return today.replace(month=q_start_month, day=1), today

    if re.search(r"\bthis\s+year\b|\bytd\b", t):
        return today.replace(month=1, day=1), today

    # ── single-day anchors ─────────────────────────────────────────────────────
    if re.search(r"\byesterday\b", t):
        yesterday = today - timedelta(days=1)
        return yesterday, yesterday

    if re.search(r"\btoday\b", t):
        return today, today

    return None
