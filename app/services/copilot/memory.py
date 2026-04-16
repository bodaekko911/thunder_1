from __future__ import annotations

import calendar
import json
from datetime import date
from typing import Any

from sqlalchemy import select

from app.models.assistant import AssistantMessage, AssistantSession
from app.services.copilot.contracts import ParsedDashboardIntent


def _parse_iso_date(value: Any) -> date | None:
    if not value:
        return None


def _month_range_from_value(value: Any) -> tuple[date, date] | None:
    if not value:
        return None
    try:
        year, month_number = int(str(value)[:4]), int(str(value)[5:7])
    except (TypeError, ValueError, IndexError):
        return None
    last_day = calendar.monthrange(year, month_number)[1]
    return date(year, month_number, 1), date(year, month_number, last_day)
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


async def _load_latest_session(db, *, user_id: int, channel: str) -> AssistantSession | None:
    result = await db.execute(
        select(AssistantSession)
        .where(
            AssistantSession.user_id == user_id,
            AssistantSession.channel == channel,
        )
        .order_by(AssistantSession.updated_at.desc(), AssistantSession.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_latest_session(db, *, user_id: int, channel: str = "dashboard") -> AssistantSession | None:
    try:
        return await _load_latest_session(db, user_id=user_id, channel=channel)
    except Exception:
        return None


async def get_or_create_session(db, *, user_id: int, channel: str = "dashboard") -> AssistantSession | None:
    try:
        session = await _load_latest_session(db, user_id=user_id, channel=channel)
        if session is not None:
            return session
        session = AssistantSession(user_id=user_id, channel=channel)
        session.set_last_entity_ids([])
        db.add(session)
        await db.flush()
        return session
    except Exception:
        return None


def apply_intent_context(session: AssistantSession | None, parsed: ParsedDashboardIntent | None) -> None:
    if session is None or parsed is None:
        return
    session.last_intent = parsed.intent
    date_from = _parse_iso_date(parsed.parameters.get("date_from"))
    date_to = _parse_iso_date(parsed.parameters.get("date_to"))
    if date_from is None or date_to is None:
        month_range = _month_range_from_value(parsed.parameters.get("month"))
        if month_range is not None:
            date_from, date_to = month_range
    session.set_last_date_range(date_from, date_to)
    session.set_last_entity_ids(parsed.entity_ids)
    session.last_comparison_baseline = parsed.comparison_baseline


def _infer_entity_ids(intent: str | None, result: dict | None) -> list[int]:
    if not intent or not result:
        return []
    if intent == "customer_balance":
        selected = result.get("selected") or {}
        client_id = selected.get("client_id")
        return [int(client_id)] if isinstance(client_id, int) else []
    if intent == "overdue_customers":
        return [int(item["client_id"]) for item in result.get("customers", []) if isinstance(item.get("client_id"), int)]
    if intent in {"product_details"}:
        selected = result.get("selected") or {}
        product_id = selected.get("product_id")
        return [int(product_id)] if isinstance(product_id, int) else []
    if intent in {"stock_levels"}:
        return [int(item["product_id"]) for item in result.get("items", []) if isinstance(item.get("product_id"), int)]
    return []


def _serialize_json(payload: dict | None) -> str | None:
    if payload is None:
        return None
    return json.dumps(payload, default=str, ensure_ascii=True)


async def append_message(
    db,
    *,
    session: AssistantSession | None,
    role: str,
    message_text: str,
    intent: str | None = None,
    parameters: dict | None = None,
    result: dict | None = None,
) -> AssistantMessage | None:
    if session is None:
        return None
    try:
        message = AssistantMessage(
            session_id=session.id,
            role=role,
            message_text=message_text,
            intent=intent,
            parameters_json=_serialize_json(parameters),
            result_json=_serialize_json(result),
        )
        db.add(message)
        await db.flush()
        return message
    except Exception:
        return None


async def persist_exchange(
    db,
    *,
    user_id: int,
    question: str,
    response: dict,
    parsed: ParsedDashboardIntent | None,
    channel: str = "dashboard",
) -> None:
    try:
        session = await get_or_create_session(db, user_id=user_id, channel=channel)
        apply_intent_context(session, parsed)
        inferred_entity_ids = _infer_entity_ids(response.get("intent"), response.get("result"))
        if session is not None and inferred_entity_ids:
            session.set_last_entity_ids(inferred_entity_ids)
        await append_message(
            db,
            session=session,
            role="user",
            message_text=question,
            intent=parsed.intent if parsed else None,
            parameters=parsed.parameters if parsed else None,
        )
        await append_message(
            db,
            session=session,
            role="assistant",
            message_text=response.get("message", ""),
            intent=response.get("intent"),
            parameters=response.get("parameters"),
            result=response.get("result"),
        )
        if session is not None:
            db.add(session)
        await db.commit()
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass
