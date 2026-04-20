from __future__ import annotations

from types import SimpleNamespace

from app.services.copilot import fuzzy
from app.services.copilot.composer import ResponseComposer
from app.services.copilot.dashboard_runtime import answer_dashboard_intent
from app.services.copilot.memory import get_latest_session, persist_exchange
from app.services.copilot.router import ParsedDashboardIntent, SUPPORTED_QUESTION_HINTS, parse_dashboard_question


class InternalCopilotProvider:
    def __init__(self) -> None:
        self.composer = ResponseComposer()

    async def answer(self, db, *, question: str, current_user, dashboard_context: dict | None = None) -> dict:
        current_user_view = SimpleNamespace(
            id=getattr(current_user, "id", None),
            role=getattr(current_user, "role", None),
            permissions=getattr(current_user, "permissions", None),
        )
        user_id = current_user_view.id
        normalized_question = fuzzy.normalize(question or "")
        session = await get_latest_session(db, user_id=user_id)
        parsed = parse_dashboard_question(normalized_question, dashboard_context=dashboard_context)

        contextual = _resolve_contextual_reference(normalized_question, parsed, session)
        if contextual is not None:
            parsed = contextual

        if parsed.intent is None:
            parsed = _resolve_followup(normalized_question, session, dashboard_context)

        if parsed is None or parsed.intent is None:
            close_matches = fuzzy.closest_matches(question, SUPPORTED_QUESTION_HINTS, limit=3)
            response = self.composer.unsupported(
                supported_hints=SUPPORTED_QUESTION_HINTS,
                close_matches=close_matches,
            )
            await persist_exchange(
                db,
                user_id=user_id,
                question=question,
                response=response,
                parsed=parsed,
            )
            return response

        response = await answer_dashboard_intent(
            db,
            current_user=current_user_view,
            parsed=parsed,
            composer=self.composer,
        )
        await persist_exchange(
            db,
            user_id=user_id,
            question=question,
            response=response,
            parsed=parsed,
        )
        return response


def _resolve_contextual_reference(
    question: str,
    parsed: ParsedDashboardIntent | None,
    session,
) -> ParsedDashboardIntent | None:
    if session is None or parsed is None or parsed.intent is None:
        return None
    text = (question or "").strip().lower()
    last_entity_ids = session.get_last_entity_ids() if hasattr(session, "get_last_entity_ids") else []
    last_intent = getattr(session, "last_intent", None)

    if parsed.intent == "product_details" and "that" in text and last_entity_ids:
        return ParsedDashboardIntent("product_details", {"product_id": last_entity_ids[0]}, entity_ids=[last_entity_ids[0]])

    if last_intent in {"customer_balances_top", "overdue_customers"} and "their balance" in text and last_entity_ids:
        return ParsedDashboardIntent("customer_balance", {"customer_id": last_entity_ids[0]}, entity_ids=[last_entity_ids[0]])

    return None


def _resolve_followup(question: str, session, dashboard_context: dict | None = None) -> ParsedDashboardIntent | None:
    if session is None:
        return None

    text = (question or "").strip().lower()
    last_intent = getattr(session, "last_intent", None)
    last_entity_ids = session.get_last_entity_ids() if hasattr(session, "get_last_entity_ids") else []

    if any(phrase in text for phrase in ["show me more", "expand", "full list"]):
        if last_intent in {"top_products", "recent_activity", "stock_levels", "overdue_customers", "customer_balances_top"}:
            parameters = {"limit": 25}
            return ParsedDashboardIntent(last_intent, parameters, entity_ids=last_entity_ids)

    if any(phrase in text for phrase in ["what about yesterday", "compare to yesterday", "compared to yesterday"]):
        return parse_dashboard_question(f"what changed compared to yesterday", dashboard_context=dashboard_context)

    if "that item" in text and last_entity_ids and last_intent in {"product_details", "stock_levels"}:
        return ParsedDashboardIntent("product_details", {"product_id": last_entity_ids[0]}, entity_ids=[last_entity_ids[0]])

    return None
