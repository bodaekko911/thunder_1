from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.b2b import B2BInvoice
from app.models.invoice import Invoice
from app.models.user import User
from app.services.copilot.engine import answer_question
from app.services.copilot.router import (
    ParsedDashboardIntent,
    SUPPORTED_QUESTION_HINTS,
    parse_dashboard_question,
)


async def get_unpaid_invoice_summary(db: AsyncSession) -> dict:
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
    dashboard_context: dict | None = None,
) -> dict:
    return await answer_question(
        db,
        question=question,
        current_user=current_user,
        dashboard_context=dashboard_context,
    )


__all__ = [
    "ParsedDashboardIntent",
    "SUPPORTED_QUESTION_HINTS",
    "answer_dashboard_question",
    "get_unpaid_invoice_summary",
    "parse_dashboard_question",
]
