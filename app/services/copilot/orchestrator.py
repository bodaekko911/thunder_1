from __future__ import annotations


class ToolOrchestrator:
    def __init__(self, registry):
        self.registry = registry

    async def execute(self, db, current_user, *, tool_name: str, input_data: dict) -> dict:
        tool = self.registry.get(tool_name)
        if tool is None:
            return {"error": f"Unknown tool: {tool_name}"}
        return await tool(db, current_user=current_user, input_data=input_data)
