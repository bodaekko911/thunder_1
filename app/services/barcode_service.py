from __future__ import annotations

import unicodedata

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.product import Product


def normalize_barcode_value(value: str | None) -> str:
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(value))
    normalized = normalized.replace("\u200b", "").replace("\ufeff", "")
    normalized = "".join(normalized.split())
    return normalized.strip().casefold()


async def find_product_by_barcode(db: AsyncSession, raw_value: str | None) -> Product | None:
    barcode = normalize_barcode_value(raw_value)
    if not barcode:
        return None

    result = await db.execute(select(Product).where(Product.is_active == True))
    products = result.scalars().all()
    for product in products:
        if normalize_barcode_value(getattr(product, "sku", None)) == barcode:
            return product
    return None
