from __future__ import annotations

from app.core.log import logger
from app.services.copilot.providers.internal import InternalCopilotProvider


def _build_provider():
    logger.info("Assistant provider selected", extra={"assistant_provider": "internal"})
    return InternalCopilotProvider()


async def answer_question(db, *, question: str, current_user) -> dict:
    provider = _build_provider()
    return await provider.answer(db, question=question, current_user=current_user)
