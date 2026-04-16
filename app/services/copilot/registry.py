from __future__ import annotations


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, object] = {}

    def register(self, name: str, handler) -> None:
        self._tools[name] = handler

    def get(self, name: str):
        return self._tools.get(name)

    def list_names(self) -> list[str]:
        return sorted(self._tools)
