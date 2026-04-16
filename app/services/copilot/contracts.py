from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ParsedDashboardIntent:
    intent: str | None
    parameters: dict = field(default_factory=dict)
    entity_ids: list[int] = field(default_factory=list)
    comparison_baseline: str | None = None


@dataclass(frozen=True)
class CopilotResult:
    supported: bool
    intent: str | None
    parameters: dict
    result: dict | None
    message: str
