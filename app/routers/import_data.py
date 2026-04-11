from fastapi import APIRouter, UploadFile, File, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
import openpyxl, io

from app.core.permissions import require_permission
from app.database import get_db
from app.models.product import Product
from app.models.customer import Customer
from app.models.inventory import StockMove

router = APIRouter(
    prefix="/import",
    tags=["Import"],
    dependencies=[Depends(require_permission("page_import"))],
)


def find_col(raw_headers, names):
    for name in names:
        for i, h in enumerate(raw_headers):
            if h == name.lower().strip():
                return i + 1
    return None


def safe_str(v):
    return str(v).strip() if v is not None else None


def safe_float(v):
    try: return float(v)
    except (ValueError, TypeError): return None


# ── PREVIEW ────────────────────────────────────────────
@router.post("/api/preview")
async def preview_file(file: UploadFile = File(...)):
    contents = await file.read()
    wb = openpyxl.load_workbook(io.BytesIO(contents), data_only=True)
    ws = wb.active
    max_col = min(ws.max_column, 10)
    headers = [str(ws.cell(1, c).value or "") for c in range(1, max_col + 1)]
    rows = []
    for row in range(2, min(ws.max_row + 1, 7)):
        rows.append([str(ws.cell(row, c).value or "") for c in range(1, max_col + 1)])
    return {"headers": headers, "rows": rows, "total_rows": max(ws.max_row - 1, 0)}


# ── PRODUCTS ───────────────────────────────────────────
@router.post("/api/products")
async def import_products(file: UploadFile = File(...), db: Session = Depends(get_db)):
    contents = await file.read()
    wb = openpyxl.load_workbook(io.BytesIO(contents), data_only=True)
    ws = wb.active
    hdrs = [str(ws.cell(1, c).value or "").strip().lower() for c in range(1, ws.max_column + 2)]

    col_sku  = find_col(hdrs, ["sku","code","item code"])
    col_name = find_col(hdrs, ["item","name","product","product name","description"])
    col_unit = find_col(hdrs, ["uom","unit","unit of measure"])
    col_cost = find_col(hdrs, ["unit cost","cost","cost price"])
    col_price= find_col(hdrs, ["sales price","price","sale price","selling price"])
    col_cat  = find_col(hdrs, ["group","category","category name"])
    col_type = find_col(hdrs, ["item type","type","product type"])

    if not col_name:
        return {"error": "Cannot find Item/Name column"}

    created = updated = 0
    errors  = []

    for row in range(2, ws.max_row + 1):
        def v(c):
            if not c: return None
            x = ws.cell(row, c).value
            return str(x).strip() if x is not None else None

        name = v(col_name)
        if not name or name.lower() == "none": continue

        # SKU: convert numeric float like 10001.0 → "10001"
        raw_sku = ws.cell(row, col_sku).value if col_sku else None
        if raw_sku is not None:
            try:    sku = str(int(float(str(raw_sku))))
            except (ValueError, TypeError): sku = str(raw_sku).strip()
        else:
            sku = None

        # Auto-generate SKU if missing
        if not sku:
            nums = []
            for p in db.query(Product).all():
                try: nums.append(int(p.sku))
                except (ValueError, TypeError): pass
            sku = str(max(nums) + 1) if nums else "10001"

        unit  = v(col_unit) or "gram"
        cost  = safe_float(ws.cell(row, col_cost).value  if col_cost  else None) or 0.0
        price = safe_float(ws.cell(row, col_price).value if col_price else None) or 0.0
        cat   = v(col_cat)

        raw_t = v(col_type) or ""
        item_type = "raw" if "raw" in raw_t.lower() else "finished"

        # Find existing by SKU then by name
        existing = db.query(Product).filter(Product.sku == sku).first()
        if not existing:
            existing = db.query(Product).filter(Product.name == name, Product.is_active == True).first()

        if existing:
            existing.name = name
            existing.unit = unit
            if cost  > 0: existing.cost  = cost
            if price > 0: existing.price = price
            if cat and hasattr(existing, "category"):  existing.category  = cat
            if hasattr(existing, "item_type"):          existing.item_type = item_type
            if not existing.sku: existing.sku = sku
            updated += 1
        else:
            p = Product(sku=sku, name=name, unit=unit, cost=cost,
                        price=price, stock=0, min_stock=5)
            if cat and hasattr(p, "category"):  p.category  = cat
            if hasattr(p, "item_type"):          p.item_type = item_type
            db.add(p)
            created += 1

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        return {"error": str(e)}

    return {"ok": True, "created": created, "updated": updated,
            "errors": errors, "message": f"Done: {created} created, {updated} updated"}


# ── STOCK ──────────────────────────────────────────────
@router.post("/api/stock")
async def import_stock(file: UploadFile = File(...), db: Session = Depends(get_db)):
    contents = await file.read()
    wb = openpyxl.load_workbook(io.BytesIO(contents), data_only=True)
    ws = wb.active
    hdrs = [str(ws.cell(1, c).value or "").strip().lower() for c in range(1, ws.max_column + 2)]

    col_sku   = find_col(hdrs, ["sku","code","item code"])
    col_name  = find_col(hdrs, ["item","name","product","description"])
    col_stock = find_col(hdrs, ["stock","qty","quantity","on hand","soh"])

    if not col_stock:
        return {"error": "Cannot find Stock/Qty column"}

    updated   = 0
    not_found = []

    for row in range(2, ws.max_row + 1):
        sku_raw   = ws.cell(row, col_sku).value   if col_sku  else None
        name_raw  = ws.cell(row, col_name).value  if col_name else None
        stock_raw = ws.cell(row, col_stock).value

        if stock_raw is None: continue
        new_stock = safe_float(stock_raw)
        if new_stock is None: continue

        # Normalise SKU
        if sku_raw is not None:
            try:    sku = str(int(float(str(sku_raw))))
            except (ValueError, TypeError): sku = str(sku_raw).strip()
        else:
            sku = None

        product = None
        if sku:
            product = db.query(Product).filter(Product.sku == sku, Product.is_active == True).first()
        if not product and name_raw:
            product = db.query(Product).filter(Product.name == str(name_raw).strip(), Product.is_active == True).first()

        if product:
            before = float(product.stock)
            product.stock = new_stock
            db.add(StockMove(
                product_id=product.id, type="adjust",
                qty=round(new_stock - before, 3),
                qty_before=before, qty_after=new_stock,
                ref_type="import", note="Stock import from Excel",
            ))
            updated += 1
        else:
            label = sku or (str(name_raw)[:30] if name_raw else f"row {row}")
            not_found.append(label)

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        return {"error": str(e)}

    return {"ok": True, "updated": updated, "not_found": not_found[:30],
            "message": f"Done: {updated} updated" + (f", {len(not_found)} not found" if not_found else "")}


# ── CUSTOMERS ──────────────────────────────────────────
@router.post("/api/customers")
async def import_customers(file: UploadFile = File(...), db: Session = Depends(get_db)):
    contents = await file.read()
    wb = openpyxl.load_workbook(io.BytesIO(contents), data_only=True)
    ws = wb.active
    hdrs = [str(ws.cell(1, c).value or "").strip().lower() for c in range(1, ws.max_column + 2)]

    col_name = find_col(hdrs, ["name","customer name","client name","customer"])
    col_phone= find_col(hdrs, ["phone","mobile","tel","telephone"])
    col_email= find_col(hdrs, ["email","e-mail"])
    col_addr = find_col(hdrs, ["address","area","city","location"])

    if not col_name:
        return {"error": "Cannot find Name column"}

    created = skipped = 0

    for row in range(2, ws.max_row + 1):
        def v(c):
            if not c: return None
            x = ws.cell(row, c).value
            return str(x).strip() if x is not None else None

        name  = v(col_name)
        if not name or name.lower() == "none": continue
        phone = v(col_phone)
        email = v(col_email)
        addr  = v(col_addr)

        if phone and db.query(Customer).filter(Customer.phone == phone).first():
            skipped += 1; continue
        if db.query(Customer).filter(Customer.name == name).first():
            skipped += 1; continue

        db.add(Customer(name=name, phone=phone, email=email, address=addr))
        created += 1

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        return {"error": str(e)}

    return {"ok": True, "created": created, "skipped": skipped,
            "message": f"Done: {created} imported, {skipped} skipped"}


# ── UI ─────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
def import_ui():
    return """<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Import Data — Thunder ERP</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#060810;--card:#0f1424;--card2:#151c30;--border:rgba(255,255,255,0.06);--border2:rgba(255,255,255,0.11);--green:#00ff9d;--blue:#4d9fff;--orange:#fb923c;--teal:#2dd4bf;--danger:#ff4d6d;--warn:#ffb547;--lime:#84cc16;--purple:#a855f7;--text:#f0f4ff;--sub:#8899bb;--muted:#445066;--sans:'Outfit',sans-serif;--mono:'JetBrains Mono',monospace;--r:12px;}
body.light{
    --bg:#f4f5ef;--surface:#f1f3eb;--card:#eceee6;--card2:#e4e6de;
    --border:rgba(0,0,0,0.08);--border2:rgba(0,0,0,0.14);
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
nav{position:sticky;top:0;z-index:100;display:flex;align-items:center;gap:8px;padding:0 24px;height:58px;background:rgba(10,13,24,.92);backdrop-filter:blur(20px);border-bottom:1px solid var(--border);}
.logo{font-size:17px;font-weight:900;background:linear-gradient(135deg,var(--green),var(--blue));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;margin-right:10px;text-decoration:none;display:flex;align-items:center;gap:8px;}
.nav-link{padding:7px 12px;border-radius:8px;color:var(--sub);font-size:12px;font-weight:600;text-decoration:none;transition:all .2s;}
.nav-link:hover{background:rgba(255,255,255,.05);color:var(--text);}
.nav-link.active{background:rgba(77,159,255,.1);color:var(--blue);}
.nav-spacer{flex:1;}
.content{max-width:1100px;margin:0 auto;padding:32px 24px;display:flex;flex-direction:column;gap:24px;}
.page-title{font-size:24px;font-weight:800;letter-spacing:-.5px;}
.page-sub{color:var(--muted);font-size:13px;margin-top:3px;}
.import-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:20px;}
.import-card{background:var(--card);border:1px solid var(--border);border-radius:16px;overflow:hidden;display:flex;flex-direction:column;}
.import-card-header{padding:20px 22px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px;}
.import-card-icon{width:42px;height:42px;border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:20px;flex-shrink:0;}
.icon-products{background:rgba(132,204,22,.1);}
.icon-stock{background:rgba(45,212,191,.1);}
.icon-customers{background:rgba(77,159,255,.1);}
.import-card-title{font-size:15px;font-weight:800;}
.import-card-sub{font-size:12px;color:var(--muted);margin-top:2px;}
.import-card-body{padding:18px 22px;flex:1;display:flex;flex-direction:column;gap:14px;}
.col-map{background:var(--card2);border:1px solid var(--border);border-radius:10px;padding:12px 14px;}
.col-map-title{font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-bottom:10px;}
.col-row{display:flex;align-items:center;gap:8px;margin-bottom:6px;font-size:12px;}
.col-row:last-child{margin-bottom:0;}
.col-excel{font-family:var(--mono);color:var(--lime);font-size:11px;background:rgba(132,204,22,.08);padding:2px 7px;border-radius:4px;white-space:nowrap;}
.col-arrow{color:var(--muted);font-size:10px;}
.col-field{color:var(--sub);}
.col-opt{color:var(--muted);font-size:10px;font-style:italic;}
.drop-zone{border:2px dashed var(--border2);border-radius:12px;padding:28px 20px;text-align:center;cursor:pointer;transition:all .2s;position:relative;}
.drop-zone:hover,.drop-zone.drag-over{border-color:var(--blue);background:rgba(77,159,255,.04);}
.drop-zone input[type=file]{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%;}
.drop-icon{font-size:28px;margin-bottom:8px;}
.drop-text{font-size:13px;font-weight:600;color:var(--sub);}
.drop-hint{font-size:11px;color:var(--muted);margin-top:4px;}
.preview-wrap{overflow-x:auto;border:1px solid var(--border);border-radius:10px;}
.preview-info{font-size:12px;color:var(--muted);padding:8px 12px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;}
table{width:100%;border-collapse:collapse;font-size:12px;}
thead{background:var(--card2);}
th{text-align:left;font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);padding:8px 12px;white-space:nowrap;}
td{padding:8px 12px;border-top:1px solid var(--border);color:var(--sub);white-space:nowrap;max-width:160px;overflow:hidden;text-overflow:ellipsis;}
.import-btn{width:100%;padding:12px;border-radius:var(--r);font-family:var(--sans);font-size:14px;font-weight:700;cursor:pointer;border:none;transition:all .2s;display:flex;align-items:center;justify-content:center;gap:8px;}
.import-btn:disabled{opacity:.4;cursor:not-allowed;}
.btn-lime{background:linear-gradient(135deg,var(--lime),var(--green));color:#0a1a00;}
.btn-teal{background:linear-gradient(135deg,var(--teal),var(--blue));color:#001a18;}
.btn-blue{background:linear-gradient(135deg,var(--blue),var(--purple));color:white;}
.btn-lime:not(:disabled):hover,.btn-teal:not(:disabled):hover,.btn-blue:not(:disabled):hover{filter:brightness(1.1);transform:translateY(-1px);}
.result-box{border-radius:10px;padding:12px 16px;font-size:13px;font-weight:600;display:none;}
.result-ok{background:rgba(0,255,157,.08);border:1px solid rgba(0,255,157,.2);color:var(--green);}
.result-err{background:rgba(255,77,109,.08);border:1px solid rgba(255,77,109,.2);color:var(--danger);}
.result-warn{background:rgba(255,181,71,.08);border:1px solid rgba(255,181,71,.2);color:var(--warn);}
.not-found-list{margin-top:6px;font-size:11px;font-weight:400;color:var(--muted);max-height:80px;overflow-y:auto;}
.progress-wrap{height:4px;background:var(--border2);border-radius:4px;overflow:hidden;display:none;}
.progress-fill{height:100%;border-radius:4px;transition:width .3s;background:linear-gradient(90deg,var(--green),var(--lime));}
.section-label{font-size:11px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--muted);display:flex;align-items:center;gap:10px;}
.section-label::after{content:'';flex:1;height:1px;background:linear-gradient(90deg,var(--border2),transparent);}
::-webkit-scrollbar{width:4px;height:4px;}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:4px;}
</style>
</head>
<body>
<nav>
    <a href="/home" class="logo">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none"><polygon points="13,2 4,14 11,14 11,22 20,10 13,10" fill="#f59e0b"/></svg>
        Thunder ERP
    </a>
    <a href="/dashboard" class="nav-link">Dashboard</a>
    <a href="/products/"  class="nav-link">Products</a>
    <a href="/import/"    class="nav-link active">Import</a>
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
        <div class="page-title">Import Data</div>
        <div class="page-sub">Import or update products, stock, and customers from Excel (.xlsx)</div>
    </div>

    <div class="section-label">Products & Stock</div>
    <div class="import-grid">

        <!-- PRODUCTS -->
        <div class="import-card">
            <div class="import-card-header">
                <div class="import-card-icon icon-products">🌿</div>
                <div>
                    <div class="import-card-title">Import Products</div>
                    <div class="import-card-sub">Creates new products — updates existing by SKU</div>
                </div>
            </div>
            <div class="import-card-body">
                <div class="col-map">
                    <div class="col-map-title">Expected Excel Columns</div>
                    <div class="col-row"><span class="col-excel">SKU</span><span class="col-arrow">→</span><span class="col-field">Product SKU</span><span class="col-opt">(auto-generated if missing)</span></div>
                    <div class="col-row"><span class="col-excel">Item</span><span class="col-arrow">→</span><span class="col-field">Product Name</span><span style="color:var(--danger);font-size:10px;margin-left:4px">required</span></div>
                    <div class="col-row"><span class="col-excel">UOM</span><span class="col-arrow">→</span><span class="col-field">Unit</span><span class="col-opt">(gram / kg / pcs…)</span></div>
                    <div class="col-row"><span class="col-excel">Unit Cost</span><span class="col-arrow">→</span><span class="col-field">Cost Price</span></div>
                    <div class="col-row"><span class="col-excel">Sales price</span><span class="col-arrow">→</span><span class="col-field">Sale Price</span></div>
                    <div class="col-row"><span class="col-excel">Group</span><span class="col-arrow">→</span><span class="col-field">Category</span></div>
                    <div class="col-row"><span class="col-excel">Item Type</span><span class="col-arrow">→</span><span class="col-field">Raw / Finished</span><span class="col-opt">(defaults to finished)</span></div>
                </div>
                <div class="drop-zone" id="drop-products" ondragover="onDrag(event,'products')" ondragleave="offDrag('products')" ondrop="onDrop(event,'products')">
                    <input type="file" accept=".xlsx,.xls" onchange="onFile(this,'products')">
                    <div class="drop-icon">📄</div>
                    <div class="drop-text">Click or drag products.xlsx here</div>
                    <div class="drop-hint" id="hint-products">Same SKU = update existing product</div>
                </div>
                <div class="progress-wrap" id="prog-products"><div class="progress-fill" id="progfill-products" style="width:0%"></div></div>
                <div id="preview-products"></div>
                <div class="result-box" id="res-products"></div>
                <button class="import-btn btn-lime" id="btn-products" onclick="doImport('products')" disabled>⬆ Import Products</button>
            </div>
        </div>

        <!-- STOCK -->
        <div class="import-card">
            <div class="import-card-header">
                <div class="import-card-icon icon-stock">📦</div>
                <div>
                    <div class="import-card-title">Import Stock on Hand</div>
                    <div class="import-card-sub">Updates current stock for existing products by SKU</div>
                </div>
            </div>
            <div class="import-card-body">
                <div class="col-map">
                    <div class="col-map-title">Expected Excel Columns</div>
                    <div class="col-row"><span class="col-excel">SKU</span><span class="col-arrow">→</span><span class="col-field">Must match existing product</span><span style="color:var(--danger);font-size:10px;margin-left:4px">required</span></div>
                    <div class="col-row"><span class="col-excel">Item</span><span class="col-arrow">→</span><span class="col-field">Fallback if no SKU match</span></div>
                    <div class="col-row"><span class="col-excel">Stock</span><span class="col-arrow">→</span><span class="col-field">New Stock Quantity</span><span style="color:var(--danger);font-size:10px;margin-left:4px">required</span></div>
                    <div style="margin-top:8px;font-size:11px;color:var(--warn);padding:8px 10px;background:rgba(255,181,71,.06);border-radius:6px;border:1px solid rgba(255,181,71,.15);">
                        ⚠ Sets stock to exact value. Import products first if they don't exist.
                    </div>
                </div>
                <div class="drop-zone" id="drop-stock" ondragover="onDrag(event,'stock')" ondragleave="offDrag('stock')" ondrop="onDrop(event,'stock')">
                    <input type="file" accept=".xlsx,.xls" onchange="onFile(this,'stock')">
                    <div class="drop-icon">📊</div>
                    <div class="drop-text">Click or drag SOH.xlsx here</div>
                    <div class="drop-hint" id="hint-stock">Overwrites current stock for matched SKUs</div>
                </div>
                <div class="progress-wrap" id="prog-stock"><div class="progress-fill" id="progfill-stock" style="width:0%"></div></div>
                <div id="preview-stock"></div>
                <div class="result-box" id="res-stock"></div>
                <button class="import-btn btn-teal" id="btn-stock" onclick="doImport('stock')" disabled>⬆ Import Stock</button>
            </div>
        </div>

    </div>

    <div class="section-label">Customers</div>
    <div class="import-grid" style="grid-template-columns:minmax(320px,500px)">

        <!-- CUSTOMERS -->
        <div class="import-card">
            <div class="import-card-header">
                <div class="import-card-icon icon-customers">👥</div>
                <div>
                    <div class="import-card-title">Import Customers</div>
                    <div class="import-card-sub">Skips duplicates by phone or name</div>
                </div>
            </div>
            <div class="import-card-body">
                <div class="col-map">
                    <div class="col-map-title">Expected Excel Columns</div>
                    <div class="col-row"><span class="col-excel">Name</span><span class="col-arrow">→</span><span class="col-field">Customer Name</span><span style="color:var(--danger);font-size:10px;margin-left:4px">required</span></div>
                    <div class="col-row"><span class="col-excel">Phone</span><span class="col-arrow">→</span><span class="col-field">Phone Number</span><span class="col-opt">(used for duplicate check)</span></div>
                    <div class="col-row"><span class="col-excel">Email</span><span class="col-arrow">→</span><span class="col-field">Email</span></div>
                    <div class="col-row"><span class="col-excel">Address</span><span class="col-arrow">→</span><span class="col-field">Address / Area</span></div>
                </div>
                <div class="drop-zone" id="drop-customers" ondragover="onDrag(event,'customers')" ondragleave="offDrag('customers')" ondrop="onDrop(event,'customers')">
                    <input type="file" accept=".xlsx,.xls" onchange="onFile(this,'customers')">
                    <div class="drop-icon">📋</div>
                    <div class="drop-text">Click or drag Customers.xlsx here</div>
                    <div class="drop-hint" id="hint-customers">Duplicates automatically skipped</div>
                </div>
                <div class="progress-wrap" id="prog-customers"><div class="progress-fill" id="progfill-customers" style="width:0%"></div></div>
                <div id="preview-customers"></div>
                <div class="result-box" id="res-customers"></div>
                <button class="import-btn btn-blue" id="btn-customers" onclick="doImport('customers')" disabled>⬆ Import Customers</button>
            </div>
        </div>

    </div>
</div>

<script>
  const __erpToken = localStorage.getItem("token");
  const __erpUserRole = localStorage.getItem("user_role") || "";
  const __erpUserPermissions = new Set(
      (localStorage.getItem("user_permissions") || "")
          .split(",")
          .map(p => p.trim())
          .filter(Boolean)
  );
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
function setUserInfo(){
    const name = localStorage.getItem("user_name") || "Admin";
    const avatar = document.getElementById("user-avatar");
    const userName = document.getElementById("user-name");
    if(avatar) avatar.innerText = name.charAt(0).toUpperCase();
    if(userName) userName.innerText = name;
}
function logout(){
    localStorage.removeItem("token");
    localStorage.removeItem("user_name");
    localStorage.removeItem("user_role");
    localStorage.removeItem("user_permissions");
    document.cookie = "access_token=; Max-Age=0; path=/; SameSite=Lax";
    window.location.href = "/";
}
  function requirePageAccess(permission){
      if(!__erpToken){
          window.location.href = "/";
          throw new Error("Not authenticated");
      }
      if(__erpUserRole === "admin" || __erpUserPermissions.has(permission)) return;
      document.body.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100vh;flex-direction:column;gap:16px;color:#445066;font-family:'Outfit',sans-serif;background:#060810"><div style="font-size:48px">🔒</div><div style="font-size:20px;font-weight:800;color:#f0f4ff">Access Restricted</div><div style="font-size:14px">You do not have permission to open this page.</div><a href="/home" style="color:#00ff9d;text-decoration:none;font-weight:700">Back to Home</a></div>`;
      throw new Error("Access denied");
  }
  function applyNavPermissions(){
      const navPermissions = {
          "/home": null,
          "/dashboard": "page_dashboard",
          "/pos": "page_pos",
          "/b2b/": "page_b2b",
          "/inventory/": "page_inventory",
          "/products/": "page_products",
          "/customers-mgmt/": "page_customers",
          "/suppliers/": "page_suppliers",
          "/production/": "page_production",
          "/farm/": "page_farm",
          "/hr/": "page_hr",
          "/accounting/": "page_accounting",
          "/reports/": "page_reports",
          "/import": "page_import",
          "/users/": "admin_only"
      };
      document.querySelectorAll("a.nav-link[href]").forEach(link => {
          const href = link.getAttribute("href");
          const requirement = navPermissions[href];
          if(requirement === undefined || requirement === null) return;
          if(requirement === "admin_only"){
              if(__erpUserRole !== "admin") link.style.display = "none";
              return;
          }
          if(__erpUserRole !== "admin" && !__erpUserPermissions.has(requirement)){
              link.style.display = "none";
          }
      });
  }
  requirePageAccess("page_import");
  applyNavPermissions();
  initializeColorMode();
  setUserInfo();
  const files = {products:null, stock:null, customers:null};

function onDrag(e,t){ e.preventDefault(); document.getElementById('drop-'+t).classList.add('drag-over'); }
function offDrag(t){ document.getElementById('drop-'+t).classList.remove('drag-over'); }
function onDrop(e,t){ e.preventDefault(); offDrag(t); let f=e.dataTransfer.files[0]; if(f) loadFile(f,t); }
function onFile(inp,t){ let f=inp.files[0]; if(f) loadFile(f,t); }

async function loadFile(f, type){
    files[type] = f;
    document.getElementById('drop-'+type).querySelector('.drop-text').innerText = f.name;
    document.getElementById('btn-'+type).disabled = false;
    showResult(type, '', '');

    let fd = new FormData(); fd.append('file', f);
    let prev = await (await fetch('/import/api/preview', {method:'POST', body:fd})).json();
    if(prev.headers){
        document.getElementById('hint-'+type).innerText = prev.total_rows + ' rows detected';
        document.getElementById('preview-'+type).innerHTML = `
            <div class="preview-wrap">
                <div class="preview-info">
                    <span>Preview — first 5 rows</span>
                    <span style="color:var(--lime);font-family:var(--mono)">${prev.total_rows} total rows</span>
                </div>
                <table><thead><tr>${prev.headers.map(h=>`<th>${h||'—'}</th>`).join('')}</tr></thead>
                <tbody>${prev.rows.map(r=>`<tr>${r.map(c=>`<td>${c}</td>`).join('')}</tr>`).join('')}</tbody>
                </table>
            </div>`;
    }
}

function showResult(type, msg, kind){
    let el = document.getElementById('res-'+type);
    if(!msg){ el.style.display='none'; return; }
    el.className = 'result-box result-'+kind;
    el.innerHTML = msg;
    el.style.display = 'block';
}

function showProg(type, pct){
    let w=document.getElementById('prog-'+type);
    let f=document.getElementById('progfill-'+type);
    w.style.display='block'; f.style.width=pct+'%';
    if(pct>=100) setTimeout(()=>{w.style.display='none';f.style.width='0%';},800);
}

async function doImport(type){
    let f = files[type];
    if(!f){ showResult(type,'Please select a file first','err'); return; }
    let btn = document.getElementById('btn-'+type);
    btn.disabled=true; btn.innerHTML='⏳ Importing…';
    showProg(type, 40); showResult(type,'','');

    let fd = new FormData(); fd.append('file', f);
    let res  = await fetch('/import/api/'+type, {method:'POST', body:fd});
    let data = await res.json();
    showProg(type, 100);

    let cap = type.charAt(0).toUpperCase()+type.slice(1);
    btn.disabled=false; btn.innerHTML='⬆ Import '+cap;

    if(data.error){ showResult(type, '✗ '+data.error, 'err'); return; }

    let msg='', kind='ok';
    if(type==='products'){
        msg = `✓ <b>${data.created}</b> products created &nbsp;·&nbsp; <b>${data.updated}</b> updated`;
        if(data.errors&&data.errors.length){ msg+=`<br><span style="font-size:11px;font-weight:400">${data.errors.join(', ')}</span>`; kind='warn'; }
    } else if(type==='stock'){
        msg = `✓ <b>${data.updated}</b> products stock updated`;
        if(data.not_found&&data.not_found.length){
            msg += `<br><b>${data.not_found.length} SKUs not found:</b>`;
            msg += `<div class="not-found-list">${data.not_found.join(', ')}</div>`;
            kind='warn';
        }
    } else {
        msg = `✓ <b>${data.created}</b> customers imported &nbsp;·&nbsp; <b>${data.skipped}</b> skipped`;
    }
    showResult(type, msg, kind);
}
</script>
</body>
</html>"""
