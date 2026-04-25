from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.services.copilot.composer import ResponseComposer, SUPPORTED_QUESTIONS_BY_CATEGORY
from app.services.copilot.contracts import ParsedDashboardIntent
from app.services.copilot.suggestions import build_highlights, build_suggestions, build_table
from app.services.copilot.tools import execute_tool


@dataclass(frozen=True)
class ToolCall:
    key: str
    name: str
    input_data: dict


async def answer_dashboard_intent(
    db,
    *,
    current_user,
    parsed: ParsedDashboardIntent,
    composer: ResponseComposer,
) -> dict:
    if parsed.intent == "help":
        result = {"categories": SUPPORTED_QUESTIONS_BY_CATEGORY}
        return composer.compose(
            supported=True,
            intent="help",
            parameters=parsed.parameters,
            result=result,
            message="I can help with sales, products, stock, expenses, receivables, customers, recent activity, and period comparisons.",
            confidence=1.0,
            suggestions=build_suggestions("help", result),
            highlights=build_highlights("help", result),
            table=build_table("help", result),
        )

    if parsed.intent == "kpi_explanation":
        result = _kpi_explanation(parsed.parameters.get("topic"))
        return composer.compose(
            supported=True,
            intent="kpi_explanation",
            parameters=parsed.parameters,
            result=result,
            message=result["message"],
            confidence=parsed.confidence,
            suggestions=result["suggestions"],
            highlights=[],
            table=None,
        )

    plan = _build_tool_plan(parsed)
    results = {}
    for call in plan:
        results[call.key] = await execute_tool(
            db,
            current_user=current_user,
            name=call.name,
            input_data=call.input_data,
        )

    primary = results.get("primary")
    if isinstance(primary, dict) and "error" in primary:
        return composer.compose(
            supported=True,
            intent=parsed.intent,
            parameters=parsed.parameters,
            result=primary,
            message=str(primary["error"]),
            confidence=parsed.confidence,
            suggestions=[],
            highlights=[],
            table=None,
        )

    result = _merge_results(parsed, results)
    message = _message_for_intent(parsed, result)
    return composer.compose(
        supported=True,
        intent=parsed.intent,
        parameters=parsed.parameters,
        result=result,
        message=message,
        confidence=parsed.confidence,
        suggestions=build_suggestions(parsed.intent, result),
        highlights=build_highlights(parsed.intent, result),
        table=build_table(parsed.intent, result),
    )


def _build_tool_plan(parsed: ParsedDashboardIntent) -> list[ToolCall]:
    params = dict(parsed.parameters)
    simple_map = {
        "sales_summary": "get_sales_summary",
        "sales_by_period": "get_sales_by_period",
        "top_products": "get_top_products",
        "low_stock": "get_low_stock_items",
        "product_details": "get_product_details",
        "stock_levels": "get_stock_levels",
        "product_stock_value": "get_stock_value_summary",
        "expense_breakdown": "get_expense_breakdown",
        "unpaid_invoices": "get_unpaid_invoices_summary",
        "customer_balances_top": "get_customer_balances",
        "customer_balance": "get_customer_balance",
        "overdue_customers": "get_overdue_customers",
        "profit_loss_summary": "get_profit_loss_summary",
        "recent_activity": "get_recent_activity",
        "customer_growth": "get_customer_growth_summary",
        "b2b_performance": "get_b2b_performance_summary",
    }
    if parsed.intent in simple_map:
        return [ToolCall("primary", simple_map[parsed.intent], params)]

    if parsed.intent == "expenses_month":
        if params.get("date_from") and params.get("date_to"):
            return [ToolCall("primary", "get_expenses_range_summary", params)]
        return [ToolCall("primary", "get_expenses_summary", params)]

    if parsed.intent == "change_summary":
        focus = params.get("focus", "sales")
        if focus == "profit":
            tool_name = "get_profit_loss_summary"
        elif focus == "expenses":
            tool_name = "get_expenses_range_summary"
        else:
            tool_name = "get_sales_summary"
        current = {
            "date_from": params["date_from"],
            "date_to": params["date_to"],
        }
        comparison = {
            "date_from": params["comparison_date_from"],
            "date_to": params["comparison_date_to"],
        }
        calls = [
            ToolCall("primary", tool_name, current),
            ToolCall("comparison", tool_name, comparison),
        ]
        if focus == "sales":
            calls.extend(
                [
                    ToolCall("current_top", "get_top_products", {**current, "limit": 3}),
                    ToolCall("comparison_top", "get_top_products", {**comparison, "limit": 3}),
                ]
            )
        return calls

    return []


def _merge_results(parsed: ParsedDashboardIntent, results: dict) -> dict:
    if parsed.intent != "change_summary":
        return results.get("primary") or {}
    payload = {
        "focus": parsed.parameters.get("focus", "sales"),
        "current": results.get("primary") or {},
        "comparison": results.get("comparison") or {},
        "comparison_date_from": parsed.parameters.get("comparison_date_from"),
        "comparison_date_to": parsed.parameters.get("comparison_date_to"),
        "date_from": parsed.parameters.get("date_from"),
        "date_to": parsed.parameters.get("date_to"),
    }
    if "current_top" in results:
        payload["current_top"] = results["current_top"]
    if "comparison_top" in results:
        payload["comparison_top"] = results["comparison_top"]
    return payload


def _message_for_intent(parsed: ParsedDashboardIntent, result: dict) -> str:
    intent = parsed.intent
    if intent == "sales_summary":
        return (
            f"Sales for {_period_phrase(result.get('date_from'), result.get('date_to'))} are "
            f"{_money(result.get('total'))}, with {_money(result.get('net_pos'))} from POS, "
            f"{_money(result.get('b2b_sales'))} from B2B, and {_money(result.get('refunds'))} in refunds."
        )
    if intent == "sales_by_period":
        return (
            f"I grouped sales by {result.get('period', 'daily')} for "
            f"{_period_phrase(result.get('date_from'), result.get('date_to'))}."
        )
    if intent == "top_products":
        items = result.get("items") or []
        if not items:
            return f"No top-product sales were found for {_period_phrase(result.get('date_from'), result.get('date_to'))}."
        leader = items[0]
        return (
            f"Top products for {_period_phrase(result.get('date_from'), result.get('date_to'))} are ready. "
            f"The current leader is {leader.get('name', 'Unknown')} at {_money(leader.get('revenue'))}."
        )
    if intent == "low_stock":
        return (
            f"There are {int(result.get('out_of_stock_count', 0))} out-of-stock items and "
            f"{int(result.get('low_stock_count', 0))} low-stock items right now."
        )
    if intent == "product_details":
        selected = result.get("selected")
        if selected:
            return (
                f"{selected['name']} ({selected.get('sku') or 'no SKU'}) costs {_money(selected.get('price'))}, "
                f"has {_number(selected.get('stock'))} in stock, and a minimum stock level of {_number(selected.get('min_stock'))}."
            )
        matches = result.get("matches") or []
        if matches:
            shortlist = ", ".join(
                f"{item.get('name', 'Unknown')} ({item.get('sku') or 'no SKU'})"
                for item in matches[:4]
            )
            return f"I found a few close product matches for '{result.get('query', '')}': {shortlist}. Which one do you mean?"
        return f"I couldn't find a product match for '{result.get('query', '')}'."
    if intent == "stock_levels":
        items = result.get("items") or []
        if result.get("query") and items:
            first = items[0]
            return (
                f"{first.get('name', 'That product')} has {_number(first.get('stock'))} in stock "
                f"against a minimum of {_number(first.get('min_stock'))}."
            )
        return f"I found {int(result.get('count', 0))} stock records in the requested inventory view."
    if intent == "product_stock_value":
        return (
            f"Total inventory value is {_money(result.get('total_value'))} across "
            f"{int(result.get('item_count', 0))} active products."
        )
    if intent == "expenses_month":
        if "this_month" in result:
            return (
                f"Expenses for {result.get('month', date.today().strftime('%Y-%m'))} are "
                f"{_money(result.get('this_month'))}."
            )
        return (
            f"Expenses for {_period_phrase(result.get('date_from'), result.get('date_to'))} total "
            f"{_money(result.get('total'))}."
        )
    if intent == "expense_breakdown":
        breakdown = result.get("breakdown") or []
        if breakdown:
            leader = breakdown[0]
            return (
                f"Expenses for {_period_phrase(result.get('date_from') or result.get('month'), result.get('date_to'))} total "
                f"{_money(result.get('total'))}. The biggest category is {leader.get('name', 'Unknown')} at {_money(leader.get('total'))}."
            )
        return "I found no expense records for that period."
    if intent == "unpaid_invoices":
        return (
            f"There are {int(result.get('pos_unpaid_count', 0))} unpaid POS invoices and "
            f"{int(result.get('b2b_unpaid_count', 0))} unpaid or partial B2B invoices, with "
            f"{_money(result.get('b2b_outstanding_amount'))} still outstanding in B2B."
        )
    if intent == "customer_balances_top":
        clients = result.get("clients") or []
        if not clients:
            return "I didn't find any customers with outstanding balances."
        leader = clients[0]
        return f"{leader.get('name', 'That customer')} currently owes the most at {_money(leader.get('outstanding'))}."
    if intent == "customer_balance":
        selected = result.get("selected")
        if selected:
            return (
                f"{selected['name']} has {_money(selected['outstanding'])} outstanding across "
                f"{int(selected['open_invoice_count'])} open invoices."
            )
        matches = result.get("matches") or []
        if matches:
            names = ", ".join(item.get("name", "Unknown") for item in matches[:4])
            return f"I found several close customer matches for '{result.get('query', '')}': {names}. Which one do you mean?"
        return f"I couldn't find a customer match for '{result.get('query', '')}'."
    if intent == "overdue_customers":
        return (
            f"There are {int(result.get('count', 0))} overdue customers, with "
            f"{_money(result.get('total_overdue_amount'))} overdue in total."
        )
    if intent == "profit_loss_summary":
        return (
            f"For {_period_phrase(result.get('date_from'), result.get('date_to'))}, revenue is {_money(result.get('revenue'))}, "
            f"expenses are {_money(result.get('expenses'))}, and gross profit is {_money(result.get('gross_profit'))} "
            f"at a margin of {float(result.get('margin_pct', 0)):.1f}%."
        )
    if intent == "recent_activity":
        return (
            f"I found {int(result.get('count', 0))} recent transactions for "
            f"{_period_phrase(result.get('date_from'), result.get('date_to'))}."
        )
    if intent == "customer_growth":
        change_pct = result.get("change_pct")
        change_text = f", up {change_pct:.1f}% from the prior period" if isinstance(change_pct, (int, float)) and change_pct >= 0 else (
            f", down {abs(change_pct):.1f}% from the prior period" if isinstance(change_pct, (int, float)) else ""
        )
        return (
            f"You added {int(result.get('new_customers', 0))} new customers in "
            f"{_period_phrase(result.get('date_from'), result.get('date_to'))}{change_text}."
        )
    if intent == "b2b_performance":
        return (
            f"B2B paid sales for {_period_phrase(result.get('date_from'), result.get('date_to'))} are {_money(result.get('paid_sales'))}, "
            f"with {_money(result.get('outstanding'))} currently outstanding across {int(result.get('clients_with_balance', 0))} clients."
        )
    if intent == "change_summary":
        return _change_message(result)
    return "Done."


def _change_message(result: dict) -> str:
    focus = result.get("focus", "sales")
    current = result.get("current") or {}
    comparison = result.get("comparison") or {}
    current_total = _change_value(focus, current)
    comparison_total = _change_value(focus, comparison)
    delta = current_total - comparison_total
    direction = "up" if delta > 0 else "down" if delta < 0 else "flat"
    if comparison_total:
        pct = abs((delta / comparison_total) * 100)
        pct_text = f" ({pct:.1f}%)"
    else:
        pct_text = ""

    message = (
        f"{focus.title()} is {direction} by {_money(abs(delta))}{pct_text}, from "
        f"{_money(comparison_total)} in {_period_phrase(result.get('comparison_date_from'), result.get('comparison_date_to'))} "
        f"to {_money(current_total)} in {_period_phrase(result.get('date_from'), result.get('date_to'))}."
    )
    if focus == "sales":
        current_items = (result.get("current_top") or {}).get("items") or []
        if current_items:
            names = ", ".join(item.get("name", "Unknown") for item in current_items[:3])
            message += f" Top products in the current period are {names}."
    return message


def _change_value(focus: str, payload: dict) -> float:
    if focus == "profit":
        return float(payload.get("gross_profit", 0) or 0)
    if focus == "expenses":
        return float(payload.get("total", 0) or 0)
    return float(payload.get("total", 0) or 0)


def _kpi_explanation(topic: str | None) -> dict:
    explanations = {
        "sales": {
            "message": "Sales is the money brought in from completed sales for the selected period, after refunds are deducted where that metric uses net sales.",
            "suggestions": ["How much did we sell today?", "What changed compared to yesterday?"],
        },
        "gross_profit": {
            "message": "Gross profit here is revenue minus recorded expenses for the selected period. It is a quick operating snapshot, not a full accounting close.",
            "suggestions": ["What is the gross profit this month?", "What are my biggest expenses this month?"],
        },
        "margin": {
            "message": "Margin is gross profit divided by revenue for the selected period, shown as a percentage.",
            "suggestions": ["What is the gross profit this month?", "How much did we sell this month?"],
        },
        "stock_alerts": {
            "message": "Stock alerts shows products that are either out of stock or at risk because current stock is at or below the minimum threshold.",
            "suggestions": ["Which items are low in stock?", "Show stock levels for olive oil"],
        },
        "receivables": {
            "message": "Receivables are customer balances that are still unpaid or partially paid, mainly from B2B invoices.",
            "suggestions": ["Which customer owes us the most?", "Which invoices are unpaid?"],
        },
        "top_products": {
            "message": "Top products ranks products by sales in the selected period, usually by revenue unless you ask for quantity.",
            "suggestions": ["What are my top products this month?", "Show product details for olive oil"],
        },
    }
    return explanations.get(
        topic or "",
        {
            "message": "Ask me about a dashboard KPI like sales, gross profit, stock alerts, receivables, or top products and I’ll explain it.",
            "suggestions": ["What is gross profit?", "What does stock alerts mean?"],
        },
    )


def _money(value) -> str:
    return f"{float(value or 0):,.2f}"


def _number(value) -> str:
    return f"{float(value or 0):,.3f}".rstrip("0").rstrip(".")


def _period_phrase(date_from: str | None, date_to: str | None) -> str:
    if not date_from and not date_to:
        return "the selected period"
    if date_from and date_to and date_from == date_to:
        return date_from
    if date_from and date_to:
        return f"{date_from} to {date_to}"
    return date_from or date_to or "the selected period"
