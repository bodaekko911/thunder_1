"""Deterministic question parser for the dashboard assistant."""
from __future__ import annotations

from datetime import date, timedelta
import re

from app.services.copilot import fuzzy, time_parser
from app.services.copilot.contracts import ParsedDashboardIntent
from app.services.dashboard_summary_service import resolve_range


SUPPORTED_QUESTION_HINTS = [
    "How much did we sell today?",
    "What are my top products this month?",
    "Show product details for olive oil",
    "Which items are low in stock?",
    "What are my biggest expenses this month?",
    "Which invoices are unpaid?",
    "Which customer owes us the most?",
    "Show recent sales activity",
    "What changed compared to yesterday?",
    "What is the gross profit this month?",
]

_TIME_RANGE_INTENTS = frozenset(
    {
        "sales_summary",
        "sales_by_period",
        "top_products",
        "expenses_month",
        "expense_breakdown",
        "profit_loss_summary",
        "recent_activity",
        "change_summary",
        "customer_growth",
        "b2b_performance",
    }
)

_METRIC_ALIASES = {
    "sales": {"sales", "revenue", "sold", "sell", "made", "income", "turnover"},
    "profit": {"profit", "gross profit", "margin", "p&l", "profit and loss"},
    "expenses": {"expenses", "expense", "spending", "spent"},
}

_KPI_HELP = {
    "sales": "sales",
    "revenue": "sales",
    "gross profit": "gross_profit",
    "profit": "gross_profit",
    "margin": "margin",
    "stock alerts": "stock_alerts",
    "receivables": "receivables",
    "customer balances": "receivables",
    "top products": "top_products",
}


def parse_dashboard_question(
    question: str | None,
    dashboard_context: dict | None = None,
) -> ParsedDashboardIntent:
    raw = (question or "").strip()
    if not raw:
        return ParsedDashboardIntent(None, {})
    if raw == "?":
        return ParsedDashboardIntent("help", {}, confidence=1.0)

    text = fuzzy.normalize(raw)
    if not text:
        return ParsedDashboardIntent(None, {})

    if _contains_any(text, ["help", "what can you do", "what can you answer", "what can i ask"]):
        return ParsedDashboardIntent("help", {}, confidence=1.0)

    resolved_context = _resolve_dashboard_context(dashboard_context)
    explicit_range = time_parser.parse_time_expression(text)
    active_range = _range_to_params(explicit_range) or _range_to_params_from_context(resolved_context)

    product_query = _extract_after_markers(
        text,
        [
            "product details for ",
            "details for product ",
            "show me details for ",
            "tell me about product ",
            "tell me about ",
            "show product ",
        ],
    )
    if product_query:
        return ParsedDashboardIntent(
            "product_details",
            {"product_query": product_query},
            confidence=1.0,
        )

    stock_query = _extract_after_markers(
        text,
        [
            "stock levels for ",
            "stock level for ",
            "stock for ",
            "inventory for ",
        ],
    )
    if stock_query:
        return ParsedDashboardIntent(
            "stock_levels",
            {"product_query": stock_query, "limit": 10},
            confidence=1.0,
        )

    customer_query = _extract_after_markers(
        text,
        [
            "customer balance for ",
            "balance for customer ",
            "balance for ",
        ],
    )
    if customer_query:
        return ParsedDashboardIntent(
            "customer_balance",
            {"customer_query": customer_query},
            confidence=1.0,
        )

    kpi_topic = _extract_kpi_help_topic(text)
    if kpi_topic:
        return ParsedDashboardIntent("kpi_explanation", {"topic": kpi_topic}, confidence=0.98)

    if _contains_any(text, ["recent sales activity", "recent activity", "recent transactions", "latest sales"]):
        return ParsedDashboardIntent(
            "recent_activity",
            _with_range({"limit": 10}, "recent_activity", active_range),
            confidence=0.99,
        )

    if _contains_any(text, ["top products", "best sellers", "best selling", "top sellers", "most sold"]):
        return ParsedDashboardIntent(
            "top_products",
            _with_range({"limit": 10}, "top_products", active_range or _default_month_range()),
            confidence=0.99,
        )

    if _contains_any(text, ["low stock", "low-stock", "low in stock", "out of stock", "stock risk", "running low", "reorder"]):
        return ParsedDashboardIntent("low_stock", {"threshold": 5}, confidence=0.98)

    if _contains_any(text, ["stock value", "inventory value", "inventory worth", "value of inventory"]):
        return ParsedDashboardIntent("product_stock_value", {}, confidence=0.97)

    if _contains_any(text, ["expense breakdown", "biggest expenses", "largest expenses", "expenses by category"]):
        return ParsedDashboardIntent(
            "expense_breakdown",
            _with_month_or_range(active_range, text),
            confidence=0.99,
        )

    if _contains_any(text, ["expenses", "spending", "spent"]) and _contains_any(
        text,
        ["today", "yesterday", "week", "month", "mtd", "year", "last", "this", "between", "from", "custom"],
    ):
        return ParsedDashboardIntent(
            "expenses_month",
            _with_month_or_range(active_range or _default_month_range(), text),
            confidence=0.98,
        )

    if _contains_any(text, ["unpaid invoices", "open invoices", "outstanding invoices", "which invoices are unpaid"]):
        return ParsedDashboardIntent("unpaid_invoices", {"status": "unpaid"}, confidence=0.98)

    if _contains_any(text, ["who owes", "owes us the most", "largest balance", "biggest balance", "customer balances"]):
        return ParsedDashboardIntent("customer_balances_top", {"limit": 10}, confidence=0.98)

    if _contains_any(text, ["overdue customers", "late customers", "customers overdue"]):
        return ParsedDashboardIntent("overdue_customers", {"limit": 10}, confidence=0.97)

    if _contains_any(text, ["customer growth", "new customers", "customer count growth"]):
        return ParsedDashboardIntent(
            "customer_growth",
            _with_range({}, "customer_growth", active_range or _default_month_range()),
            confidence=0.96,
        )

    if _contains_any(text, ["b2b performance", "b2b sales", "wholesale performance", "business client performance"]):
        return ParsedDashboardIntent(
            "b2b_performance",
            _with_range({}, "b2b_performance", active_range or _default_month_range()),
            confidence=0.96,
        )

    if _contains_any(text, ["profit", "gross profit", "profit and loss", "margin", "p&l"]):
        return ParsedDashboardIntent(
            "profit_loss_summary",
            _with_range({}, "profit_loss_summary", active_range or _default_month_range()),
            confidence=0.98,
        )

    if _looks_like_change_question(text):
        focus = _change_focus(text)
        comparison_only_phrase = _contains_any(
            text,
            ["compared to yesterday", "compare to yesterday", "vs yesterday", "versus yesterday"],
        )
        current_range = active_range
        if comparison_only_phrase:
            current_range = _range_to_params_from_context(resolved_context) or _default_today_range()
        params = _with_range({"focus": focus}, "change_summary", current_range or _default_today_range())
        comparison = _comparison_range(text, params)
        if comparison:
            params.update(comparison)
        return ParsedDashboardIntent("change_summary", params, confidence=0.97)

    if _contains_any(text, ["sales by period", "sales over time", "daily sales", "weekly sales", "monthly sales", "sales trend", "sales chart"]):
        period = "daily"
        if "weekly" in text or "by week" in text:
            period = "weekly"
        elif "monthly" in text or "by month" in text:
            period = "monthly"
        return ParsedDashboardIntent(
            "sales_by_period",
            _with_range({"period": period}, "sales_by_period", active_range or _default_month_range()),
            confidence=0.98,
        )

    if _contains_sales_metric(text):
        return ParsedDashboardIntent(
            "sales_summary",
            _with_range({}, "sales_summary", active_range or _default_today_range()),
            confidence=0.96,
        )

    return ParsedDashboardIntent(None, {})


def _contains_any(text: str, phrases: list[str]) -> bool:
    return any(phrase in text for phrase in phrases)


def _extract_after_markers(text: str, markers: list[str]) -> str | None:
    for marker in markers:
        if marker in text:
            value = text.split(marker, 1)[1].strip(" ?.")
            if value:
                return value
    return None


def _resolve_dashboard_context(dashboard_context: dict | None) -> dict | None:
    if not dashboard_context:
        return None
    range_key = str(dashboard_context.get("range") or "today")
    start = dashboard_context.get("start")
    end = dashboard_context.get("end")
    try:
        return resolve_range(range_key, start, end)
    except Exception:
        return None


def _range_to_params(range_value: tuple[date, date] | None) -> dict | None:
    if range_value is None:
        return None
    return {"date_from": range_value[0].isoformat(), "date_to": range_value[1].isoformat()}


def _range_to_params_from_context(context: dict | None) -> dict | None:
    if not context:
        return None
    return {"date_from": context["start"], "date_to": context["end"]}


def _default_today_range() -> dict:
    today = date.today().isoformat()
    return {"date_from": today, "date_to": today}


def _default_month_range() -> dict:
    today = date.today()
    return {"date_from": today.replace(day=1).isoformat(), "date_to": today.isoformat()}


def _with_range(parameters: dict, intent: str, active_range: dict | None) -> dict:
    if intent not in _TIME_RANGE_INTENTS or not active_range:
        return dict(parameters)
    merged = dict(parameters)
    merged.update(active_range)
    return merged


def _with_month_or_range(active_range: dict | None, text: str) -> dict:
    if active_range and active_range.get("date_from") and active_range.get("date_to"):
        return dict(active_range)
    today = date.today()
    month_value = today.strftime("%Y-%m")
    if "last month" in text:
        previous_month_end = today.replace(day=1) - timedelta(days=1)
        month_value = previous_month_end.strftime("%Y-%m")
    return {"month": month_value}


def _contains_sales_metric(text: str) -> bool:
    return any(term in text for term in _METRIC_ALIASES["sales"])


def _looks_like_change_question(text: str) -> bool:
    return _contains_any(
        text,
        [
            "what changed",
            "what has changed",
            "compared to",
            "compare to",
            "vs yesterday",
            "versus yesterday",
            "why is revenue",
            "why are sales",
            "up compared to",
            "down compared to",
        ],
    )


def _change_focus(text: str) -> str:
    if any(term in text for term in _METRIC_ALIASES["profit"]):
        return "profit"
    if any(term in text for term in _METRIC_ALIASES["expenses"]):
        return "expenses"
    return "sales"


def _comparison_range(text: str, current_params: dict) -> dict | None:
    current_from = date.fromisoformat(current_params["date_from"])
    current_to = date.fromisoformat(current_params["date_to"])

    comparison = None
    if re.search(r"\b(yesterday)\b", text):
        comparison = (current_to - timedelta(days=1), current_to - timedelta(days=1))
    elif re.search(r"\b(last|previous)\s+week\b", text):
        comparison = time_parser.parse_time_expression("last week")
    elif re.search(r"\b(last|previous)\s+month\b", text):
        comparison = time_parser.parse_time_expression("last month")

    if comparison is None:
        span = (current_to - current_from).days
        prior_end = current_from - timedelta(days=1)
        prior_start = prior_end - timedelta(days=span)
        comparison = (prior_start, prior_end)

    return {
        "comparison_date_from": comparison[0].isoformat(),
        "comparison_date_to": comparison[1].isoformat(),
    }


def _extract_kpi_help_topic(text: str) -> str | None:
    if not _contains_any(text, ["what is", "what are", "what does", "explain", "define"]):
        return None
    if _contains_any(
        text,
        [
            "today",
            "yesterday",
            "this week",
            "this month",
            "last month",
            "top products this",
            "show",
            "how much",
            "compare",
        ],
    ):
        return None
    for phrase, topic in _KPI_HELP.items():
        if phrase in text:
            return topic
    return None
