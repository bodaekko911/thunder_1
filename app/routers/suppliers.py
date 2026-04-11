from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from typing import Optional, List
from pydantic import BaseModel

from app.database import get_db
from app.core.permissions import get_current_user, require_permission
from app.models.supplier import Supplier, Purchase, PurchaseItem
from app.models.user import User
from app.models.product import Product
from app.models.inventory import StockMove

router = APIRouter(
    prefix="/suppliers",
    tags=["Suppliers"],
    dependencies=[Depends(require_permission("page_suppliers"))],
)


# ── Schemas ────────────────────────────────────────────
class SupplierCreate(BaseModel):
    name:    str
    phone:   Optional[str] = None
    email:   Optional[str] = None
    address: Optional[str] = None

class SupplierUpdate(BaseModel):
    name:    Optional[str] = None
    phone:   Optional[str] = None
    email:   Optional[str] = None
    address: Optional[str] = None

class PurchaseItemIn(BaseModel):
    product_id: int
    qty:        float
    unit_cost:  float

class PurchaseCreate(BaseModel):
    supplier_id: int
    notes:       Optional[str] = None
    items:       List[PurchaseItemIn]


# ── SUPPLIER API ───────────────────────────────────────
@router.get("/api/list")
def get_suppliers(q: str = "", db: Session = Depends(get_db)):
    query = db.query(Supplier)
    if q:
        query = query.filter(Supplier.name.ilike(f"%{q}%"))
    items = query.order_by(Supplier.name).all()
    return [
        {
            "id":      s.id,
            "name":    s.name,
            "phone":   s.phone or "—",
            "email":   s.email or "—",
            "address": s.address or "—",
            "purchases": len(s.purchases),
        }
        for s in items
    ]

@router.post("/api/add")
def add_supplier(data: SupplierCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    s = Supplier(**data.model_dump())
    db.add(s); db.commit(); db.refresh(s)
    return {"id": s.id, "name": s.name}

@router.put("/api/edit/{supplier_id}")
def edit_supplier(supplier_id: int, data: SupplierUpdate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    s = db.query(Supplier).filter(Supplier.id == supplier_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Supplier not found")
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(s, k, v)
    db.commit()
    return {"ok": True}

@router.delete("/api/delete/{supplier_id}")
def delete_supplier(supplier_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    s = db.query(Supplier).filter(Supplier.id == supplier_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Supplier not found")
    db.delete(s); db.commit()
    return {"ok": True}


# ── PURCHASE API ───────────────────────────────────────
@router.get("/api/purchases")
def get_purchases(supplier_id: int = None, db: Session = Depends(get_db)):
    query = db.query(Purchase)
    if supplier_id:
        query = query.filter(Purchase.supplier_id == supplier_id)
    purchases = query.order_by(Purchase.created_at.desc()).limit(100).all()
    return [
        {
            "id":              p.id,
            "purchase_number": p.purchase_number,
            "supplier":        p.supplier.name if p.supplier else "—",
            "supplier_id":     p.supplier_id,
            "status":          p.status,
            "total":           float(p.total),
            "items_count":     len(p.items),
            "created_at":      p.created_at.strftime("%Y-%m-%d %H:%M") if p.created_at else "—",
            "notes":           p.notes or "",
        }
        for p in purchases
    ]

@router.get("/api/purchase/{purchase_id}")
def get_purchase(purchase_id: int, db: Session = Depends(get_db)):
    p = db.query(Purchase).filter(Purchase.id == purchase_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Purchase not found")
    return {
        "id":              p.id,
        "purchase_number": p.purchase_number,
        "supplier":        p.supplier.name if p.supplier else "—",
        "status":          p.status,
        "subtotal":        float(p.subtotal),
        "discount":        float(p.discount),
        "total":           float(p.total),
        "notes":           p.notes or "",
        "created_at":      p.created_at.strftime("%Y-%m-%d %H:%M") if p.created_at else "—",
        "items": [
            {
                "product":   item.product.name if item.product else "—",
                "sku":       item.product.sku  if item.product else "—",
                "qty":       float(item.qty),
                "unit_cost": float(item.unit_cost),
                "total":     float(item.total),
            }
            for item in p.items
        ],
    }

@router.post("/api/purchase/create")
def create_purchase(data: PurchaseCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if not data.items:
        raise HTTPException(status_code=400, detail="Purchase must have at least one item")

    supplier = db.query(Supplier).filter(Supplier.id == data.supplier_id).first()
    if not supplier:
        raise HTTPException(status_code=404, detail="Supplier not found")

    # Generate purchase number
    from sqlalchemy import func as sqlfunc
    max_id = db.query(sqlfunc.max(Purchase.id)).scalar() or 0
    purchase_number = f"PO-{str(max_id + 1).zfill(5)}"

    subtotal = 0
    line_items = []

    for item in data.items:
        product = db.query(Product).filter(Product.id == item.product_id).first()
        if not product:
            raise HTTPException(status_code=404, detail=f"Product ID not found: {item.product_id}")
        line_total = item.qty * item.unit_cost
        subtotal  += line_total
        line_items.append((product, item.qty, item.unit_cost, line_total))

    purchase = Purchase(
        purchase_number=purchase_number,
        supplier_id=data.supplier_id,
        status="received",
        subtotal=round(subtotal, 2),
        discount=0,
        total=round(subtotal, 2),
        notes=data.notes,
    )
    db.add(purchase); db.flush()

    for product, qty, unit_cost, line_total in line_items:
        db.add(PurchaseItem(
            purchase_id=purchase.id,
            product_id=product.id,
            qty=qty,
            unit_cost=unit_cost,
            total=round(line_total, 2),
        ))
        # Add stock
        before = float(product.stock)
        product.stock = before + qty
        product.cost  = unit_cost
        db.add(StockMove(
            product_id=product.id,
            type="in",
            qty=qty,
            qty_before=before,
            qty_after=float(product.stock),
            ref_type="purchase",
            ref_id=purchase.id,
            note=f"Purchase {purchase_number}",
        ))

    db.commit(); db.refresh(purchase)
    return {"id": purchase.id, "purchase_number": purchase_number, "total": float(purchase.total)}

@router.get("/api/products-list")
def products_list(db: Session = Depends(get_db)):
    products = db.query(Product).filter(Product.is_active == True).order_by(Product.name).all()
    return [
        {"id": p.id, "sku": p.sku, "name": p.name, "cost": float(p.cost), "stock": float(p.stock)}
        for p in products
    ]


# ── UI ─────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
def suppliers_ui():
    return """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Suppliers</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root {
    --bg:      #060810;
    --surface: #0a0d18;
    --card:    #0f1424;
    --card2:   #151c30;
    --border:  rgba(255,255,255,0.06);
    --border2: rgba(255,255,255,0.11);
    --green:   #00ff9d;
    --blue:    #4d9fff;
    --purple:  #a855f7;
    --danger:  #ff4d6d;
    --warn:    #ffb547;
    --text:    #f0f4ff;
    --sub:     #8899bb;
    --muted:   #445066;
    --sans:    'Outfit', sans-serif;
    --mono:    'JetBrains Mono', monospace;
    --r:       12px;
}
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
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: var(--sans); background: var(--bg); color: var(--text); min-height: 100vh; font-size: 14px; }

nav {
    position: sticky; top: 0; z-index: 100;
    display: flex; align-items: center; gap: 10px;
    padding: 0 24px; height: 58px;
    background: rgba(10,13,24,.92);
    backdrop-filter: blur(20px);
    border-bottom: 1px solid var(--border);
}
.logo {
    font-size: 18px; font-weight: 900;
    background: linear-gradient(135deg, var(--green), var(--blue));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    background-clip: text; margin-right: 12px;
}
.nav-link {
    padding: 7px 14px; border-radius: 8px;
    color: var(--sub); font-size: 13px; font-weight: 600;
    text-decoration: none; transition: all .2s;
}
.nav-link:hover { background: rgba(255,255,255,.05); color: var(--text); }
.nav-link.active { background: rgba(0,255,157,.1); color: var(--green); }
.nav-spacer { flex: 1; }

.content { max-width: 1300px; margin: 0 auto; padding: 28px 24px; display: flex; flex-direction: column; gap: 20px; }
.page-title { font-size: 24px; font-weight: 800; letter-spacing: -.5px; }
.page-sub   { color: var(--muted); font-size: 13px; margin-top: 3px; }

/* TABS */
.tabs { display: flex; gap: 4px; background: var(--card); border: 1px solid var(--border); border-radius: var(--r); padding: 4px; width: fit-content; }
.tab {
    padding: 8px 20px; border-radius: 9px;
    font-size: 13px; font-weight: 700; cursor: pointer;
    border: none; background: transparent; color: var(--muted);
    transition: all .2s; font-family: var(--sans);
}
.tab.active { background: var(--card2); color: var(--text); }

.toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.search-box {
    display: flex; align-items: center; gap: 9px;
    background: var(--card); border: 1px solid var(--border);
    border-radius: var(--r); padding: 0 14px; flex: 1; min-width: 200px;
    transition: border-color .2s;
}
.search-box:focus-within { border-color: rgba(0,255,157,.3); }
.search-box svg { color: var(--muted); flex-shrink: 0; }
.search-box input {
    background: transparent; border: none; outline: none;
    color: var(--text); font-family: var(--sans);
    font-size: 14px; padding: 11px 0; width: 100%;
}
.search-box input::placeholder { color: var(--muted); }

.btn {
    display: flex; align-items: center; gap: 7px;
    padding: 10px 16px; border-radius: var(--r);
    font-family: var(--sans); font-size: 13px; font-weight: 700;
    cursor: pointer; border: none; transition: all .2s; white-space: nowrap;
}
.btn-green  { background: linear-gradient(135deg, var(--green), #00d4ff); color: #021a10; }
.btn-green:hover { filter: brightness(1.1); transform: translateY(-1px); }
.btn-blue   { background: linear-gradient(135deg, var(--blue), var(--purple)); color: white; }
.btn-blue:hover { filter: brightness(1.1); transform: translateY(-1px); }

.table-wrap { background: var(--card); border: 1px solid var(--border); border-radius: var(--r); overflow: hidden; }
table { width: 100%; border-collapse: collapse; }
thead { background: var(--card2); }
th { text-align: left; font-size: 10px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; color: var(--muted); padding: 12px 16px; }
td { padding: 13px 16px; border-top: 1px solid var(--border); color: var(--sub); font-size: 13px; }
tr:hover td { background: rgba(255,255,255,.02); }
td.name { color: var(--text); font-weight: 600; }
td.mono { font-family: var(--mono); color: var(--green); }

.action-btn {
    background: transparent; border: 1px solid var(--border2);
    color: var(--sub); font-size: 12px; font-weight: 600;
    padding: 5px 10px; border-radius: 7px; cursor: pointer;
    transition: all .15s; font-family: var(--sans);
}
.action-btn:hover { border-color: var(--blue); color: var(--blue); }
.action-btn.danger:hover { border-color: var(--danger); color: var(--danger); }
.action-btn.green:hover  { border-color: var(--green); color: var(--green); }

/* MODAL */
.modal-bg {
    position: fixed; inset: 0; z-index: 500;
    background: rgba(0,0,0,.7); backdrop-filter: blur(4px);
    display: none; align-items: center; justify-content: center;
}
.modal-bg.open { display: flex; }
.modal {
    background: var(--card); border: 1px solid var(--border2);
    border-radius: 16px; padding: 28px;
    width: 520px; max-width: 95vw; max-height: 90vh; overflow-y: auto;
    animation: modalIn .2s ease;
}
@keyframes modalIn { from{opacity:0;transform:scale(.95)} to{opacity:1;transform:scale(1)} }
.modal-title { font-size: 18px; font-weight: 800; margin-bottom: 20px; }
.fld { display: flex; flex-direction: column; gap: 6px; margin-bottom: 14px; }
.fld label { font-size: 11px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; color: var(--muted); }
.fld input, .fld select, .fld textarea {
    background: var(--card2); border: 1px solid var(--border2);
    border-radius: 10px; padding: 10px 12px;
    color: var(--text); font-family: var(--sans); font-size: 14px;
    outline: none; transition: border-color .2s; width: 100%;
}
.fld input:focus, .fld select:focus { border-color: rgba(0,255,157,.4); }
.modal-actions { display: flex; gap: 10px; margin-top: 6px; justify-content: flex-end; }
.btn-cancel {
    background: transparent; border: 1px solid var(--border2);
    color: var(--sub); padding: 10px 18px; border-radius: var(--r);
    font-family: var(--sans); font-size: 13px; font-weight: 700; cursor: pointer;
}
.btn-cancel:hover { border-color: var(--danger); color: var(--danger); }

/* PURCHASE ITEMS */
.item-row {
    display: grid; grid-template-columns: 1fr 80px 100px 30px;
    gap: 8px; align-items: center; margin-bottom: 8px;
}
.item-row select, .item-row input {
    background: var(--card2); border: 1px solid var(--border2);
    border-radius: 8px; padding: 8px 10px;
    color: var(--text); font-family: var(--sans); font-size: 13px;
    outline: none; width: 100%;
}
.item-row select:focus, .item-row input:focus { border-color: rgba(0,255,157,.4); }
.remove-item-btn {
    background: none; border: none; color: var(--muted);
    font-size: 18px; cursor: pointer; padding: 0;
    transition: color .15s;
}
.remove-item-btn:hover { color: var(--danger); }
.add-item-btn {
    background: rgba(77,159,255,.1); border: 1px dashed rgba(77,159,255,.3);
    color: var(--blue); font-family: var(--sans); font-size: 13px; font-weight: 600;
    padding: 9px; border-radius: 8px; cursor: pointer; width: 100%;
    transition: all .2s; margin-bottom: 14px;
}
.add-item-btn:hover { background: rgba(77,159,255,.2); }
.purchase-total {
    display: flex; justify-content: space-between; align-items: center;
    background: var(--card2); border: 1px solid var(--border2);
    border-radius: 10px; padding: 12px 14px; margin-bottom: 14px;
}
.purchase-total-label { font-size: 13px; font-weight: 700; color: var(--sub); }
.purchase-total-val   { font-family: var(--mono); font-size: 20px; font-weight: 700; color: var(--green); }

/* DETAIL PANEL */
.side-bg { position: fixed; inset: 0; z-index: 400; background: rgba(0,0,0,.5); display: none; }
.side-bg.open { display: block; }
.side-panel {
    position: fixed; right: 0; top: 0; bottom: 0; width: 460px; max-width: 95vw;
    background: var(--card); border-left: 1px solid var(--border2);
    display: flex; flex-direction: column;
    transform: translateX(100%); transition: transform .3s ease; z-index: 401;
}
.side-panel.open { transform: translateX(0); }
.side-header {
    padding: 20px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
}
.side-header h3 { font-size: 16px; font-weight: 800; }
.close-btn { background: none; border: none; color: var(--muted); font-size: 22px; cursor: pointer; padding: 0; transition: color .15s; }
.close-btn:hover { color: var(--danger); }
.side-body { flex: 1; overflow-y: auto; padding: 16px 20px; }

.toast {
    position: fixed; bottom: 22px; left: 50%;
    transform: translateX(-50%) translateY(16px);
    background: var(--card2); border: 1px solid var(--border2);
    border-radius: var(--r); padding: 12px 20px;
    font-size: 13px; font-weight: 600; color: var(--text);
    box-shadow: 0 20px 50px rgba(0,0,0,.5);
    opacity: 0; pointer-events: none;
    transition: opacity .25s, transform .25s; z-index: 999;
}
.toast.show { opacity:1; transform: translateX(-50%) translateY(0); }
::-webkit-scrollbar { width: 4px; }
::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 4px; }
</style>
</head>
<body>

<nav>
    <a href="/home" class="logo" style="text-decoration:none;display:flex;align-items:center;gap:8px;"><svg width="22" height="22" viewBox="0 0 24 24" fill="none"><polygon points="13,2 4,14 11,14 11,22 20,10 13,10" fill="#f59e0b" stroke="#fbbf24" stroke-width="0.5"/></svg>Thunder ERP</a>
    <a href="/dashboard"       class="nav-link">Dashboard</a>
    <a href="/pos"             class="nav-link">POS</a>
    <a href="/products/"       class="nav-link">Products</a>
    <a href="/customers-mgmt/" class="nav-link">Customers</a>
    <a href="/suppliers/"      class="nav-link active">Suppliers</a>
    <a href="/import"          class="nav-link">Import</a>
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
        <div class="page-title">Suppliers & Purchasing</div>
        <div class="page-sub">Manage suppliers and create purchase orders</div>
    </div>

    <!-- TABS -->
    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;">
        <div class="tabs">
            <button class="tab active" id="tab-suppliers" onclick="switchTab('suppliers')">Suppliers</button>
            <button class="tab"        id="tab-purchases" onclick="switchTab('purchases')">Purchase Orders</button>
        </div>
        <div style="display:flex;gap:10px;">
            <button class="btn btn-green" id="add-supplier-btn" onclick="openAddSupplierModal()">+ Add Supplier</button>
            <button class="btn btn-blue"  id="new-po-btn"       onclick="openNewPOModal()" style="display:none">+ New Purchase Order</button>
        </div>
    </div>

    <!-- SEARCH -->
    <div class="toolbar">
        <div class="search-box">
            <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24">
                <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
            </svg>
            <input id="search" placeholder="Search…" oninput="onSearch()">
        </div>
    </div>

    <!-- SUPPLIERS TABLE -->
    <div id="suppliers-section">
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>Name</th>
                        <th>Phone</th>
                        <th>Email</th>
                        <th>Address</th>
                        <th>Orders</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody id="suppliers-body">
                    <tr><td colspan="6" style="text-align:center;color:var(--muted);padding:40px">Loading…</td></tr>
                </tbody>
            </table>
        </div>
    </div>

    <!-- PURCHASES TABLE -->
    <div id="purchases-section" style="display:none">
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>PO Number</th>
                        <th>Supplier</th>
                        <th>Items</th>
                        <th>Total</th>
                        <th>Status</th>
                        <th>Date</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody id="purchases-body">
                    <tr><td colspan="7" style="text-align:center;color:var(--muted);padding:40px">Loading…</td></tr>
                </tbody>
            </table>
        </div>
    </div>
</div>

<!-- ADD/EDIT SUPPLIER MODAL -->
<div class="modal-bg" id="supplier-modal">
    <div class="modal">
        <div class="modal-title" id="supplier-modal-title">Add Supplier</div>
        <div class="fld"><label>Name *</label><input id="s-name" placeholder="Supplier name"></div>
        <div class="fld"><label>Phone</label><input id="s-phone" placeholder="+20 100 000 0000"></div>
        <div class="fld"><label>Email</label><input id="s-email" placeholder="supplier@email.com"></div>
        <div class="fld"><label>Address</label><input id="s-address" placeholder="City / Area"></div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeSupplierModal()">Cancel</button>
            <button class="btn btn-green" onclick="saveSupplier()">Save Supplier</button>
        </div>
    </div>
</div>

<!-- NEW PURCHASE ORDER MODAL -->
<div class="modal-bg" id="po-modal">
    <div class="modal" style="width:620px">
        <div class="modal-title">New Purchase Order</div>

        <div class="fld">
            <label>Supplier *</label>
            <select id="po-supplier"></select>
        </div>

        <div class="fld">
            <label>Notes</label>
            <input id="po-notes" placeholder="Optional notes">
        </div>

        <div style="margin-bottom:8px;font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted)">
            Items
        </div>

        <div style="display:grid;grid-template-columns:1fr 80px 100px 30px;gap:8px;margin-bottom:6px;">
            <span style="font-size:10px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:1px">Product</span>
            <span style="font-size:10px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:1px">Qty</span>
            <span style="font-size:10px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:1px">Unit Cost</span>
            <span></span>
        </div>

        <div id="po-items"></div>

        <button class="add-item-btn" onclick="addItemRow()">+ Add Item</button>

        <div class="purchase-total">
            <span class="purchase-total-label">Total</span>
            <span class="purchase-total-val" id="po-total">0.00</span>
        </div>

        <div class="modal-actions">
            <button class="btn-cancel" onclick="closePOModal()">Cancel</button>
            <button class="btn btn-blue" onclick="savePO()">Create Purchase Order</button>
        </div>
    </div>
</div>

<!-- PURCHASE DETAIL SIDE PANEL -->
<div class="side-bg" id="side-bg" onclick="closeSide()"></div>
<div class="side-panel" id="side-panel">
    <div class="side-header">
        <h3 id="side-title">Purchase Order</h3>
        <button class="close-btn" onclick="closeSide()">×</button>
    </div>
    <div class="side-body" id="side-body"></div>
</div>

<div class="toast" id="toast"></div>

<script>
  const __erpToken = localStorage.getItem("token");
  const __erpUserRole = localStorage.getItem("user_role") || "";
  const __erpUserPermissions = new Set(
      (localStorage.getItem("user_permissions") || "")
          .split(",")
          .map(p => p.trim())
          .filter(Boolean)
  );
  function authHeaders(extraHeaders = {}){
      return __erpToken
          ? { ...extraHeaders, "Authorization": "Bearer " + __erpToken }
          : { ...extraHeaders };
  }
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
  requirePageAccess("page_suppliers");
  applyNavPermissions();
  initializeColorMode();
  setUserInfo();
  let suppliers  = [];
let allProducts = [];
let currentTab  = "suppliers";
let editingSupplierId = null;
let searchTimer = null;

/* ── INIT ── */
async function init(){
    await loadSuppliers();
    allProducts = await (await fetch("/suppliers/api/products-list")).json();
}

/* ── TABS ── */
function switchTab(tab){
    currentTab = tab;
    document.getElementById("tab-suppliers").classList.toggle("active", tab==="suppliers");
    document.getElementById("tab-purchases").classList.toggle("active", tab==="purchases");
    document.getElementById("suppliers-section").style.display = tab==="suppliers" ? "" : "none";
    document.getElementById("purchases-section").style.display = tab==="purchases" ? "" : "none";
    document.getElementById("add-supplier-btn").style.display  = tab==="suppliers" ? "" : "none";
    document.getElementById("new-po-btn").style.display        = tab==="purchases" ? "" : "none";
    document.getElementById("search").value = "";
    if(tab==="purchases") loadPurchases();
}

function onSearch(){
    clearTimeout(searchTimer);
    searchTimer = setTimeout(()=>{
        if(currentTab==="suppliers") loadSuppliers();
        else loadPurchases();
    }, 300);
}

/* ── SUPPLIERS ── */
async function loadSuppliers(){
    let q    = document.getElementById("search").value.trim();
    let url  = `/suppliers/api/list${q?"?q="+encodeURIComponent(q):""}`;
    suppliers = await (await fetch(url)).json();

    if(!suppliers.length){
        document.getElementById("suppliers-body").innerHTML =
            `<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:40px">No suppliers found</td></tr>`;
        return;
    }

    document.getElementById("suppliers-body").innerHTML = suppliers.map(s => `
        <tr>
            <td class="name">${s.name}</td>
            <td style="font-family:var(--mono);font-size:12px">${s.phone}</td>
            <td style="font-size:12px">${s.email}</td>
            <td style="font-size:12px">${s.address}</td>
            <td style="font-family:var(--mono);color:var(--blue)">${s.purchases}</td>
            <td style="display:flex;gap:6px">
                <button class="action-btn" onclick="openEditSupplierModal(${s.id},'${s.name.replace(/'/g,"\\'")}','${s.phone}','${s.email}','${s.address}')">Edit</button>
                <button class="action-btn danger" onclick="deleteSupplier(${s.id},'${s.name.replace(/'/g,"\\'")}')">Delete</button>
            </td>
        </tr>`).join("");
}

function openAddSupplierModal(){
    editingSupplierId = null;
    document.getElementById("supplier-modal-title").innerText = "Add Supplier";
    ["s-name","s-phone","s-email","s-address"].forEach(id => document.getElementById(id).value="");
    document.getElementById("supplier-modal").classList.add("open");
}

function openEditSupplierModal(id,name,phone,email,address){
    editingSupplierId = id;
    document.getElementById("supplier-modal-title").innerText = "Edit Supplier";
    document.getElementById("s-name").value    = name;
    document.getElementById("s-phone").value   = phone==="—"?"":phone;
    document.getElementById("s-email").value   = email==="—"?"":email;
    document.getElementById("s-address").value = address==="—"?"":address;
    document.getElementById("supplier-modal").classList.add("open");
}

function closeSupplierModal(){ document.getElementById("supplier-modal").classList.remove("open"); }

async function saveSupplier(){
    let name = document.getElementById("s-name").value.trim();
    if(!name){ showToast("Name is required"); return; }
    let body = {
        name,
        phone:   document.getElementById("s-phone").value.trim()||null,
        email:   document.getElementById("s-email").value.trim()||null,
        address: document.getElementById("s-address").value.trim()||null,
    };
    let url    = editingSupplierId ? `/suppliers/api/edit/${editingSupplierId}` : "/suppliers/api/add";
    let method = editingSupplierId ? "PUT" : "POST";
    let res    = await fetch(url,{
        method,
        headers:authHeaders({"Content-Type":"application/json"}),
        body:JSON.stringify(body)
    });
    let data   = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    closeSupplierModal();
    showToast(editingSupplierId?"Supplier updated ✓":"Supplier added ✓");
    loadSuppliers();
}

async function deleteSupplier(id,name){
    if(!confirm(`Delete "${name}"?`)) return;
    let res = await fetch(`/suppliers/api/delete/${id}`,{
        method:"DELETE",
        headers:authHeaders()
    });
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    showToast("Supplier deleted ✓");
    loadSuppliers();
}

/* ── PURCHASES ── */
async function loadPurchases(){
    let q    = document.getElementById("search").value.trim();
    let purchases = await (await fetch("/suppliers/api/purchases")).json();
    if(q) purchases = purchases.filter(p =>
        p.supplier.toLowerCase().includes(q.toLowerCase()) ||
        p.purchase_number.toLowerCase().includes(q.toLowerCase())
    );

    if(!purchases.length){
        document.getElementById("purchases-body").innerHTML =
            `<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:40px">No purchase orders yet</td></tr>`;
        return;
    }

    document.getElementById("purchases-body").innerHTML = purchases.map(p => `
        <tr>
            <td style="font-family:var(--mono);font-size:12px;color:var(--blue)">${p.purchase_number}</td>
            <td class="name">${p.supplier}</td>
            <td style="color:var(--sub)">${p.items_count} items</td>
            <td class="mono">${p.total.toFixed(2)}</td>
            <td><span style="color:var(--green);font-size:12px">● ${p.status}</span></td>
            <td style="font-size:12px;color:var(--muted)">${p.created_at}</td>
            <td>
                <button class="action-btn green" onclick="viewPO(${p.id})">View</button>
            </td>
        </tr>`).join("");
}

/* ── NEW PO MODAL ── */
function openNewPOModal(){
    // Fill supplier dropdown
    let sel = document.getElementById("po-supplier");
    sel.innerHTML = suppliers.map(s=>`<option value="${s.id}">${s.name}</option>`).join("");
    document.getElementById("po-notes").value = "";
    document.getElementById("po-items").innerHTML = "";
    document.getElementById("po-total").innerText = "0.00";
    addItemRow();
    document.getElementById("po-modal").classList.add("open");
}

function closePOModal(){ document.getElementById("po-modal").classList.remove("open"); }

function addItemRow(){
    let div = document.createElement("div");
    div.className = "item-row";
    div.innerHTML = `
        <select onchange="updateTotal()">
            <option value="">Select product…</option>
            ${allProducts.map(p=>`<option value="${p.id}" data-cost="${p.cost}">${p.name} (${p.sku})</option>`).join("")}
        </select>
        <input type="number" placeholder="Qty" min="0.001" step="any" value="1" oninput="updateTotal()">
        <input type="number" placeholder="Cost" min="0" step="any" oninput="updateTotal()">
        <button class="remove-item-btn" onclick="this.parentElement.remove();updateTotal()">×</button>
    `;
    // Auto-fill cost when product selected
    div.querySelector("select").addEventListener("change", function(){
        let opt  = this.options[this.selectedIndex];
        let cost = opt.dataset.cost;
        if(cost) div.querySelectorAll("input")[1].value = cost;
        updateTotal();
    });
    document.getElementById("po-items").appendChild(div);
}

function updateTotal(){
    let rows  = document.querySelectorAll("#po-items .item-row");
    let total = 0;
    rows.forEach(row => {
        let qty  = parseFloat(row.querySelectorAll("input")[0].value)||0;
        let cost = parseFloat(row.querySelectorAll("input")[1].value)||0;
        total += qty * cost;
    });
    document.getElementById("po-total").innerText = total.toFixed(2);
}

async function savePO(){
    let supplier_id = parseInt(document.getElementById("po-supplier").value);
    let notes       = document.getElementById("po-notes").value.trim();
    let rows        = document.querySelectorAll("#po-items .item-row");
    let items       = [];

    for(let row of rows){
        let product_id = parseInt(row.querySelector("select").value);
        let qty        = parseFloat(row.querySelectorAll("input")[0].value)||0;
        let unit_cost  = parseFloat(row.querySelectorAll("input")[1].value)||0;
        if(!product_id){ showToast("Please select a product for all rows"); return; }
        if(qty <= 0)   { showToast("Quantity must be greater than 0"); return; }
        items.push({product_id, qty, unit_cost});
    }

    if(!items.length){ showToast("Add at least one item"); return; }

    let res  = await fetch("/suppliers/api/purchase/create",{
        method:"POST",
        headers:authHeaders({"Content-Type":"application/json"}),
        body:JSON.stringify({supplier_id, notes, items}),
    });
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }

    closePOModal();
    showToast(`Purchase Order ${data.purchase_number} created ✓ — Stock updated`);
    switchTab("purchases");
}

/* ── VIEW PO ── */
async function viewPO(id){
    document.getElementById("side-body").innerHTML = `<div style="color:var(--muted);font-size:13px">Loading…</div>`;
    document.getElementById("side-bg").classList.add("open");
    document.getElementById("side-panel").classList.add("open");

    let p = await (await fetch(`/suppliers/api/purchase/${id}`)).json();
    document.getElementById("side-title").innerText = p.purchase_number;

    document.getElementById("side-body").innerHTML = `
        <div style="display:flex;flex-direction:column;gap:14px">
            <div style="background:var(--card2);border:1px solid var(--border2);border-radius:10px;padding:14px">
                <div style="display:flex;justify-content:space-between;margin-bottom:6px">
                    <span style="color:var(--muted);font-size:12px">Supplier</span>
                    <span style="font-weight:700">${p.supplier}</span>
                </div>
                <div style="display:flex;justify-content:space-between;margin-bottom:6px">
                    <span style="color:var(--muted);font-size:12px">Date</span>
                    <span style="font-size:12px">${p.created_at}</span>
                </div>
                <div style="display:flex;justify-content:space-between;margin-bottom:6px">
                    <span style="color:var(--muted);font-size:12px">Status</span>
                    <span style="color:var(--green);font-size:12px;font-weight:700">● ${p.status}</span>
                </div>
                ${p.notes ? `<div style="color:var(--muted);font-size:12px;margin-top:8px">${p.notes}</div>` : ""}
            </div>

            <div>
                <div style="font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-bottom:10px">Items</div>
                ${p.items.map(item=>`
                    <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid var(--border)">
                        <div>
                            <div style="font-weight:600;font-size:13px">${item.product}</div>
                            <div style="font-family:var(--mono);font-size:10px;color:var(--muted)">${item.sku}</div>
                        </div>
                        <div style="text-align:right">
                            <div style="font-family:var(--mono);font-size:13px;color:var(--green)">${item.total.toFixed(2)}</div>
                            <div style="font-size:11px;color:var(--muted)">${item.qty} × ${item.unit_cost.toFixed(2)}</div>
                        </div>
                    </div>`).join("")}
            </div>

            <div style="display:flex;justify-content:space-between;align-items:center;padding:14px;background:var(--card2);border:1px solid var(--border2);border-radius:10px">
                <span style="font-weight:700;color:var(--sub)">Total</span>
                <span style="font-family:var(--mono);font-size:22px;font-weight:700;color:var(--green)">${p.total.toFixed(2)}</span>
            </div>
        </div>`;
}

function closeSide(){
    document.getElementById("side-bg").classList.remove("open");
    document.getElementById("side-panel").classList.remove("open");
}

document.getElementById("supplier-modal").addEventListener("click",function(e){ if(e.target===this)closeSupplierModal(); });
document.getElementById("po-modal").addEventListener("click",function(e){ if(e.target===this)closePOModal(); });

let toastTimer=null;
function showToast(msg){
    let t=document.getElementById("toast");
    t.innerText=msg; t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer=setTimeout(()=>t.classList.remove("show"),3500);
}

init();
</script>
</body>
</html>
"""
