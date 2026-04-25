from __future__ import annotations

from app.services.copilot.providers.internal import InternalCopilotProvider
from app.services.copilot.router import parse_dashboard_question


async def answer_dashboard_question(
    db,
    *,
    question: str,
    current_user,
    dashboard_context: dict | None = None,
) -> dict:
    provider = InternalCopilotProvider()
    return await provider.answer(
        db,
        question=question,
        current_user=current_user,
        dashboard_context=dashboard_context,
    )


__all__ = ["answer_dashboard_question", "parse_dashboard_question"]
