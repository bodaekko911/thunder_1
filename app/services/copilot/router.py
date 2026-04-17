"""Intent parser for the dashboard assistant — fully rule-based, no external calls."""
from __future__ import annotations

import difflib
from datetime import date, timedelta

from app.services.copilot import fuzzy, keywords as kw_module, time_parser
from app.services.copilot.contracts import ParsedDashboardIntent


SUPPORTED_QUESTION_HINTS = [
    "today's sales",
    "sales by period",
    "sales this week",
    "sales last month",
    "overdue customers",
    "customer balance for Acme",
    "product details for olive oil",
    "stock levels",
    "stock value",
    "expense breakdown",
    "profit/loss summary",
    "top products",
    "low-stock items",
    "expenses this month",
    "unpaid invoices",
    "customer balances",
    "who owes me the most",
]

# Intents where time_parser may override the default date range
_DATE_RANGE_INTENTS = frozenset(
    {"sales_summary", "sales_by_period", "profit_loss_summary", "expense_breakdown"}
)

# Score threshold — a single fully-matched group scores exactly 1.0
_THRESHOLD = 0.9


def parse_dashboard_question(question: str | None) -> ParsedDashboardIntent:
    raw = (question or "").strip()
    if not raw:
        return ParsedDashboardIntent(None, {})

    # Special case: bare "?"
    if raw == "?":
        return ParsedDashboardIntent("help", {}, confidence=1.0)

    text = fuzzy.normalize(raw)
    if not text:
        return ParsedDashboardIntent(None, {})

    tokens = text.split()

    # ── Deterministic marker extraction (highest priority, unchanged) ──────────
    customer_markers = [
        "customer balance for ",
        "balance for customer ",
        "customer balance ",
        "balance for ",
    ]
    for marker in customer_markers:
        if marker in text:
            query = text.split(marker, 1)[1].strip()
            if query:
                return ParsedDashboardIntent(
                    "customer_balance", {"customer_query": query}, confidence=1.0
                )

    product_markers = [
        "product details for ",
        "details for product ",
        "product info ",
        "product details ",
    ]
    for marker in product_markers:
        if marker in text:
            query = text.split(marker, 1)[1].strip()
            if query:
                return ParsedDashboardIntent(
                    "product_details", {"product_query": query}, confidence=1.0
                )

    stock_markers = ["stock for ", "stock level for ", "stock levels for "]
    for marker in stock_markers:
        if marker in text:
            query = text.split(marker, 1)[1].strip()
            if query:
                return ParsedDashboardIntent(
                    "stock_levels",
                    {"product_query": query, "limit": 10},
                    confidence=1.0,
                )

    # ── Arabic boost (pre-normalisation pass) ──────────────────────────────────
    arabic_boost: dict[str, float] = {}
    for intent_name, phrases in kw_module.ARABIC_KEYWORDS.items():
        for phrase in phrases:
            if phrase in raw:
                arabic_boost[intent_name] = arabic_boost.get(intent_name, 0.0) + 1.0
                break

    # ── Score every intent ─────────────────────────────────────────────────────
    scores: dict[str, float] = {}
    for intent_name, intent_kws in kw_module.INTENT_KEYWORDS.items():
        base = _score_intent(text, tokens, intent_kws)
        scores[intent_name] = base + arabic_boost.get(intent_name, 0.0)

    best_intent = max(scores, key=lambda k: scores[k])
    best_score = scores[best_intent]

    if best_score < _THRESHOLD:
        return ParsedDashboardIntent(None, {})

    # ── Build default parameters for the winning intent ────────────────────────
    params, comparison_baseline = _build_params(best_intent, text)

    # ── Time-parser override for date-ranged intents ───────────────────────────
    if best_intent in _DATE_RANGE_INTENTS:
        parsed_range = time_parser.parse_time_expression(text)
        if parsed_range:
            date_from, date_to = parsed_range
            params = dict(params)
            params["date_from"] = date_from.isoformat()
            params["date_to"] = date_to.isoformat()

    return ParsedDashboardIntent(
        best_intent,
        params,
        comparison_baseline=comparison_baseline,
        confidence=round(min(best_score, 1.0), 3),
    )


# ── Scoring helpers ────────────────────────────────────────────────────────────

def _score_intent(text: str, tokens: list[str], intent_kws: dict) -> float:
    """All require_any groups must have at least one keyword hit; partial match = 0."""
    require_any: list[list[str]] = intent_kws.get("require_any", [])
    boost: list[str] = intent_kws.get("boost", [])

    if not require_any:
        return 0.0

    total_groups = len(require_any)
    matched = sum(
        1
        for group in require_any
        if any(_keyword_matches(kw, text, tokens) for kw in group)
    )

    if matched < total_groups:
        # Partial credit — intentionally below threshold so it never fires alone
        return matched * 0.3

    base = float(total_groups)
    boost_score = sum(
        0.5 for kw in boost if _keyword_matches(kw, text, tokens)
    )
    return base + boost_score


def _keyword_matches(keyword: str, text: str, tokens: list[str]) -> bool:
    """Exact substring first; single-word keywords also try token-level fuzzy (cutoff=0.75)."""
    if keyword in text:
        return True
    if " " not in keyword:
        return bool(difflib.get_close_matches(keyword, tokens, n=1, cutoff=0.75))
    return False


# ── Parameter builders ─────────────────────────────────────────────────────────

def _build_params(intent: str, text: str) -> tuple[dict, str | None]:
    """Return (parameters_dict, comparison_baseline | None)."""
    today = date.today()

    if intent == "sales_today":
        iso = today.isoformat()
        return {"date_from": iso, "date_to": iso}, None

    if intent == "sales_summary":
        iso = today.isoformat()
        return {"date_from": iso, "date_to": iso}, None

    if intent == "sales_by_period":
        period = "daily"
        if "weekly" in text:
            period = "weekly"
        elif "monthly" in text:
            period = "monthly"
        return (
            {
                "period": period,
                "date_from": (today - timedelta(days=29)).isoformat(),
                "date_to": today.isoformat(),
            },
            period,
        )

    if intent == "top_products":
        return (
            {
                "date_from": today.replace(day=1).isoformat(),
                "date_to": today.isoformat(),
                "limit": 10,
            },
            None,
        )

    if intent == "overdue_customers":
        return {"limit": 10}, None

    if intent == "customer_balances_top":
        return {"limit": 10}, None

    if intent == "low_stock":
        return {"status": "low_stock"}, None

    if intent == "stock_levels":
        return {"limit": 10}, None

    if intent == "product_stock_value":
        return {}, None

    if intent in {"expenses_month", "expense_breakdown"}:
        return {"month": today.strftime("%Y-%m")}, None

    if intent == "profit_loss_summary":
        return (
            {
                "date_from": today.replace(day=1).isoformat(),
                "date_to": today.isoformat(),
            },
            "month_to_date",
        )

    if intent == "unpaid_invoices":
        return {"status": "unpaid"}, None

    if intent == "help":
        return {}, None

    return {}, None
