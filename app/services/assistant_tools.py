"""
Query helpers backing the dashboard AI assistant tools.

Each function performs a focused database query and returns a plain dict
that can be serialised to JSON for Claude's tool_result messages.
"""
from __future__ import annotations

import difflib
from datetime import date as date_type, datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.b2b import B2BClient, B2BInvoice
from app.models.accounting import Account, Journal, JournalEntry
from app.models.customer import Customer
from app.models.expense import Expense, ExpenseCategory
from app.models.invoice import Invoice, InvoiceItem
from app.models.product import Product
from app.models.refund import RetailRefund
from app.services.copilot import fuzzy as copilot_fuzzy
from app.services.expense_service import get_summary as _expense_summary


def _as_date(value: str | date_type | None, *, fallback: date_type | None = None) -> date_type:
    if isinstance(value, date_type):
        return value
    if isinstance(value, str) and value.strip():
        return date_type.fromisoformat(value.strip())
    if fallback is not None:
        return fallback
    return date_type.today()


def _normalized_product_parts(product: Product) -> tuple[str, str, str]:
    sku = copilot_fuzzy.normalize(product.sku or "")
    name = copilot_fuzzy.normalize(product.name or "")
    combined = " ".join(part for part in [sku, name] if part).strip()
    return sku, name, combined


def _product_match_score(query: str, product: Product) -> float:
    normalized_query = copilot_fuzzy.normalize(query or "")
    if not normalized_query:
        return 0.0

    sku, name, combined = _normalized_product_parts(product)
    query_tokens = [token for token in normalized_query.split() if token]
    combined_tokens = set(combined.split())
    score = 0.0

    if sku and normalized_query == sku:
        score += 140.0
    if name and normalized_query == name:
        score += 125.0
    if sku and sku.startswith(normalized_query):
        score += 100.0
    if name and name.startswith(normalized_query):
        score += 90.0
    if sku and normalized_query in sku:
        score += 70.0
    if name and normalized_query in name:
        score += 65.0

    if query_tokens:
        matched_tokens = sum(1 for token in query_tokens if token in combined_tokens)
        score += matched_tokens * 18.0
        if matched_tokens == len(query_tokens):
            score += 35.0

    score += difflib.SequenceMatcher(None, normalized_query, sku).ratio() * 40.0 if sku else 0.0
    score += difflib.SequenceMatcher(None, normalized_query, name).ratio() * 55.0 if name else 0.0
    return score


def _serialize_product(product: Product) -> dict:
    return {
        "product_id": int(product.id),
        "sku": product.sku,
        "name": product.name,
        "category": product.category,
        "item_type": product.item_type,
        "unit": product.unit,
        "price": round(float(product.price or 0), 2),
        "cost": round(float(product.cost or 0), 2),
        "stock": round(float(product.stock or 0), 3),
        "min_stock": round(float(product.min_stock or 0), 3),
        "reorder_level": round(float(product.reorder_level or 0), 3) if product.reorder_level is not None else None,
        "reorder_qty": round(float(product.reorder_qty or 0), 3) if product.reorder_qty is not None else None,
    }


def _serialize_product_row(row) -> dict:
    return {
        "product_id": int(row.id),
        "sku": row.sku,
        "name": row.name,
        "category": row.category,
        "item_type": row.item_type,
        "unit": row.unit,
        "price": round(float(row.price or 0), 2),
        "cost": round(float(row.cost or 0), 2),
        "stock": round(float(row.stock or 0), 3),
        "min_stock": round(float(row.min_stock or 0), 3),
        "reorder_level": round(float(row.reorder_level or 0), 3) if row.reorder_level is not None else None,
        "reorder_qty": round(float(row.reorder_qty or 0), 3) if row.reorder_qty is not None else None,
    }


def _product_snapshot_query():
    return select(
        Product.id,
        Product.sku,
        Product.name,
        Product.category,
        Product.item_type,
        Product.unit,
        Product.price,
        Product.cost,
        Product.stock,
        Product.min_stock,
        Product.reorder_level,
        Product.reorder_qty,
    ).where(Product.is_active == True)


async def get_sales_summary(db: AsyncSession, *, date_from: str, date_to: str) -> dict:
    """POS + B2B revenue with refunds deducted for a date range."""
    start_date = _as_date(date_from)
    end_date = _as_date(date_to, fallback=start_date)
    r = await db.execute(
        select(func.coalesce(func.sum(Invoice.total), 0))
        .where(func.date(Invoice.created_at).between(start_date, end_date), Invoice.status == "paid")
    )
    pos_sales = float(r.scalar() or 0)

    r = await db.execute(
        select(func.coalesce(func.sum(RetailRefund.total), 0))
        .where(func.date(RetailRefund.created_at).between(start_date, end_date))
    )
    refunds = float(r.scalar() or 0)

    r = await db.execute(
        select(func.coalesce(func.sum(B2BInvoice.total), 0))
        .where(func.date(B2BInvoice.created_at).between(start_date, end_date))
    )
    b2b_sales = float(r.scalar() or 0)

    net_pos = max(0.0, pos_sales - refunds)
    return {
        "date_from": date_from,
        "date_to": date_to,
        "pos_sales": round(pos_sales, 2),
        "b2b_sales": round(b2b_sales, 2),
        "refunds": round(refunds, 2),
        "net_pos": round(net_pos, 2),
        "total": round(net_pos + b2b_sales, 2),
    }


async def get_top_products(
    db: AsyncSession,
    *,
    date_from: str,
    date_to: str,
    limit: int = 10,
) -> dict:
    """Top-selling products by revenue (POS invoices only)."""
    r = await db.execute(
        select(
            InvoiceItem.name,
            func.sum(InvoiceItem.qty).label("qty_sold"),
            func.sum(InvoiceItem.total).label("revenue"),
        )
        .join(Invoice, InvoiceItem.invoice_id == Invoice.id)
        .where(
            func.date(Invoice.created_at).between(_as_date(date_from), _as_date(date_to, fallback=_as_date(date_from))),
            Invoice.status == "paid",
        )
        .group_by(InvoiceItem.name)
        .order_by(func.sum(InvoiceItem.total).desc())
        .limit(limit)
    )
    items = [
        {"name": row.name, "qty": float(row.qty_sold), "revenue": float(row.revenue)}
        for row in r.all()
    ]
    return {"date_from": date_from, "date_to": date_to, "items": items, "count": len(items)}


async def get_low_stock_items(db: AsyncSession, *, threshold: int = 5) -> dict:
    """Products at or below the threshold (includes out-of-stock)."""
    r = await db.execute(
        _product_snapshot_query().where(Product.stock <= threshold)
    )
    products = r.all()
    out_of_stock = [p for p in products if float(p.stock or 0) <= 0]
    low_stock = [p for p in products if float(p.stock or 0) > 0]
    return {
        "threshold": threshold,
        "out_of_stock_count": len(out_of_stock),
        "low_stock_count": len(low_stock),
        "out_of_stock": [{"sku": p.sku, "name": p.name, "stock": float(p.stock or 0)} for p in out_of_stock[:20]],
        "low_stock": [{"sku": p.sku, "name": p.name, "stock": float(p.stock or 0)} for p in low_stock[:20]],
    }


async def get_expenses_summary(db: AsyncSession) -> dict:
    """Expense totals for the current and previous month with category breakdown."""
    return await _expense_summary(db)


async def get_expenses_range_summary(
    db: AsyncSession,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Expense total for any explicit date range."""
    today = date_type.today()
    start_date = _as_date(date_from, fallback=today.replace(day=1))
    end_date = _as_date(date_to, fallback=today)

    result = await db.execute(
        select(func.coalesce(func.sum(Expense.amount), 0)).where(
            Expense.expense_date >= start_date,
            Expense.expense_date <= end_date,
        )
    )
    return {
        "date_from": start_date.isoformat(),
        "date_to": end_date.isoformat(),
        "total": round(float(result.scalar() or 0), 2),
    }


async def get_unpaid_invoices_summary(db: AsyncSession) -> dict:
    """Count and outstanding amounts for unpaid POS and B2B invoices."""
    r = await db.execute(
        select(func.count(Invoice.id)).where(Invoice.status == "unpaid")
    )
    pos_unpaid = int(r.scalar() or 0)

    r = await db.execute(
        select(func.count(B2BInvoice.id)).where(B2BInvoice.status.in_(["unpaid", "partial"]))
    )
    b2b_count = int(r.scalar() or 0)

    r = await db.execute(
        select(func.coalesce(func.sum(B2BInvoice.total - B2BInvoice.amount_paid), 0))
        .where(B2BInvoice.status.in_(["unpaid", "partial"]))
    )
    b2b_outstanding = float(r.scalar() or 0)

    return {
        "pos_unpaid_count": pos_unpaid,
        "b2b_unpaid_count": b2b_count,
        "b2b_outstanding_amount": round(b2b_outstanding, 2),
    }


async def get_customer_balances(db: AsyncSession, *, limit: int = 10) -> dict:
    """B2B clients ranked by outstanding (unpaid) balance."""
    r = await db.execute(
        select(
            B2BClient.name,
            func.coalesce(
                func.sum(B2BInvoice.total - B2BInvoice.amount_paid), 0
            ).label("outstanding"),
        )
        .join(B2BInvoice, B2BInvoice.client_id == B2BClient.id)
        .where(B2BInvoice.status.in_(["unpaid", "partial"]))
        .group_by(B2BClient.id, B2BClient.name)
        .order_by(func.sum(B2BInvoice.total - B2BInvoice.amount_paid).desc())
        .limit(limit)
    )
    clients = [{"name": row.name, "outstanding": round(float(row.outstanding), 2)} for row in r.all()]
    return {
        "clients": clients,
        "count": len(clients),
        "total_outstanding": round(sum(c["outstanding"] for c in clients), 2),
    }


async def get_sales_by_period(
    db: AsyncSession,
    *,
    period: str = "daily",
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """POS sales aggregated by day, week, or month."""
    today = date_type.today()
    if not date_from:
        date_from = (today - timedelta(days=29)).isoformat()
    if not date_to:
        date_to = today.isoformat()
    start_date = _as_date(date_from)
    end_date = _as_date(date_to, fallback=start_date)

    r = await db.execute(
        select(
            func.date(Invoice.created_at).label("day"),
            func.coalesce(func.sum(Invoice.total), 0).label("total"),
        )
        .where(
            func.date(Invoice.created_at).between(start_date, end_date),
            Invoice.status == "paid",
        )
        .group_by(func.date(Invoice.created_at))
        .order_by(func.date(Invoice.created_at))
    )
    daily = [{"date": str(row.day), "total": float(row.total)} for row in r.all()]

    if period == "daily":
        return {"period": "daily", "date_from": date_from, "date_to": date_to, "data": daily}

    # Aggregate to weekly or monthly in Python
    from collections import defaultdict

    aggregated: dict[str, float] = defaultdict(float)
    for entry in daily:
        d = datetime.strptime(entry["date"], "%Y-%m-%d").date()
        if period == "weekly":
            iso = d.isocalendar()
            key = f"{iso[0]}-W{iso[1]:02d}"
        else:  # monthly
            key = d.strftime("%Y-%m")
        aggregated[key] += entry["total"]

    data = [{"period": k, "total": round(v, 2)} for k, v in sorted(aggregated.items())]
    return {"period": period, "date_from": date_from, "date_to": date_to, "data": data}


async def get_overdue_customers(db: AsyncSession, *, limit: int = 10) -> dict:
    """B2B customers with overdue unpaid or partial invoices."""
    today = date_type.today()
    result = await db.execute(
        select(
            B2BClient.id,
            B2BClient.name,
            func.count(B2BInvoice.id).label("invoice_count"),
            func.coalesce(func.sum(B2BInvoice.total - B2BInvoice.amount_paid), 0).label("overdue_amount"),
            func.min(B2BInvoice.due_date).label("oldest_due_date"),
        )
        .join(B2BInvoice, B2BInvoice.client_id == B2BClient.id)
        .where(
            B2BInvoice.status.in_(["unpaid", "partial"]),
            B2BInvoice.due_date.is_not(None),
            B2BInvoice.due_date < today,
        )
        .group_by(B2BClient.id, B2BClient.name)
        .order_by(func.sum(B2BInvoice.total - B2BInvoice.amount_paid).desc(), B2BClient.name.asc())
        .limit(limit)
    )
    customers = []
    for row in result.all():
        oldest_due = row.oldest_due_date.isoformat() if row.oldest_due_date else None
        days_overdue = (today - row.oldest_due_date).days if row.oldest_due_date else 0
        customers.append(
            {
                "client_id": int(row.id),
                "name": row.name,
                "invoice_count": int(row.invoice_count or 0),
                "overdue_amount": round(float(row.overdue_amount or 0), 2),
                "oldest_due_date": oldest_due,
                "days_overdue": days_overdue,
            }
        )
    return {
        "count": len(customers),
        "customers": customers,
        "total_overdue_amount": round(sum(item["overdue_amount"] for item in customers), 2),
    }


async def get_customer_balance(
    db: AsyncSession,
    *,
    customer_query: str | None = None,
    customer_id: int | None = None,
) -> dict:
    """Find a B2B customer balance by name or exact id-like input."""
    query = (customer_query or "").strip()
    if not query and customer_id is None:
        return {"matches": [], "count": 0, "query": query}

    clauses = []
    if query:
        clauses.append(B2BClient.name.ilike(f"%{query}%"))
    if query and query.isdigit():
        clauses.append(B2BClient.id == int(query))
    if customer_id is not None:
        clauses.append(B2BClient.id == int(customer_id))

    result = await db.execute(
        select(
            B2BClient.id,
            B2BClient.name,
            func.coalesce(func.sum(B2BInvoice.total - B2BInvoice.amount_paid), 0).label("outstanding"),
            func.count(B2BInvoice.id).label("open_invoice_count"),
        )
        .join(B2BInvoice, B2BInvoice.client_id == B2BClient.id, isouter=True)
        .where(or_(*clauses))
        .where(or_(B2BInvoice.id.is_(None), B2BInvoice.status.in_(["unpaid", "partial"])))
        .group_by(B2BClient.id, B2BClient.name)
        .order_by(func.coalesce(func.sum(B2BInvoice.total - B2BInvoice.amount_paid), 0).desc(), B2BClient.name.asc())
        .limit(5)
    )
    matches = [
        {
            "client_id": int(row.id),
            "name": row.name,
            "outstanding": round(float(row.outstanding or 0), 2),
            "open_invoice_count": int(row.open_invoice_count or 0),
        }
        for row in result.all()
    ]
    return {
        "query": query,
        "count": len(matches),
        "matches": matches,
        "selected": matches[0] if matches else None,
    }


async def get_product_details(
    db: AsyncSession,
    *,
    product_query: str | None = None,
    product_id: int | None = None,
) -> dict:
    """Find product details by SKU or name fragment, with fuzzy fallback."""
    query = (product_query or "").strip()
    if not query and product_id is None:
        return {"matches": [], "count": 0, "query": query}

    if product_id is not None:
        result = await db.execute(
            _product_snapshot_query()
            .where(Product.id == int(product_id))
            .limit(1)
        )
        product = result.one_or_none()
        matches = [_serialize_product_row(product)] if product else []
        return {
            "query": query,
            "count": len(matches),
            "matches": matches,
            "selected": matches[0] if matches else None,
            "ambiguous": False,
        }

    result = await db.execute(
        _product_snapshot_query()
    )
    products = result.all()
    scored = [
        (score, product)
        for product in products
        if (score := _product_match_score(query, product)) >= 38.0
    ]
    scored.sort(key=lambda item: (-item[0], (item[1].name or "").lower(), (item[1].sku or "").lower()))

    top_ranked = scored[:5]
    matches = [_serialize_product_row(product) for _, product in top_ranked]
    selected = matches[0] if matches else None
    ambiguous = False
    if len(top_ranked) > 1:
        top_score = top_ranked[0][0]
        next_score = top_ranked[1][0]
        ambiguous = (top_score - next_score) < 18.0
        if ambiguous:
            selected = None

    return {
        "query": query,
        "count": len(matches),
        "matches": matches,
        "selected": selected,
        "ambiguous": ambiguous,
    }


async def get_stock_levels(
    db: AsyncSession,
    *,
    product_query: str | None = None,
    limit: int = 10,
) -> dict:
    """Stock snapshot, optionally filtered to a product query."""
    query = (product_query or "").strip()
    products: list[Product]
    if query:
        result = await db.execute(_product_snapshot_query())
        all_products = result.all()
        scored = [
            (score, product)
            for product in all_products
            if (score := _product_match_score(query, product)) >= 32.0
        ]
        scored.sort(key=lambda item: (-item[0], float(item[1].stock or 0), (item[1].name or "").lower()))
        products = [product for _, product in scored[:limit]]
    else:
        result = await db.execute(
            _product_snapshot_query()
            .order_by(Product.stock.asc(), Product.name.asc())
            .limit(limit)
        )
        products = result.all()

    items = [
        {
            "product_id": int(product.id),
            "sku": product.sku,
            "name": product.name,
            "stock": round(float(product.stock or 0), 3),
            "min_stock": round(float(product.min_stock or 0), 3),
            "status": (
                "out_of_stock"
                if float(product.stock or 0) <= 0
                else "low_stock"
                if float(product.stock or 0) <= float(product.min_stock or 0)
                else "in_stock"
            ),
        }
        for product in products
    ]
    return {"query": query or None, "count": len(items), "items": items}


async def get_expense_breakdown(
    db: AsyncSession,
    *,
    month: str | None = None,
) -> dict:
    """Expense breakdown for a given month or current month."""
    if month:
        try:
            year, month_number = int(month[:4]), int(month[5:7])
        except (TypeError, ValueError, IndexError):
            year = datetime.utcnow().year
            month_number = datetime.utcnow().month
    else:
        now = datetime.utcnow()
        year = now.year
        month_number = now.month

    result = await db.execute(
        select(
            ExpenseCategory.name,
            func.coalesce(func.sum(Expense.amount), 0).label("total"),
        )
        .join(Expense, Expense.category_id == ExpenseCategory.id)
        .where(
            func.extract("year", Expense.expense_date) == year,
            func.extract("month", Expense.expense_date) == month_number,
        )
        .group_by(ExpenseCategory.name)
        .order_by(func.sum(Expense.amount).desc(), ExpenseCategory.name.asc())
    )
    breakdown = [{"name": row.name, "total": round(float(row.total or 0), 2)} for row in result.all()]
    total = round(sum(item["total"] for item in breakdown), 2)
    return {"month": f"{year:04d}-{month_number:02d}", "breakdown": breakdown, "total": total}


async def get_stock_value_summary(db: AsyncSession) -> dict:
    """Total inventory value (SUM stock * cost) for active products, grouped by top-5 categories."""
    r = await db.execute(
        select(
            Product.category,
            func.coalesce(func.sum(Product.stock * Product.cost), 0).label("value"),
            func.count(Product.id).label("count"),
        )
        .where(Product.is_active == True)
        .group_by(Product.category)
        .order_by(func.sum(Product.stock * Product.cost).desc())
        .limit(5)
    )
    by_category = [
        {"category": row.category or "Uncategorised", "value": round(float(row.value or 0), 2), "count": int(row.count or 0)}
        for row in r.all()
    ]

    r2 = await db.execute(
        select(
            func.coalesce(func.sum(Product.stock * Product.cost), 0),
            func.count(Product.id),
        ).where(Product.is_active == True)
    )
    row2 = r2.one()
    return {
        "total_value": round(float(row2[0] or 0), 2),
        "item_count": int(row2[1] or 0),
        "by_category": by_category,
    }


async def get_profit_loss_summary(
    db: AsyncSession,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Simple profit and loss summary using revenue minus expenses."""
    today = date_type.today()
    if not date_from:
        date_from = today.replace(day=1).isoformat()
    if not date_to:
        date_to = today.isoformat()
    start_date = _as_date(date_from)
    end_date = _as_date(date_to, fallback=start_date)

    sales = await get_sales_summary(db, date_from=date_from, date_to=date_to)
    expense_result = await db.execute(
        select(func.coalesce(func.sum(Expense.amount), 0)).where(
            Expense.expense_date >= start_date,
            Expense.expense_date <= end_date,
        )
    )
    expenses = round(float(expense_result.scalar() or 0), 2)
    gross_profit = round(sales["total"] - expenses, 2)
    margin_pct = round((gross_profit / sales["total"] * 100) if sales["total"] > 0 else 0, 2)

    return {
        "date_from": date_from,
        "date_to": date_to,
        "revenue": round(float(sales["total"]), 2),
        "expenses": expenses,
        "gross_profit": gross_profit,
        "margin_pct": margin_pct,
    }


async def get_recent_activity(
    db: AsyncSession,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 10,
) -> dict:
    """Recent paid sales and refunds in the requested period."""
    today = date_type.today()
    start_date = _as_date(date_from, fallback=today)
    end_date = _as_date(date_to, fallback=start_date)

    invoice_result = await db.execute(
        select(
            Invoice.id,
            Invoice.invoice_number,
            Invoice.customer_id,
            Invoice.total,
            Invoice.payment_method,
            Invoice.created_at,
        )
        .where(
            func.date(Invoice.created_at).between(start_date, end_date),
            Invoice.status == "paid",
        )
        .order_by(Invoice.created_at.desc())
        .limit(limit)
    )
    refund_result = await db.execute(
        select(
            RetailRefund.id,
            RetailRefund.refund_number,
            RetailRefund.customer_id,
            RetailRefund.total,
            RetailRefund.refund_method,
            RetailRefund.created_at,
            RetailRefund.invoice_id,
        )
        .where(func.date(RetailRefund.created_at).between(start_date, end_date))
        .order_by(RetailRefund.created_at.desc())
        .limit(limit)
    )
    invoice_rows = invoice_result.all()
    refund_rows = refund_result.all()

    customer_ids = {
        row.customer_id
        for row in [*invoice_rows, *refund_rows]
        if getattr(row, "customer_id", None) is not None
    }
    customer_map: dict[int, str] = {}
    if customer_ids:
        customer_result = await db.execute(select(Customer.id, Customer.name).where(Customer.id.in_(customer_ids)))
        customer_map = {int(row.id): row.name for row in customer_result.all()}

    items = []
    for row in invoice_rows:
        items.append(
            {
                "type": "sale",
                "invoice_id": int(row.id),
                "invoice_number": row.invoice_number,
                "customer": customer_map.get(int(row.customer_id), "Walk-in") if row.customer_id is not None else "Walk-in",
                "total": round(float(row.total or 0), 2),
                "method": row.payment_method or "cash",
                "timestamp": row.created_at.isoformat() if row.created_at else None,
            }
        )
    for row in refund_rows:
        items.append(
            {
                "type": "refund",
                "invoice_id": int(row.invoice_id) if row.invoice_id is not None else None,
                "invoice_number": row.refund_number,
                "customer": customer_map.get(int(row.customer_id), "-") if row.customer_id is not None else "-",
                "total": round(-float(row.total or 0), 2),
                "method": row.refund_method or "cash",
                "timestamp": row.created_at.isoformat() if row.created_at else None,
            }
        )
    items.sort(key=lambda item: item.get("timestamp") or "", reverse=True)
    items = items[:limit]
    return {
        "date_from": start_date.isoformat(),
        "date_to": end_date.isoformat(),
        "count": len(items),
        "items": items,
    }


async def get_customer_growth_summary(
    db: AsyncSession,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Customer growth in a period vs the prior equal-length period."""
    today = date_type.today()
    start_date = _as_date(date_from, fallback=today.replace(day=1))
    end_date = _as_date(date_to, fallback=today)
    span = (end_date - start_date).days
    prior_end = start_date - timedelta(days=1)
    prior_start = prior_end - timedelta(days=span)

    total_result = await db.execute(select(func.count(Customer.id)))
    current_result = await db.execute(
        select(func.count(Customer.id)).where(
            func.date(Customer.created_at).between(start_date, end_date)
        )
    )
    prior_result = await db.execute(
        select(func.count(Customer.id)).where(
            func.date(Customer.created_at).between(prior_start, prior_end)
        )
    )

    total_customers = int(total_result.scalar() or 0)
    current_new = int(current_result.scalar() or 0)
    prior_new = int(prior_result.scalar() or 0)
    change_pct = round(((current_new - prior_new) / prior_new) * 100, 1) if prior_new else None
    return {
        "date_from": start_date.isoformat(),
        "date_to": end_date.isoformat(),
        "total_customers": total_customers,
        "new_customers": current_new,
        "prior_new_customers": prior_new,
        "change_pct": change_pct,
    }


async def get_b2b_performance_summary(
    db: AsyncSession,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Paid B2B sales and current outstanding balance."""
    today = date_type.today()
    start_date = _as_date(date_from, fallback=today.replace(day=1))
    end_date = _as_date(date_to, fallback=today)

    paid_sales_result = await db.execute(
        select(func.coalesce(func.sum(B2BInvoice.total), 0)).where(
            func.date(B2BInvoice.created_at).between(start_date, end_date),
            B2BInvoice.status == "paid",
        )
    )
    outstanding_result = await db.execute(
        select(func.coalesce(func.sum(B2BInvoice.total - B2BInvoice.amount_paid), 0)).where(
            B2BInvoice.status.in_(["unpaid", "partial"])
        )
    )
    active_clients_result = await db.execute(select(func.count(B2BClient.id)).where(B2BClient.is_active == True))
    unpaid_clients_result = await db.execute(
        select(func.count(func.distinct(B2BInvoice.client_id))).where(
            B2BInvoice.status.in_(["unpaid", "partial"])
        )
    )

    return {
        "date_from": start_date.isoformat(),
        "date_to": end_date.isoformat(),
        "paid_sales": round(float(paid_sales_result.scalar() or 0), 2),
        "outstanding": round(float(outstanding_result.scalar() or 0), 2),
        "active_clients": int(active_clients_result.scalar() or 0),
        "clients_with_balance": int(unpaid_clients_result.scalar() or 0),
    }
