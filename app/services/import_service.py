import pandas as pd
from sqlalchemy.orm import Session
from app.models.customer import Customer
from app.models.product import Product


def import_customers(filepath: str, db: Session) -> dict:
    df = pd.read_excel(filepath)

    added   = 0
    skipped = 0

    for _, row in df.iterrows():
        customer_id = str(row.get("ID", "")).strip()
        name        = str(row.get("Vendor  ", "")).strip()

        if not name or name == "nan":
            skipped += 1
            continue

        # Skip if already exists by name
        exists = db.query(Customer).filter(Customer.name == name).first()
        if exists:
            skipped += 1
            continue

        phone    = str(row.get("Mobile No", "")).strip()
        address  = str(row.get("Location", "")).strip()

        phone   = None if phone   == "nan" else phone
        address = None if address == "nan" else address

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