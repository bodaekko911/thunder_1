from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.log import record
from app.models.farm import Farm, FarmDelivery, FarmDeliveryItem
from app.models.inventory import StockMove
from app.models.product import Product


async def create_farm_delivery(
    db: AsyncSession,
    *,
    farm: Farm,
    delivery_date: date,
    user_id: int | None,
    items: list[dict[str, Any]],
    received_by: str | None = None,
    quality_notes: str | None = None,
    notes: str | None = None,
    record_stock_movement: bool = True,
    activity_user=None,
) -> tuple[FarmDelivery, int]:
    if not items:
        raise ValueError("Delivery must have at least one item")

    max_id_result = await db.execute(select(func.max(FarmDelivery.id)))
    max_id = max_id_result.scalar() or 0
    number = f"FD-{str(max_id + 1).zfill(4)}"

    delivery = FarmDelivery(
        delivery_number=number,
        farm_id=farm.id,
        user_id=user_id,
        delivery_date=delivery_date,
        received_by=received_by,
        quality_notes=quality_notes,
        notes=notes,
    )
    db.add(delivery)
    await db.flush()

    stock_moves_created = 0
    for item in items:
        product_id = int(item["product_id"])
        qty = float(item["qty"])
        item_notes = item.get("notes")

        prod_result = await db.execute(select(Product).where(Product.id == product_id))
        product = prod_result.scalar_one_or_none()
        if not product:
            raise ValueError(f"Product not found: {product_id}")

        db.add(
            FarmDeliveryItem(
                delivery_id=delivery.id,
                product_id=product.id,
                qty=qty,
                unit=product.unit,
                notes=item_notes,
            )
        )

        if record_stock_movement:
            before = float(product.stock or 0)
            after = before + qty
            product.stock = after
            db.add(
                StockMove(
                    product_id=product.id,
                    type="in",
                    user_id=user_id,
                    qty=qty,
                    qty_before=before,
                    qty_after=after,
                    ref_type="farm_intake",
                    ref_id=delivery.id,
                    note=f"{farm.name} — {number}",
                )
            )
            stock_moves_created += 1

    stock_note = "with stock movement" if record_stock_movement else "without stock movement"
    record(
        db,
        "Farm",
        "create_delivery",
        f"Delivery {number} from {farm.name} — {len(items)} product(s) {stock_note}",
        user=activity_user,
        ref_type="farm_delivery",
        ref_id=delivery.id,
    )
    await db.flush()
    return delivery, stock_moves_created
