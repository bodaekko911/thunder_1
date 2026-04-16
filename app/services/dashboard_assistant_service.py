from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.permissions import ensure_permission, has_permission
from app.models.b2b import B2BInvoice
from app.models.invoice import Invoice
from app.models.user import User


SUPPORTED_QUESTION_HINTS = [
    "today's sales",
    "top products",
    "low-stock items",
    "expenses this month",
    "unpaid invoices",
    "customer balances",
    "sales by period",
]

# ── Tool definitions for Claude ───────────────────────────────────────────────

_TOOL_DEFINITIONS = [
    {
        "name": "get_sales_summary",
        "description": (
            "Get total POS and B2B sales revenue with refunds deducted for a given date range. "
            "Use today's date for daily queries, or first-of-month through today for monthly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "Start date in YYYY-MM-DD format"},
                "date_to": {"type": "string", "description": "End date in YYYY-MM-DD format"},
            },
            "required": ["date_from", "date_to"],
        },
    },
    {
        "name": "get_top_products",
        "description": "Get the top-selling products by revenue for a date range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "Start date YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "End date YYYY-MM-DD"},
                "limit": {"type": "integer", "description": "Maximum number of products to return (default 10)"},
            },
            "required": ["date_from", "date_to"],
        },
    },
    {
        "name": "get_low_stock_items",
        "description": "Get products with low or zero stock.",
        "input_schema": {
            "type": "object",
            "properties": {
                "threshold": {
                    "type": "integer",
                    "description": "Stock level at or below which a product is considered low-stock (default 5)",
                },
            },
        },
    },
    {
        "name": "get_expenses_summary",
        "description": (
            "Get expense totals for the current and previous month with category breakdown. "
            "Requires accounting permission."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_unpaid_invoices_summary",
        "description": (
            "Get count and outstanding amounts for unpaid POS and B2B invoices. "
            "Requires page_pos or page_b2b permission."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_customer_balances",
        "description": (
            "Get B2B customers ranked by their outstanding unpaid balance. "
            "Requires page_b2b permission."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Maximum number of customers to return (default 10)"},
            },
        },
    },
    {
        "name": "get_sales_by_period",
        "description": "Get POS sales aggregated by day, week, or month for a date range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "enum": ["daily", "weekly", "monthly"],
                    "description": "Aggregation period",
                },
                "date_from": {"type": "string", "description": "Start date YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "End date YYYY-MM-DD"},
            },
            "required": ["period", "date_from", "date_to"],
        },
    },
]


# ── Tool executor ─────────────────────────────────────────────────────────────

async def _execute_tool(
    db: AsyncSession,
    current_user: User,
    name: str,
    input_data: dict,
) -> dict:
    from app.services.assistant_tools import (
        get_customer_balances as _get_customer_balances,
        get_expenses_summary as _get_expenses_summary,
        get_low_stock_items as _get_low_stock_items,
        get_sales_by_period as _get_sales_by_period,
        get_sales_summary as _get_sales_summary,
        get_top_products as _get_top_products,
        get_unpaid_invoices_summary as _get_unpaid_invoices_summary,
    )

    today = date.today()

    if name == "get_sales_summary":
        return await _get_sales_summary(
            db,
            date_from=input_data.get("date_from", today.isoformat()),
            date_to=input_data.get("date_to", today.isoformat()),
        )

    if name == "get_top_products":
        return await _get_top_products(
            db,
            date_from=input_data.get("date_from", today.replace(day=1).isoformat()),
            date_to=input_data.get("date_to", today.isoformat()),
            limit=int(input_data.get("limit", 10)),
        )

    if name == "get_low_stock_items":
        return await _get_low_stock_items(db, threshold=int(input_data.get("threshold", 5)))

    if name == "get_expenses_summary":
        if not has_permission(current_user, "page_accounting"):
            return {"error": "Permission denied: page_accounting is required to view expenses."}
        return await _get_expenses_summary(db)

    if name == "get_unpaid_invoices_summary":
        if not (has_permission(current_user, "page_pos") or has_permission(current_user, "page_b2b")):
            return {"error": "Permission denied: page_pos or page_b2b is required to view unpaid invoices."}
        return await _get_unpaid_invoices_summary(db)

    if name == "get_customer_balances":
        if not has_permission(current_user, "page_b2b"):
            return {"error": "Permission denied: page_b2b is required to view customer balances."}
        return await _get_customer_balances(db, limit=int(input_data.get("limit", 10)))

    if name == "get_sales_by_period":
        return await _get_sales_by_period(
            db,
            period=input_data.get("period", "daily"),
            date_from=input_data.get("date_from"),
            date_to=input_data.get("date_to"),
        )

    return {"error": f"Unknown tool: {name}"}


# ── Claude agentic path ───────────────────────────────────────────────────────

async def _answer_with_claude(
    db: AsyncSession,
    *,
    question: str,
    current_user: User,
) -> dict:
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    today_str = date.today().isoformat()

    system = (
        f"You are a business analytics assistant for Thunder ERP. Today is {today_str}. "
        "Always call one of the available tools to retrieve real data before answering. "
        "Answer in 1–3 concise sentences that include the actual numbers from the tool result. "
        "Reply in the same language the user used to ask the question."
    )

    messages: list[dict] = [{"role": "user", "content": question}]

    last_intent: str | None = None
    last_parameters: dict = {}
    last_result: Any = None

    for _ in range(5):  # guard against runaway loops
        response = await client.messages.create(
            model=settings.ANTHROPIC_MODEL,
            max_tokens=1024,
            system=system,
            tools=_TOOL_DEFINITIONS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            message_text = next(
                (block.text for block in response.content if hasattr(block, "text")),
                "",
            )
            return {
                "supported": last_intent is not None,
                "intent": last_intent,
                "parameters": last_parameters,
                "result": last_result,
                "message": message_text,
            }

        if response.stop_reason == "tool_use":
            assistant_content = []
            tool_result_content = []

            for block in response.content:
                if block.type == "text":
                    assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    assistant_content.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })
                    tool_output = await _execute_tool(db, current_user, block.name, block.input)
                    last_intent = block.name
                    last_parameters = dict(block.input)
                    last_result = tool_output
                    tool_result_content.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(tool_output, default=str),
                    })

            messages = messages + [
                {"role": "assistant", "content": assistant_content},
                {"role": "user", "content": tool_result_content},
            ]
        else:
            break

    return {
        "supported": False,
        "intent": None,
        "parameters": {},
        "result": None,
        "message": "I was unable to answer that question.",
    }


# ── Keyword-matching fallback (used when API key is absent) ───────────────────

@dataclass(frozen=True)
class ParsedDashboardIntent:
    intent: str | None
    parameters: dict


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

    if any(phrase in text for phrase in ["top products", "best selling", "top sellers", "best sellers"]):
        month_start = date.today().replace(day=1).isoformat()
        return ParsedDashboardIntent("top_products", {"date_from": month_start, "date_to": date.today().isoformat(), "limit": 10})

    if any(phrase in text for phrase in ["low stock", "low-stock", "out of stock", "stock running low"]):
        return ParsedDashboardIntent("low_stock", {"status": "low_stock"})

    if any(phrase in text for phrase in ["expenses this month", "this month expenses", "monthly expenses", "month expenses"]):
        month = date.today().strftime("%Y-%m")
        return ParsedDashboardIntent("expenses_month", {"month": month})

    if any(phrase in text for phrase in ["unpaid invoices", "open invoices", "outstanding invoices", "settle later invoices"]):
        return ParsedDashboardIntent("unpaid_invoices", {"status": "unpaid"})

    return ParsedDashboardIntent(None, {})


async def _get_unpaid_invoice_summary(db: AsyncSession) -> dict:
    pos_result = await db.execute(select(func.count(Invoice.id)).where(Invoice.status == "unpaid"))
    b2b_count_result = await db.execute(
        select(func.count(B2BInvoice.id)).where(B2BInvoice.status.in_(["unpaid", "partial"]))
    )
    b2b_outstanding_result = await db.execute(
        select(func.coalesce(func.sum(B2BInvoice.total - B2BInvoice.amount_paid), 0)).where(
            B2BInvoice.status.in_(["unpaid", "partial"])
        )
    )
    return {
        "pos_unpaid_count": int(pos_result.scalar() or 0),
        "b2b_unpaid_count": int(b2b_count_result.scalar() or 0),
        "b2b_outstanding_amount": float(b2b_outstanding_result.scalar() or 0),
    }


async def _answer_with_keywords(
    db: AsyncSession,
    *,
    question: str,
    current_user: User,
) -> dict:
    """Legacy keyword-matching path used when ANTHROPIC_API_KEY is not configured."""
    parsed = parse_dashboard_question(question)
    if parsed.intent is None:
        return {
            "supported": False,
            "intent": None,
            "parameters": {},
            "result": None,
            "message": (
                "AI assistant is not configured. "
                "Supported questions: " + ", ".join(SUPPORTED_QUESTION_HINTS) + "."
            ),
        }

    if parsed.intent == "expenses_month":
        await ensure_permission(db, current_user, "page_accounting", path="/dashboard/assistant")
    elif parsed.intent == "unpaid_invoices":
        if not (has_permission(current_user, "page_pos") or has_permission(current_user, "page_b2b")):
            await ensure_permission(db, current_user, "page_pos", path="/dashboard/assistant")

    if parsed.intent in {"sales_today", "top_products", "low_stock"}:
        from app.routers.dashboard import dashboard_data

        snapshot = await dashboard_data(db=db)
        if parsed.intent == "sales_today":
            return {
                "supported": True,
                "intent": parsed.intent,
                "parameters": parsed.parameters,
                "result": {
                    "total_sales": snapshot["total_today"],
                    "pos_sales": snapshot["pos_today"],
                    "b2b_sales": snapshot["b2b_today"],
                    "refunds": snapshot["ref_today"],
                },
                "message": f"Today's total sales are {snapshot['total_today']:.2f}.",
            }
        if parsed.intent == "top_products":
            return {
                "supported": True,
                "intent": parsed.intent,
                "parameters": parsed.parameters,
                "result": {
                    "items": snapshot["top_products"],
                    "count": len(snapshot["top_products"]),
                },
                "message": "Here are the current top products for this month.",
            }
        return {
            "supported": True,
            "intent": parsed.intent,
            "parameters": parsed.parameters,
            "result": {
                "items": snapshot["low_stock"],
                "count": snapshot["low_stock_count"],
            },
            "message": f"There are {snapshot['low_stock_count']} low-stock items right now.",
        }

    if parsed.intent == "expenses_month":
        from app.services.expense_service import get_summary as get_expense_summary

        summary = await get_expense_summary(db)
        return {
            "supported": True,
            "intent": parsed.intent,
            "parameters": parsed.parameters,
            "result": {
                "this_month": float(summary["this_month"]),
                "last_month": float(summary["last_month"]),
                "breakdown": summary["breakdown"][:5],
            },
            "message": f"Expenses this month are {float(summary['this_month']):.2f}.",
        }

    unpaid = await _get_unpaid_invoice_summary(db)
    return {
        "supported": True,
        "intent": parsed.intent,
        "parameters": parsed.parameters,
        "result": unpaid,
        "message": (
            f"There are {unpaid['pos_unpaid_count']} unpaid POS invoices and "
            f"{unpaid['b2b_unpaid_count']} unpaid or partial B2B invoices."
        ),
    }


# ── Public entry point ────────────────────────────────────────────────────────

async def answer_dashboard_question(
    db: AsyncSession,
    *,
    question: str,
    current_user: User,
) -> dict:
    if not settings.ANTHROPIC_API_KEY:
        return await _answer_with_keywords(db, question=question, current_user=current_user)
    return await _answer_with_claude(db, question=question, current_user=current_user)
