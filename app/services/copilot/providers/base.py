from __future__ import annotations

from typing import Protocol


class CopilotProvider(Protocol):
    async def answer(self, db, *, question: str, current_user) -> dict: ...
