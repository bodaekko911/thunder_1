from __future__ import annotations

from datetime import date, timedelta

from app.core.permissions import ensure_permission, has_permission
from app.services.copilot import fuzzy, time_parser
from app.services.copilot.composer import ResponseComposer, SUPPORTED_QUESTIONS_BY_CATEGORY
from app.services.copilot.memory import get_latest_session, persist_exchange
from app.services.copilot.router import ParsedDashboardIntent, SUPPORTED_QUESTION_HINTS, parse_dashboard_question
from app.services.copilot.suggestions import build_highlights, build_suggestions, build_table
from app.services.copilot.tools import execute_tool


class InternalCopilotProvider:
    def __init__(self) -> None:
        self.composer = ResponseComposer()

    async def answer(self, db, *, question: str, current_user) -> dict:
        normalized_question = fuzzy.normalize(question or "")
        parsed = parse_dashboard_question(normalized_question)
        session = None
        if parsed.intent is None or _requires_followup_context(parsed):
            session = await get_latest_session(db, user_id=current_user.id)
            contextual = _resolve_contextual_reference(normalized_question, parsed, session)
            if contextual is not None:
                parsed = contextual
        if parsed.intent is None:
            if session is None:
                session = await get_latest_session(db, user_id=current_user.id)
            parsed = _resolve_followup(normalized_question, session)
        if parsed is None or _requires_followup_context(parsed):
            if _looks_like_followup(normalized_question):
                response = self.composer.insufficient_followup()
            else:
                close_matches = fuzzy.closest_matches(question, SUPPORTED_QUESTION_HINTS, limit=3)
                response = self.composer.unsupported(
                    supported_hints=SUPPORTED_QUESTION_HINTS,
                    close_matches=close_matches,
                )
            await persist_exchange(
                db,
                user_id=current_user.id,
                question=question,
                response=response,
                parsed=parsed,
            )
            return response

        response = await _answer_from_intent(
            db,
            current_user=current_user,
            parsed=parsed,
            composer=self.composer,
        )
        await persist_exchange(
            db,
            user_id=current_user.id,
            question=question,
            response=response,
            parsed=parsed,
        )
        return response


# ── Follow-up detection ────────────────────────────────────────────────────────

def _requires_followup_context(parsed: ParsedDashboardIntent | None) -> bool:
    if parsed is None or parsed.intent is None:
        return False
    if parsed.intent == "product_details" and str(parsed.parameters.get("product_query", "")).strip().lower() == "that item":
        return True
    return False


def _looks_like_followup(question: str) -> bool:
    text = (question or "").strip().lower()
    return any(
        phrase in text
        for phrase in [
            "what about",
            "compare that",
            "previous week",
            "last month",
            "and last week",
            "same for",
            "prior year",
            "prior month",
            "which customers",
            "that item",
            "that customer",
            "show their",
            "their invoices",
            "details for them",
            "show me more",
            "expand",
            "give me the full list",
            "export",
            "download",
        ]
    )


def _resolve_contextual_reference(
    question: str,
    parsed: ParsedDashboardIntent | None,
    session,
) -> ParsedDashboardIntent | None:
    if session is None:
        return None
    text = (question or "").strip().lower()
    last_entity_ids = session.get_last_entity_ids() if hasattr(session, "get_last_entity_ids") else []
    last_intent = getattr(session, "last_intent", None)

    # "that item" → resolve product from prior entity
    if parsed is not None and parsed.intent == "product_details" and "that item" in text and last_entity_ids:
        return ParsedDashboardIntent(
            "product_details",
            {"product_id": last_entity_ids[0]},
            entity_ids=[last_entity_ids[0]],
        )

    # Pronoun resolution after overdue_customers
    _pronoun_phrases = [
        "show their invoices",
        "details for them",
        "what about their balance",
        "their invoices",
    ]
    if last_intent == "overdue_customers" and any(p in text for p in _pronoun_phrases) and last_entity_ids:
        return ParsedDashboardIntent(
            "customer_balance",
            {"customer_id": last_entity_ids[0]},
            entity_ids=[last_entity_ids[0]],
        )

    return None


def _resolve_followup(question: str, session) -> ParsedDashboardIntent | None:
    if session is None:
        return None

    text = (question or "").strip().lower()
    last_intent = session.last_intent
    last_entity_ids = session.get_last_entity_ids() if hasattr(session, "get_last_entity_ids") else []
    last_date_from = session.last_date_from
    last_date_to = session.last_date_to

    # ── "show me the product details for that item" ────────────────────────────
    if "show me the product details for that item" in text or "product details for that item" in text:
        if last_entity_ids and last_intent in {"stock_levels", "product_details"}:
            return ParsedDashboardIntent(
                "product_details", {"product_id": last_entity_ids[0]}, entity_ids=[last_entity_ids[0]]
            )
        return None

    # ── "which customers caused most of it" ───────────────────────────────────
    if "which customers caused most of it" in text:
        if last_intent in {"unpaid_invoices", "overdue_customers", "customer_balance"}:
            return ParsedDashboardIntent("overdue_customers", {"limit": 10})
        return None

    # ── "export" / "download" ─────────────────────────────────────────────────
    if any(w in text for w in ["export", "download"]):
        return ParsedDashboardIntent(
            "export_placeholder",
            {},
        )

    # ── "show me more" / "expand" / "give me the full list" ───────────────────
    if any(p in text for p in ["show me more", "expand", "give me the full list"]):
        if last_intent:
            last_limit = session.last_parameters.get("limit") if hasattr(session, "last_parameters") else None
            new_limit = min(50, (int(last_limit) if last_limit else 10) * 3)
            return ParsedDashboardIntent(last_intent, {"limit": new_limit})
        return None

    # ── "what about last month" ────────────────────────────────────────────────
    if "what about last month" in text:
        if last_intent == "expense_breakdown":
            month = _previous_month_from_range(last_date_from, last_date_to)
            if month:
                return ParsedDashboardIntent("expense_breakdown", {"month": month}, comparison_baseline="last_month")
            return None
        if last_intent in {"sales_by_period", "profit_loss_summary", "sales_summary"}:
            shifted = _shift_to_previous_month(last_date_from, last_date_to)
            if shifted is None:
                return None
            params = {
                "date_from": shifted[0].isoformat(),
                "date_to": shifted[1].isoformat(),
            }
            if last_intent == "sales_by_period":
                params["period"] = session.last_comparison_baseline or "daily"
            return ParsedDashboardIntent(last_intent, params, comparison_baseline="last_month")
        if last_intent == "expenses_month":
            month = _previous_month_from_range(last_date_from, last_date_to)
            if month:
                return ParsedDashboardIntent("expenses_month", {"month": month}, comparison_baseline="last_month")
        return None

    # ── "compare that to previous week" ───────────────────────────────────────
    if "compare that to previous week" in text:
        if last_intent not in {"sales_by_period", "profit_loss_summary", "sales_summary"}:
            return None
        if last_date_from is None or last_date_to is None:
            return None
        comparison_parameters = {
            "date_from": last_date_from.isoformat(),
            "date_to": last_date_to.isoformat(),
            "comparison_date_from": (last_date_from - timedelta(days=7)).isoformat(),
            "comparison_date_to": (last_date_to - timedelta(days=7)).isoformat(),
        }
        if last_intent == "sales_by_period":
            comparison_parameters["period"] = session.last_comparison_baseline or "daily"
        return ParsedDashboardIntent(last_intent, comparison_parameters, comparison_baseline="previous_week")

    # ── Date-shifted follow-ups via time_parser ────────────────────────────────
    _date_shiftable = {"sales_by_period", "profit_loss_summary", "sales_summary", "expense_breakdown", "expenses_month"}
    if last_intent in _date_shiftable:
        shifted = _try_date_shift_followup(text, last_intent, last_date_from, last_date_to, session)
        if shifted is not None:
            return shifted

    return None


def _try_date_shift_followup(
    text: str,
    last_intent: str,
    last_date_from: date | None,
    last_date_to: date | None,
    session,
) -> ParsedDashboardIntent | None:
    """Detect "and last week?", "same for yesterday", "prior year", etc. as relative date shifts."""
    # Patterns that signal a date-shifted follow-up
    _shift_triggers = [
        "and last week",
        "same for last week",
        "same for yesterday",
        "same for this week",
        "and yesterday",
        "and this week",
        "and last month",
        "same for last month",
        "prior year",
        "and last year",
        "same for last year",
        "previous month",
        "what about the previous week",
        "what about last week",
        "what about yesterday",
    ]
    if not any(trigger in text for trigger in _shift_triggers):
        return None

    if last_date_from is None or last_date_to is None:
        return None

    # Use time_parser to get a new absolute range
    new_range = time_parser.parse_time_expression(text)
    if new_range is None:
        return None

    new_from, new_to = new_range

    # If new range equals the current session range, shift back by the session window length
    duration = (last_date_to - last_date_from).days
    if new_from == last_date_from and new_to == last_date_to and duration > 0:
        new_from = last_date_from - timedelta(days=duration + 1)
        new_to = last_date_to - timedelta(days=duration + 1)

    if last_intent in {"expense_breakdown", "expenses_month"}:
        month = new_from.strftime("%Y-%m")
        return ParsedDashboardIntent(last_intent, {"month": month}, comparison_baseline="shifted")

    params: dict = {"date_from": new_from.isoformat(), "date_to": new_to.isoformat()}
    if last_intent == "sales_by_period":
        params["period"] = getattr(session, "last_comparison_baseline", None) or "daily"
    return ParsedDashboardIntent(last_intent, params, comparison_baseline="shifted")


# ── Answer dispatch ────────────────────────────────────────────────────────────

async def _answer_from_intent(
    db, *, current_user, parsed: ParsedDashboardIntent, composer: ResponseComposer
) -> dict:
    confidence = parsed.confidence

    # ── Permission guards ──────────────────────────────────────────────────────
    if parsed.intent == "expenses_month":
        await ensure_permission(db, current_user, "page_accounting", path="/dashboard/assistant")
    elif parsed.intent == "unpaid_invoices":
        if not (has_permission(current_user, "page_pos") or has_permission(current_user, "page_b2b")):
            await ensure_permission(db, current_user, "page_pos", path="/dashboard/assistant")

    # ── Help ──────────────────────────────────────────────────────────────────
    if parsed.intent == "help":
        _result = {"categories": SUPPORTED_QUESTIONS_BY_CATEGORY}
        return composer.compose(
            supported=True,
            intent="help",
            parameters={},
            result=_result,
            message="Here's what I can help with. Try any of the examples below or ask in your own words.",
            confidence=1.0,
            suggestions=build_suggestions("help", _result),
            highlights=build_highlights("help", _result),
            table=build_table("help", _result),
        )

    # ── Export placeholder ────────────────────────────────────────────────────
    if parsed.intent == "export_placeholder":
        return composer.compose(
            supported=True,
            intent="export_placeholder",
            parameters={},
            result=None,
            message="Export isn't wired up yet — ask me again once the report endpoint exists.",
            confidence=0.0,
            suggestions=[],
            highlights=[],
            table=None,
        )

    # ── Dashboard snapshot intents (sales_today, top_products, low_stock) ─────
    if parsed.intent in {"sales_today", "top_products", "low_stock"}:
        from app.routers.dashboard import dashboard_data

        snapshot = await dashboard_data(db=db)
        if parsed.intent == "sales_today":
            _result = {
                "total_sales": snapshot["total_today"],
                "pos_sales": snapshot["pos_today"],
                "b2b_sales": snapshot["b2b_today"],
                "refunds": snapshot["ref_today"],
            }
            return composer.compose(
                supported=True,
                intent=parsed.intent,
                parameters=parsed.parameters,
                result=_result,
                message=f"Today's total sales are {snapshot['total_today']:.2f}.",
                confidence=confidence,
                suggestions=build_suggestions(parsed.intent, _result),
                highlights=build_highlights(parsed.intent, _result),
                table=build_table(parsed.intent, _result),
            )
        if parsed.intent == "top_products":
            _result = {
                "items": snapshot["top_products"],
                "count": len(snapshot["top_products"]),
            }
            return composer.compose(
                supported=True,
                intent=parsed.intent,
                parameters=parsed.parameters,
                result=_result,
                message="Here are the current top products for this month.",
                confidence=confidence,
                suggestions=build_suggestions(parsed.intent, _result),
                highlights=build_highlights(parsed.intent, _result),
                table=build_table(parsed.intent, _result),
            )
        _result = {
            "items": snapshot["low_stock"],
            "count": snapshot["low_stock_count"],
        }
        return composer.compose(
            supported=True,
            intent=parsed.intent,
            parameters=parsed.parameters,
            result=_result,
            message=f"There are {snapshot['low_stock_count']} low-stock items right now.",
            confidence=confidence,
            suggestions=build_suggestions(parsed.intent, _result),
            highlights=build_highlights(parsed.intent, _result),
            table=build_table(parsed.intent, _result),
        )

    # ── sales_summary ─────────────────────────────────────────────────────────
    if parsed.intent == "sales_summary":
        result = await execute_tool(
            db, current_user=current_user, name="get_sales_summary", input_data=parsed.parameters
        )
        date_from = parsed.parameters.get("date_from", "")
        date_to = parsed.parameters.get("date_to", "")
        total = result.get("total", 0) if isinstance(result, dict) else 0
        return composer.compose(
            supported=True,
            intent=parsed.intent,
            parameters=parsed.parameters,
            result=result,
            message=f"Revenue from {date_from} to {date_to}: {float(total):.2f}.",
            confidence=confidence,
            suggestions=build_suggestions(parsed.intent, result),
            highlights=build_highlights(parsed.intent, result),
            table=build_table(parsed.intent, result),
        )

    # ── customer_balances_top ─────────────────────────────────────────────────
    if parsed.intent == "customer_balances_top":
        if not has_permission(current_user, "page_b2b"):
            result = {"error": "Permission denied: page_b2b is required to view customer balances."}
        else:
            result = await execute_tool(
                db, current_user=current_user, name="get_customer_balances", input_data=parsed.parameters
            )
        return composer.compose(
            supported=True,
            intent=parsed.intent,
            parameters=parsed.parameters,
            result=result,
            message=_message_for_customer_balances_top(result),
            confidence=confidence,
            suggestions=build_suggestions(parsed.intent, result),
            highlights=build_highlights(parsed.intent, result),
            table=build_table(parsed.intent, result),
        )

    # ── product_stock_value ───────────────────────────────────────────────────
    if parsed.intent == "product_stock_value":
        result = await execute_tool(
            db, current_user=current_user, name="get_stock_value_summary", input_data={}
        )
        return composer.compose(
            supported=True,
            intent=parsed.intent,
            parameters=parsed.parameters,
            result=result,
            message=_message_for_stock_value(result),
            confidence=confidence,
            suggestions=build_suggestions(parsed.intent, result),
            highlights=build_highlights(parsed.intent, result),
            table=build_table(parsed.intent, result),
        )

    # ── Tool-backed intents ───────────────────────────────────────────────────
    _tool_map = {
        "sales_by_period": "get_sales_by_period",
        "overdue_customers": "get_overdue_customers",
        "customer_balance": "get_customer_balance",
        "product_details": "get_product_details",
        "stock_levels": "get_stock_levels",
        "expense_breakdown": "get_expense_breakdown",
        "profit_loss_summary": "get_profit_loss_summary",
    }
    if parsed.intent in _tool_map:
        result = await _execute_intent_tool(db, current_user=current_user, parsed=parsed)
        return composer.compose(
            supported=True,
            intent=parsed.intent,
            parameters=parsed.parameters,
            result=result,
            message=_message_for_tool(parsed.intent, result),
            confidence=confidence,
            suggestions=build_suggestions(parsed.intent, result),
            highlights=build_highlights(parsed.intent, result),
            table=build_table(parsed.intent, result),
        )

    # ── expenses_month ────────────────────────────────────────────────────────
    if parsed.intent == "expenses_month":
        from app.services.expense_service import get_summary as get_expense_summary

        summary = await get_expense_summary(db)
        _result = {
            "this_month": float(summary["this_month"]),
            "last_month": float(summary["last_month"]),
            "breakdown": summary["breakdown"][:5],
        }
        return composer.compose(
            supported=True,
            intent=parsed.intent,
            parameters=parsed.parameters,
            result=_result,
            message=f"Expenses this month are {float(summary['this_month']):.2f}.",
            confidence=confidence,
            suggestions=build_suggestions(parsed.intent, _result),
            highlights=build_highlights(parsed.intent, _result),
            table=build_table(parsed.intent, _result),
        )

    # ── unpaid_invoices (fallthrough) ─────────────────────────────────────────
    from app.services.dashboard_assistant_service import get_unpaid_invoice_summary

    unpaid = await get_unpaid_invoice_summary(db)
    return composer.compose(
        supported=True,
        intent=parsed.intent,
        parameters=parsed.parameters,
        result=unpaid,
        message=(
            f"There are {unpaid['pos_unpaid_count']} unpaid POS invoices and "
            f"{unpaid['b2b_unpaid_count']} unpaid or partial B2B invoices."
        ),
        confidence=confidence,
        suggestions=build_suggestions(parsed.intent, unpaid),
        highlights=build_highlights(parsed.intent, unpaid),
        table=build_table(parsed.intent, unpaid),
    )


# ── Tool execution helpers ─────────────────────────────────────────────────────

async def _execute_intent_tool(db, *, current_user, parsed: ParsedDashboardIntent) -> dict:
    _tool_map = {
        "sales_by_period": "get_sales_by_period",
        "overdue_customers": "get_overdue_customers",
        "customer_balance": "get_customer_balance",
        "product_details": "get_product_details",
        "stock_levels": "get_stock_levels",
        "expense_breakdown": "get_expense_breakdown",
        "profit_loss_summary": "get_profit_loss_summary",
    }
    if parsed.comparison_baseline != "previous_week":
        return await execute_tool(
            db,
            current_user=current_user,
            name=_tool_map[parsed.intent],
            input_data=parsed.parameters,
        )

    current_parameters = {
        key: value
        for key, value in parsed.parameters.items()
        if not str(key).startswith("comparison_")
    }
    comparison_parameters = {
        "date_from": parsed.parameters["comparison_date_from"],
        "date_to": parsed.parameters["comparison_date_to"],
    }
    if "period" in parsed.parameters:
        comparison_parameters["period"] = parsed.parameters["period"]

    current_result = await execute_tool(
        db,
        current_user=current_user,
        name=_tool_map[parsed.intent],
        input_data=current_parameters,
    )
    comparison_result = await execute_tool(
        db,
        current_user=current_user,
        name=_tool_map[parsed.intent],
        input_data=comparison_parameters,
    )
    return {
        "current": current_result,
        "comparison": comparison_result,
        "comparison_baseline": "previous_week",
    }


# ── Message formatters ─────────────────────────────────────────────────────────

def _message_for_tool(intent: str, result: dict) -> str:
    if "error" in result:
        return str(result["error"])
    if result.get("comparison_baseline") == "previous_week":
        current = result.get("current", {})
        comparison = result.get("comparison", {})
        if intent == "sales_by_period":
            return (
                f"I compared the current period to the previous week. Current range is "
                f"{current.get('date_from')} to {current.get('date_to')}, and the comparison range is "
                f"{comparison.get('date_from')} to {comparison.get('date_to')}."
            )
        if intent == "profit_loss_summary":
            return (
                f"I compared the current period to the previous week. Gross profit is "
                f"{current.get('gross_profit', 0):.2f} now versus {comparison.get('gross_profit', 0):.2f} previously."
            )
    if intent == "sales_by_period":
        return (
            f"I grouped sales by {result.get('period', 'period')} from "
            f"{result.get('date_from')} to {result.get('date_to')}."
        )
    if intent == "overdue_customers":
        return (
            f"The biggest contributors are {result.get('count', 0)} overdue customers with "
            f"{result.get('total_overdue_amount', 0):.2f} outstanding."
        )
    if intent == "customer_balance":
        selected = result.get("selected")
        if selected:
            return (
                f"{selected['name']} currently has {selected['outstanding']:.2f} outstanding across "
                f"{selected['open_invoice_count']} open invoices."
            )
        return f"No customer match was found for '{result.get('query', '')}'."
    if intent == "product_details":
        selected = result.get("selected")
        if selected:
            return (
                f"{selected['name']} ({selected['sku']}) is priced at {selected['price']:.2f} "
                f"with stock {selected['stock']:.3f}."
            )
        return f"No product match was found for '{result.get('query', '')}'."
    if intent == "stock_levels":
        return f"I found {result.get('count', 0)} stock records in the current inventory snapshot."
    if intent == "expense_breakdown":
        return f"The expense breakdown for {result.get('month')} totals {result.get('total', 0):.2f}."
    if intent == "profit_loss_summary":
        return (
            f"For {result.get('date_from')} to {result.get('date_to')}, revenue is {result.get('revenue', 0):.2f}, "
            f"expenses are {result.get('expenses', 0):.2f}, and gross profit is {result.get('gross_profit', 0):.2f}."
        )
    return "Done."


def _message_for_customer_balances_top(result: dict) -> str:
    if "error" in result:
        return str(result["error"])
    count = result.get("count", 0)
    total = result.get("total_outstanding", 0)
    return f"Top {count} customers by outstanding balance, totalling {float(total):.2f}."


def _message_for_stock_value(result: dict) -> str:
    if "error" in result:
        return str(result["error"])
    total = result.get("total_value", 0)
    count = result.get("item_count", 0)
    return f"Total inventory value is {float(total):.2f} across {count} active products."


# ── Date-shifting helpers ──────────────────────────────────────────────────────

def _previous_month_from_range(last_date_from: date | None, last_date_to: date | None) -> str | None:
    shifted = _shift_to_previous_month(last_date_from, last_date_to)
    if shifted is None:
        return None
    return shifted[0].strftime("%Y-%m")


def _shift_to_previous_month(last_date_from: date | None, last_date_to: date | None) -> tuple[date, date] | None:
    if last_date_from is None or last_date_to is None:
        return None
    prev_month_end = last_date_from.replace(day=1) - timedelta(days=1)
    prev_month_start = prev_month_end.replace(day=1)
    duration = (last_date_to - last_date_from).days
    shifted_end = min(prev_month_end, prev_month_start + timedelta(days=duration))
    return prev_month_start, shifted_end
