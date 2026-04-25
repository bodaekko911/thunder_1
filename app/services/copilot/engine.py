from __future__ import annotations

from app.core.log import logger
from app.core.config import settings
from app.services.copilot.providers.cloud_llm import CloudCopilotProvider
from app.services.copilot.providers.internal import InternalCopilotProvider


def _build_provider():
    if settings.AI_API_KEY:
        logger.info("Assistant provider selected", extra={"assistant_provider": "cloud_llm"})
        return CloudCopilotProvider()
    logger.info("Assistant provider selected", extra={"assistant_provider": "internal"})
    return InternalCopilotProvider()


async def answer_question(db, *, question: str, current_user, dashboard_context: dict | None = None) -> dict:
    provider = _build_provider()
    return await provider.answer(
        db,
        question=question,
        current_user=current_user,
        dashboard_context=dashboard_context,
    )
