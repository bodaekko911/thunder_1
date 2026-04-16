from __future__ import annotations

import re
from datetime import date, timedelta

from app.services.copilot.contracts import ParsedDashboardIntent


SUPPORTED_QUESTION_HINTS = [
    "today's sales",
    "sales by period",
    "overdue customers",
    "customer balance for Acme",
    "product details for olive oil",
    "stock levels",
    "expense breakdown",
    "profit/loss summary",
    "top products",
    "low-stock items",
    "expenses this month",
    "unpaid invoices",
    "customer balances",
]


def _normalize_question(question: str | None) -> str:
    if question is None:
        return ""
    text = str(question).strip().lower()
    text = re.sub(r"[^\w\s'-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_dashboard_question(question: str | None) -> ParsedDashboardIntent:
    text = _normalize_question(question)
    if not text:
        return ParsedDashboardIntent(None, {})

    if any(phrase in text for phrase in ["today sales", "today's sales", "sales today", "revenue today"]):
        today = date.today().isoformat()
        return ParsedDashboardIntent("sales_today", {"date_from": today, "date_to": today})

    if any(phrase in text for phrase in ["sales by period", "sales by day", "daily sales", "weekly sales", "monthly sales"]):
        today = date.today()
        period = "daily"
        if "weekly" in text:
            period = "weekly"
        elif "monthly" in text:
            period = "monthly"
        return ParsedDashboardIntent(
            "sales_by_period",
            {
                "period": period,
                "date_from": (today - timedelta(days=29)).isoformat(),
                "date_to": today.isoformat(),
            },
            comparison_baseline=period,
        )

    if any(phrase in text for phrase in ["top products", "best selling", "top sellers", "best sellers"]):
        month_start = date.today().replace(day=1).isoformat()
        return ParsedDashboardIntent(
            "top_products",
            {"date_from": month_start, "date_to": date.today().isoformat(), "limit": 10},
        )

    if any(phrase in text for phrase in ["overdue customers", "customers overdue", "late customers", "overdue invoices"]):
        return ParsedDashboardIntent("overdue_customers", {"limit": 10})

    customer_markers = ["customer balance for ", "balance for customer ", "customer balance ", "balance for "]
    for marker in customer_markers:
        if marker in text:
            customer_query = text.split(marker, 1)[1].strip()
            if customer_query:
                return ParsedDashboardIntent("customer_balance", {"customer_query": customer_query})

    if any(phrase in text for phrase in ["low stock", "low-stock", "out of stock", "stock running low"]):
        return ParsedDashboardIntent("low_stock", {"status": "low_stock"})

    product_markers = ["product details for ", "details for product ", "product info ", "product details "]
    for marker in product_markers:
        if marker in text:
            product_query = text.split(marker, 1)[1].strip()
            if product_query:
                return ParsedDashboardIntent("product_details", {"product_query": product_query})

    stock_markers = ["stock for ", "stock level for ", "stock levels for "]
    for marker in stock_markers:
        if marker in text:
            product_query = text.split(marker, 1)[1].strip()
            if product_query:
                return ParsedDashboardIntent("stock_levels", {"product_query": product_query, "limit": 10})

    if any(phrase in text for phrase in ["stock levels", "inventory levels", "current stock"]):
        return ParsedDashboardIntent("stock_levels", {"limit": 10})

    if any(phrase in text for phrase in ["expenses this month", "this month expenses", "monthly expenses", "month expenses"]):
        month = date.today().strftime("%Y-%m")
        return ParsedDashboardIntent("expenses_month", {"month": month})

    if any(phrase in text for phrase in ["expense breakdown", "expenses breakdown", "expense categories"]):
        month = date.today().strftime("%Y-%m")
        return ParsedDashboardIntent("expense_breakdown", {"month": month})

    if any(phrase in text for phrase in ["profit and loss", "profit loss", "p l", "pl summary", "profit summary"]):
        today = date.today()
        return ParsedDashboardIntent(
            "profit_loss_summary",
            {
                "date_from": today.replace(day=1).isoformat(),
                "date_to": today.isoformat(),
            },
            comparison_baseline="month_to_date",
        )

    if any(phrase in text for phrase in ["unpaid invoices", "open invoices", "outstanding invoices", "settle later invoices"]):
        return ParsedDashboardIntent("unpaid_invoices", {"status": "unpaid"})

    return ParsedDashboardIntent(None, {})
