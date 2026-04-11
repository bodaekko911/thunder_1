import pandas as pd
from sqlalchemy.orm import Session
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


def import_customers(filepath: str, db: Session) -> dict:
    df = pd.read_excel(filepath)
    columns = _build_column_lookup(df)

    added   = 0
    skipped = 0

    for _, row in df.iterrows():
        customer_id = str(_get_row_value(row, columns, "ID", default="")).strip()
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
            existing_by_phone = db.query(Customer).filter(Customer.phone == phone).first()
        if name:
            existing_by_name = db.query(Customer).filter(Customer.name == name).first()

        exists = existing_by_phone or (existing_by_name if not phone else None)

        if exists:
            skipped += 1
            continue

        customer = Customer(
            name    = name,
            phone   = phone,
            address = address,
        )
        db.add(customer)
        added += 1

    db.commit()
    return {"added": added, "skipped": skipped}


def import_products(products_path: str, soh_path: str, db: Session) -> dict:
    df_products = pd.read_excel(products_path)
    df_soh      = pd.read_excel(soh_path)

    # Build stock lookup from SOH file
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

        # Skip if already exists
        exists = db.query(Product).filter(Product.sku == sku).first()
        if exists:
            skipped += 1
            continue

        cost  = row.get("Unit Cost",   0)
        price = row.get("Sales price", 0)
        unit  = str(row.get("UOM",   "pcs")).strip()
        stock = stock_lookup.get(sku, 0)

        cost  = float(cost)  if str(cost)  != "nan" else 0.0
        price = float(price) if str(price) != "nan" else 0.0

        product = Product(
            sku       = sku,
            name      = name,
            price     = price,
            cost      = cost,
            stock     = stock,
            min_stock = 5,
            unit      = unit,
            is_active = True,
        )
        db.add(product)
        added += 1

    db.commit()
    return {"added": added, "skipped": skipped}
