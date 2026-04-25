"""Smart tests for the upgraded dashboard assistant intent parser.

Covers:
  - memory._parse_iso_date fix
  - fuzzy typo tolerance
  - time_parser date-range extraction
  - Arabic-Indic digit normalisation
  - Arabic keyword boost
  - New intents (sales_summary, customer_balances_top, help)
  - Follow-up date shift ("and last week?")
  - Pronoun resolution after overdue_customers
  - Nonsense query fallback
"""
import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

import app.services.copilot.providers.internal as internal_provider
from app.services.copilot.memory import _parse_iso_date
from app.services.copilot.router import parse_dashboard_question
from app.services.copilot.contracts import ParsedDashboardIntent
from app.services.dashboard_assistant_service import answer_dashboard_question


# ── Helpers reused from the original test file ────────────────────────────────

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
        return _FakeScalar(None)


class _FakeScalar:
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


# ── _parse_iso_date ───────────────────────────────────────────────────────────

def test_parse_iso_date_valid_string():
    assert _parse_iso_date("2026-03-15") == date(2026, 3, 15)


def test_parse_iso_date_none():
    assert _parse_iso_date(None) is None


def test_parse_iso_date_garbage():
    assert _parse_iso_date("garbage") is None


# ── Typo tolerance ────────────────────────────────────────────────────────────

def test_typo_sals_today_resolves_sales_today():
    result = parse_dashboard_question("sals today")
    assert result.intent == "sales_today", f"Got: {result.intent}"


def test_typo_todays_sale_resolves_sales_today():
    result = parse_dashboard_question("todays sale")
    assert result.intent == "sales_today", f"Got: {result.intent}"


# ── Time expression extraction ────────────────────────────────────────────────

def test_time_expression_last_week_mapped_to_sales_summary():
    result = parse_dashboard_question("how much did we make last week")
    assert result.intent == "sales_summary", f"Got: {result.intent}"

    today = date.today()
    expected_from = today - timedelta(days=today.weekday() + 7)
    expected_to = expected_from + timedelta(days=6)

    assert result.parameters.get("date_from") == expected_from.isoformat(), (
        f"date_from: expected {expected_from.isoformat()}, got {result.parameters.get('date_from')}"
    )
    assert result.parameters.get("date_to") == expected_to.isoformat(), (
        f"date_to: expected {expected_to.isoformat()}, got {result.parameters.get('date_to')}"
    )


# ── Arabic-Indic digits ───────────────────────────────────────────────────────

def test_arabic_indic_digits_parse_to_7_day_window():
    """'last ٧ days' should normalise '٧' → '7' and produce a 7-day window."""
    result = parse_dashboard_question("how much did we earn last ٧ days")
    assert result.intent == "sales_summary", f"Got: {result.intent}"

    date_from_str = result.parameters.get("date_from")
    date_to_str = result.parameters.get("date_to")
    assert date_from_str and date_to_str

    d_from = date.fromisoformat(date_from_str)
    d_to = date.fromisoformat(date_to_str)
    assert (d_to - d_from).days == 6, (
        f"Expected window of 6 days (7-day range), got {(d_to - d_from).days}"
    )


# ── Arabic keyword boost ──────────────────────────────────────────────────────

def test_arabic_keyword_sales_today():
    result = parse_dashboard_question("مبيعات اليوم")
    assert result.intent == "sales_today", f"Got: {result.intent}"


# ── New intents ───────────────────────────────────────────────────────────────

def test_who_owes_me_most_resolves_customer_balances_top():
    result = parse_dashboard_question("who owes me the most")
    assert result.intent == "customer_balances_top", f"Got: {result.intent}"


def test_nonsense_returns_none_intent():
    result = parse_dashboard_question("xyzabc qqq")
    assert result == ParsedDashboardIntent(None, {}), f"Got: {result}"


# ── Help intent ───────────────────────────────────────────────────────────────

def test_help_intent_returns_categories(monkeypatch):
    user = SimpleNamespace(id=1, name="Admin", role="admin", permissions="", is_active=True)

    result = asyncio.run(
        answer_dashboard_question(
            FakePermissionSession(),
            question="help",
            current_user=user,
        )
    )

    assert result["supported"] is True
    assert result["intent"] == "help"
    assert isinstance(result["result"], dict)
    categories = result["result"].get("categories", {})
    assert len(categories) >= 3, f"Expected ≥3 categories, got {len(categories)}: {list(categories.keys())}"


# ── Follow-up: date shift "and last week?" ────────────────────────────────────

def test_followup_and_last_week_shifts_date_range(monkeypatch):
    """After a sales_by_period session that covers last week,
    'and last week?' should detect the collision and produce the week before that."""
    captured = {}
    today = date.today()

    # Make the session cover exactly "last week" so the collision-detection
    # code shifts us one window earlier.
    last_monday = today - timedelta(days=today.weekday() + 7)
    last_sunday = last_monday + timedelta(days=6)

    session_date_from = last_monday
    session_date_to = last_sunday

    async def fake_get_latest_session(db, *, user_id: int, channel: str = "dashboard"):
        return FakeAssistantSession(
            last_intent="sales_by_period",
            last_date_from=session_date_from,
            last_date_to=session_date_to,
            last_comparison_baseline="daily",
        )

    async def fake_execute_tool(db, *, current_user, name, input_data):
        captured["input_data"] = dict(input_data)
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
            question="and last week?",
            current_user=SimpleNamespace(id=1, name="Admin", role="admin", permissions="page_dashboard", is_active=True),
        )
    )

    assert result["supported"] is True
    assert result["intent"] == "sales_by_period"

    new_from = date.fromisoformat(captured["input_data"]["date_from"])
    new_to = date.fromisoformat(captured["input_data"]["date_to"])

    # New range must be entirely before the session range
    assert new_to < session_date_from, (
        f"Expected new range to end before {session_date_from}, got {new_from} – {new_to}"
    )
    # Duration (window length) must be preserved
    assert (new_to - new_from).days == (session_date_to - session_date_from).days, (
        f"Duration mismatch: session was {(session_date_to - session_date_from).days} days, "
        f"got {(new_to - new_from).days}"
    )


# ── Pronoun resolution after overdue_customers ────────────────────────────────

def test_pronoun_show_their_invoices_resolves_customer_balance(monkeypatch):
    """After an overdue_customers session with entity_ids=[5],
    'show their invoices' should resolve to customer_balance for id=5."""
    captured = {}

    async def fake_get_latest_session(db, *, user_id: int, channel: str = "dashboard"):
        return FakeAssistantSession(
            last_intent="overdue_customers",
            last_entity_ids=[5],
        )

    async def fake_execute_tool(db, *, current_user, name, input_data):
        captured["name"] = name
        captured["input_data"] = dict(input_data)
        return {
            "query": "",
            "count": 1,
            "matches": [{"client_id": 5, "name": "Acme", "outstanding": 900.0, "open_invoice_count": 2}],
            "selected": {"client_id": 5, "name": "Acme", "outstanding": 900.0, "open_invoice_count": 2},
        }

    monkeypatch.setattr(internal_provider, "get_latest_session", fake_get_latest_session)
    monkeypatch.setattr(internal_provider, "execute_tool", fake_execute_tool)

    result = asyncio.run(
        answer_dashboard_question(
            FakePermissionSession(),
            question="show their invoices",
            current_user=SimpleNamespace(id=1, name="Admin", role="admin", permissions="page_b2b", is_active=True),
        )
    )

    assert result["supported"] is True
    assert result["intent"] == "customer_balance"
    assert captured.get("name") == "get_customer_balance"
    assert captured["input_data"].get("customer_id") == 5


# ── suggestions / highlights / tables ────────────────────────────────────────

from app.services.copilot.suggestions import build_suggestions, build_highlights, build_table


def test_build_suggestions_top_products_with_items_includes_name():
    result = {"items": [{"name": "Olive Oil", "qty": 100, "revenue": 500}]}
    suggestions = build_suggestions("top_products", result)
    assert any("Olive Oil" in s for s in suggestions)


def test_build_suggestions_top_products_empty_items_has_defaults():
    suggestions = build_suggestions("top_products", {"items": []})
    assert 2 <= len(suggestions) <= 4


def test_build_suggestions_none_intent_returns_close_matches():
    result = {"close_matches": ["top products", "today's sales"]}
    suggestions = build_suggestions(None, result)
    assert suggestions == ["top products", "today's sales"]


def test_build_highlights_sales_today_returns_4():
    result = {"total_sales": 1000.0, "pos_sales": 700.0, "b2b_sales": 300.0, "refunds": 50.0}
    highlights = build_highlights("sales_today", result)
    assert len(highlights) == 4
    for h in highlights:
        assert "label" in h and "value" in h and "tone" in h


def test_build_highlights_profit_positive_gross_profit_tone_good():
    result = {"revenue": 10000.0, "expenses": 4000.0, "gross_profit": 6000.0, "margin_pct": 60.0}
    highlights = build_highlights("profit_loss_summary", result)
    gp = next(h for h in highlights if h["label"] == "Gross Profit")
    assert gp["tone"] == "good"


def test_build_highlights_profit_negative_gross_profit_tone_bad():
    result = {"revenue": 3000.0, "expenses": 5000.0, "gross_profit": -2000.0, "margin_pct": -66.7}
    highlights = build_highlights("profit_loss_summary", result)
    gp = next(h for h in highlights if h["label"] == "Gross Profit")
    assert gp["tone"] == "bad"


def test_build_table_top_products_returns_dict_with_rows():
    result = {"items": [{"name": "Oil", "qty": 10, "revenue": 500}]}
    table = build_table("top_products", result)
    assert isinstance(table, dict)
    assert "columns" in table and "rows" in table
    assert table["rows"][0]["name"] == "Oil"


def test_build_table_top_products_16_items_sets_truncated():
    items = [{"name": f"P{i}", "qty": i, "revenue": i * 10.0} for i in range(16)]
    table = build_table("top_products", {"items": items})
    assert table.get("truncated") is True
    assert len(table["rows"]) == 15


def test_end_to_end_sales_today_has_highlights_and_suggestions(monkeypatch):
    """Full round-trip for 'today's sales' populates highlights (4) and suggestions (2-4)."""
    import app.routers.dashboard as dashboard_module

    async def fake_dashboard_data(db):
        return {
            "total_today": 1200.0,
            "pos_today": 800.0,
            "b2b_today": 400.0,
            "ref_today": 0.0,
            "low_stock_count": 3,
            "low_stock": [],
            "top_products": [],
        }

    async def fake_get_latest_session(db, *, user_id, channel="dashboard"):
        return None

    monkeypatch.setattr(dashboard_module, "dashboard_data", fake_dashboard_data)
    monkeypatch.setattr(internal_provider, "get_latest_session", fake_get_latest_session)

    user = SimpleNamespace(
        id=1, name="Admin", role="admin", permissions="page_dashboard", is_active=True
    )
    result = asyncio.run(
        answer_dashboard_question(
            FakePermissionSession(),
            question="today's sales",
            current_user=user,
        )
    )

    assert result["supported"] is True
    assert result["intent"] == "sales_today"
    assert len(result["highlights"]) == 4
    assert 2 <= len(result["suggestions"]) <= 4
    assert result["table"] is None


def test_near_miss_unsupported_suggestions_contain_close_match():
    """'stock level' (missing trailing s) → unsupported but suggestions includes 'stock levels'."""
    result = asyncio.run(
        answer_dashboard_question(
            FakePermissionSession(),
            question="stock level",
            current_user=SimpleNamespace(
                id=1, name="Admin", role="admin", permissions="", is_active=True
            ),
        )
    )
    assert result["supported"] is False
    assert isinstance(result["suggestions"], list)
    assert "stock levels" in result["suggestions"]
