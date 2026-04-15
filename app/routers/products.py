from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, or_, select
from typing import Optional, List
from pydantic import BaseModel

from app.database import get_async_session
from app.core.permissions import get_current_user, require_permission
from app.models.inventory import StockMove
from app.models.product import Product
from app.models.supplier import Supplier
from app.models.user import User
from app.core.log import record
from app.schemas.product import ProductCreate, ProductUpdate
from app.services.location_inventory_service import (
    ensure_default_stock_location,
    get_or_create_location_stock,
    quantize_qty,
    sync_product_stock_to_default_location,
)

router = APIRouter(
    prefix="/products",
    tags=["Products"],
    dependencies=[Depends(require_permission("page_products"))],
)

ITEM_TYPE_OPTIONS = [
    ("finished", "Finished Product"),
    ("raw", "Raw Material"),
    ("fresh", "Fresh"),
    ("packing", "Packing"),
    ("ingredient", "Ingredient"),
]

ITEM_TYPE_LABELS = {value: label for value, label in ITEM_TYPE_OPTIONS}


# ── API ────────────────────────────────────────────────
@router.get("/api/next-sku")
async def next_sku(db: AsyncSession = Depends(get_async_session)):
    """Return the next available numeric SKU based on existing ones."""
    result = await db.execute(select(Product))
    products = result.scalars().all()
    numeric_skus = []
    for p in products:
        try:
            numeric_skus.append(int(str(p.sku).strip()))
        except (ValueError, TypeError):
            pass
    next_num = max(numeric_skus) + 1 if numeric_skus else 10001
    return {"sku": str(next_num)}


@router.get("/api/categories")
async def get_categories(db: AsyncSession = Depends(get_async_session)):
    """Return all distinct categories from products."""
    result = await db.execute(
        select(Product.category)
        .where(
            Product.category != None,
            Product.category != "",
            or_(Product.is_active.is_(True), Product.is_active.is_(None)),
        )
        .distinct()
        .order_by(Product.category)
    )
    rows = result.all()
    return [r[0] for r in rows if r[0]]


@router.get("/api/list")
async def get_products(
    q:         str  = "",
    low_stock: bool = False,
    category:  str  = "",
    item_type: str  = "",
    skip:      int  = 0,
    limit:     int  = 50,
    db: AsyncSession = Depends(get_async_session),
):
    conditions = [or_(Product.is_active.is_(True), Product.is_active.is_(None))]
    low_stock_threshold = func.coalesce(Product.reorder_level, Product.min_stock)
    if q:
        conditions.append(
            Product.name.ilike(f"%{q}%") | Product.sku.ilike(f"%{q}%")
        )
    if low_stock:
        conditions.append(Product.stock <= low_stock_threshold)
    if category:
        conditions.append(Product.category == category)
    if item_type:
        conditions.append(Product.item_type == item_type)

    cnt_result = await db.execute(
        select(func.count()).select_from(Product).where(*conditions)
    )
    total = cnt_result.scalar()

    result = await db.execute(
        select(Product).where(*conditions).order_by(Product.name).offset(skip).limit(limit)
    )
    items = result.scalars().all()
    return {
        "total": total,
        "items": [
            {
                "id":        p.id,
                "sku":       p.sku,
                "name":      p.name,
                "price":     float(p.price or 0),
                "cost":      float(p.cost or 0),
                "stock":     float(p.stock or 0),
                "min_stock": float(p.min_stock or 0),
                "reorder_level": float(p.reorder_level) if p.reorder_level is not None else None,
                "reorder_qty": float(p.reorder_qty) if p.reorder_qty is not None else None,
                "preferred_supplier_id": p.preferred_supplier_id,
                "unit":      p.unit,
                "category":  p.category or "—",
                "item_type": p.item_type or "finished",
                "is_active": True if p.is_active is None else p.is_active,
                "low":       float(p.stock or 0) <= float(
                    p.reorder_level if p.reorder_level is not None else (p.min_stock or 0)
                ),
            }
            for p in items
        ],
    }


@router.post("/api/add")
async def add_product(data: ProductCreate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    sku_result = await db.execute(select(Product).where(Product.sku == data.sku))
    if sku_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="SKU already exists")
    if data.preferred_supplier_id is not None:
        supplier_result = await db.execute(select(Supplier).where(Supplier.id == data.preferred_supplier_id))
        if supplier_result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Preferred supplier not found")
    initial_stock = quantize_qty(data.stock)
    p = Product(
        sku=data.sku, name=data.name, price=data.price,
        cost=data.cost, stock=initial_stock, min_stock=data.min_stock,
        reorder_level=data.reorder_level, reorder_qty=data.reorder_qty,
        preferred_supplier_id=data.preferred_supplier_id,
        unit=data.unit, is_active=True,
    )
    if hasattr(p, 'category'):  p.category  = data.category
    if hasattr(p, 'item_type'): p.item_type = data.item_type
    db.add(p)
    await db.flush()
    if initial_stock > 0:
        location = await ensure_default_stock_location(db)
        location_stock = await get_or_create_location_stock(
            db,
            product_id=p.id,
            location_id=location.id,
        )
        location_stock.qty = initial_stock
        db.add(
            StockMove(
                product_id=p.id,
                type="adjust",
                qty=initial_stock,
                qty_before=Decimal("0.000"),
                qty_after=initial_stock,
                ref_type="product_create",
                ref_id=p.id,
                note=f"Initial stock created with product at {location.name}",
                user_id=current_user.id,
            )
        )
    record(db, "Products", "add_product",
           f"Added product: [{p.sku}] {p.name} — price: {float(p.price):.2f}",
           ref_type="product", ref_id=p.id)
    await db.commit()
    await db.refresh(p)
    return {"id": p.id, "sku": p.sku, "name": p.name}


@router.put("/api/edit/{product_id}", dependencies=[Depends(require_permission("action_products_edit"))])
async def edit_product(product_id: int, data: ProductUpdate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    result = await db.execute(select(Product).where(Product.id == product_id))
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")
    if data.preferred_supplier_id is not None:
        supplier_result = await db.execute(select(Supplier).where(Supplier.id == data.preferred_supplier_id))
        if supplier_result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Preferred supplier not found")
    changes = data.model_dump(exclude_unset=True)
    stock_value = changes.pop("stock", None)
    stock_before = quantize_qty(p.stock)
    for k, v in changes.items():
        if hasattr(p, k):
            setattr(p, k, v)
    if stock_value is not None:
        stock_after = quantize_qty(stock_value)
        stock_delta = quantize_qty(stock_after - stock_before)
        if stock_delta != 0:
            location, location_stock = await sync_product_stock_to_default_location(db, product=p)
            location_stock.qty = stock_after
            p.stock = stock_after
            db.add(
                StockMove(
                    product_id=p.id,
                    type="adjust",
                    qty=stock_delta,
                    qty_before=stock_before,
                    qty_after=stock_after,
                    ref_type="product_edit",
                    ref_id=p.id,
                    note=f"Stock updated from product edit at {location.name}",
                    user_id=current_user.id,
                )
            )
        else:
            p.stock = stock_after
    record(db, "Products", "edit_product",
           f"Edited product: [{p.sku}] {p.name}",
           ref_type="product", ref_id=product_id)
    await db.commit()
    await db.refresh(p)
    return {"ok": True}


@router.delete("/api/delete/{product_id}", dependencies=[Depends(require_permission("action_products_delete"))])
async def delete_product(product_id: int, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    result = await db.execute(select(Product).where(Product.id == product_id))
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")
    p.is_active = False
    record(db, "Products", "deactivate_product",
           f"Deactivated product: [{p.sku}] {p.name}",
           ref_type="product", ref_id=product_id)
    await db.commit()
    return {"ok": True}


# ── UI ─────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
def products_ui():
    return """<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Products — Thunder ERP</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root{
    --bg:#060810;--surface:#0a0d18;--card:#0f1424;--card2:#151c30;
    --border:rgba(255,255,255,0.06);--border2:rgba(255,255,255,0.11);
    --green:#00ff9d;--blue:#4d9fff;--purple:#a855f7;--orange:#fb923c;
    --danger:#ff4d6d;--warn:#ffb547;--teal:#2dd4bf;--lime:#84cc16;
    --text:#f0f4ff;--sub:#8899bb;--muted:#445066;
    --sans:'Outfit',sans-serif;--mono:'JetBrains Mono',monospace;--r:12px;
}
body.light{
    --bg:#f4f5ef;--surface:#f1f3eb;--card:#eceee6;--card2:#e4e6de;
    --border:rgba(0,0,0,0.08);--border2:rgba(0,0,0,0.14);
    --green:#0f8a43;
    --text:#1a1e14;--sub:#4a5040;--muted:#7b816f;
}
body.light nav{background:rgba(244,245,239,.92);}
body.light .nav-link:hover{background:rgba(0,0,0,.05);}
body.light tr:hover td{background:rgba(0,0,0,.03);}
.mode-btn{display:flex;align-items:center;justify-content:center;width:36px;height:36px;border-radius:10px;border:1px solid var(--border);background:var(--card);color:var(--sub);font-size:16px;cursor:pointer;transition:all .2s;font-family:var(--sans);}
.mode-btn:hover{border-color:var(--border2);transform:scale(1.06);}
.topbar-right{display:flex;align-items:center;gap:12px;}
.user-pill{display:flex;align-items:center;gap:10px;background:var(--card);border:1px solid var(--border);border-radius:40px;padding:7px 16px 7px 10px;}
.user-avatar{width:28px;height:28px;background:linear-gradient(135deg,#7ecb6f,#d4a256);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;color:#0a0c08;}
.user-name{font-size:13px;font-weight:500;color:var(--sub);}
.logout-btn{background:transparent;border:1px solid var(--border);color:var(--muted);font-family:var(--sans);font-size:12px;font-weight:500;padding:8px 16px;border-radius:8px;cursor:pointer;transition:all .2s;letter-spacing:.3px;}
.logout-btn:hover{border-color:#c97a7a;color:#c97a7a;}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
body{font-family:var(--sans);background:var(--bg);color:var(--text);min-height:100vh;font-size:14px;}
nav{position:sticky;top:0;z-index:100;display:flex;align-items:center;gap:8px;padding:0 24px;height:58px;background:rgba(10,13,24,.92);backdrop-filter:blur(20px);border-bottom:1px solid var(--border);flex-wrap:wrap;}
.logo{font-size:17px;font-weight:900;background:linear-gradient(135deg,var(--green),var(--blue));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;margin-right:10px;text-decoration:none;display:flex;align-items:center;gap:8px;}
.nav-link{padding:7px 12px;border-radius:8px;color:var(--sub);font-size:12px;font-weight:600;text-decoration:none;transition:all .2s;white-space:nowrap;}
.nav-link:hover{background:rgba(255,255,255,.05);color:var(--text);}
.nav-link.active{background:rgba(77,159,255,.1);color:var(--blue);}
.nav-spacer{flex:1;}
.content{max-width:1300px;margin:0 auto;padding:28px 24px;display:flex;flex-direction:column;gap:20px;}
.page-title{font-size:24px;font-weight:800;letter-spacing:-.5px;}
.page-sub{color:var(--muted);font-size:13px;margin-top:3px;}
.tabs{display:flex;gap:4px;background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:4px;}
.tab{padding:8px 18px;border-radius:9px;font-size:13px;font-weight:700;cursor:pointer;border:none;background:transparent;color:var(--muted);transition:all .2s;font-family:var(--sans);}
.tab.active{background:var(--card2);color:var(--text);}
.toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;}
.search-box{display:flex;align-items:center;gap:9px;background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:0 14px;flex:1;min-width:200px;}
.search-box input{background:transparent;border:none;outline:none;color:var(--text);font-family:var(--sans);font-size:14px;padding:11px 0;width:100%;}
.search-box input::placeholder{color:var(--muted);}
.filter-sel{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:10px 13px;color:var(--text);font-family:var(--sans);font-size:13px;outline:none;}
.filter-sel:focus{border-color:var(--blue);}
.btn{display:flex;align-items:center;gap:7px;padding:10px 16px;border-radius:var(--r);font-family:var(--sans);font-size:13px;font-weight:700;cursor:pointer;border:none;transition:all .2s;white-space:nowrap;}
.btn-blue{background:linear-gradient(135deg,var(--blue),var(--purple));color:white;}
.btn-blue:hover{filter:brightness(1.1);transform:translateY(-1px);}
.btn-lime{background:linear-gradient(135deg,var(--lime),var(--green));color:#0a1a00;}
.btn-lime:hover{filter:brightness(1.1);transform:translateY(-1px);}
.table-wrap{background:var(--card);border:1px solid var(--border);border-radius:var(--r);overflow:hidden;}
table{width:100%;border-collapse:collapse;}
thead{background:var(--card2);}
th{text-align:left;font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);padding:12px 16px;}
td{padding:12px 16px;border-top:1px solid var(--border);color:var(--sub);font-size:13px;}
td.name{color:var(--text);font-weight:600;}
td.sku{font-family:var(--mono);font-size:12px;color:var(--muted);}
td.mono{font-family:var(--mono);}
tr:hover td{background:rgba(255,255,255,.02);}
.action-btn{background:transparent;border:1px solid var(--border2);color:var(--sub);font-size:12px;font-weight:600;padding:5px 10px;border-radius:7px;cursor:pointer;transition:all .15s;font-family:var(--sans);}
.action-btn:hover{border-color:var(--blue);color:var(--blue);}
.action-btn.danger:hover{border-color:var(--danger);color:var(--danger);}
.badge{display:inline-flex;padding:2px 8px;border-radius:20px;font-size:11px;font-weight:700;}
.badge-raw     {background:rgba(251,146,60,.1);color:var(--orange);}
.badge-finished{background:rgba(0,255,157,.1); color:var(--green);}
.badge-fresh   {background:rgba(45,212,191,.1);color:var(--teal);}
.badge-packing {background:rgba(77,159,255,.1);color:var(--blue);}
.badge-ingredient{background:rgba(255,181,71,.12);color:var(--warn);}
.badge-low     {background:rgba(255,181,71,.1);color:var(--warn);}
.pagination{display:flex;align-items:center;justify-content:space-between;padding:14px 16px;border-top:1px solid var(--border);font-size:13px;color:var(--muted);}
.page-btns{display:flex;gap:6px;}
.page-btn{background:var(--card2);border:1px solid var(--border2);color:var(--sub);font-family:var(--sans);font-size:12px;padding:6px 12px;border-radius:7px;cursor:pointer;transition:all .15s;}
.page-btn:hover{border-color:var(--blue);color:var(--blue);}
.page-btn:disabled{opacity:.3;cursor:not-allowed;}
/* MODAL */
.modal-bg{position:fixed;inset:0;z-index:500;background:rgba(0,0,0,.75);backdrop-filter:blur(4px);display:none;align-items:center;justify-content:center;}
.modal-bg.open{display:flex;}
.modal{background:var(--card);border:1px solid var(--border2);border-radius:16px;padding:28px;width:600px;max-width:95vw;max-height:90vh;overflow-y:auto;animation:modalIn .2s ease;}
@keyframes modalIn{from{opacity:0;transform:scale(.95)}to{opacity:1;transform:scale(1)}}
.modal-title{font-size:18px;font-weight:800;margin-bottom:18px;}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px;}
.fld{display:flex;flex-direction:column;gap:6px;}
.fld.span2{grid-column:span 2;}
.fld label{font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);}
.fld input,.fld select{background:var(--card2);border:1px solid var(--border2);border-radius:10px;padding:10px 12px;color:var(--text);font-family:var(--sans);font-size:14px;outline:none;transition:border-color .2s;width:100%;}
.fld input:focus,.fld select:focus{border-color:rgba(77,159,255,.4);}
.fld input:disabled{opacity:.5;cursor:not-allowed;}
.sku-row{display:flex;gap:8px;align-items:flex-end;}
.sku-row input{flex:1;}
.sku-auto-btn{padding:10px 14px;border-radius:10px;border:1px solid var(--border2);background:var(--card2);color:var(--lime);font-family:var(--sans);font-size:12px;font-weight:700;cursor:pointer;white-space:nowrap;transition:all .2s;}
.sku-auto-btn:hover{border-color:var(--lime);background:rgba(132,204,22,.08);}
.modal-actions{display:flex;gap:10px;justify-content:flex-end;}
.btn-cancel{background:transparent;border:1px solid var(--border2);color:var(--sub);padding:10px 18px;border-radius:var(--r);font-family:var(--sans);font-size:13px;font-weight:700;cursor:pointer;}
.btn-cancel:hover{border-color:var(--danger);color:var(--danger);}
/* CATEGORIES */
.cat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;}
.cat-card{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:16px 18px;display:flex;align-items:center;justify-content:space-between;gap:10px;}
.cat-name{font-weight:700;font-size:14px;}
.cat-count{font-size:12px;color:var(--muted);}
.new-cat-row{display:flex;gap:10px;align-items:center;}
.new-cat-row input{flex:1;background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:10px 13px;color:var(--text);font-family:var(--sans);font-size:14px;outline:none;}
.new-cat-row input:focus{border-color:var(--lime);}
/* TOAST */
.toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%) translateY(16px);background:var(--card2);border:1px solid var(--border2);border-radius:var(--r);padding:12px 20px;font-size:13px;font-weight:600;color:var(--text);box-shadow:0 20px 50px rgba(0,0,0,.5);opacity:0;pointer-events:none;transition:opacity .25s,transform .25s;z-index:999;}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0);}
::-webkit-scrollbar{width:4px;}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:4px;}
</style>
</head>
<body>
<nav>
    <a href="/home" class="logo">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
            <polygon points="13,2 4,14 11,14 11,22 20,10 13,10" fill="#f59e0b"/>
        </svg>
        Thunder ERP
    </a>
    <a href="/dashboard"  class="nav-link">Dashboard</a>
    <a href="/pos"        class="nav-link">POS</a>
    <a href="/products/"  class="nav-link active">Products</a>
    <a href="/inventory/" class="nav-link">Inventory</a>
    <span class="nav-spacer"></span>
    <div class="topbar-right">
        <button class="mode-btn" id="mode-btn" onclick="toggleMode()" title="Toggle color mode">??</button>
        <div class="user-pill">
            <div class="user-avatar" id="user-avatar">A</div>
            <span class="user-name" id="user-name">Admin</span>
        </div>
        <button class="logout-btn" onclick="logout()">Sign out</button>
    </div>
</nav>

<div class="content">
    <div>
        <div class="page-title">Products</div>
        <div class="page-sub">Manage your product catalog, categories and item types</div>
    </div>

    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;">
        <div class="tabs">
            <button class="tab active" id="tab-products"   onclick="switchTab('products')">Products</button>
            <button class="tab"        id="tab-categories" onclick="switchTab('categories')">Categories</button>
        </div>
        <div style="display:flex;gap:10px;">
            <button class="btn btn-blue" id="btn-add-product" onclick="openAddModal()">+ Add Product</button>
            <button class="btn btn-lime" id="btn-add-cat"     onclick="openAddCatModal()" style="display:none">+ Add Category</button>
        </div>
    </div>

    <!-- PRODUCTS TAB -->
    <div id="section-products">
        <div class="toolbar">
            <div class="search-box">
                <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
                <input id="search" placeholder="Search by name or SKU…" oninput="onSearch()">
            </div>
            <select class="filter-sel" id="cat-filter" onchange="onSearch()">
                <option value="">All Categories</option>
            </select>
            <select class="filter-sel" id="type-filter" onchange="onSearch()">
                <option value="">All Types</option>
                <option value="raw">Raw Material</option>
                <option value="finished">Finished Product</option>
                <option value="fresh">Fresh</option>
                <option value="packing">Packing</option>
                <option value="ingredient">Ingredient</option>
            </select>
        </div>
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>SKU</th><th>Name</th><th>Category</th><th>Type</th>
                        <th>Price</th><th>Cost</th><th>Stock</th><th>Unit</th><th>Actions</th>
                    </tr>
                </thead>
                <tbody id="table-body">
                    <tr><td colspan="9" style="text-align:center;color:var(--muted);padding:40px">Loading…</td></tr>
                </tbody>
            </table>
            <div class="pagination">
                <span id="page-info">—</span>
                <div class="page-btns">
                    <button class="page-btn" id="prev-btn" onclick="prevPage()">← Prev</button>
                    <button class="page-btn" id="next-btn" onclick="nextPage()">Next →</button>
                </div>
            </div>
        </div>
    </div>

    <!-- CATEGORIES TAB -->
    <div id="section-categories" style="display:none">
        <div id="cat-grid" class="cat-grid">
            <div style="color:var(--muted);padding:20px">Loading…</div>
        </div>
    </div>
</div>

<!-- PRODUCT MODAL -->
<div class="modal-bg" id="modal">
    <div class="modal">
        <div class="modal-title" id="modal-title">Add Product</div>
        <div class="form-grid">

            <!-- SKU with Auto-generate button -->
            <div class="fld">
                <label>SKU *</label>
                <div class="sku-row">
                    <input id="f-sku" placeholder="e.g. 10168">
                    <button class="sku-auto-btn" onclick="autoSKU()">⚡ Auto</button>
                </div>
            </div>

            <!-- Unit -->
            <div class="fld">
                <label>Unit</label>
                <select id="f-unit">
                    <option value="gram">gram</option>
                    <option value="kg">kg</option>
                    <option value="pcs">pcs</option>
                    <option value="ltr">ltr</option>
                    <option value="ml">ml</option>
                    <option value="box">box</option>
                    <option value="pack">pack</option>
                </select>
            </div>

            <!-- Name -->
            <div class="fld span2">
                <label>Name *</label>
                <input id="f-name" placeholder="e.g. Moringa Powder (50g) HOF">
            </div>

            <!-- Category -->
            <div class="fld">
                <label>Category</label>
                <select id="f-category">
                    <option value="">— No Category —</option>
                </select>
            </div>

            <!-- Item Type -->
            <div class="fld">
                <label>Item Type</label>
                <select id="f-item-type">
                    <option value="finished">Finished Product</option>
                    <option value="raw">Raw Material</option>
                    <option value="fresh">Fresh</option>
                    <option value="packing">Packing</option>
                    <option value="ingredient">Ingredient</option>
                </select>
            </div>

            <!-- Prices -->
            <div class="fld">
                <label>Sale Price *</label>
                <input id="f-price" type="number" placeholder="0.00" step="0.01">
            </div>
            <div class="fld">
                <label>Cost Price</label>
                <input id="f-cost" type="number" placeholder="0.00" step="0.01">
            </div>

            <!-- Stock -->
            <div class="fld">
                <label>Initial Stock</label>
                <input id="f-stock" type="number" placeholder="0">
            </div>
            <div class="fld">
                <label>Min Stock Alert</label>
                <input id="f-min-stock" type="number" placeholder="5">
            </div>
        </div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeModal()">Cancel</button>
            <button class="btn btn-blue" onclick="saveProduct()">Save Product</button>
        </div>
    </div>
</div>

<!-- ADD CATEGORY MODAL -->
<div class="modal-bg" id="cat-modal">
    <div class="modal" style="width:420px">
        <div class="modal-title">Add Category</div>
        <div class="fld" style="margin-bottom:16px">
            <label>Category Name *</label>
            <input id="cat-name-input" placeholder="e.g. Herbs & Spices">
        </div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="document.getElementById('cat-modal').classList.remove('open')">Cancel</button>
            <button class="btn btn-lime" onclick="saveCategory()">Add Category</button>
        </div>
    </div>
</div>

<div class="toast" id="toast"></div>

<script>
  // Auth guard: redirect to login if the readable session cookie is absent
  function _hasAuthCookie() {
      return document.cookie.split(";").some(c => c.trim().startsWith("logged_in="));
  }
  if (!_hasAuthCookie()) { window.location.href = "/"; }

  function setModeButton(isLight){
    const btn = document.getElementById("mode-btn");
    if(btn) btn.innerText = isLight ? "☀️" : "🌙";
}
function toggleMode(){
    const isLight = document.body.classList.toggle("light");
    localStorage.setItem("colorMode", isLight ? "light" : "dark");
    setModeButton(isLight);
}
function initializeColorMode(){
    const isLight = localStorage.getItem("colorMode") === "light";
    document.body.classList.toggle("light", isLight);
    setModeButton(isLight);
}
async function initUser() {
    try {
        const r = await fetch("/auth/me");
        if (!r.ok) { window.location.href = "/"; return; }
        const u = await r.json();
        const nameEl = document.getElementById("user-name");
        const avatarEl = document.getElementById("user-avatar");
        if (nameEl) nameEl.innerText = u.name;
        if (avatarEl) avatarEl.innerText = u.name.charAt(0).toUpperCase();
        return u;
    } catch(e) { window.location.href = "/"; }
}
async function logout(){
    await fetch("/auth/logout", { method: "POST" });
    window.location.href = "/";
}
  function hasPermission(permission, u){
      const role = u ? (u.role || "") : "";
      const perms = new Set(u ? (u.permissions || []) : []);
      return role === "admin" || perms.has(permission);
  }
  function applyProductActionPermissions(u){
      if(hasPermission("action_products_edit", u) && hasPermission("action_products_delete", u)) return;
      document.querySelectorAll("#table-body tr td:last-child").forEach(cell => {
          cell.querySelectorAll("button").forEach(btn => {
              const label = btn.innerText.trim().toLowerCase();
              if(label === "edit" && !hasPermission("action_products_edit", u)) btn.remove();
              if(label === "delete" && !hasPermission("action_products_delete", u)) btn.remove();
          });
      });
  }
  initializeColorMode();
  let currentUser = null;
  initUser().then(u => { currentUser = u; if(u) applyProductActionPermissions(u); });
  let products    = [];
let categories  = [];
let editingId   = null;
let page        = 0;
let pageSize    = 50;
let totalItems  = 0;
let toastTimer  = null;
const ITEM_TYPE_LABELS = {
    finished: "Finished Product",
    raw: "Raw Material",
    fresh: "Fresh",
    packing: "Packing",
    ingredient: "Ingredient",
};

function escapeJsString(value){
    const text = String(value == null ? "" : value);
    const backslash = String.fromCharCode(92);
    const quote = String.fromCharCode(39);
    const carriageReturn = String.fromCharCode(13);
    const newline = String.fromCharCode(10);
    return text
        .split(backslash).join(backslash + backslash)
        .split(quote).join(backslash + quote)
        .split(carriageReturn).join(backslash + "r")
        .split(newline).join(backslash + "n");
}

async function init(){
    await loadCategories();
    await loadProducts();
}

/* ── TABS ── */
function switchTab(tab){
    document.getElementById("section-products").style.display   = tab==="products"  ?"":"none";
    document.getElementById("section-categories").style.display = tab==="categories"?"":"none";
    document.getElementById("tab-products").classList.toggle("active",   tab==="products");
    document.getElementById("tab-categories").classList.toggle("active", tab==="categories");
    document.getElementById("btn-add-product").style.display = tab==="products"  ?"":"none";
    document.getElementById("btn-add-cat").style.display     = tab==="categories"?"":"none";
    if(tab==="categories") renderCategories();
}

/* ── CATEGORIES ── */
async function loadCategories(){
    categories = await (await fetch("/products/api/categories")).json();
    // Fill category filter dropdown
    let sel = document.getElementById("cat-filter");
    sel.innerHTML = '<option value="">All Categories</option>' +
        categories.map(c=>`<option value="${c}">${c}</option>`).join("");
    // Fill modal category dropdown
    let fcat = document.getElementById("f-category");
    fcat.innerHTML = '<option value="">— No Category —</option>' +
        categories.map(c=>`<option value="${c}">${c}</option>`).join("");
}

async function renderCategories(){
    // Get product counts per category
    let data = await (await fetch("/products/api/list?limit=1000")).json();
    let counts = {};
    data.items.forEach(p=>{
        let c = p.category && p.category!=="—" ? p.category : "Uncategorized";
        counts[c] = (counts[c]||0)+1;
    });
    let cats = categories.length ? categories : Object.keys(counts).filter(c=>c!=="Uncategorized");
    if(!cats.length){
        document.getElementById("cat-grid").innerHTML =
            `<div style="color:var(--muted);font-size:13px;padding:20px 0">No categories yet. Click <b>+ Add Category</b>.</div>`;
        return;
    }
    document.getElementById("cat-grid").innerHTML = cats.map(c=>`
        <div class="cat-card">
            <div>
                <div class="cat-name">${c}</div>
                <div class="cat-count">${counts[c]||0} products</div>
            </div>
            <div style="display:flex;gap:6px">
                <button class="action-btn" onclick="filterByCategory('${c.replace(/'/g,"\\'")}')">View</button>
                <button class="action-btn danger" onclick="deleteCategory('${c.replace(/'/g,"\\'")}')">Remove</button>
            </div>
        </div>`).join("");
}

function filterByCategory(cat){
    switchTab("products");
    document.getElementById("cat-filter").value = cat;
    page = 0;
    loadProducts();
}

function openAddCatModal(){
    document.getElementById("cat-name-input").value = "";
    document.getElementById("cat-modal").classList.add("open");
}

async function saveCategory(){
    let name = document.getElementById("cat-name-input").value.trim();
    if(!name){ showToast("Enter a category name"); return; }
    if(categories.includes(name)){ showToast("Category already exists"); return; }
    // Category is created implicitly by assigning to a product
    // Just add to local list and dropdowns for now
    categories.push(name);
    categories.sort();
    await loadCategories();
    document.getElementById("cat-modal").classList.remove("open");
    showToast(`Category "${name}" added ✓`);
    renderCategories();
}

async function deleteCategory(cat){
    if(!confirm(`Remove category "${cat}"? Products will keep the category name but it will be removed from the dropdown.`)) return;
    // Remove from local list
    categories = categories.filter(c=>c!==cat);
    await loadCategories();
    showToast(`Category "${cat}" removed`);
    renderCategories();
}

/* ── PRODUCTS ── */
async function loadProducts(){
    let q    = document.getElementById("search").value.trim();
    let cat  = document.getElementById("cat-filter").value;
    let type = document.getElementById("type-filter").value;
    let url  = `/products/api/list?skip=${page*pageSize}&limit=${pageSize}`;
    if(q)    url += `&q=${encodeURIComponent(q)}`;
    if(cat)  url += `&category=${encodeURIComponent(cat)}`;
    if(type) url += `&item_type=${type}`;
    let data = await (await fetch(url)).json();
    products   = data.items;
    totalItems = data.total;

    document.getElementById("page-info").innerText =
        totalItems===0 ? "No products" :
        `${page*pageSize+1}–${Math.min((page+1)*pageSize,totalItems)} of ${totalItems}`;
    document.getElementById("prev-btn").disabled = page===0;
    document.getElementById("next-btn").disabled = (page+1)*pageSize>=totalItems;

    if(!products.length){
        document.getElementById("table-body").innerHTML =
            `<tr><td colspan="9" style="text-align:center;color:var(--muted);padding:60px">No products found.</td></tr>`;
        return;
    }

    document.getElementById("table-body").innerHTML = products.map(p=>`<tr>
        <td class="sku">${p.sku}</td>
        <td class="name">${p.name}</td>
        <td style="font-size:12px;color:var(--sub)">${p.category}</td>
        <td><span class="badge badge-${p.item_type}">${getItemTypeLabel(p.item_type)}</span></td>
        <td class="mono">${p.price.toFixed(2)}</td>
        <td class="mono" style="color:var(--muted)">${p.cost>0?p.cost.toFixed(2):"—"}</td>
        <td class="mono" style="color:${p.stock<=0?"var(--danger)":p.low?"var(--warn)":"var(--text)"};font-weight:700">${p.stock.toFixed(0)}</td>
        <td style="font-size:12px;color:var(--muted)">${p.unit}</td>
        <td style="display:flex;gap:6px">
            <button class="action-btn" onclick="openEditModal(${p.id},'${escapeJsString(p.sku)}','${escapeJsString(p.name)}',${p.price},${p.cost},${p.stock},${p.min_stock},'${escapeJsString(p.unit)}','${escapeJsString(p.category==="—"?"":p.category)}','${escapeJsString(p.item_type)}')">Edit</button>
            <button class="action-btn danger" onclick="deleteProduct(${p.id},'${escapeJsString(p.name)}')">Delete</button>
        </td>
    </tr>`).join("");
    applyProductActionPermissions(currentUser);
}

let searchTimer = null;
function onSearch(){ clearTimeout(searchTimer); searchTimer=setTimeout(()=>{page=0;loadProducts();},300); }
function prevPage(){ if(page>0){ page--; loadProducts(); } }
function nextPage(){ if((page+1)*pageSize<totalItems){ page++; loadProducts(); } }
function getItemTypeLabel(itemType){
    return ITEM_TYPE_LABELS[itemType] || itemType || "Finished Product";
}

/* ── SKU AUTO-GENERATE ── */
async function autoSKU(){
    let data = await (await fetch("/products/api/next-sku")).json();
    document.getElementById("f-sku").value = data.sku;
    showToast(`SKU set to ${data.sku}`);
}

/* ── ADD / EDIT MODAL ── */
async function openAddModal(){
    editingId = null;
    document.getElementById("modal-title").innerText = "Add Product";
    document.getElementById("f-sku").disabled  = false;
    document.getElementById("f-sku").value     = "";
    document.getElementById("f-name").value    = "";
    document.getElementById("f-price").value   = "";
    document.getElementById("f-cost").value    = "";
    document.getElementById("f-stock").value   = "0";
    document.getElementById("f-min-stock").value = "5";
    document.getElementById("f-unit").value    = "gram";
    document.getElementById("f-category").value = "";
    document.getElementById("f-item-type").value = "finished";
    // Auto-fill SKU
    let skuData = await (await fetch("/products/api/next-sku")).json();
    document.getElementById("f-sku").value = skuData.sku;
    document.getElementById("modal").classList.add("open");
}

function openEditModal(id,sku,name,price,cost,stock,min_stock,unit,category,item_type){
    editingId = id;
    document.getElementById("modal-title").innerText   = "Edit Product";
    document.getElementById("f-sku").value             = sku;
    document.getElementById("f-sku").disabled          = true;
    document.getElementById("f-name").value            = name;
    document.getElementById("f-price").value           = price;
    document.getElementById("f-cost").value            = cost;
    document.getElementById("f-stock").value           = stock;
    document.getElementById("f-min-stock").value       = min_stock;
    document.getElementById("f-unit").value            = unit;
    document.getElementById("f-category").value        = category||"";
    document.getElementById("f-item-type").value       = item_type||"finished";
    document.getElementById("modal").classList.add("open");
}

function closeModal(){
    document.getElementById("modal").classList.remove("open");
    editingId = null;
}

async function saveProduct(){
    let sku      = document.getElementById("f-sku").value.trim();
    let name     = document.getElementById("f-name").value.trim();
    let price    = parseFloat(document.getElementById("f-price").value)||0;
    let cost     = parseFloat(document.getElementById("f-cost").value)||0;
    let stock    = parseFloat(document.getElementById("f-stock").value)||0;
    let minStock = parseFloat(document.getElementById("f-min-stock").value)||5;
    let unit     = document.getElementById("f-unit").value;
    let category = document.getElementById("f-category").value||null;
    let itemType = document.getElementById("f-item-type").value;

    if(!sku)  { showToast("SKU is required"); return; }
    if(!name) { showToast("Product name is required"); return; }
    if(!price){ showToast("Sale price is required"); return; }

    if(editingId){
        let res  = await fetch(`/products/api/edit/${editingId}`,{
            method:"PUT", headers:{"Content-Type":"application/json"},
            body:JSON.stringify({name,price,cost,stock,min_stock:minStock,unit,category,item_type:itemType}),
        });
        let data = await res.json();
        if(data.detail){ showToast("Error: "+data.detail); return; }
        showToast("Product updated ✓");
    } else {
        let res  = await fetch("/products/api/add",{
            method:"POST", headers:{"Content-Type":"application/json"},
            body:JSON.stringify({sku,name,price,cost,stock,min_stock:minStock,unit,category,item_type:itemType}),
        });
        let data = await res.json();
        if(data.detail){ showToast("Error: "+data.detail); return; }
        showToast(`Product ${data.sku} added ✓`);
        // Add category to list if new
        if(category && !categories.includes(category)){
            categories.push(category); categories.sort();
            await loadCategories();
        }
    }
    closeModal();
    loadProducts();
}

async function deleteProduct(id, name){
    if(!confirm(`Delete "${name}"?`)) return;
    let res  = await fetch(`/products/api/delete/${id}`,{method:"DELETE"});
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    showToast(`"${name}" deleted`);
    loadProducts();
}

document.getElementById("modal").addEventListener("click",function(e){ if(e.target===this) closeModal(); });
document.getElementById("cat-modal").addEventListener("click",function(e){ if(e.target===this) this.classList.remove("open"); });

function showToast(msg){
    let t=document.getElementById("toast");
    t.innerText=msg; t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer=setTimeout(()=>t.classList.remove("show"),3500);
}

init();
</script>
</body>
</html>"""
