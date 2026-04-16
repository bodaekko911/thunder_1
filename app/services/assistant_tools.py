"""
Query helpers backing the dashboard AI assistant tools.

Each function performs a focused database query and returns a plain dict
that can be serialised to JSON for Claude's tool_result messages.
"""
from __future__ import annotations

from datetime import date as date_type, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.b2b import B2BClient, B2BInvoice
from app.models.invoice import Invoice, InvoiceItem
from app.models.product import Product
from app.models.refund import RetailRefund
from app.services.expense_service import get_summary as _expense_summary


async def get_sales_summary(db: AsyncSession, *, date_from: str, date_to: str) -> dict:
    """POS + B2B revenue with refunds deducted for a date range."""
    r = await db.execute(
        select(func.coalesce(func.sum(Invoice.total), 0))
        .where(func.date(Invoice.created_at).between(date_from, date_to), Invoice.status == "paid")
    )
    pos_sales = float(r.scalar() or 0)

    r = await db.execute(
        select(func.coalesce(func.sum(RetailRefund.total), 0))
        .where(func.date(RetailRefund.created_at).between(date_from, date_to))
    )
    refunds = float(r.scalar() or 0)

    r = await db.execute(
        select(func.coalesce(func.sum(B2BInvoice.total), 0))
        .where(func.date(B2BInvoice.created_at).between(date_from, date_to))
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
            func.date(Invoice.created_at).between(date_from, date_to),
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
        select(Product).where(Product.is_active == True, Product.stock <= threshold)
    )
    products = r.scalars().all()
    out_of_stock = [p for p in products if float(p.stock) <= 0]
    low_stock = [p for p in products if float(p.stock) > 0]
    return {
        "threshold": threshold,
        "out_of_stock_count": len(out_of_stock),
        "low_stock_count": len(low_stock),
        "out_of_stock": [{"sku": p.sku, "name": p.name, "stock": float(p.stock)} for p in out_of_stock[:20]],
        "low_stock": [{"sku": p.sku, "name": p.name, "stock": float(p.stock)} for p in low_stock[:20]],
    }


async def get_expenses_summary(db: AsyncSession) -> dict:
    """Expense totals for the current and previous month with category breakdown."""
    return await _expense_summary(db)


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

    r = await db.execute(
        select(
            func.date(Invoice.created_at).label("day"),
            func.coalesce(func.sum(Invoice.total), 0).label("total"),
        )
        .where(
            func.date(Invoice.created_at).between(date_from, date_to),
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
