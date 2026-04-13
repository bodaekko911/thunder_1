from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import LocationStock, StockLocation, StockTransfer
from app.models.product import Product

QTY_PRECISION = Decimal("0.001")


def quantize_qty(value: object | None) -> Decimal:
    if value is None:
        value = 0
    return Decimal(str(value)).quantize(QTY_PRECISION, rounding=ROUND_HALF_UP)


def serialize_location(
    location: StockLocation,
    *,
    product_count: int = 0,
    total_qty: object | None = None,
) -> dict[str, Any]:
    return {
        "id": location.id,
        "name": location.name,
        "code": location.code,
        "location_type": location.location_type,
        "is_active": bool(location.is_active),
        "product_count": product_count,
        "total_qty": float(quantize_qty(total_qty)),
    }


def serialize_product_location_stock(
    product: Product,
    location_stocks: Iterable[LocationStock],
) -> dict[str, Any]:
    locations = []
    assigned_stock = Decimal("0.000")
    for location_stock in sorted(location_stocks, key=lambda item: (item.location.name if item.location else "", item.id or 0)):
        qty = quantize_qty(location_stock.qty)
        assigned_stock += qty
        location = location_stock.location
        locations.append(
            {
                "location_id": location_stock.location_id,
                "location_name": location.name if location else None,
                "location_code": location.code if location else None,
                "location_type": location.location_type if location else None,
                "qty": float(qty),
            }
        )

    total_stock = quantize_qty(getattr(product, "stock", 0))
    unassigned_stock = quantize_qty(total_stock - assigned_stock)
    return {
        "product_id": product.id,
        "sku": product.sku,
        "name": product.name,
        "unit": product.unit,
        "total_stock": float(total_stock),
        "assigned_stock": float(assigned_stock),
        "unassigned_stock": float(unassigned_stock),
        "locations": locations,
    }


def serialize_transfer(transfer: StockTransfer) -> dict[str, Any]:
    product = getattr(transfer, "product", None)
    source = getattr(transfer, "source_location", None)
    destination = getattr(transfer, "destination_location", None)
    actor = getattr(transfer, "user", None)
    return {
        "id": transfer.id,
        "product_id": transfer.product_id,
        "product": product.name if product else None,
        "sku": product.sku if product else None,
        "source_location_id": transfer.source_location_id,
        "source_location": source.name if source else None,
        "destination_location_id": transfer.destination_location_id,
        "destination_location": destination.name if destination else None,
        "qty": float(quantize_qty(transfer.qty)),
        "note": transfer.note or "",
        "user_id": transfer.user_id,
        "actor": actor.name if actor else None,
        "created_at": transfer.created_at.strftime("%Y-%m-%d %H:%M") if transfer.created_at else None,
    }


async def get_or_create_location_stock(
    db: AsyncSession,
    *,
    product_id: int,
    location_id: int,
) -> LocationStock:
    result = await db.execute(
        select(LocationStock).where(
            LocationStock.product_id == product_id,
            LocationStock.location_id == location_id,
        )
    )
    location_stock = result.scalar_one_or_none()
    if location_stock is None:
        location_stock = LocationStock(
            product_id=product_id,
            location_id=location_id,
            qty=Decimal("0.000"),
        )
        db.add(location_stock)
        await db.flush()
    return location_stock


async def create_stock_transfer(
    db: AsyncSession,
    *,
    product: Product,
    source_location: StockLocation,
    destination_location: StockLocation,
    qty: object,
    user_id: int | None,
    note: str | None = None,
) -> dict[str, Any]:
    transfer_qty = quantize_qty(qty)
    if transfer_qty <= 0:
        raise ValueError("Transfer quantity must be greater than 0")
    if source_location.id == destination_location.id:
        raise ValueError("Source and destination locations must be different")
    if not source_location.is_active or not destination_location.is_active:
        raise ValueError("Transfers require active source and destination locations")

    source_stock = await get_or_create_location_stock(
        db,
        product_id=product.id,
        location_id=source_location.id,
    )
    destination_stock = await get_or_create_location_stock(
        db,
        product_id=product.id,
        location_id=destination_location.id,
    )

    source_before = quantize_qty(source_stock.qty)
    if source_before < transfer_qty:
        raise ValueError(
            f"Insufficient stock in {source_location.name}. Available: {float(source_before)}"
        )

    source_after = quantize_qty(source_before - transfer_qty)
    destination_before = quantize_qty(destination_stock.qty)
    destination_after = quantize_qty(destination_before + transfer_qty)

    source_stock.qty = source_after
    destination_stock.qty = destination_after

    transfer = StockTransfer(
        product_id=product.id,
        source_location_id=source_location.id,
        destination_location_id=destination_location.id,
        qty=transfer_qty,
        note=note,
        user_id=user_id,
    )
    db.add(transfer)
    await db.flush()
    transfer.product = product
    transfer.source_location = source_location
    transfer.destination_location = destination_location

    return {
        "transfer": serialize_transfer(transfer),
        "source_qty_before": float(source_before),
        "source_qty_after": float(source_after),
        "destination_qty_before": float(destination_before),
        "destination_qty_after": float(destination_after),
    }
