import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.customer import Customer
from app.models.product import Product


def _normalize_column_name(value) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _build_column_lookup(df: pd.DataFrame) -> dict:
    return {_normalize_column_name(column): column for column in df.columns}


def _get_row_value(row, columns: dict, *aliases, default=""):
    for alias in aliases:
        actual = columns.get(_normalize_column_name(alias))
        if actual is not None:
            return row.get(actual, default)
    return default


async def import_customers(filepath: str, db: AsyncSession) -> dict:
    df = pd.read_excel(filepath)
    columns = _build_column_lookup(df)

    added   = 0
    skipped = 0

    for _, row in df.iterrows():
        name = str(
            _get_row_value(row, columns, "Vendor", "Vendor  ", "Customer", "Customer Name", default="")
        ).strip()

        if not name or name == "nan":
            skipped += 1
            continue

        phone = str(_get_row_value(row, columns, "Mobile No", "Phone", "Phone Number", default="")).strip()
        address = str(_get_row_value(row, columns, "Location", "Address", default="")).strip()

        phone   = None if phone   == "nan" else phone
        address = None if address == "nan" else address

        existing_by_phone = None
        existing_by_name = None

        if phone:
            _r = await db.execute(select(Customer).where(Customer.phone == phone))
            existing_by_phone = _r.scalar_one_or_none()
        if name:
            _r = await db.execute(select(Customer).where(Customer.name == name))
            existing_by_name = _r.scalar_one_or_none()

        exists = existing_by_phone or (existing_by_name if not phone else None)

        if exists:
            skipped += 1
            continue

        db.add(Customer(name=name, phone=phone, address=address))
        added += 1

    await db.commit()
    return {"added": added, "skipped": skipped}


async def import_products(products_path: str, soh_path: str, db: AsyncSession) -> dict:
    df_products = pd.read_excel(products_path)
    df_soh      = pd.read_excel(soh_path)

    stock_lookup = {}
    for _, row in df_soh.iterrows():
        sku   = str(row.get("SKU", "")).strip()
        stock = row.get("Stock", 0)
        if sku:
            stock_lookup[sku] = float(stock) if str(stock) != "nan" else 0

    added   = 0
    skipped = 0

    for _, row in df_products.iterrows():
        sku  = str(row.get("SKU", "")).strip()
        name = str(row.get("Item", "")).strip()

        if not sku or not name or sku == "nan" or name == "nan":
            skipped += 1
            continue

        _r = await db.execute(select(Product).where(Product.sku == sku))
        if _r.scalar_one_or_none():
            skipped += 1
            continue

        cost  = row.get("Unit Cost",   0)
        price = row.get("Sales price", 0)
        unit  = str(row.get("UOM",   "pcs")).strip()
        stock = stock_lookup.get(sku, 0)

        cost  = float(cost)  if str(cost)  != "nan" else 0.0
        price = float(price) if str(price) != "nan" else 0.0

        db.add(Product(
            sku=sku, name=name, price=price, cost=cost,
            stock=stock, min_stock=5, unit=unit, is_active=True,
        ))
        added += 1

    await db.commit()
    return {"added": added, "skipped": skipped}
