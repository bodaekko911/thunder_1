from __future__ import annotations

from datetime import date

from app.core.permissions import has_permission


TOOL_DEFINITIONS = [
    {
        "name": "get_sales_summary",
        "description": (
            "Get total POS and B2B sales revenue with refunds deducted for a given date range. "
            "Use today's date for daily queries, or first-of-month through today for monthly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "Start date in YYYY-MM-DD format"},
                "date_to": {"type": "string", "description": "End date in YYYY-MM-DD format"},
            },
            "required": ["date_from", "date_to"],
        },
    },
    {
        "name": "get_top_products",
        "description": "Get the top-selling products by revenue for a date range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "Start date YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "End date YYYY-MM-DD"},
                "limit": {"type": "integer", "description": "Maximum number of products to return (default 10)"},
            },
            "required": ["date_from", "date_to"],
        },
    },
    {
        "name": "get_low_stock_items",
        "description": "Get products with low or zero stock.",
        "input_schema": {
            "type": "object",
            "properties": {
                "threshold": {
                    "type": "integer",
                    "description": "Stock level at or below which a product is considered low-stock (default 5)",
                },
            },
        },
    },
    {
        "name": "get_expenses_summary",
        "description": (
            "Get expense totals for the current and previous month with category breakdown. "
            "Requires accounting permission."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_unpaid_invoices_summary",
        "description": (
            "Get count and outstanding amounts for unpaid POS and B2B invoices. "
            "Requires page_pos or page_b2b permission."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_customer_balances",
        "description": (
            "Get B2B customers ranked by their outstanding unpaid balance. "
            "Requires page_b2b permission."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Maximum number of customers to return (default 10)"},
            },
        },
    },
    {
        "name": "get_overdue_customers",
        "description": "Get B2B customers with overdue invoices and outstanding balances.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Maximum number of customers to return (default 10)"},
            },
        },
    },
    {
        "name": "get_customer_balance",
        "description": "Get the balance for a specific B2B customer by name or id fragment.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_query": {"type": "string", "description": "Customer name or numeric id"},
            },
            "required": ["customer_query"],
        },
    },
    {
        "name": "get_product_details",
        "description": "Get details for a product by SKU or name fragment.",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_query": {"type": "string", "description": "Product SKU or name"},
            },
            "required": ["product_query"],
        },
    },
    {
        "name": "get_stock_levels",
        "description": "Get stock levels, optionally filtered to a specific product.",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_query": {"type": "string", "description": "Optional product SKU or name"},
                "limit": {"type": "integer", "description": "Maximum number of products to return (default 10)"},
            },
        },
    },
    {
        "name": "get_expense_breakdown",
        "description": "Get expense breakdown totals for a month. Requires accounting permission.",
        "input_schema": {
            "type": "object",
            "properties": {
                "month": {"type": "string", "description": "Month in YYYY-MM format"},
            },
        },
    },
    {
        "name": "get_profit_loss_summary",
        "description": "Get revenue, expenses, and gross profit for a date range. Requires accounting permission.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "Start date YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "End date YYYY-MM-DD"},
            },
        },
    },
    {
        "name": "get_stock_value_summary",
        "description": (
            "Get the total inventory value (stock × cost) for all active products, "
            "with a breakdown by top-5 categories. "
            "Requires page_inventory or page_products permission."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_sales_by_period",
        "description": "Get POS sales aggregated by day, week, or month for a date range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "enum": ["daily", "weekly", "monthly"],
                    "description": "Aggregation period",
                },
                "date_from": {"type": "string", "description": "Start date YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "End date YYYY-MM-DD"},
            },
            "required": ["period", "date_from", "date_to"],
        },
    },
]


async def execute_tool(db, *, current_user, name: str, input_data: dict) -> dict:
    from app.services.assistant_tools import (
        get_customer_balance as _get_customer_balance,
        get_customer_balances as _get_customer_balances,
        get_expense_breakdown as _get_expense_breakdown,
        get_expenses_summary as _get_expenses_summary,
        get_low_stock_items as _get_low_stock_items,
        get_overdue_customers as _get_overdue_customers,
        get_product_details as _get_product_details,
        get_profit_loss_summary as _get_profit_loss_summary,
        get_sales_by_period as _get_sales_by_period,
        get_sales_summary as _get_sales_summary,
        get_stock_levels as _get_stock_levels,
        get_stock_value_summary as _get_stock_value_summary,
        get_top_products as _get_top_products,
        get_unpaid_invoices_summary as _get_unpaid_invoices_summary,
    )

    today = date.today()

    if name == "get_sales_summary":
        return await _get_sales_summary(
            db,
            date_from=input_data.get("date_from", today.isoformat()),
            date_to=input_data.get("date_to", today.isoformat()),
        )

    if name == "get_top_products":
        return await _get_top_products(
            db,
            date_from=input_data.get("date_from", today.replace(day=1).isoformat()),
            date_to=input_data.get("date_to", today.isoformat()),
            limit=int(input_data.get("limit", 10)),
        )

    if name == "get_low_stock_items":
        return await _get_low_stock_items(db, threshold=int(input_data.get("threshold", 5)))

    if name == "get_expenses_summary":
        if not has_permission(current_user, "page_accounting"):
            return {"error": "Permission denied: page_accounting is required to view expenses."}
        return await _get_expenses_summary(db)

    if name == "get_unpaid_invoices_summary":
        if not (has_permission(current_user, "page_pos") or has_permission(current_user, "page_b2b")):
            return {"error": "Permission denied: page_pos or page_b2b is required to view unpaid invoices."}
        return await _get_unpaid_invoices_summary(db)

    if name == "get_customer_balances":
        if not has_permission(current_user, "page_b2b"):
            return {"error": "Permission denied: page_b2b is required to view customer balances."}
        return await _get_customer_balances(db, limit=int(input_data.get("limit", 10)))

    if name == "get_overdue_customers":
        if not has_permission(current_user, "page_b2b"):
            return {"error": "Permission denied: page_b2b is required to view overdue customers."}
        return await _get_overdue_customers(db, limit=int(input_data.get("limit", 10)))

    if name == "get_customer_balance":
        if not has_permission(current_user, "page_b2b"):
            return {"error": "Permission denied: page_b2b is required to view customer balances."}
        return await _get_customer_balance(
            db,
            customer_query=input_data.get("customer_query"),
            customer_id=input_data.get("customer_id"),
        )

    if name == "get_product_details":
        if not (has_permission(current_user, "page_products") or has_permission(current_user, "page_inventory")):
            return {"error": "Permission denied: page_products or page_inventory is required to view product details."}
        return await _get_product_details(
            db,
            product_query=input_data.get("product_query"),
            product_id=input_data.get("product_id"),
        )

    if name == "get_stock_levels":
        if not (has_permission(current_user, "page_inventory") or has_permission(current_user, "page_products")):
            return {"error": "Permission denied: page_inventory or page_products is required to view stock levels."}
        return await _get_stock_levels(
            db,
            product_query=input_data.get("product_query"),
            limit=int(input_data.get("limit", 10)),
        )

    if name == "get_expense_breakdown":
        if not has_permission(current_user, "page_accounting"):
            return {"error": "Permission denied: page_accounting is required to view expense breakdown."}
        return await _get_expense_breakdown(db, month=input_data.get("month"))

    if name == "get_profit_loss_summary":
        if not has_permission(current_user, "page_accounting"):
            return {"error": "Permission denied: page_accounting is required to view profit and loss."}
        return await _get_profit_loss_summary(
            db,
            date_from=input_data.get("date_from"),
            date_to=input_data.get("date_to"),
        )

    if name == "get_sales_by_period":
        return await _get_sales_by_period(
            db,
            period=input_data.get("period", "daily"),
            date_from=input_data.get("date_from"),
            date_to=input_data.get("date_to"),
        )

    if name == "get_stock_value_summary":
        if not (has_permission(current_user, "page_inventory") or has_permission(current_user, "page_products")):
            return {"error": "Permission denied: page_inventory or page_products is required to view stock value."}
        return await _get_stock_value_summary(db)

    return {"error": f"Unknown tool: {name}"}
