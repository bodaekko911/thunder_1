"""Response composer for the dashboard assistant."""
from __future__ import annotations


SUPPORTED_QUESTIONS_BY_CATEGORY: dict[str, list[str]] = {
    "Sales": [
        "today's sales",
        "sales this week",
        "sales last month",
        "how much did we make last week",
        "top products",
        "sales by period",
        "what changed compared to yesterday",
        "show recent sales activity",
    ],
    "Inventory": [
        "low-stock items",
        "stock levels",
        "product details for <name>",
        "stock value",
    ],
    "Customers": [
        "overdue customers",
        "customer balance for <name>",
        "who owes me the most",
        "customer growth this month",
    ],
    "Expenses": [
        "expenses this month",
        "expense breakdown",
        "expenses last month",
    ],
    "Profit": [
        "profit and loss",
        "profit last month",
        "margin this month",
        "what is gross profit this month",
    ],
}


class ResponseComposer:
    def unsupported(
        self,
        *,
        supported_hints: list[str],
        close_matches: list[str] | None = None,
    ) -> dict:
        examples = supported_hints[:6]
        return {
            "supported": False,
            "intent": None,
            "parameters": {},
            "result": None,
            "message": (
                "I can help with dashboard questions about sales, products, stock, expenses, receivables, and customer balances. "
                "Try something like: "
                + ", ".join(examples)
                + "."
            ),
            "confidence": 0.0,
            "suggestions": close_matches or examples[:3],
            "highlights": [],
            "table": None,
        }

    def insufficient_followup(self) -> dict:
        return {
            "supported": False,
            "intent": None,
            "parameters": {},
            "result": None,
            "message": (
                "I do not have enough recent assistant context to resolve that follow-up. "
                "Please restate the business question with the metric, customer, product, or date range."
            ),
            "confidence": 0.0,
            "suggestions": [],
            "highlights": [],
            "table": None,
        }

    def compose(
        self,
        *,
        supported: bool,
        intent: str | None,
        parameters: dict,
        result: dict | None,
        message: str,
        confidence: float = 0.0,
        suggestions: list | None = None,
        highlights: list | None = None,
        table: dict | None = None,
    ) -> dict:
        return {
            "supported": supported,
            "intent": intent,
            "parameters": parameters,
            "result": result,
            "message": message.strip(),
            "confidence": round(confidence, 3),
            "suggestions": suggestions if suggestions is not None else [],
            "highlights": highlights if highlights is not None else [],
            "table": table,
        }
