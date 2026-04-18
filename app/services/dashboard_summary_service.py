"""
Range-based aggregations for GET /dashboard/summary.
All "today" / range boundaries are computed in APP_TIMEZONE (default Africa/Cairo).
Queries use UTC datetimes converted from local midnight boundaries.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, true as sa_true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models.accounting import Account, Journal, JournalEntry
from app.models.b2b import B2BClient, B2BInvoice
from app.models.customer import Customer
from app.models.invoice import Invoice, InvoiceItem
from app.models.product import Product
from app.models.refund import RetailRefund
from app.models.user import User

_B2B_REF_TYPES = ("b2b", "b2b_invoice", "consignment_payment", "consignment")


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.APP_TIMEZONE)


def _utc_range(local_start: date, local_end: date) -> tuple[datetime, datetime]:
    tz = _tz()
    utc = ZoneInfo("UTC")
    start = datetime(local_start.year, local_start.month, local_start.day, 0, 0, 0, tzinfo=tz).astimezone(utc)
    end   = datetime(local_end.year,   local_end.month,   local_end.day,   23, 59, 59, 999999, tzinfo=tz).astimezone(utc)
    return start, end


def resolve_range(
    range_param: str,
    custom_start: str | None = None,
    custom_end: str | None = None,
) -> dict[str, Any]:
    today = datetime.now(_tz()).date()

    if range_param == "7d":
        rs, re, label = today - timedelta(days=6), today, "Last 7 days"
    elif range_param == "30d":
        rs, re, label = today - timedelta(days=29), today, "Last 30 days"
    elif range_param == "mtd":
        rs, re, label = today.replace(day=1), today, "Month to date"
    elif range_param == "qtd":
        qm = ((today.month - 1) // 3) * 3 + 1
        rs, re, label = today.replace(month=qm, day=1), today, "Quarter to date"
    elif range_param == "custom" and custom_start and custom_end:
        rs = date.fromisoformat(custom_start)
        re = date.fromisoformat(custom_end)
        label = f"{rs} – {re}"
    else:
        rs, re, label = today, today, "Today"

    num_days = (re - rs).days + 1
    prior_end   = rs - timedelta(days=1)
    prior_start = prior_end - timedelta(days=num_days - 1)

    utc_s, utc_e         = _utc_range(rs, re)
    p_utc_s, p_utc_e     = _utc_range(prior_start, prior_end)

    return {
        "label":         label,
        "start":         str(rs),
        "end":           str(re),
        "prior_start":   str(prior_start),
        "prior_end":     str(prior_end),
        "utc_start":     utc_s,
        "utc_end":       utc_e,
        "prior_utc_start": p_utc_s,
        "prior_utc_end":   p_utc_e,
        "num_days":      num_days,
    }


# ── helpers ────────────────────────────────────────────────────────────────

def _delta_pct(cur: float, prior: float) -> float | None:
    if prior == 0:
        return None
    return round((cur - prior) / abs(prior) * 100, 1)


def _direction(delta: float | None, higher_is_better: bool) -> str:
    if delta is None or abs(delta) <= 1:
        return "flat"
    if delta > 1:
        return "up" if higher_is_better else "bad_up"
    return "down" if higher_is_better else "bad_down"


async def _rev_account_id(db: AsyncSession) -> int | None:
    r = await db.execute(select(Account.id).where(Account.code == "4000"))
    return r.scalar_one_or_none()


async def _pos_net(db: AsyncSession, utc_s: datetime, utc_e: datetime) -> float:
    r = await db.execute(
        select(func.sum(Invoice.total))
        .where(Invoice.created_at >= utc_s, Invoice.created_at <= utc_e, Invoice.status == "paid")
    )
    pos = float(r.scalar() or 0)
    r = await db.execute(
        select(func.sum(RetailRefund.total))
        .where(RetailRefund.created_at >= utc_s, RetailRefund.created_at <= utc_e)
    )
    return max(0.0, pos - float(r.scalar() or 0))


async def _b2b_net(db: AsyncSession, utc_s: datetime, utc_e: datetime, acc_id: int | None) -> float:
    if not acc_id:
        return 0.0
    r = await db.execute(
        select(func.sum(JournalEntry.credit))
        .join(Journal, JournalEntry.journal_id == Journal.id)
        .where(
            JournalEntry.account_id == acc_id,
            Journal.created_at >= utc_s,
            Journal.created_at <= utc_e,
            Journal.ref_type.in_(_B2B_REF_TYPES),
        )
    )
    return float(r.scalar() or 0)


async def _cogs(db: AsyncSession, utc_s: datetime, utc_e: datetime) -> float:
    r = await db.execute(
        select(func.sum(InvoiceItem.qty * Product.cost))
        .join(Invoice, InvoiceItem.invoice_id == Invoice.id)
        .join(Product,  InvoiceItem.product_id == Product.id)
        .where(Invoice.created_at >= utc_s, Invoice.created_at <= utc_e, Invoice.status == "paid")
    )
    return float(r.scalar() or 0)


async def _daily_sparkline(
    db: AsyncSession,
    utc_s: datetime,
    utc_e: datetime,
    acc_id: int | None,
) -> list[float]:
    tz = _tz()
    tz_name = settings.APP_TIMEZONE

    pos_rows = await db.execute(
        select(
            func.date(func.timezone(tz_name, Invoice.created_at)).label("day"),
            func.sum(Invoice.total).label("total"),
        )
        .where(Invoice.created_at >= utc_s, Invoice.created_at <= utc_e, Invoice.status == "paid")
        .group_by(func.date(func.timezone(tz_name, Invoice.created_at)))
    )
    pos_by_day = {str(r.day): float(r.total) for r in pos_rows}

    ref_rows = await db.execute(
        select(
            func.date(func.timezone(tz_name, RetailRefund.created_at)).label("day"),
            func.sum(RetailRefund.total).label("total"),
        )
        .where(RetailRefund.created_at >= utc_s, RetailRefund.created_at <= utc_e)
        .group_by(func.date(func.timezone(tz_name, RetailRefund.created_at)))
    )
    ref_by_day = {str(r.day): float(r.total) for r in ref_rows}

    b2b_by_day: dict[str, float] = {}
    if acc_id:
        b2b_rows = await db.execute(
            select(
                func.date(func.timezone(tz_name, Journal.created_at)).label("day"),
                func.sum(JournalEntry.credit).label("total"),
            )
            .join(JournalEntry, JournalEntry.journal_id == Journal.id)
            .where(
                JournalEntry.account_id == acc_id,
                Journal.created_at >= utc_s,
                Journal.created_at <= utc_e,
                Journal.ref_type.in_(_B2B_REF_TYPES),
            )
            .group_by(func.date(func.timezone(tz_name, Journal.created_at)))
        )
        b2b_by_day = {str(r.day): float(r.total) for r in b2b_rows}

    local_s = utc_s.astimezone(tz).date()
    local_e = utc_e.astimezone(tz).date()
    result, d = [], local_s
    while d <= local_e:
        ds  = str(d)
        pos = max(0.0, pos_by_day.get(ds, 0.0) - ref_by_day.get(ds, 0.0))
        result.append(round(pos + b2b_by_day.get(ds, 0.0), 2))
        d += timedelta(days=1)
    return result


# ── hero sections ──────────────────────────────────────────────────────────

async def _hero_admin(db: AsyncSession, rng: dict, acc_id: int | None) -> dict:
    utc_s, utc_e   = rng["utc_start"], rng["utc_end"]
    p_utc_s, p_utc_e = rng["prior_utc_start"], rng["prior_utc_end"]

    rev_cur   = await _pos_net(db, utc_s, utc_e)   + await _b2b_net(db, utc_s, utc_e, acc_id)
    rev_prior = await _pos_net(db, p_utc_s, p_utc_e) + await _b2b_net(db, p_utc_s, p_utc_e, acc_id)
    rev_spark = await _daily_sparkline(db, utc_s, utc_e, acc_id)

    cogs_cur  = await _cogs(db, utc_s, utc_e)
    gp_cur    = rev_cur - cogs_cur
    cogs_pri  = await _cogs(db, p_utc_s, p_utc_e)
    gp_prior  = rev_prior - cogs_pri
    margin    = (gp_cur / rev_cur * 100) if rev_cur > 0 else 0.0

    r = await db.execute(select(Account.balance).where(Account.code == "1000"))
    cash = float(r.scalar() or 0)
    r = await db.execute(
        select(func.sum(B2BInvoice.total - B2BInvoice.amount_paid))
        .where(B2BInvoice.status.in_(["unpaid", "partial"]))
    )
    ar = float(r.scalar() or 0)

    r = await db.execute(
        select(func.count(Customer.id))
        .where(Customer.created_at >= utc_s, Customer.created_at <= utc_e)
    )
    new_cust = int(r.scalar() or 0)
    r = await db.execute(
        select(func.count(B2BClient.id))
        .where(B2BClient.created_at >= utc_s, B2BClient.created_at <= utc_e)
    )
    new_b2b = int(r.scalar() or 0)
    r = await db.execute(select(func.count(Customer.id)))
    total_cust = int(r.scalar() or 0)

    r = await db.execute(
        select(func.count(Customer.id))
        .where(Customer.created_at >= p_utc_s, Customer.created_at <= p_utc_e)
    )
    p_new_cust = int(r.scalar() or 0)
    r = await db.execute(
        select(func.count(B2BClient.id))
        .where(B2BClient.created_at >= p_utc_s, B2BClient.created_at <= p_utc_e)
    )
    p_new_b2b = int(r.scalar() or 0)

    cg_cur   = new_cust + new_b2b
    cg_prior = p_new_cust + p_new_b2b
    rev_d    = _delta_pct(rev_cur, rev_prior)
    gp_d     = _delta_pct(gp_cur,  gp_prior)
    cg_d     = _delta_pct(cg_cur,  cg_prior)

    return {
        "revenue": {
            "value":     round(rev_cur,  2), "prior": round(rev_prior, 2),
            "delta_pct": rev_d, "sparkline": rev_spark,
            "direction": _direction(rev_d, True),
        },
        "gross_profit": {
            "value":      round(gp_cur,  2), "prior": round(gp_prior, 2),
            "delta_pct":  gp_d, "sparkline": rev_spark,
            "direction":  _direction(gp_d, True), "margin_pct": round(margin, 1),
        },
        "cash_position": {"value": round(cash, 2), "ar": round(ar, 2)},
        "customer_growth": {
            "value":      cg_cur,  "prior": cg_prior,
            "delta_pct":  cg_d,   "total_active": total_cust,
            "direction":  _direction(cg_d, True),
        },
    }


async def _hero_cashier(db: AsyncSession, user_id: int) -> dict:
    tz = _tz()
    today = datetime.now(tz).date()
    utc_s, utc_e = _utc_range(today, today)

    r = await db.execute(
        select(func.count(Invoice.id), func.sum(Invoice.total))
        .where(Invoice.user_id == user_id, Invoice.created_at >= utc_s,
               Invoice.created_at <= utc_e, Invoice.status == "paid")
    )
    cnt, tot = r.one()
    cnt = int(cnt or 0)
    tot = float(tot or 0)
    avg = tot / cnt if cnt > 0 else 0.0

    r2 = await db.execute(
        select(InvoiceItem.name, func.sum(InvoiceItem.qty).label("qty"))
        .join(Invoice, InvoiceItem.invoice_id == Invoice.id)
        .where(Invoice.user_id == user_id, Invoice.created_at >= utc_s,
               Invoice.created_at <= utc_e, Invoice.status == "paid")
        .group_by(InvoiceItem.name)
        .order_by(func.sum(InvoiceItem.qty).desc())
        .limit(1)
    )
    top = r2.one_or_none()

    r3 = await db.execute(
        select(func.count(RetailRefund.id), func.sum(RetailRefund.total))
        .where(RetailRefund.user_id == user_id,
               RetailRefund.created_at >= utc_s, RetailRefund.created_at <= utc_e)
    )
    rc, rt = r3.one()
    rc = int(rc or 0)
    rt = float(rt or 0)

    return {
        "shift_sales":  {"count": cnt, "value": round(tot, 2)},
        "avg_basket":   {"value": round(avg, 2)},
        "top_item":     {"name": top.name if top else "—"},
        "refunds":      {"count": rc, "value": round(rt, 2), "direction": "bad_up" if rc > 0 else "flat"},
    }


async def _hero_farm(db: AsyncSession, rng: dict) -> dict:
    from app.models.farm import FarmDelivery
    from app.models.production import ProductionBatch
    from app.models.spoilage import SpoilageRecord

    utc_s, utc_e = rng["utc_start"], rng["utc_end"]
    tz = _tz()
    rs = datetime.fromisoformat(rng["start"])
    re = datetime.fromisoformat(rng["end"])
    rs_date = rs.date()
    re_date = re.date()

    r = await db.execute(
        select(func.count(FarmDelivery.id))
        .where(FarmDelivery.delivery_date >= rs_date, FarmDelivery.delivery_date <= re_date)
    )
    deliveries = int(r.scalar() or 0)

    r = await db.execute(
        select(func.sum(SpoilageRecord.qty))
        .where(SpoilageRecord.spoilage_date >= rs_date, SpoilageRecord.spoilage_date <= re_date)
    )
    spoilage_qty = float(r.scalar() or 0)

    r = await db.execute(
        select(func.count(ProductionBatch.id))
        .where(ProductionBatch.created_at >= utc_s, ProductionBatch.created_at <= utc_e)
    )
    batches = int(r.scalar() or 0)

    return {
        "deliveries":         {"value": deliveries},
        "spoilage":           {"qty": round(spoilage_qty, 2)},
        "production_batches": {"value": batches},
        "upcoming_deliveries": {"value": 0, "note": "Scheduling coming soon"},
    }


# ── chart data ─────────────────────────────────────────────────────────────

async def _chart(db: AsyncSession, rng: dict, acc_id: int | None) -> dict:
    tz     = _tz()
    tz_name = settings.APP_TIMEZONE
    utc_s, utc_e = rng["utc_start"], rng["utc_end"]

    pos_rows = await db.execute(
        select(
            func.date(func.timezone(tz_name, Invoice.created_at)).label("day"),
            func.sum(Invoice.total).label("pos"),
            func.count(Invoice.id).label("orders"),
        )
        .where(Invoice.created_at >= utc_s, Invoice.created_at <= utc_e, Invoice.status == "paid")
        .group_by(func.date(func.timezone(tz_name, Invoice.created_at)))
    )
    pos_by_day: dict[str, tuple[float, int]] = {
        str(r.day): (float(r.pos), int(r.orders)) for r in pos_rows
    }

    ref_rows = await db.execute(
        select(
            func.date(func.timezone(tz_name, RetailRefund.created_at)).label("day"),
            func.sum(RetailRefund.total).label("refunds"),
        )
        .where(RetailRefund.created_at >= utc_s, RetailRefund.created_at <= utc_e)
        .group_by(func.date(func.timezone(tz_name, RetailRefund.created_at)))
    )
    ref_by_day = {str(r.day): float(r.refunds) for r in ref_rows}

    b2b_by_day: dict[str, float] = {}
    if acc_id:
        b2b_rows = await db.execute(
            select(
                func.date(func.timezone(tz_name, Journal.created_at)).label("day"),
                func.sum(JournalEntry.credit).label("b2b"),
            )
            .join(JournalEntry, JournalEntry.journal_id == Journal.id)
            .where(
                JournalEntry.account_id == acc_id,
                Journal.created_at >= utc_s,
                Journal.created_at <= utc_e,
                Journal.ref_type.in_(_B2B_REF_TYPES),
            )
            .group_by(func.date(func.timezone(tz_name, Journal.created_at)))
        )
        b2b_by_day = {str(r.day): float(r.b2b) for r in b2b_rows}

    local_s = utc_s.astimezone(tz).date()
    local_e = utc_e.astimezone(tz).date()
    buckets, totals = [], []
    d = local_s
    while d <= local_e:
        ds    = str(d)
        pos, orders = pos_by_day.get(ds, (0.0, 0))
        ref   = ref_by_day.get(ds, 0.0)
        b2b   = b2b_by_day.get(ds, 0.0)
        day_total = pos + b2b
        buckets.append({
            "date":    ds,
            "pos":     round(pos, 2),
            "b2b":     round(b2b, 2),
            "refunds": round(-ref, 2),
            "orders":  orders,
        })
        totals.append(day_total)
        d += timedelta(days=1)

    moving_avg = []
    for i in range(len(buckets)):
        window = totals[max(0, i - 6): i + 1]
        moving_avg.append(round(sum(window) / len(window), 2))

    return {"buckets": buckets, "moving_avg_7d": moving_avg}


# ── panels ─────────────────────────────────────────────────────────────────

async def _panels(db: AsyncSession, rng: dict) -> dict:
    tz     = _tz()
    utc_s, utc_e = rng["utc_start"], rng["utc_end"]
    today  = datetime.now(tz).date()

    # Top products ──────────────────────────────────────────────────────────
    r_total = await db.execute(
        select(func.coalesce(func.sum(Invoice.total), 0))
        .where(Invoice.created_at >= utc_s, Invoice.created_at <= utc_e, Invoice.status == "paid")
    )
    total_rev = float(r_total.scalar() or 1)

    by_rev_rows = await db.execute(
        select(InvoiceItem.name,
               func.sum(InvoiceItem.total).label("rev"),
               func.sum(InvoiceItem.qty).label("qty"))
        .join(Invoice, InvoiceItem.invoice_id == Invoice.id)
        .where(Invoice.created_at >= utc_s, Invoice.created_at <= utc_e, Invoice.status == "paid")
        .group_by(InvoiceItem.name)
        .order_by(func.sum(InvoiceItem.total).desc())
        .limit(8)
    )
    by_revenue = [
        {"name": r.name, "revenue": round(float(r.rev), 2),
         "qty": round(float(r.qty), 2), "share": round(float(r.rev) / total_rev * 100, 1)}
        for r in by_rev_rows
    ]

    by_qty_rows = await db.execute(
        select(InvoiceItem.name,
               func.sum(InvoiceItem.qty).label("qty"),
               func.sum(InvoiceItem.total).label("rev"))
        .join(Invoice, InvoiceItem.invoice_id == Invoice.id)
        .where(Invoice.created_at >= utc_s, Invoice.created_at <= utc_e, Invoice.status == "paid")
        .group_by(InvoiceItem.name)
        .order_by(func.sum(InvoiceItem.qty).desc())
        .limit(8)
    )
    by_qty = [{"name": r.name, "qty": round(float(r.qty), 2), "revenue": round(float(r.rev), 2)}
              for r in by_qty_rows]

    by_margin_rows = await db.execute(
        select(InvoiceItem.name,
               func.sum(InvoiceItem.total).label("rev"),
               func.sum(InvoiceItem.qty * Product.cost).label("cogs"))
        .join(Invoice,  InvoiceItem.invoice_id == Invoice.id)
        .join(Product,  InvoiceItem.product_id == Product.id)
        .where(Invoice.created_at >= utc_s, Invoice.created_at <= utc_e, Invoice.status == "paid")
        .group_by(InvoiceItem.name)
        .order_by((func.sum(InvoiceItem.total) - func.sum(InvoiceItem.qty * Product.cost)).desc())
        .limit(8)
    )
    by_margin = []
    for r in by_margin_rows:
        rev  = float(r.rev)
        cogs = float(r.cogs or 0)
        gp   = rev - cogs
        by_margin.append({
            "name":       r.name,
            "revenue":    round(rev, 2),
            "margin":     round(gp, 2),
            "margin_pct": round(gp / rev * 100, 1) if rev > 0 else 0.0,
        })

    # Receivables ───────────────────────────────────────────────────────────
    b2b_rows = await db.execute(
        select(
            B2BClient.name,
            B2BClient.id,
            func.sum(B2BInvoice.total - B2BInvoice.amount_paid).label("outstanding"),
            func.min(B2BInvoice.due_date).label("oldest_due"),
        )
        .join(B2BInvoice, B2BInvoice.client_id == B2BClient.id)
        .where(B2BInvoice.status.in_(["unpaid", "partial"]))
        .group_by(B2BClient.id, B2BClient.name)
        .order_by(func.sum(B2BInvoice.total - B2BInvoice.amount_paid).desc())
        .limit(8)
    )
    receivables_b2b = []
    for r in b2b_rows:
        days_overdue = max(0, (today - r.oldest_due).days) if r.oldest_due else 0
        receivables_b2b.append({
            "name":        r.name,
            "client_id":   r.id,
            "outstanding": round(float(r.outstanding), 2),
            "days_overdue": days_overdue,
        })

    # Stock pressure ────────────────────────────────────────────────────────
    d14_s, d14_e = _utc_range(today - timedelta(days=13), today)
    sales_rows = await db.execute(
        select(InvoiceItem.product_id, func.sum(InvoiceItem.qty).label("qty"))
        .join(Invoice, InvoiceItem.invoice_id == Invoice.id)
        .where(Invoice.created_at >= d14_s, Invoice.created_at <= d14_e, Invoice.status == "paid")
        .group_by(InvoiceItem.product_id)
    )
    avg_daily: dict[int, float] = {r.product_id: float(r.qty) / 14.0 for r in sales_rows}

    prod_rows = await db.execute(
        select(Product.id, Product.name, Product.sku, Product.stock, Product.min_stock)
        .where(Product.is_active == True, Product.stock > 0)
    )
    all_active = prod_rows.all()

    stockout_risk = []
    for p in all_active:
        ad = avg_daily.get(p.id, 0.0)
        if ad > 0:
            days_left = float(p.stock) / ad
            if days_left < 7:
                stockout_risk.append({
                    "product_id": p.id, "name": p.name, "sku": p.sku,
                    "stock": float(p.stock), "days_left": round(days_left, 1),
                    "avg_daily": round(ad, 2),
                })
    stockout_risk.sort(key=lambda x: x["days_left"])

    low_stock_rows = await db.execute(
        select(Product.id, Product.name, Product.sku, Product.stock, Product.min_stock)
        .where(Product.is_active == True, Product.stock > 0, Product.stock <= Product.min_stock)
        .order_by(Product.stock.asc())
        .limit(8)
    )
    low_stock = [
        {"name": r.name, "sku": r.sku, "stock": float(r.stock), "min_stock": float(r.min_stock)}
        for r in low_stock_rows
    ]

    d60_s, d60_e = _utc_range(today - timedelta(days=59), today)
    sold_ids_r = await db.execute(
        select(InvoiceItem.product_id.distinct())
        .join(Invoice, InvoiceItem.invoice_id == Invoice.id)
        .where(Invoice.created_at >= d60_s, Invoice.created_at <= d60_e, Invoice.status == "paid")
    )
    sold_ids = {row[0] for row in sold_ids_r}

    dead_rows = await db.execute(
        select(Product.id, Product.name, Product.sku, Product.stock)
        .where(Product.is_active == True, Product.stock > 0,
               Product.id.notin_(sold_ids) if sold_ids else sa_true())
        .order_by(Product.stock.desc())
        .limit(8)
    )
    dead_stock = [{"name": r.name, "sku": r.sku, "stock": float(r.stock)} for r in dead_rows]

    # Recent activity ───────────────────────────────────────────────────────
    inv_r = await db.execute(
        select(Invoice)
        .options(selectinload(Invoice.customer))
        .where(Invoice.status == "paid")
        .order_by(Invoice.created_at.desc())
        .limit(10)
    )
    recent_invoices = inv_r.scalars().all()

    ref_r = await db.execute(
        select(RetailRefund)
        .options(selectinload(RetailRefund.customer))
        .order_by(RetailRefund.created_at.desc())
        .limit(5)
    )
    recent_refunds = ref_r.scalars().all()

    recent: list[dict] = []
    for inv in recent_invoices:
        recent.append({
            "type":     "sale",
            "ref":      inv.invoice_number,
            "customer": inv.customer.name if inv.customer else "Walk-in",
            "total":    float(inv.total),
            "method":   inv.payment_method or "cash",
            "at":       inv.created_at.isoformat() if inv.created_at else "",
        })
    for ref in recent_refunds:
        recent.append({
            "type":     "refund",
            "ref":      ref.refund_number,
            "customer": ref.customer.name if ref.customer else "—",
            "total":    -float(ref.total),
            "method":   ref.refund_method,
            "at":       ref.created_at.isoformat() if ref.created_at else "",
        })
    recent.sort(key=lambda x: x["at"], reverse=True)

    return {
        "top_products": {"by_revenue": by_revenue, "by_qty": by_qty, "by_margin": by_margin},
        "receivables":  {"b2b": receivables_b2b, "retail": []},
        "stock_pressure": {
            "stockout_risk": stockout_risk[:8],
            "low_stock":     low_stock,
            "dead_stock":    dead_stock,
        },
        "recent_activity": recent[:10],
    }


# ── main entry point ───────────────────────────────────────────────────────

async def get_summary(
    db: AsyncSession,
    range_param: str,
    custom_start: str | None,
    custom_end: str | None,
    user: User,
) -> dict:
    from app.core.permissions import has_permission

    rng    = resolve_range(range_param, custom_start, custom_end)
    acc_id = await _rev_account_id(db)

    role = getattr(user, "role", "admin")
    if role == "cashier":
        hero      = await _hero_cashier(db, user.id)
        hero_type = "cashier"
    elif has_permission(user, "page_farm") and not has_permission(user, "page_accounting"):
        hero      = await _hero_farm(db, rng)
        hero_type = "farm_manager"
    else:
        hero      = await _hero_admin(db, rng, acc_id)
        hero_type = "admin"

    chart  = await _chart(db, rng, acc_id)
    panels = await _panels(db, rng)

    generated_at = datetime.now(_tz()).isoformat()

    return {
        "range": {
            "label":       rng["label"],
            "start":       rng["start"],
            "end":         rng["end"],
            "prior_start": rng["prior_start"],
            "prior_end":   rng["prior_end"],
        },
        "hero":        hero,
        "hero_type":   hero_type,
        "chart":       chart,
        "panels":      panels,
        "generated_at": generated_at,
        "timezone":    settings.APP_TIMEZONE,
    }
