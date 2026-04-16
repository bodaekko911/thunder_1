from __future__ import annotations


class ResponseComposer:
    def unsupported(self, *, supported_hints: list[str]) -> dict:
        return {
            "supported": False,
            "intent": None,
            "parameters": {},
            "result": None,
            "message": (
                "I can currently help with deterministic ERP questions such as "
                + ", ".join(supported_hints)
                + "."
            ),
        }

    def insufficient_followup(self) -> dict:
        return {
            "supported": False,
            "intent": None,
            "parameters": {},
            "result": None,
            "message": (
                "I do not have enough recent assistant context to resolve that follow-up. "
                "Please restate the business question with the metric, customer, product, or date range."
            ),
        }

    def compose(
        self,
        *,
        supported: bool,
        intent: str | None,
        parameters: dict,
        result: dict | None,
        message: str,
    ) -> dict:
        return {
            "supported": supported,
            "intent": intent,
            "parameters": parameters,
            "result": result,
            "message": message.strip(),
        }
