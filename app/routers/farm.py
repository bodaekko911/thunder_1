from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import Optional, List
from pydantic import BaseModel
from datetime import date

from app.database import get_db
from app.core.permissions import get_current_user
from app.models.farm import Farm, FarmDelivery, FarmDeliveryItem
from app.models.product import Product
from app.models.inventory import StockMove
from app.models.user import User

router = APIRouter(prefix="/farm", tags=["Farm"])


# ── Schemas ────────────────────────────────────────────
class DeliveryItemIn(BaseModel):
    product_id: int
    qty:        float
    notes:      Optional[str] = None

class DeliveryCreate(BaseModel):
    farm_id:       int
    delivery_date: str
    received_by:   Optional[str] = None
    quality_notes: Optional[str] = None
    notes:         Optional[str] = None
    items:         List[DeliveryItemIn]


# ── SEED FARMS ─────────────────────────────────────────
@router.post("/api/seed-farms")
def seed_farms(db: Session = Depends(get_db)):
    if db.query(Farm).count() > 0:
        return {"message": "Farms already exist"}
    db.add(Farm(name="Organic Farm",      location="Habiba, South Sinai"))
    db.add(Farm(name="Regenerative Farm", location="Habiba, South Sinai"))
    db.commit()
    return {"message": "2 farms created"}


# ── FARM API ───────────────────────────────────────────
@router.get("/api/farms")
def get_farms(db: Session = Depends(get_db)):
    farms = db.query(Farm).filter(Farm.is_active == 1).order_by(Farm.name).all()
    return [
        {
            "id":             f.id,
            "name":           f.name,
            "location":       f.location or "—",
            "delivery_count": len(f.deliveries),
        }
        for f in farms
    ]

@router.post("/api/farms")
def create_farm(name: str, location: str = "", db: Session = Depends(get_db)):
    if db.query(Farm).filter(Farm.name == name).first():
        raise HTTPException(status_code=400, detail="Farm name already exists")
    f = Farm(name=name, location=location)
    db.add(f); db.commit(); db.refresh(f)
    return {"id": f.id, "name": f.name}


# ── DELIVERY API ───────────────────────────────────────
@router.get("/api/deliveries")
def get_deliveries(farm_id: int = None, skip: int = 0, limit: int = 50, db: Session = Depends(get_db)):
    query = db.query(FarmDelivery)
    if farm_id:
        query = query.filter(FarmDelivery.farm_id == farm_id)
    total      = query.count()
    deliveries = query.order_by(FarmDelivery.delivery_date.desc(), FarmDelivery.created_at.desc()).offset(skip).limit(limit).all()
    return {
        "total": total,
        "deliveries": [
            {
                "id":              d.id,
                "delivery_number": d.delivery_number,
                "farm":            d.farm.name if d.farm else "—",
                "farm_id":         d.farm_id,
                "delivery_date":   str(d.delivery_date),
                "received_by":     d.received_by or "—",
                "quality_notes":   d.quality_notes or "",
                "notes":           d.notes or "",
                "created_at":      d.created_at.strftime("%Y-%m-%d %H:%M") if d.created_at else "—",
                "items": [
                    {
                        "product":    i.product.name if i.product else "—",
                        "product_id": i.product_id,
                        "qty":        float(i.qty),
                        "unit":       i.unit or (i.product.unit if i.product else ""),
                        "notes":      i.notes or "",
                    }
                    for i in d.items
                ],
                "total_items": len(d.items),
                "total_qty":   sum(float(i.qty) for i in d.items),
            }
            for d in deliveries
        ],
    }

@router.post("/api/deliveries")
def create_delivery(data: DeliveryCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    farm = db.query(Farm).filter(Farm.id == data.farm_id).first()
    if not farm:
        raise HTTPException(status_code=404, detail="Farm not found")
    if not data.items:
        raise HTTPException(status_code=400, detail="Delivery must have at least one item")

    count  = db.query(FarmDelivery).count()
    number = f"FD-{str(count + 1).zfill(4)}"

    delivery = FarmDelivery(
        delivery_number=number,
        farm_id=data.farm_id,
        user_id=current_user.id,
        delivery_date=date.fromisoformat(data.delivery_date),
        received_by=data.received_by,
        quality_notes=data.quality_notes,
        notes=data.notes,
    )
    db.add(delivery); db.flush()

    for item in data.items:
        product = db.query(Product).filter(Product.id == item.product_id).first()
        if not product:
            raise HTTPException(status_code=404, detail=f"Product not found: {item.product_id}")
        db.add(FarmDeliveryItem(
            delivery_id=delivery.id,
            product_id=product.id,
            qty=item.qty,
            unit=product.unit,
            notes=item.notes,
        ))
        before = float(product.stock)
        after  = before + item.qty
        product.stock = after
        db.add(StockMove(
            product_id=product.id, type="in",
            user_id=current_user.id,
            qty=item.qty, qty_before=before, qty_after=after,
            ref_type="farm_intake", ref_id=delivery.id,
            note=f"{farm.name} — {number}",
        ))

    db.commit(); db.refresh(delivery)
    return {"id": delivery.id, "delivery_number": number, "items_count": len(data.items)}

@router.put("/api/deliveries/{delivery_id}")
def edit_delivery(delivery_id: int, data: DeliveryCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    delivery = db.query(FarmDelivery).filter(FarmDelivery.id == delivery_id).first()
    if not delivery:
        raise HTTPException(status_code=404, detail="Delivery not found")

    farm = db.query(Farm).filter(Farm.id == data.farm_id).first()
    if not farm:
        raise HTTPException(status_code=404, detail="Farm not found")

    # Reverse old stock moves
    for item in delivery.items:
        product = db.query(Product).filter(Product.id == item.product_id).first()
        if product:
            before = float(product.stock)
            after  = before - float(item.qty)
            product.stock = after
            db.add(StockMove(
                product_id=product.id, type="out",
                user_id=current_user.id,
                qty=-float(item.qty), qty_before=before, qty_after=after,
                ref_type="farm_intake_reversal", ref_id=delivery.id,
                note=f"Edit reversal — {delivery.delivery_number}",
            ))
        db.delete(item)

    # Update delivery header
    delivery.farm_id       = data.farm_id
    delivery.user_id       = current_user.id
    delivery.delivery_date = date.fromisoformat(data.delivery_date)
    delivery.received_by   = data.received_by
    delivery.quality_notes = data.quality_notes
    delivery.notes         = data.notes

    # Apply new items
    for item in data.items:
        product = db.query(Product).filter(Product.id == item.product_id).first()
        if not product:
            raise HTTPException(status_code=404, detail=f"Product not found: {item.product_id}")
        before = float(product.stock)
        after  = before + item.qty
        product.stock = after
        db.add(FarmDeliveryItem(
            delivery_id=delivery.id,
            product_id=product.id,
            qty=item.qty,
            unit=product.unit,
            notes=item.notes,
        ))
        db.add(StockMove(
            product_id=product.id, type="in",
            user_id=current_user.id,
            qty=item.qty, qty_before=before, qty_after=after,
            ref_type="farm_intake", ref_id=delivery.id,
            note=f"{farm.name} — {delivery.delivery_number} (edited)",
        ))

    db.commit()
    return {"ok": True, "delivery_number": delivery.delivery_number}

@router.delete("/api/deliveries/{delivery_id}")
def delete_delivery(delivery_id: int, db: Session = Depends(get_db)):
    delivery = db.query(FarmDelivery).filter(FarmDelivery.id == delivery_id).first()
    if not delivery:
        raise HTTPException(status_code=404, detail="Delivery not found")
    for item in delivery.items:
        product = db.query(Product).filter(Product.id == item.product_id).first()
        if product:
            before = float(product.stock)
            after  = before - float(item.qty)
            product.stock = after
            db.add(StockMove(
                product_id=product.id, type="out",
                qty=-float(item.qty), qty_before=before, qty_after=after,
                ref_type="farm_intake_reversal", ref_id=delivery.id,
                note=f"Deleted — {delivery.delivery_number}",
            ))
    db.delete(delivery)
    db.commit()
    return {"ok": True}


# ── STATS API ──────────────────────────────────────────
@router.get("/api/stats")
def get_stats(db: Session = Depends(get_db)):
    from datetime import datetime
    now         = datetime.utcnow()
    month_start = date(now.year, now.month, 1)
    return {
        "total_farms":      db.query(func.count(Farm.id)).filter(Farm.is_active == 1).scalar() or 0,
        "total_deliveries": db.query(func.count(FarmDelivery.id)).scalar() or 0,
        "this_month":       db.query(func.count(FarmDelivery.id)).filter(FarmDelivery.delivery_date >= month_start).scalar() or 0,
    }

@router.get("/api/products-list")
def products_list(db: Session = Depends(get_db)):
    products = db.query(Product).filter(Product.is_active == True).order_by(Product.name).all()
    return [{"id": p.id, "name": p.name, "sku": p.sku, "stock": float(p.stock), "unit": p.unit} for p in products]


# ── UI ─────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
def farm_ui():
    return """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Farm Intake — Thunder ERP</title>
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
body::before{content:'';position:fixed;inset:0;background:radial-gradient(ellipse 700px 500px at 10% 20%,rgba(132,204,22,.04) 0%,transparent 70%),radial-gradient(ellipse 500px 600px at 90% 80%,rgba(0,255,157,.03) 0%,transparent 70%);pointer-events:none;z-index:0;}
body>*{position:relative;z-index:1;}
nav{position:sticky;top:0;z-index:100;display:flex;align-items:center;gap:8px;padding:0 24px;height:58px;background:rgba(10,13,24,.92);backdrop-filter:blur(20px);border-bottom:1px solid var(--border);flex-wrap:wrap;}
.logo{font-size:17px;font-weight:900;background:linear-gradient(135deg,var(--green),var(--blue));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;margin-right:10px;text-decoration:none;display:flex;align-items:center;gap:8px;}
.nav-link{padding:7px 12px;border-radius:8px;color:var(--sub);font-size:12px;font-weight:600;text-decoration:none;transition:all .2s;white-space:nowrap;}
.nav-link:hover{background:rgba(255,255,255,.05);color:var(--text);}
.nav-link.active{background:rgba(132,204,22,.1);color:var(--lime);}
.nav-spacer{flex:1;}
.content{max-width:1300px;margin:0 auto;padding:28px 24px;display:flex;flex-direction:column;gap:20px;}
.page-title{font-size:24px;font-weight:800;letter-spacing:-.5px;}
.page-sub{color:var(--muted);font-size:13px;margin-top:3px;}
.farms-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px;}
.farm-card{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:20px;position:relative;overflow:hidden;cursor:pointer;transition:border-color .2s,box-shadow .2s;}
.farm-card::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;}
.farm-card:nth-child(1)::before{background:linear-gradient(90deg,var(--green),var(--lime));}
.farm-card:nth-child(2)::before{background:linear-gradient(90deg,var(--teal),var(--blue));}
.farm-card:hover{border-color:var(--border2);box-shadow:0 8px 24px rgba(0,0,0,.3);}
.farm-name{font-size:17px;font-weight:800;margin-bottom:4px;}
.farm-loc{font-size:12px;color:var(--muted);margin-bottom:14px;}
.farm-stat{display:flex;justify-content:space-between;font-size:12px;padding:6px 0;border-top:1px solid var(--border);}
.farm-stat-label{color:var(--muted);}
.farm-stat-val{font-family:var(--mono);color:var(--green);font-weight:700;}
.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;}
.stat-card{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:16px 18px;display:flex;flex-direction:column;gap:6px;position:relative;overflow:hidden;}
.stat-card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;}
.stat-card.green::before{background:linear-gradient(90deg,var(--green),transparent);}
.stat-card.lime::before {background:linear-gradient(90deg,var(--lime),transparent);}
.stat-card.teal::before {background:linear-gradient(90deg,var(--teal),transparent);}
.stat-label{font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);}
.stat-value{font-family:var(--mono);font-size:24px;font-weight:700;}
.stat-value.green{color:var(--green);}
.stat-value.lime {color:var(--lime);}
.stat-value.teal {color:var(--teal);}
.tabs{display:flex;gap:4px;background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:4px;}
.tab{padding:8px 18px;border-radius:9px;font-size:13px;font-weight:700;cursor:pointer;border:none;background:transparent;color:var(--muted);transition:all .2s;font-family:var(--sans);}
.tab.active{background:var(--card2);color:var(--text);}
.btn{display:flex;align-items:center;gap:7px;padding:10px 16px;border-radius:var(--r);font-family:var(--sans);font-size:13px;font-weight:700;cursor:pointer;border:none;transition:all .2s;white-space:nowrap;}
.btn-lime{background:linear-gradient(135deg,var(--lime),var(--green));color:#0a1a00;}
.btn-lime:hover{filter:brightness(1.1);transform:translateY(-1px);}
.toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;}
.filter-sel{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:10px 14px;color:var(--text);font-family:var(--sans);font-size:13px;outline:none;}
.table-wrap{background:var(--card);border:1px solid var(--border);border-radius:var(--r);overflow:hidden;}
table{width:100%;border-collapse:collapse;}
thead{background:var(--card2);}
th{text-align:left;font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);padding:12px 16px;}
td{padding:12px 16px;border-top:1px solid var(--border);color:var(--sub);font-size:13px;}
tr.expandable{cursor:pointer;}
tr.expandable:hover td{background:rgba(255,255,255,.02);}
td.name{color:var(--text);font-weight:600;}
.farm-badge{display:inline-flex;padding:2px 9px;border-radius:20px;font-size:11px;font-weight:700;}
.farm-organic    {background:rgba(132,204,22,.1);color:var(--lime);}
.farm-regenerative{background:rgba(45,212,191,.1);color:var(--teal);}
.delivery-detail{background:var(--card2);border-top:1px solid var(--border);padding:14px 16px;display:none;}
.delivery-detail.open{display:block;}
.detail-items{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:8px;}
.detail-item{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:10px 12px;}
.detail-item-name{font-weight:600;font-size:13px;margin-bottom:4px;}
.detail-item-qty{font-family:var(--mono);color:var(--green);font-size:15px;font-weight:700;}
.detail-item-unit{color:var(--muted);font-size:11px;}
.detail-item-note{color:var(--muted);font-size:11px;margin-top:4px;font-style:italic;}
.action-btn{background:transparent;border:1px solid var(--border2);color:var(--sub);font-size:12px;font-weight:600;padding:5px 10px;border-radius:7px;cursor:pointer;transition:all .15s;font-family:var(--sans);}
.action-btn:hover      {border-color:var(--blue);  color:var(--blue);}
.action-btn.danger:hover{border-color:var(--danger);color:var(--danger);}
.pagination{display:flex;align-items:center;justify-content:space-between;padding:14px 16px;border-top:1px solid var(--border);font-size:13px;color:var(--muted);}
.page-btns{display:flex;gap:6px;}
.page-btn{background:var(--card2);border:1px solid var(--border2);color:var(--sub);font-family:var(--sans);font-size:12px;padding:6px 12px;border-radius:7px;cursor:pointer;transition:all .15s;}
.page-btn:hover{border-color:var(--lime);color:var(--lime);}
.page-btn:disabled{opacity:.3;cursor:not-allowed;}
.modal-bg{position:fixed;inset:0;z-index:500;background:rgba(0,0,0,.75);backdrop-filter:blur(4px);display:none;align-items:center;justify-content:center;}
.modal-bg.open{display:flex;}
.modal{background:var(--card);border:1px solid var(--border2);border-radius:16px;padding:28px;width:660px;max-width:95vw;max-height:90vh;overflow-y:auto;animation:modalIn .2s ease;}
@keyframes modalIn{from{opacity:0;transform:scale(.95)}to{opacity:1;transform:scale(1)}}
.modal-title{font-size:18px;font-weight:800;margin-bottom:4px;}
.modal-sub{font-size:13px;color:var(--muted);margin-bottom:20px;}
.fld{display:flex;flex-direction:column;gap:6px;margin-bottom:14px;}
.fld label{font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);}
.fld input,.fld select,.fld textarea{background:var(--card2);border:1px solid var(--border2);border-radius:10px;padding:10px 12px;color:var(--text);font-family:var(--sans);font-size:14px;outline:none;transition:border-color .2s;width:100%;}
.fld input:focus,.fld select:focus,.fld textarea:focus{border-color:rgba(132,204,22,.4);}
.fld textarea{resize:vertical;min-height:70px;}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:12px;}
.fld.span2{grid-column:span 2;}
.modal-actions{display:flex;gap:10px;margin-top:8px;justify-content:flex-end;}
.btn-cancel{background:transparent;border:1px solid var(--border2);color:var(--sub);padding:10px 18px;border-radius:var(--r);font-family:var(--sans);font-size:13px;font-weight:700;cursor:pointer;}
.btn-cancel:hover{border-color:var(--danger);color:var(--danger);}
.farm-selector{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px;}
.farm-opt{background:var(--card2);border:2px solid var(--border2);border-radius:10px;padding:14px;cursor:pointer;text-align:center;transition:all .2s;}
.farm-opt:hover{border-color:var(--lime);}
.farm-opt.selected{border-color:var(--lime);background:rgba(132,204,22,.08);}
.farm-opt.selected.regen{border-color:var(--teal);background:rgba(45,212,191,.08);}
.farm-opt-icon{font-size:22px;margin-bottom:6px;}
.farm-opt-name{font-size:13px;font-weight:700;color:var(--text);}
.farm-opt-loc{font-size:10px;color:var(--muted);margin-top:2px;}
.item-row{display:grid;grid-template-columns:1.5fr 90px 60px 1fr 30px;gap:8px;align-items:center;margin-bottom:8px;}
.item-row input,.item-row select{background:var(--card2);border:1px solid var(--border2);border-radius:8px;padding:8px 10px;color:var(--text);font-family:var(--sans);font-size:13px;outline:none;width:100%;}
.item-row input:focus{border-color:rgba(132,204,22,.4);}
.unit-label{font-size:11px;color:var(--muted);font-family:var(--mono);text-align:center;}
.rm-btn{background:none;border:none;color:var(--muted);font-size:18px;cursor:pointer;padding:0;transition:color .15s;}
.rm-btn:hover{color:var(--danger);}
.add-item-btn{border:1px dashed rgba(132,204,22,.3);color:var(--lime);font-family:var(--sans);font-size:13px;font-weight:600;padding:8px;border-radius:8px;cursor:pointer;width:100%;transition:all .2s;margin-bottom:14px;background:transparent;}
.add-item-btn:hover{background:rgba(132,204,22,.08);}
/* SEARCH DROPDOWN */
.prod-search-wrap{position:relative;}
.prod-search-input{background:var(--card2);border:1px solid var(--border2);border-radius:8px;padding:8px 10px;color:var(--text);font-family:var(--sans);font-size:13px;outline:none;width:100%;}
.prod-search-input:focus{border-color:rgba(132,204,22,.4);}
.prod-dropdown{display:none;position:absolute;top:100%;left:0;right:0;background:var(--card);border:1px solid var(--border2);border-radius:8px;z-index:200;max-height:200px;overflow-y:auto;margin-top:4px;box-shadow:0 8px 24px rgba(0,0,0,.5);}
.prod-option{padding:10px 12px;cursor:pointer;border-bottom:1px solid var(--border);font-size:13px;}
.prod-option:last-child{border-bottom:none;}
.prod-option:hover{background:rgba(255,255,255,.05);}
.prod-option-name{font-weight:600;color:var(--text);}
.prod-option-meta{font-size:11px;color:var(--muted);margin-top:2px;}
/* HISTORY */
.history-section{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:20px;margin-bottom:14px;}
.history-title{font-size:14px;font-weight:700;margin-bottom:16px;display:flex;align-items:center;gap:10px;}
.history-bar-row{display:flex;align-items:center;gap:12px;margin-bottom:10px;}
.history-bar-label{font-size:12px;color:var(--sub);width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.history-bar-track{flex:1;background:var(--card2);border-radius:4px;height:8px;overflow:hidden;}
.history-bar-fill{height:100%;border-radius:4px;transition:width .6s ease;}
.history-bar-val{font-family:var(--mono);font-size:12px;color:var(--green);width:60px;text-align:right;}
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
    <a href="/farm/"      class="nav-link active">Farm Intake</a>
    <a href="/inventory/" class="nav-link">Inventory</a>
    <a href="/production/"class="nav-link">Production</a>
    <a href="/b2b/"       class="nav-link">B2B</a>
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
        <div class="page-title">🌾 Farm Intake</div>
        <div class="page-sub">Receive crops from your farms — stock updates automatically</div>
    </div>

    <div class="farms-row" id="farms-row">
        <div style="color:var(--muted);padding:20px">Loading farms...</div>
    </div>

    <div class="stats-grid">
        <div class="stat-card green"><div class="stat-label">Total Farms</div><div class="stat-value green" id="stat-farms">—</div></div>
        <div class="stat-card lime"><div class="stat-label">Total Deliveries</div><div class="stat-value lime" id="stat-total">—</div></div>
        <div class="stat-card teal"><div class="stat-label">This Month</div><div class="stat-value teal" id="stat-month">—</div></div>
    </div>

    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;">
        <div class="tabs">
            <button class="tab active" id="tab-deliveries" onclick="switchTab('deliveries')">Deliveries</button>
            <button class="tab"        id="tab-history"    onclick="switchTab('history')">Farm History</button>
        </div>
        <button class="btn btn-lime" onclick="openDeliveryModal()">+ Record Delivery</button>
    </div>

    <!-- DELIVERIES -->
    <div id="section-deliveries">
        <div class="toolbar">
            <select class="filter-sel" id="farm-filter" onchange="loadDeliveries()">
                <option value="">All Farms</option>
            </select>
        </div>
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>Delivery #</th>
                        <th>Farm</th>
                        <th>Date</th>
                        <th>Received By</th>
                        <th>Items</th>
                        <th>Total Qty</th>
                        <th>Quality Notes</th>
                        <th></th>
                    </tr>
                </thead>
                <tbody id="deliveries-body">
                    <tr><td colspan="8" style="text-align:center;color:var(--muted);padding:40px">Loading...</td></tr>
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

    <!-- HISTORY -->
    <div id="section-history" style="display:none">
        <div id="history-content">
            <div style="color:var(--muted);padding:40px;text-align:center">Loading...</div>
        </div>
    </div>
</div>

<!-- DELIVERY MODAL -->
<div class="modal-bg" id="delivery-modal">
    <div class="modal">
        <div class="modal-title" id="modal-title">Record Farm Delivery</div>
        <div class="modal-sub">Stock will be updated automatically on save</div>

        <div style="font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-bottom:10px">Select Farm *</div>
        <div class="farm-selector" id="farm-selector"></div>

        <div class="form-row">
            <div class="fld">
                <label>Delivery Date *</label>
                <input id="d-date" type="date">
            </div>
            <div class="fld">
                <label>Received By</label>
                <input id="d-receiver" placeholder="Your name">
            </div>
            <div class="fld span2">
                <label>Quality Notes</label>
                <textarea id="d-quality" placeholder="e.g. Fresh, good condition. Some wilting on kale."></textarea>
            </div>
        </div>

        <div style="font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-bottom:8px">Products Received</div>
        <div style="display:grid;grid-template-columns:1.5fr 90px 60px 1fr 30px;gap:8px;margin-bottom:6px;">
            <span style="font-size:10px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:1px">Product</span>
            <span style="font-size:10px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:1px">Qty</span>
            <span style="font-size:10px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:1px">Unit</span>
            <span style="font-size:10px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:1px">Note</span>
            <span></span>
        </div>
        <div id="delivery-items"></div>
        <button class="add-item-btn" onclick="addDeliveryItem()">+ Add Product</button>

        <div class="fld">
            <label>General Notes</label>
            <input id="d-notes" placeholder="Any other notes about this delivery">
        </div>

        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeDeliveryModal()">Cancel</button>
            <button class="btn btn-lime" id="save-btn" onclick="saveDelivery()">✓ Save Delivery & Update Stock</button>
        </div>
    </div>
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
  const __erpFetch = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
      const url = typeof input === "string" ? input : (input && input.url) || "";
      const isRelativeUrl = typeof url === "string" && !(url.startsWith("http://") || url.startsWith("https://"));
      const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
      if(__erpToken && isRelativeUrl && !headers.has("Authorization")){
          headers.set("Authorization", "Bearer " + __erpToken);
      }
      return __erpFetch(input, {...init, headers});
  };
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
  requirePageAccess("page_farm");
  applyNavPermissions();
  initializeColorMode();
  setUserInfo();
  let allProducts     = [];
let allFarms        = [];
let selectedFarmId  = null;
let editingDeliveryId = null;   // null = creating new, number = editing existing
let deliveryPage    = 0;
let pageSize        = 20;
let totalDeliveries = 0;
let isAdmin         = localStorage.getItem("user_role") === "admin";

async function init(){
    await fetch("/farm/api/seed-farms", {method:"POST"});
    allProducts = await (await fetch("/farm/api/products-list")).json();
    allFarms    = await (await fetch("/farm/api/farms")).json();
    renderFarmCards();
    fillFarmFilter();
    await loadStats();
    await loadDeliveries();
}

/* ── FARMS ── */
function renderFarmCards(){
    document.getElementById("farms-row").innerHTML = allFarms.map((f,i)=>`
        <div class="farm-card" onclick="filterByFarm(${f.id})">
            <div class="farm-name">${i===0?"🌿":"♻️"} ${f.name}</div>
            <div class="farm-loc">${f.location}</div>
            <div class="farm-stat">
                <span class="farm-stat-label">Total Deliveries</span>
                <span class="farm-stat-val">${f.delivery_count}</span>
            </div>
        </div>`).join("") || `<div style="color:var(--muted)">No farms found.</div>`;

    document.getElementById("farm-selector").innerHTML = allFarms.map((f,i)=>`
        <div class="farm-opt ${i===0?"":"regen"}" id="farm-opt-${f.id}" onclick="selectFarm(${f.id})">
            <div class="farm-opt-icon">${i===0?"🌿":"♻️"}</div>
            <div class="farm-opt-name">${f.name}</div>
            <div class="farm-opt-loc">${f.location}</div>
        </div>`).join("");
}

function fillFarmFilter(){
    document.getElementById("farm-filter").innerHTML =
        `<option value="">All Farms</option>` +
        allFarms.map(f=>`<option value="${f.id}">${f.name}</option>`).join("");
}

function filterByFarm(id){
    document.getElementById("farm-filter").value = id;
    deliveryPage = 0;
    loadDeliveries();
    switchTab("deliveries");
}

function selectFarm(id){
    selectedFarmId = id;
    allFarms.forEach(f=>{
        let el = document.getElementById("farm-opt-"+f.id);
        if(el) el.classList.toggle("selected", f.id===id);
    });
}

/* ── STATS ── */
async function loadStats(){
    let d = await (await fetch("/farm/api/stats")).json();
    document.getElementById("stat-farms").innerText = d.total_farms;
    document.getElementById("stat-total").innerText = d.total_deliveries;
    document.getElementById("stat-month").innerText = d.this_month;
}

/* ── TABS ── */
function switchTab(tab){
    document.getElementById("section-deliveries").style.display = tab==="deliveries"?"":"none";
    document.getElementById("section-history").style.display    = tab==="history"?"":"none";
    document.getElementById("tab-deliveries").classList.toggle("active", tab==="deliveries");
    document.getElementById("tab-history").classList.toggle("active",    tab==="history");
    if(tab==="history") loadHistory();
}

/* ── DELIVERIES TABLE ── */
async function loadDeliveries(){
    let farmId = document.getElementById("farm-filter").value;
    let url    = `/farm/api/deliveries?skip=${deliveryPage*pageSize}&limit=${pageSize}`;
    if(farmId) url += `&farm_id=${farmId}`;

    let data = await (await fetch(url)).json();
    totalDeliveries = data.total;

    document.getElementById("page-info").innerText =
        totalDeliveries === 0 ? "No deliveries" :
        `${deliveryPage*pageSize+1}–${Math.min((deliveryPage+1)*pageSize,totalDeliveries)} of ${totalDeliveries}`;
    document.getElementById("prev-btn").disabled = deliveryPage===0;
    document.getElementById("next-btn").disabled = (deliveryPage+1)*pageSize>=totalDeliveries;

    if(!data.deliveries.length){
        document.getElementById("deliveries-body").innerHTML=
            `<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:60px">No deliveries yet. Click <b>+ Record Delivery</b>.</td></tr>`;
        return;
    }

    let html="";
    data.deliveries.forEach(d=>{
        let farmIdx = allFarms.findIndex(f=>f.id===d.farm_id);
        let farmCls = farmIdx===0?"farm-organic":"farm-regenerative";
        let adminBtns = isAdmin
            ? `<div style="display:flex;gap:6px">
                <button class="action-btn" onclick="event.stopPropagation();openEditDelivery(${d.id})">Edit</button>
                <button class="action-btn danger" onclick="event.stopPropagation();deleteDelivery(${d.id},'${d.delivery_number}')">Delete</button>
               </div>`
            : `<span></span>`;

        html+=`
        <tr class="expandable" onclick="toggleDetail('det-${d.id}')">
            <td style="font-family:var(--mono);font-size:12px;color:var(--lime)">${d.delivery_number}</td>
            <td><span class="farm-badge ${farmCls}">${d.farm}</span></td>
            <td style="font-family:var(--mono);font-size:12px">${d.delivery_date}</td>
            <td style="font-size:12px">${d.received_by}</td>
            <td style="font-family:var(--mono);color:var(--blue)">${d.total_items}</td>
            <td style="font-family:var(--mono);color:var(--green);font-weight:700">${d.total_qty.toFixed(1)}</td>
            <td style="font-size:12px;color:var(--muted);max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${d.quality_notes||"—"}</td>
            <td>${adminBtns}</td>
        </tr>
        <tr><td colspan="8" style="padding:0;border:none">
            <div class="delivery-detail" id="det-${d.id}">
                ${d.quality_notes?`<div style="font-size:12px;color:var(--muted);margin-bottom:12px;padding:8px 12px;background:rgba(132,204,22,.05);border-radius:8px;border-left:3px solid var(--lime)">Quality: ${d.quality_notes}</div>`:""}
                <div class="detail-items">
                    ${d.items.map(item=>`
                        <div class="detail-item">
                            <div class="detail-item-name">${item.product}</div>
                            <div>
                                <span class="detail-item-qty">${item.qty.toFixed(2)}</span>
                                <span class="detail-item-unit"> ${item.unit}</span>
                            </div>
                            ${item.notes?`<div class="detail-item-note">${item.notes}</div>`:""}
                        </div>`).join("")}
                </div>
                ${d.notes?`<div style="font-size:12px;color:var(--muted);margin-top:10px">${d.notes}</div>`:""}
            </div>
        </td></tr>`;
    });
    document.getElementById("deliveries-body").innerHTML = html;
}

function toggleDetail(id){
    let el = document.getElementById(id);
    if(el) el.classList.toggle("open");
}

function prevPage(){ if(deliveryPage>0){ deliveryPage--; loadDeliveries(); } }
function nextPage(){ if((deliveryPage+1)*pageSize<totalDeliveries){ deliveryPage++; loadDeliveries(); } }

/* ── HISTORY ── */
async function loadHistory(){
    let all = await (await fetch("/farm/api/deliveries?limit=500")).json();
    let byFarm = {};
    allFarms.forEach(f=>{ byFarm[f.id]={name:f.name, deliveries:[], products:{}}; });
    all.deliveries.forEach(d=>{
        if(byFarm[d.farm_id]){
            byFarm[d.farm_id].deliveries.push(d);
            d.items.forEach(item=>{
                byFarm[d.farm_id].products[item.product] = (byFarm[d.farm_id].products[item.product]||0) + item.qty;
            });
        }
    });
    document.getElementById("history-content").innerHTML = Object.values(byFarm).map((farm,fi)=>{
        let products = Object.entries(farm.products).sort((a,b)=>b[1]-a[1]);
        let maxQty   = products.length ? products[0][1] : 1;
        let color    = fi===0?"var(--lime)":"var(--teal)";
        return `
        <div class="history-section">
            <div class="history-title">
                <span style="font-size:20px">${fi===0?"🌿":"♻️"}</span>
                <span>${farm.name}</span>
                <span style="font-size:12px;color:var(--muted);font-weight:400">${farm.deliveries.length} deliveries</span>
            </div>
            ${products.length===0
                ? `<div style="color:var(--muted);font-size:13px">No deliveries recorded yet.</div>`
                : products.map(([name,qty])=>`
                    <div class="history-bar-row">
                        <div class="history-bar-label">${name}</div>
                        <div class="history-bar-track">
                            <div class="history-bar-fill" style="width:${(qty/maxQty*100).toFixed(1)}%;background:linear-gradient(90deg,${color},var(--green))"></div>
                        </div>
                        <div class="history-bar-val">${qty.toFixed(1)}</div>
                    </div>`).join("")}
        </div>`;
    }).join("");
}

/* ── DELIVERY MODAL ── */
function openDeliveryModal(){
    // Reset to CREATE mode
    editingDeliveryId = null;
    document.getElementById("modal-title").innerText = "Record Farm Delivery";
    document.getElementById("save-btn").innerText    = "✓ Save Delivery & Update Stock";

    document.getElementById("delivery-items").innerHTML = "";
    document.getElementById("d-date").value     = new Date().toISOString().split("T")[0];
    document.getElementById("d-receiver").value = "";
    document.getElementById("d-quality").value  = "";
    document.getElementById("d-notes").value    = "";

    if(allFarms.length) selectFarm(allFarms[0].id);
    addDeliveryItem();
    document.getElementById("delivery-modal").classList.add("open");
}

function closeDeliveryModal(){
    editingDeliveryId = null;
    document.getElementById("modal-title").innerText = "Record Farm Delivery";
    document.getElementById("save-btn").innerText    = "✓ Save Delivery & Update Stock";
    document.getElementById("delivery-modal").classList.remove("open");
}

/* ── EDIT DELIVERY ── */
async function openEditDelivery(id){
    // Load this specific delivery
    let data = await (await fetch(`/farm/api/deliveries?limit=1000`)).json();
    let d    = data.deliveries.find(x=>x.id===id);
    if(!d){ showToast("Could not load delivery"); return; }

    // Switch to EDIT mode
    editingDeliveryId = id;
    document.getElementById("modal-title").innerText = `Edit Delivery — ${d.delivery_number}`;
    document.getElementById("save-btn").innerText    = "✓ Save Changes & Reverse/Reapply Stock";

    // Fill header fields
    selectFarm(d.farm_id);
    document.getElementById("d-date").value     = d.delivery_date;
    document.getElementById("d-receiver").value = d.received_by === "—" ? "" : d.received_by;
    document.getElementById("d-quality").value  = d.quality_notes;
    document.getElementById("d-notes").value    = d.notes;

    // Fill items
    document.getElementById("delivery-items").innerHTML = "";
    d.items.forEach(item=>{
        addDeliveryItem();
        let rows = document.querySelectorAll("#delivery-items .item-row");
        let row  = rows[rows.length - 1];
        // Set product
        row.dataset.productId = item.product_id;
        row.querySelector(".prod-search-input").value = item.product;
        row.querySelector(".unit-label").innerText    = item.unit;
        // Set qty
        row.querySelectorAll("input")[1].value = item.qty;
        // Set note
        if(row.querySelectorAll("input")[2]) row.querySelectorAll("input")[2].value = item.notes || "";
    });

    document.getElementById("delivery-modal").classList.add("open");
}

/* ── DELETE DELIVERY ── */
async function deleteDelivery(id, number){
    if(!confirm(`Delete ${number}? This will reverse all stock changes. This cannot be undone.`)) return;
    let res  = await fetch(`/farm/api/deliveries/${id}`, {method:"DELETE"});
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    showToast(`${number} deleted — stock reversed ✓`);
    await refreshAll();
}

/* ── SAVE (create or edit) ── */
async function saveDelivery(){
    if(!selectedFarmId){ showToast("Select a farm first"); return; }

    let rows  = document.querySelectorAll("#delivery-items .item-row");
    let items = [];
    for(let row of rows){
        let product_id = parseInt(row.dataset.productId);
        let qty        = parseFloat(row.querySelectorAll("input")[1].value)||0;
        let notes      = row.querySelectorAll("input")[2] ? row.querySelectorAll("input")[2].value.trim()||null : null;
        if(!product_id){ showToast("Select a product for all rows"); return; }
        if(qty <= 0)   { showToast("Quantity must be greater than 0"); return; }
        items.push({product_id, qty, notes});
    }
    if(!items.length){ showToast("Add at least one product"); return; }

    let body = {
        farm_id:       selectedFarmId,
        delivery_date: document.getElementById("d-date").value,
        received_by:   document.getElementById("d-receiver").value.trim()||null,
        quality_notes: document.getElementById("d-quality").value.trim()||null,
        notes:         document.getElementById("d-notes").value.trim()||null,
        items,
    };

    // If editingDeliveryId is set → PUT (edit), otherwise → POST (create)
    let url    = editingDeliveryId ? `/farm/api/deliveries/${editingDeliveryId}` : "/farm/api/deliveries";
    let method = editingDeliveryId ? "PUT" : "POST";

    let res  = await fetch(url, {method, headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }

    closeDeliveryModal();
    let msg = editingDeliveryId
        ? `${data.delivery_number} updated ✓ — stock reversed & reapplied`
        : `${data.delivery_number} saved ✓ — ${data.items_count} products added to stock`;
    showToast(msg);
    await refreshAll();
}

async function refreshAll(){
    allProducts = await (await fetch("/farm/api/products-list")).json();
    allFarms    = await (await fetch("/farm/api/farms")).json();
    renderFarmCards();
    loadDeliveries();
    loadStats();
}

/* ── PRODUCT SEARCH IN ITEMS ── */
function addDeliveryItem(){
    let div = document.createElement("div");
    div.className  = "item-row";
    div.dataset.productId = "";
    div.innerHTML  = `
        <div class="prod-search-wrap">
            <input type="text" class="prod-search-input" placeholder="Search by name or SKU..." autocomplete="off">
            <div class="prod-dropdown"></div>
        </div>
        <input type="number" placeholder="0" min="0.001" step="any">
        <span class="unit-label">—</span>
        <input type="text" placeholder="e.g. fresh, grade A">
        <button class="rm-btn" onclick="this.closest('.item-row').remove()">×</button>
    `;

    let searchInput = div.querySelector(".prod-search-input");
    let dropdown    = div.querySelector(".prod-dropdown");

    searchInput.addEventListener("input", ()=> showDropdown(searchInput, dropdown, div));
    searchInput.addEventListener("focus", ()=> showDropdown(searchInput, dropdown, div));

    // Close dropdown when clicking outside
    document.addEventListener("click", function(e){
        if(!div.contains(e.target)) dropdown.style.display = "none";
    });

    document.getElementById("delivery-items").appendChild(div);
}

function showDropdown(input, dropdown, row){
    let q = input.value.toLowerCase();
    let matches = allProducts.filter(p=>
        p.name.toLowerCase().includes(q) || p.sku.toLowerCase().includes(q)
    ).slice(0,10);

    if(!matches.length){
        dropdown.innerHTML = `<div style="padding:10px 12px;color:var(--muted);font-size:13px">No products found</div>`;
        dropdown.style.display = "block";
        return;
    }

    dropdown.innerHTML = matches.map(p=>`
        <div class="prod-option" data-id="${p.id}" data-name="${p.name}" data-unit="${p.unit}" data-stock="${p.stock}">
            <div class="prod-option-name">${p.name}</div>
            <div class="prod-option-meta">
                <span style="font-family:var(--mono)">${p.sku}</span>
                &nbsp;·&nbsp;
                <span style="color:var(--green)">${p.stock.toFixed(0)} ${p.unit} in stock</span>
            </div>
        </div>`).join("");

    dropdown.querySelectorAll(".prod-option").forEach(opt=>{
        opt.addEventListener("click", ()=>{
            input.value          = opt.dataset.name;
            row.dataset.productId= opt.dataset.id;
            row.querySelector(".unit-label").innerText = opt.dataset.unit;
            dropdown.style.display = "none";
            row.querySelectorAll("input")[1].focus();
        });
    });

    dropdown.style.display = "block";
}

let toastTimer=null;
function showToast(msg){
    let t=document.getElementById("toast");
    t.innerText=msg; t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer=setTimeout(()=>t.classList.remove("show"),4000);
}

document.getElementById("delivery-modal").addEventListener("click",function(e){
    if(e.target===this) closeDeliveryModal();
});

init();
</script>
</body>
</html>
"""


