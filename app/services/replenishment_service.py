from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.product import Product
from app.models.supplier import Purchase, PurchaseItem

QTY_PRECISION = Decimal("0.001")
MONEY_PRECISION = Decimal("0.01")


def _decimal(value: object | None) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _qty(value: object | None) -> Decimal:
    return max(_decimal(value), Decimal("0")).quantize(QTY_PRECISION, rounding=ROUND_HALF_UP)


def _money(value: object | None) -> Decimal:
    return max(_decimal(value), Decimal("0")).quantize(MONEY_PRECISION, rounding=ROUND_HALF_UP)


def reorder_threshold(product: Product) -> Decimal:
    if getattr(product, "reorder_level", None) is not None:
        return _qty(product.reorder_level)
    return _qty(getattr(product, "min_stock", 0))


def is_low_stock(product: Product) -> bool:
    return _qty(getattr(product, "stock", 0)) <= reorder_threshold(product)


def suggested_reorder_qty(product: Product) -> Decimal:
    current_stock = _qty(getattr(product, "stock", 0))
    threshold = reorder_threshold(product)
    if current_stock > threshold:
        return _qty(0)
    configured = _qty(getattr(product, "reorder_qty", 0))
    shortage = _qty(threshold - current_stock)
    return _qty(max(configured, shortage))


def serialize_low_stock_product(product: Product) -> dict[str, Any]:
    threshold = reorder_threshold(product)
    suggested = suggested_reorder_qty(product)
    supplier = getattr(product, "preferred_supplier", None)
    return {
        "id": product.id,
        "sku": product.sku,
        "name": product.name,
        "stock": float(_qty(getattr(product, "stock", 0))),
        "min_stock": float(_qty(getattr(product, "min_stock", 0))),
        "reorder_level": float(threshold),
        "reorder_qty": float(_qty(getattr(product, "reorder_qty", 0))) if getattr(product, "reorder_qty", None) is not None else None,
        "suggested_reorder_qty": float(suggested),
        "unit": product.unit,
        "preferred_supplier": (
            {"id": supplier.id, "name": supplier.name}
            if supplier is not None
            else None
        ),
        "preferred_supplier_id": getattr(product, "preferred_supplier_id", None),
        "alert_state": "low_stock" if is_low_stock(product) else "ok",
        "alert_active": is_low_stock(product),
        "draft_purchase_eligible": supplier is not None and suggested > 0,
    }


def _draft_signature(products: list[Product]) -> str:
    parts = []
    for product in sorted(products, key=lambda item: item.id or 0):
        parts.append(f"{product.id}:{suggested_reorder_qty(product)}:{_money(getattr(product, 'cost', 0))}")
    return "LOW-STOCK-DRAFT|" + "|".join(parts)


async def _next_purchase_number(db: AsyncSession) -> str:
    result = await db.execute(select(Purchase))
    purchases = result.scalars().all()
    max_id = max((purchase.id or 0) for purchase in purchases) if purchases else 0
    return f"PO-{str(max_id + 1).zfill(5)}"


async def create_or_reuse_draft_purchases(
    db: AsyncSession,
    *,
    products: list[Product],
    user_id: int | None,
) -> list[dict[str, Any]]:
    grouped: dict[int, list[Product]] = {}
    for product in products:
        grouped.setdefault(product.preferred_supplier_id, []).append(product)

    summaries: list[dict[str, Any]] = []
    for supplier_id, supplier_products in sorted(grouped.items(), key=lambda item: item[0]):
        signature = _draft_signature(supplier_products)
        existing_result = await db.execute(
            select(Purchase)
            .options(selectinload(Purchase.supplier), selectinload(Purchase.items))
            .where(
                Purchase.status == "draft",
                Purchase.supplier_id == supplier_id,
                Purchase.notes == signature,
            )
        )
        existing = existing_result.scalar_one_or_none()
        if existing is not None:
            summaries.append(
                {
                    "id": existing.id,
                    "purchase_number": existing.purchase_number,
                    "supplier_id": existing.supplier_id,
                    "supplier": existing.supplier.name if existing.supplier else "—",
                    "status": existing.status,
                    "items_count": len(existing.items),
                    "total": float(existing.total or 0),
                    "reused": True,
                }
            )
            continue

        purchase = Purchase(
            purchase_number=await _next_purchase_number(db),
            supplier_id=supplier_id,
            user_id=user_id,
            status="draft",
            subtotal=Decimal("0.00"),
            discount=Decimal("0.00"),
            total=Decimal("0.00"),
            notes=signature,
        )
        db.add(purchase)
        await db.flush()

        subtotal = Decimal("0.00")
        for product in sorted(supplier_products, key=lambda item: item.id or 0):
            qty = suggested_reorder_qty(product)
            unit_cost = _money(getattr(product, "cost", 0))
            line_total = _money(qty * unit_cost)
            subtotal += line_total
            db.add(
                PurchaseItem(
                    purchase_id=purchase.id,
                    product_id=product.id,
                    qty=qty,
                    unit_cost=unit_cost,
                    total=line_total,
                )
            )

        purchase.subtotal = _money(subtotal)
        purchase.discount = Decimal("0.00")
        purchase.total = _money(subtotal)
        summaries.append(
            {
                "id": purchase.id,
                "purchase_number": purchase.purchase_number,
                "supplier_id": purchase.supplier_id,
                "supplier": supplier_products[0].preferred_supplier.name if supplier_products[0].preferred_supplier else "—",
                "status": purchase.status,
                "items_count": len(supplier_products),
                "total": float(purchase.total or 0),
                "reused": False,
            }
        )

    return summaries
