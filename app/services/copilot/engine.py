from __future__ import annotations

from app.core.log import logger
from app.services.copilot.providers.local_llm import LocalCopilotProvider


def _build_provider():
    logger.info("Assistant provider selected", extra={"assistant_provider": "local_llm"})
    return LocalCopilotProvider()


async def answer_question(db, *, question: str, current_user, dashboard_context: dict | None = None) -> dict:
    provider = _build_provider()
    return await provider.answer(
        db,
        question=question,
        current_user=current_user,
        dashboard_context=dashboard_context,
    )
