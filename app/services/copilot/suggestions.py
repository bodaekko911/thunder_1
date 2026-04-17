"""Pure-function builders for suggestions, highlights, and tables.

Called after every successful intent resolution and attached to the response
envelope.  No DB access — they transform the result dict already fetched by
the tool layer.
"""
from __future__ import annotations


# ── Formatting helpers ─────────────────────────────────────────────────────────

def _fmt_money(value) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_count(value) -> str:
    try:
        return str(int(float(value)))
    except (TypeError, ValueError):
        return "—"


def _trunc(name: str, max_len: int = 40) -> str:
    name = (name or "").strip()
    if not name:
        return ""
    return name[:max_len] + ("…" if len(name) > max_len else "")


# ── Suggestions ────────────────────────────────────────────────────────────────

_DEFAULT_SUGGESTIONS: dict[str, list[str]] = {
    "sales_today": ["View sales this week", "Top products this month", "Profit this month"],
    "sales_summary": ["Compare to previous week", "Sales by period", "Top products this month"],
    "sales_by_period": ["Compare that to previous week", "Top products", "Profit and loss"],
    "top_products": ["Sales this month", "Low-stock items", "Stock levels"],
    "low_stock": ["Show stock levels", "Stock value", "Top products"],
    "stock_levels": ["Low stock items", "Stock value", "Top products"],
    "overdue_customers": ["Who owes me the most", "Unpaid invoices"],
    "customer_balances_top": ["Overdue customers", "Unpaid invoices"],
    "customer_balance": ["Who owes me the most", "Overdue customers"],
    "expenses_month": ["Expense breakdown", "Profit and loss", "Expenses last month"],
    "expense_breakdown": ["Expenses this month", "Profit and loss"],
    "unpaid_invoices": ["Overdue customers", "Who owes me the most"],
    "profit_loss_summary": ["Expense breakdown", "Sales this month", "Compare to previous week"],
    "product_stock_value": ["Low-stock items", "Stock levels", "Top products"],
    "help": [],
    "export_placeholder": [],
}


def build_suggestions(
    intent: str | None,
    result: dict | None,
    *,
    confidence: float = 1.0,
) -> list[str]:
    """Return 2-4 follow-up suggestion strings for the UI chip row."""
    result = result or {}

    # Unsupported / None intent — return close_matches if present
    if intent is None:
        close_matches = result.get("close_matches") or []
        return [str(m) for m in close_matches if m]

    defaults = list(_DEFAULT_SUGGESTIONS.get(intent, []))

    # Enrich with entity names where available
    if intent == "top_products":
        items = result.get("items") or []
        names = [_trunc(item.get("name", "")) for item in items[:3] if item.get("name")]
        if names:
            return ([f"Tell me about {n}" for n in names] + defaults)[:4]

    if intent in {"overdue_customers", "customer_balances_top"}:
        key = "customers" if intent == "overdue_customers" else "clients"
        items = result.get(key) or []
        names = [_trunc(item.get("name", "")) for item in items[:2] if item.get("name")]
        if names:
            return ([f"Customer balance for {n}" for n in names] + defaults)[:4]

    return defaults


# ── Highlights ─────────────────────────────────────────────────────────────────

def _money_tone(value, *, bad_if_positive: bool = False) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "neutral"
    if bad_if_positive:
        return "bad" if v > 0 else "neutral"
    return "good" if v > 0 else "neutral"


def build_highlights(intent: str | None, result: dict | None) -> list[dict]:
    """Return a list of {label, value, tone} highlight cards."""
    result = result or {}
    if not intent:
        return []

    if intent == "sales_today":
        total = result.get("total_sales", 0)
        pos   = result.get("pos_sales", 0)
        b2b   = result.get("b2b_sales", 0)
        ref   = result.get("refunds", 0)
        return [
            {"label": "Total Sales",  "value": _fmt_money(total), "tone": _money_tone(total)},
            {"label": "POS Sales",    "value": _fmt_money(pos),   "tone": "neutral"},
            {"label": "B2B Sales",    "value": _fmt_money(b2b),   "tone": "neutral"},
            {"label": "Refunds",      "value": _fmt_money(ref),   "tone": _money_tone(ref, bad_if_positive=True)},
        ]

    if intent == "sales_summary":
        total = result.get("total", 0)
        pos   = result.get("pos_sales", 0)
        b2b   = result.get("b2b_sales", 0)
        ref   = result.get("refunds", 0)
        return [
            {"label": "Total Revenue", "value": _fmt_money(total), "tone": _money_tone(total)},
            {"label": "POS Sales",     "value": _fmt_money(pos),   "tone": "neutral"},
            {"label": "B2B Sales",     "value": _fmt_money(b2b),   "tone": "neutral"},
            {"label": "Refunds",       "value": _fmt_money(ref),   "tone": _money_tone(ref, bad_if_positive=True)},
        ]

    if intent == "sales_by_period":
        data  = result.get("data") or []
        total = sum(float(item.get("total", 0)) for item in data)
        return [
            {"label": "Total Revenue", "value": _fmt_money(total),    "tone": _money_tone(total)},
            {"label": "Periods",       "value": _fmt_count(len(data)), "tone": "neutral"},
        ]

    if intent == "profit_loss_summary":
        revenue      = result.get("revenue", 0)
        expenses     = result.get("expenses", 0)
        gross_profit = result.get("gross_profit", 0)
        margin_pct   = result.get("margin_pct", 0)
        try:
            gp = float(gross_profit)
        except (TypeError, ValueError):
            gp = 0.0
        try:
            mp = float(margin_pct)
        except (TypeError, ValueError):
            mp = 0.0
        gp_tone     = "good" if gp >= 0 else "bad"
        margin_tone = "good" if mp >= 20 else ("bad" if mp < 0 else "neutral")
        return [
            {"label": "Revenue",      "value": _fmt_money(revenue),      "tone": "neutral"},
            {"label": "Expenses",     "value": _fmt_money(expenses),     "tone": "neutral"},
            {"label": "Gross Profit", "value": _fmt_money(gross_profit), "tone": gp_tone},
            {"label": "Margin",       "value": f"{mp:.1f}%",             "tone": margin_tone},
        ]

    if intent == "expenses_month":
        this_month = result.get("this_month", 0)
        last_month = result.get("last_month", 0)
        try:
            tm = float(this_month)
            lm = float(last_month)
            if lm > 0:
                change_pct  = (tm - lm) / lm * 100
                change_str  = f"{change_pct:+.1f}%"
                change_tone = "bad" if change_pct > 10 else ("good" if change_pct < -10 else "neutral")
            else:
                change_str  = "—"
                change_tone = "neutral"
        except (TypeError, ValueError):
            change_str  = "—"
            change_tone = "neutral"
        return [
            {"label": "This Month", "value": _fmt_money(this_month), "tone": "neutral"},
            {"label": "Last Month", "value": _fmt_money(last_month), "tone": "neutral"},
            {"label": "Change",     "value": change_str,             "tone": change_tone},
        ]

    if intent == "expense_breakdown":
        total     = result.get("total", 0)
        breakdown = result.get("breakdown") or []
        rows = [{"label": "Total Expenses", "value": _fmt_money(total), "tone": "neutral"}]
        if breakdown:
            top = breakdown[0]
            rows.append({
                "label": "Top Category",
                "value": f"{top.get('name', '—')}: {_fmt_money(top.get('total', 0))}",
                "tone":  "neutral",
            })
        return rows

    if intent == "overdue_customers":
        count         = result.get("count", 0)
        total_overdue = result.get("total_overdue_amount", 0)
        try:
            c = int(count)
        except (TypeError, ValueError):
            c = 0
        try:
            t = float(total_overdue)
        except (TypeError, ValueError):
            t = 0.0
        return [
            {"label": "Overdue Customers", "value": _fmt_count(count),         "tone": "bad" if c > 0 else "neutral"},
            {"label": "Total Overdue",     "value": _fmt_money(total_overdue), "tone": "bad" if t > 0 else "neutral"},
        ]

    if intent == "unpaid_invoices":
        pos     = result.get("pos_unpaid_count", 0)
        b2b     = result.get("b2b_unpaid_count", 0)
        b2b_amt = result.get("b2b_outstanding_amount", 0)
        try:
            p = int(pos)
        except (TypeError, ValueError):
            p = 0
        try:
            b = int(b2b)
        except (TypeError, ValueError):
            b = 0
        try:
            ba = float(b2b_amt)
        except (TypeError, ValueError):
            ba = 0.0
        return [
            {"label": "POS Unpaid",      "value": _fmt_count(pos),     "tone": "bad" if p  > 0 else "neutral"},
            {"label": "B2B Unpaid",      "value": _fmt_count(b2b),     "tone": "bad" if b  > 0 else "neutral"},
            {"label": "B2B Outstanding", "value": _fmt_money(b2b_amt), "tone": "bad" if ba > 0 else "neutral"},
        ]

    if intent == "product_stock_value":
        return [
            {"label": "Total Value",      "value": _fmt_money(result.get("total_value", 0)), "tone": "neutral"},
            {"label": "Active Products",  "value": _fmt_count(result.get("item_count", 0)),  "tone": "neutral"},
        ]

    if intent == "low_stock":
        out_count = result.get("out_of_stock_count", 0)
        low_count = result.get("low_stock_count", result.get("count", 0))
        try:
            oc = int(out_count)
        except (TypeError, ValueError):
            oc = 0
        try:
            lc = int(low_count)
        except (TypeError, ValueError):
            lc = 0
        return [
            {"label": "Out of Stock", "value": _fmt_count(out_count), "tone": "bad" if oc > 0 else "neutral"},
            {"label": "Low Stock",    "value": _fmt_count(low_count), "tone": "bad" if lc > 0 else "neutral"},
        ]

    return []


# ── Tables ─────────────────────────────────────────────────────────────────────

_ROW_CAP = 15


def _make_table(
    columns: list[dict],
    rows: list[dict],
    *,
    total_count: int | None = None,
) -> dict:
    out: dict = {"columns": columns, "rows": rows[:_ROW_CAP]}
    if total_count is not None and total_count > _ROW_CAP:
        out["truncated"] = True
        out["total_count"] = total_count
    return out


def build_table(intent: str | None, result: dict | None) -> dict | None:
    """Return a {columns, rows} table dict or None for non-tabular intents."""
    result = result or {}
    if not intent:
        return None

    if intent == "top_products":
        items   = result.get("items") or []
        columns = [
            {"key": "name",    "label": "Product", "align": "left"},
            {"key": "qty",     "label": "Qty",     "align": "right"},
            {"key": "revenue", "label": "Revenue", "align": "right"},
        ]
        rows = [
            {
                "name":    item.get("name", ""),
                "qty":     _fmt_count(item.get("qty", 0)),
                "revenue": _fmt_money(item.get("revenue", 0)),
            }
            for item in items
        ]
        return _make_table(columns, rows, total_count=len(items))

    if intent == "low_stock":
        out_items = result.get("out_of_stock") or []
        low_items = result.get("low_stock")    or []
        # Fallback for the snapshot result shape (items only, no status split)
        if not out_items and not low_items:
            fallback = result.get("items") or []
            if not fallback:
                return None
            columns = [
                {"key": "sku",   "label": "SKU",     "align": "left"},
                {"key": "name",  "label": "Product", "align": "left"},
                {"key": "stock", "label": "Stock",   "align": "right"},
            ]
            rows = [
                {"sku": i.get("sku", ""), "name": i.get("name", ""), "stock": _fmt_count(i.get("stock", 0))}
                for i in fallback
            ]
            return _make_table(columns, rows, total_count=len(rows))

        all_items = (
            [{**i, "_status": "Out of Stock"} for i in out_items]
            + [{**i, "_status": "Low Stock"}  for i in low_items]
        )
        columns = [
            {"key": "sku",    "label": "SKU",     "align": "left"},
            {"key": "name",   "label": "Product", "align": "left"},
            {"key": "stock",  "label": "Stock",   "align": "right"},
            {"key": "status", "label": "Status",  "align": "left"},
        ]
        rows = [
            {
                "sku":    i.get("sku", ""),
                "name":   i.get("name", ""),
                "stock":  _fmt_count(i.get("stock", 0)),
                "status": i["_status"],
            }
            for i in all_items
        ]
        return _make_table(columns, rows, total_count=len(rows))

    if intent == "stock_levels":
        items   = result.get("items") or []
        columns = [
            {"key": "sku",       "label": "SKU",     "align": "left"},
            {"key": "name",      "label": "Product", "align": "left"},
            {"key": "stock",     "label": "Stock",   "align": "right"},
            {"key": "min_stock", "label": "Min",     "align": "right"},
            {"key": "status",    "label": "Status",  "align": "left"},
        ]
        rows = [
            {
                "sku":       i.get("sku", ""),
                "name":      i.get("name", ""),
                "stock":     _fmt_count(i.get("stock", 0)),
                "min_stock": _fmt_count(i.get("min_stock", 0)),
                "status":    i.get("status", ""),
            }
            for i in items
        ]
        return _make_table(columns, rows, total_count=len(rows))

    if intent == "overdue_customers":
        customers = result.get("customers") or []
        columns   = [
            {"key": "name",        "label": "Customer",     "align": "left"},
            {"key": "invoices",    "label": "Invoices",     "align": "right"},
            {"key": "outstanding", "label": "Outstanding",  "align": "right"},
            {"key": "days_overdue","label": "Days Overdue", "align": "right"},
        ]
        rows = [
            {
                "name":        c.get("name", ""),
                "invoices":    _fmt_count(c.get("invoice_count", 0)),
                "outstanding": _fmt_money(c.get("overdue_amount", 0)),
                "days_overdue":_fmt_count(c.get("days_overdue", 0)),
            }
            for c in customers
        ]
        return _make_table(columns, rows, total_count=len(rows))

    if intent == "customer_balances_top":
        clients = result.get("clients") or []
        columns = [
            {"key": "name",        "label": "Customer",    "align": "left"},
            {"key": "outstanding", "label": "Outstanding", "align": "right"},
        ]
        rows = [
            {"name": c.get("name", ""), "outstanding": _fmt_money(c.get("outstanding", 0))}
            for c in clients
        ]
        return _make_table(columns, rows, total_count=len(rows))

    if intent == "expense_breakdown":
        breakdown = result.get("breakdown") or []
        columns   = [
            {"key": "category", "label": "Category", "align": "left"},
            {"key": "total",    "label": "Total",    "align": "right"},
        ]
        rows = [
            {"category": item.get("name", ""), "total": _fmt_money(item.get("total", 0))}
            for item in breakdown
        ]
        return _make_table(columns, rows, total_count=len(rows))

    return None
