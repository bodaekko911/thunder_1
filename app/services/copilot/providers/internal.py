from __future__ import annotations

from datetime import date, timedelta

from app.core.permissions import ensure_permission, has_permission
from app.services.copilot.composer import ResponseComposer
from app.services.copilot.memory import get_latest_session, persist_exchange
from app.services.copilot.router import ParsedDashboardIntent, SUPPORTED_QUESTION_HINTS, parse_dashboard_question
from app.services.copilot.tools import execute_tool


class InternalCopilotProvider:
    def __init__(self) -> None:
        self.composer = ResponseComposer()

    async def answer(self, db, *, question: str, current_user) -> dict:
        parsed = parse_dashboard_question(question)
        session = None
        if parsed.intent is None or _requires_followup_context(parsed):
            session = await get_latest_session(db, user_id=current_user.id)
            contextual = _resolve_contextual_reference(question, parsed, session)
            if contextual is not None:
                parsed = contextual
        if parsed.intent is None:
            if session is None:
                session = await get_latest_session(db, user_id=current_user.id)
            parsed = _resolve_followup(question, session)
        if parsed is None or _requires_followup_context(parsed):
            response = self.composer.insufficient_followup() if _looks_like_followup(question) else self.composer.unsupported(supported_hints=SUPPORTED_QUESTION_HINTS)
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


def _requires_followup_context(parsed: ParsedDashboardIntent | None) -> bool:
    if parsed is None or parsed.intent is None:
        return False
    if parsed.intent == "product_details" and str(parsed.parameters.get("product_query", "")).strip().lower() == "that item":
        return True
    return False


def _resolve_contextual_reference(
    question: str,
    parsed: ParsedDashboardIntent | None,
    session,
) -> ParsedDashboardIntent | None:
    if parsed is None or session is None:
        return None
    text = (question or "").strip().lower()
    last_entity_ids = session.get_last_entity_ids() if hasattr(session, "get_last_entity_ids") else []
    if parsed.intent == "product_details" and "that item" in text and last_entity_ids:
        return ParsedDashboardIntent("product_details", {"product_id": last_entity_ids[0]}, entity_ids=[last_entity_ids[0]])
    return None


def _looks_like_followup(question: str) -> bool:
    text = (question or "").strip().lower()
    return any(
        phrase in text
        for phrase in [
            "what about",
            "compare that",
            "previous week",
            "last month",
            "which customers",
            "that item",
            "that customer",
        ]
    )


def _resolve_followup(question: str, session) -> ParsedDashboardIntent | None:
    if session is None:
        return None

    text = (question or "").strip().lower()
    last_intent = session.last_intent
    last_entity_ids = session.get_last_entity_ids() if hasattr(session, "get_last_entity_ids") else []
    last_date_from = session.last_date_from
    last_date_to = session.last_date_to

    if "show me the product details for that item" in text or "product details for that item" in text:
        if last_entity_ids and last_intent in {"stock_levels", "product_details"}:
            return ParsedDashboardIntent("product_details", {"product_id": last_entity_ids[0]}, entity_ids=[last_entity_ids[0]])
        return None

    if "which customers caused most of it" in text:
        if last_intent in {"unpaid_invoices", "overdue_customers", "customer_balance"}:
            return ParsedDashboardIntent("overdue_customers", {"limit": 10})
        return None

    if "what about last month" in text:
        if last_intent == "expense_breakdown":
            month = _previous_month_from_range(last_date_from, last_date_to)
            if month:
                return ParsedDashboardIntent("expense_breakdown", {"month": month}, comparison_baseline="last_month")
            return None
        if last_intent in {"sales_by_period", "profit_loss_summary"}:
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

    if "compare that to previous week" in text:
        if last_intent not in {"sales_by_period", "profit_loss_summary"}:
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

    return None


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


async def _answer_from_intent(db, *, current_user, parsed: ParsedDashboardIntent, composer: ResponseComposer) -> dict:
    if parsed.intent == "expenses_month":
        await ensure_permission(db, current_user, "page_accounting", path="/dashboard/assistant")
    elif parsed.intent == "unpaid_invoices":
        if not (has_permission(current_user, "page_pos") or has_permission(current_user, "page_b2b")):
            await ensure_permission(db, current_user, "page_pos", path="/dashboard/assistant")

    if parsed.intent in {"sales_today", "top_products", "low_stock"}:
        from app.routers.dashboard import dashboard_data

        snapshot = await dashboard_data(db=db)
        if parsed.intent == "sales_today":
            return composer.compose(
                supported=True,
                intent=parsed.intent,
                parameters=parsed.parameters,
                result={
                    "total_sales": snapshot["total_today"],
                    "pos_sales": snapshot["pos_today"],
                    "b2b_sales": snapshot["b2b_today"],
                    "refunds": snapshot["ref_today"],
                },
                message=f"Today's total sales are {snapshot['total_today']:.2f}.",
            )
        if parsed.intent == "top_products":
            return composer.compose(
                supported=True,
                intent=parsed.intent,
                parameters=parsed.parameters,
                result={
                    "items": snapshot["top_products"],
                    "count": len(snapshot["top_products"]),
                },
                message="Here are the current top products for this month.",
            )
        return composer.compose(
            supported=True,
            intent=parsed.intent,
            parameters=parsed.parameters,
            result={
                "items": snapshot["low_stock"],
                "count": snapshot["low_stock_count"],
            },
            message=f"There are {snapshot['low_stock_count']} low-stock items right now.",
        )

    tool_map = {
        "sales_by_period": "get_sales_by_period",
        "overdue_customers": "get_overdue_customers",
        "customer_balance": "get_customer_balance",
        "product_details": "get_product_details",
        "stock_levels": "get_stock_levels",
        "expense_breakdown": "get_expense_breakdown",
        "profit_loss_summary": "get_profit_loss_summary",
    }
    if parsed.intent in tool_map:
        result = await _execute_intent_tool(db, current_user=current_user, parsed=parsed)
        return composer.compose(
            supported=True,
            intent=parsed.intent,
            parameters=parsed.parameters,
            result=result,
            message=_message_for_tool(parsed.intent, result),
        )

    if parsed.intent == "expenses_month":
        from app.services.expense_service import get_summary as get_expense_summary

        summary = await get_expense_summary(db)
        return composer.compose(
            supported=True,
            intent=parsed.intent,
            parameters=parsed.parameters,
            result={
                "this_month": float(summary["this_month"]),
                "last_month": float(summary["last_month"]),
                "breakdown": summary["breakdown"][:5],
            },
            message=f"Expenses this month are {float(summary['this_month']):.2f}.",
        )

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
    )


async def _execute_intent_tool(db, *, current_user, parsed: ParsedDashboardIntent) -> dict:
    tool_map = {
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
            name=tool_map[parsed.intent],
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
        name=tool_map[parsed.intent],
        input_data=current_parameters,
    )
    comparison_result = await execute_tool(
        db,
        current_user=current_user,
        name=tool_map[parsed.intent],
        input_data=comparison_parameters,
    )
    return {
        "current": current_result,
        "comparison": comparison_result,
        "comparison_baseline": "previous_week",
    }


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
