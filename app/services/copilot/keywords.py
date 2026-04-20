"""Keyword dictionaries used by the scoring intent parser."""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Each entry uses:
#   require_any: list[list[str]]  — ALL groups must have at least one hit
#   boost:       list[str]        — adds 0.5 per hit (optional)
# ---------------------------------------------------------------------------
INTENT_KEYWORDS: dict[str, dict] = {
    # ── Sales ──────────────────────────────────────────────────────────────
    "sales_today": {
        "require_any": [
            ["today", "today's", "todays"],
            ["sales", "revenue", "made", "earned", "income", "took", "turnover", "sold"],
        ],
        "boost": ["how much", "total"],
    },
    "sales_summary": {
        "require_any": [
            ["how much", "total sales", "total revenue", "how much did", "gross sales"],
            ["make", "made", "earn", "earned", "sell", "sold", "generate", "generated",
             "revenue", "income", "turnover"],
        ],
        "boost": ["period", "week", "month", "year", "quarter"],
    },
    "sales_by_period": {
        "require_any": [
            ["sales by period", "daily sales", "weekly sales", "monthly sales",
             "sales by day", "sales by week", "sales by month", "sales trend",
             "sales over time", "sales chart"],
        ],
        "boost": ["daily", "weekly", "monthly"],
    },
    "top_products": {
        "require_any": [
            ["top products", "best selling", "top sellers", "best sellers",
             "top selling products", "most sold", "highest revenue products",
             "best products"],
        ],
        "boost": ["this month", "by revenue"],
    },
    # ── Inventory ──────────────────────────────────────────────────────────
    "low_stock": {
        "require_any": [
            ["low stock", "low-stock", "out of stock", "stock running low",
             "running low", "low inventory", "reorder", "almost out"],
        ],
        "boost": ["items", "products"],
    },
    "stock_levels": {
        "require_any": [
            ["stock levels", "inventory levels", "current stock",
             "stock snapshot", "all stock", "inventory snapshot",
             "stock report", "show stock"],
        ],
        "boost": ["all", "current"],
    },
    "product_stock_value": {
        "require_any": [
            ["stock value", "inventory value", "inventory worth",
             "how much is the inventory", "how much is the stock",
             "value of stock", "value of inventory", "worth of inventory",
             "total inventory value", "total stock value"],
        ],
        "boost": ["worth", "value"],
    },
    # ── Customers ──────────────────────────────────────────────────────────
    "overdue_customers": {
        "require_any": [
            ["overdue", "late", "behind", "owes", "owe", "hasn't paid",
             "havent paid", "unpaid customers", "late customers",
             "customers overdue", "overdue invoices", "overdue customers"],
        ],
        "boost": ["who", "customer", "customers", "client"],
    },
    "customer_balances_top": {
        "require_any": [
            ["who owes me the most", "biggest customer debt", "biggest debts",
             "top customer balances", "most outstanding", "owes me the most",
             "largest balance", "largest outstanding", "biggest balance",
             "top balances", "who owes most", "most debt"],
        ],
        # "who" and "most" push this above overdue_customers when both match "owes"
        "boost": ["customer", "client", "who", "most", "biggest", "largest"],
    },
    # ── Expenses ───────────────────────────────────────────────────────────
    "expenses_month": {
        "require_any": [
            ["expenses this month", "this month expenses", "monthly expenses",
             "month expenses", "expenses for this month", "expense this month",
             "spending this month"],
        ],
        "boost": ["total", "breakdown"],
    },
    "expense_breakdown": {
        "require_any": [
            ["expense breakdown", "expenses breakdown", "expense categories",
             "breakdown of expenses", "expense by category", "spending by category"],
        ],
        "boost": ["month", "category"],
    },
    # ── Invoices ───────────────────────────────────────────────────────────
    "unpaid_invoices": {
        "require_any": [
            ["unpaid invoices", "open invoices", "outstanding invoices",
             "settle later invoices", "invoices unpaid", "unpaid bills",
             "pending invoices", "invoices outstanding"],
        ],
        "boost": ["count", "total", "amount"],
    },
    # ── Profit ─────────────────────────────────────────────────────────────
    "profit_loss_summary": {
        "require_any": [
            ["profit and loss", "profit loss", "p l", "pl summary",
             "profit summary", "profit report", "p&l", "gross profit",
             "net profit", "profit margin", "loss summary"],
        ],
        "boost": ["this month", "summary", "report"],
    },
    # ── Help ───────────────────────────────────────────────────────────────
    "help": {
        "require_any": [
            ["help", "what can you do", "what can you ask", "what do you support",
             "commands", "capabilities", "what questions", "what can i ask",
             "what can you answer",
             "what can you help with"],
        ],
        "boost": [],
    },
}


# ---------------------------------------------------------------------------
# Arabic keyword map — if any phrase appears in the *raw* (pre-normalised) text
# the matched intent receives a +1.0 score boost.
# ---------------------------------------------------------------------------
ARABIC_KEYWORDS: dict[str, list[str]] = {
    "sales_today": ["مبيعات اليوم", "مبيعات النهاردة", "إيرادات اليوم"],
    "low_stock": ["مخزون منخفض", "بضاعة قليلة", "ناقص"],
    "overdue_customers": ["عملاء متأخرين", "اللي عليهم فلوس"],
    "expenses_month": ["مصروفات الشهر", "مصاريف"],
    "unpaid_invoices": ["فواتير غير مدفوعة", "فواتير معلقة"],
    "top_products": ["أفضل المنتجات", "أكثر مبيعا"],
    "stock_levels": ["المخزون", "الرصيد"],
}
