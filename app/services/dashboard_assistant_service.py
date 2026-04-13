from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import re

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import ensure_permission, has_permission
from app.models.b2b import B2BInvoice
from app.models.invoice import Invoice
from app.models.user import User
from app.services.expense_service import get_summary as get_expense_summary


SUPPORTED_QUESTION_HINTS = [
    "today's sales",
    "top products",
    "low-stock items",
    "expenses this month",
    "unpaid invoices",
]


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


async def answer_dashboard_question(
    db: AsyncSession,
    *,
    question: str,
    current_user: User,
) -> dict:
    parsed = parse_dashboard_question(question)
    if parsed.intent is None:
        return {
            "supported": False,
            "intent": None,
            "parameters": {},
            "result": None,
            "message": "I can currently answer: " + ", ".join(SUPPORTED_QUESTION_HINTS) + ".",
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
