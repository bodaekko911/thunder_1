from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import func, select
from typing import Optional, List
from pydantic import BaseModel
from decimal import Decimal
from datetime import date as date_type

from app.database import get_async_session
from app.core.permissions import get_current_user, require_permission
from app.core.log import record as log_record
from app.models.product import Product
from app.models.inventory import StockMove
from app.models.user import User
from app.models.production import (
    Recipe, RecipeInput, RecipeOutput,
    ProductionBatch, BatchInput, BatchOutput,
)
from app.models.spoilage import SpoilageRecord

router = APIRouter(
    prefix="/production",
    tags=["Production"],
    dependencies=[Depends(require_permission("page_production"))],
)


# ── Schemas ────────────────────────────────────────────
class RecipeItemIn(BaseModel):
    product_id: int
    qty:        float

class RecipeCreate(BaseModel):
    name:        str
    description: Optional[str] = None
    inputs:      List[RecipeItemIn]
    outputs:     List[RecipeItemIn]

class BatchItemIn(BaseModel):
    product_id: int
    qty:        float

class BatchCreate(BaseModel):
    recipe_id:  Optional[int] = None
    batch_type: str = "processing"
    waste_pct:  float = 0
    notes:      Optional[str] = None
    inputs:     List[BatchItemIn]
    outputs:    List[BatchItemIn]

class SpoilageCreate(BaseModel):
    product_id:    int
    qty:           float
    spoilage_date: str
    reason:        Optional[str] = None
    farm_id:       Optional[int] = None
    notes:         Optional[str] = None


# ── RECIPE API ─────────────────────────────────────────
@router.get("/api/recipes")
async def get_recipes(db: AsyncSession = Depends(get_async_session)):
    result = await db.execute(
        select(Recipe).where(Recipe.is_active == True).order_by(Recipe.name)
        .options(
            selectinload(Recipe.inputs).selectinload(RecipeInput.product),
            selectinload(Recipe.outputs).selectinload(RecipeOutput.product),
        )
    )
    recipes = result.scalars().all()
    return [
        {
            "id":          r.id,
            "name":        r.name,
            "description": r.description or "",
            "inputs":  [{"product_id": i.product_id, "product_name": i.product.name, "qty": float(i.qty), "unit": i.product.unit} for i in r.inputs],
            "outputs": [{"product_id": o.product_id, "product_name": o.product.name, "qty": float(o.qty), "unit": o.product.unit} for o in r.outputs],
        }
        for r in recipes
    ]

@router.post("/api/recipes")
async def create_recipe(data: RecipeCreate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    if not data.inputs or not data.outputs:
        raise HTTPException(status_code=400, detail="Recipe must have at least one input and one output")
    recipe = Recipe(name=data.name, description=data.description)
    db.add(recipe); await db.flush()
    for item in data.inputs:
        db.add(RecipeInput(recipe_id=recipe.id, product_id=item.product_id, qty=item.qty))
    for item in data.outputs:
        db.add(RecipeOutput(recipe_id=recipe.id, product_id=item.product_id, qty=item.qty))
    await db.commit(); await db.refresh(recipe)
    return {"id": recipe.id, "name": recipe.name}

@router.delete("/api/recipes/{recipe_id}")
async def delete_recipe(recipe_id: int, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    result = await db.execute(select(Recipe).where(Recipe.id == recipe_id))
    r = result.scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Recipe not found")
    r.is_active = False
    await db.commit()
    return {"ok": True}


# ── BATCH API ──────────────────────────────────────────
@router.get("/api/batches")
async def get_batches(skip: int = 0, limit: int = 50, db: AsyncSession = Depends(get_async_session)):
    cnt_result = await db.execute(select(func.count()).select_from(ProductionBatch))
    total = cnt_result.scalar()
    result = await db.execute(
        select(ProductionBatch)
        .options(
            selectinload(ProductionBatch.inputs).selectinload(BatchInput.product),
            selectinload(ProductionBatch.outputs).selectinload(BatchOutput.product),
            selectinload(ProductionBatch.recipe),
        )
        .order_by(ProductionBatch.created_at.desc()).offset(skip).limit(limit)
    )
    batches = result.scalars().all()
    return {
        "total": total,
        "batches": [
            {
                "id":           b.id,
                "batch_number": b.batch_number,
                "recipe":       b.recipe.name if b.recipe else "Custom",
                "recipe_id":    b.recipe_id,
                "status":       b.status,
                "waste_pct":    float(b.waste_pct),
                "notes":        b.notes or "",
                "created_at":   b.created_at.strftime("%Y-%m-%d %H:%M") if b.created_at else "—",
                "inputs":  [{"product": i.product.name, "product_id": i.product_id, "qty": float(i.qty), "unit": i.product.unit} for i in b.inputs],
                "outputs": [{"product": o.product.name, "product_id": o.product_id, "qty": float(o.qty), "unit": o.product.unit} for o in b.outputs],
            }
            for b in batches
        ],
    }

@router.post("/api/batches")
async def create_batch(data: BatchCreate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    if not data.inputs or not data.outputs:
        raise HTTPException(status_code=400, detail="Batch must have at least one input and one output")
    for item in data.inputs:
        prod_result = await db.execute(select(Product).where(Product.id == item.product_id))
        product = prod_result.scalar_one_or_none()
        if not product:
            raise HTTPException(status_code=404, detail=f"Product not found: {item.product_id}")
        if float(product.stock) < item.qty:
            raise HTTPException(status_code=400, detail=f"Not enough stock for '{product.name}'. Available: {float(product.stock)}")

    prefix = "PKG" if data.batch_type == "packaging" else "BATCH"
    max_id_result = await db.execute(select(func.max(ProductionBatch.id)))
    max_id = max_id_result.scalar() or 0
    batch_number = f"{prefix}-{str(max_id + 1).zfill(4)}"

    WEIGHT_UNITS = {"gram","g","kg","ltr","ml","liter","litre"}
    # Build a product-unit lookup from what we already fetched for stock check
    product_units: dict[int, str] = {}
    for item in data.inputs:
        pr = await db.execute(select(Product).where(Product.id == item.product_id))
        p = pr.scalar_one_or_none()
        if p:
            product_units[p.id] = p.unit
    for item in data.outputs:
        pr = await db.execute(select(Product).where(Product.id == item.product_id))
        p = pr.scalar_one_or_none()
        if p:
            product_units[p.id] = p.unit

    if data.batch_type == "processing":
        def is_weight(pid):
            unit = product_units.get(pid, "")
            return unit.lower() in WEIGHT_UNITS
        total_in  = sum(i.qty for i in data.inputs  if is_weight(i.product_id))
        total_out = sum(o.qty for o in data.outputs if is_weight(o.product_id))
        auto_waste = round(((total_in - total_out) / total_in * 100), 2) if total_in > 0 else 0
    else:
        auto_waste = data.waste_pct

    batch = ProductionBatch(batch_number=batch_number, recipe_id=data.recipe_id, user_id=current_user.id, waste_pct=auto_waste, notes=data.notes, status="completed")
    db.add(batch); await db.flush()

    for item in data.inputs:
        prod_r = await db.execute(select(Product).where(Product.id == item.product_id))
        product = prod_r.scalar_one_or_none()
        before = float(product.stock); after = before - item.qty; product.stock = after
        db.add(BatchInput(batch_id=batch.id, product_id=product.id, qty=item.qty))
        db.add(StockMove(product_id=product.id, type="out", user_id=current_user.id, qty=-item.qty, qty_before=before, qty_after=after, ref_type="production", ref_id=batch.id, note=f"Used in {batch_number}"))

    for item in data.outputs:
        prod_r = await db.execute(select(Product).where(Product.id == item.product_id))
        product = prod_r.scalar_one_or_none()
        if not product:
            raise HTTPException(status_code=404, detail=f"Output product not found: {item.product_id}")
        before = float(product.stock); after = before + item.qty; product.stock = after
        db.add(BatchOutput(batch_id=batch.id, product_id=product.id, qty=item.qty))
        db.add(StockMove(product_id=product.id, type="in", user_id=current_user.id, qty=item.qty, qty_before=before, qty_after=after, ref_type="production", ref_id=batch.id, note=f"Produced in {batch_number}"))

    log_record(db, "Production", "create_batch",
           f"Batch {batch_number} — {len(data.inputs)} input(s), {len(data.outputs)} output(s), waste {float(batch.waste_pct):.1f}%",
           user=current_user, ref_type="production_batch", ref_id=batch.id)
    await db.commit(); await db.refresh(batch)
    return {"id": batch.id, "batch_number": batch_number, "waste_pct": float(batch.waste_pct)}


@router.put("/api/batches/{batch_id}")
async def edit_batch(batch_id: int, data: BatchCreate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    batch_result = await db.execute(
        select(ProductionBatch)
        .options(
            selectinload(ProductionBatch.inputs),
            selectinload(ProductionBatch.outputs),
        )
        .where(ProductionBatch.id == batch_id)
    )
    batch = batch_result.scalar_one_or_none()
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    for item in batch.inputs:
        prod_r = await db.execute(select(Product).where(Product.id == item.product_id))
        product = prod_r.scalar_one_or_none()
        if product:
            before = float(product.stock); after = before + float(item.qty); product.stock = after
            db.add(StockMove(product_id=product.id, type="in", user_id=current_user.id, qty=float(item.qty), qty_before=before, qty_after=after, ref_type="production_reversal", ref_id=batch.id, note=f"Edit reversal — {batch.batch_number}"))
        await db.delete(item)
    for item in batch.outputs:
        prod_r = await db.execute(select(Product).where(Product.id == item.product_id))
        product = prod_r.scalar_one_or_none()
        if product:
            before = float(product.stock); after = before - float(item.qty); product.stock = after
            db.add(StockMove(product_id=product.id, type="out", user_id=current_user.id, qty=-float(item.qty), qty_before=before, qty_after=after, ref_type="production_reversal", ref_id=batch.id, note=f"Edit reversal — {batch.batch_number}"))
        await db.delete(item)
    for item in data.inputs:
        prod_r = await db.execute(select(Product).where(Product.id == item.product_id))
        product = prod_r.scalar_one_or_none()
        if not product:
            raise HTTPException(status_code=404, detail=f"Product not found: {item.product_id}")
        if float(product.stock) < item.qty:
            raise HTTPException(status_code=400, detail=f"Not enough stock for '{product.name}'.")
    batch.recipe_id = data.recipe_id; batch.notes = data.notes; batch.user_id = current_user.id
    WEIGHT_UNITS = {"gram","g","kg","ltr","ml","liter","litre"}
    product_units2: dict[int, str] = {}
    for item in data.inputs:
        pr = await db.execute(select(Product).where(Product.id == item.product_id))
        p = pr.scalar_one_or_none()
        if p:
            product_units2[p.id] = p.unit
    for item in data.outputs:
        pr = await db.execute(select(Product).where(Product.id == item.product_id))
        p = pr.scalar_one_or_none()
        if p:
            product_units2[p.id] = p.unit
    def is_weight2(pid):
        return product_units2.get(pid, "").lower() in WEIGHT_UNITS
    total_in  = sum(i.qty for i in data.inputs  if is_weight2(i.product_id))
    total_out = sum(o.qty for o in data.outputs if is_weight2(o.product_id))
    batch.waste_pct = round(((total_in - total_out) / total_in * 100), 2) if total_in > 0 else 0
    for item in data.inputs:
        prod_r2 = await db.execute(select(Product).where(Product.id == item.product_id))
        product = prod_r2.scalar_one_or_none()
        before = float(product.stock); after = before - item.qty; product.stock = after
        db.add(BatchInput(batch_id=batch.id, product_id=product.id, qty=item.qty))
        db.add(StockMove(product_id=product.id, type="out", user_id=current_user.id, qty=-item.qty, qty_before=before, qty_after=after, ref_type="production", ref_id=batch.id, note=f"Used in {batch.batch_number} (edited)"))
    for item in data.outputs:
        prod_r2 = await db.execute(select(Product).where(Product.id == item.product_id))
        product = prod_r2.scalar_one_or_none()
        if not product:
            raise HTTPException(status_code=404, detail=f"Output product not found: {item.product_id}")
        before = float(product.stock); after = before + item.qty; product.stock = after
        db.add(BatchOutput(batch_id=batch.id, product_id=product.id, qty=item.qty))
        db.add(StockMove(product_id=product.id, type="in", user_id=current_user.id, qty=item.qty, qty_before=before, qty_after=after, ref_type="production", ref_id=batch.id, note=f"Produced in {batch.batch_number} (edited)"))
    log_record(db, "Production", "edit_batch",
           f"Edited batch {batch.batch_number} — waste {float(batch.waste_pct):.1f}%",
           user=current_user, ref_type="production_batch", ref_id=batch.id)
    await db.commit()
    return {"ok": True, "batch_number": batch.batch_number, "waste_pct": float(batch.waste_pct)}


@router.delete("/api/batches/{batch_id}")
async def delete_batch(batch_id: int, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    batch_result = await db.execute(
        select(ProductionBatch)
        .options(selectinload(ProductionBatch.inputs), selectinload(ProductionBatch.outputs))
        .where(ProductionBatch.id == batch_id)
    )
    batch = batch_result.scalar_one_or_none()
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    for item in batch.inputs:
        prod_r = await db.execute(select(Product).where(Product.id == item.product_id))
        product = prod_r.scalar_one_or_none()
        if product:
            before = float(product.stock); after = before + float(item.qty); product.stock = after
            db.add(StockMove(product_id=product.id, type="in", qty=float(item.qty), qty_before=before, qty_after=after, ref_type="production_reversal", ref_id=batch.id, note=f"Deleted batch — {batch.batch_number}"))
    for item in batch.outputs:
        prod_r = await db.execute(select(Product).where(Product.id == item.product_id))
        product = prod_r.scalar_one_or_none()
        if product:
            before = float(product.stock); after = before - float(item.qty); product.stock = after
            db.add(StockMove(product_id=product.id, type="out", qty=-float(item.qty), qty_before=before, qty_after=after, ref_type="production_reversal", ref_id=batch.id, note=f"Deleted batch — {batch.batch_number}"))
    log_record(db, "Production", "delete_batch",
           f"Deleted batch {batch.batch_number} — stock reversed",
           ref_type="production_batch", ref_id=batch_id)
    await db.delete(batch); await db.commit()
    return {"ok": True}


# ── SPOILAGE API ───────────────────────────────────────
@router.get("/api/spoilage")
async def get_spoilage(skip: int = 0, limit: int = 100, db: AsyncSession = Depends(get_async_session)):
    cnt_r = await db.execute(select(func.count()).select_from(SpoilageRecord))
    total = cnt_r.scalar()
    rec_r = await db.execute(
        select(SpoilageRecord)
        .options(selectinload(SpoilageRecord.product), selectinload(SpoilageRecord.farm))
        .order_by(SpoilageRecord.spoilage_date.desc(), SpoilageRecord.created_at.desc())
        .offset(skip).limit(limit)
    )
    records = rec_r.scalars().all()
    return {
        "total": total,
        "records": [
            {
                "id":            r.id,
                "ref_number":    r.ref_number,
                "product":       r.product.name if r.product else "—",
                "product_id":    r.product_id,
                "unit":          r.product.unit if r.product else "",
                "qty":           float(r.qty),
                "spoilage_date": str(r.spoilage_date),
                "reason":        r.reason or "—",
                "farm":          r.farm.name if r.farm else "—",
                "notes":         r.notes or "",
                "created_at":    r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "—",
            }
            for r in records
        ],
    }

@router.post("/api/spoilage")
async def create_spoilage(data: SpoilageCreate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    from app.models.accounting import Account, Journal, JournalEntry
    prod_r = await db.execute(select(Product).where(Product.id == data.product_id))
    product = prod_r.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    if float(product.stock) < data.qty:
        raise HTTPException(status_code=400, detail=f"Not enough stock. Available: {float(product.stock)} {product.unit}")
    max_id_r = await db.execute(select(func.max(SpoilageRecord.id)))
    max_id = max_id_r.scalar() or 0
    ref    = f"SPL-{str(max_id + 1).zfill(4)}"
    spoilage_rec = SpoilageRecord(
        ref_number=ref, product_id=data.product_id, qty=data.qty,
        user_id=current_user.id,
        spoilage_date=date_type.fromisoformat(data.spoilage_date),
        reason=data.reason, farm_id=data.farm_id, notes=data.notes,
    )
    db.add(spoilage_rec); await db.flush()
    before = float(product.stock); after = before - data.qty; product.stock = after
    db.add(StockMove(product_id=product.id, type="out", user_id=current_user.id, qty=-data.qty, qty_before=before, qty_after=after, ref_type="spoilage", ref_id=spoilage_rec.id, note=f"Spoilage — {ref}"))
    cost_per_unit = float(product.cost) if product.cost else 0
    loss_value    = round(data.qty * cost_per_unit, 2)
    if loss_value > 0:
        journal = Journal(ref_type="spoilage", description=f"Spoilage — {ref} — {product.name}", user_id=current_user.id)
        db.add(journal); await db.flush()
        for code, debit, credit in [("5600", loss_value, 0), ("1200", 0, loss_value)]:
            acc_r = await db.execute(select(Account).where(Account.code == code))
            acc = acc_r.scalar_one_or_none()
            if acc:
                db.add(JournalEntry(journal_id=journal.id, account_id=acc.id, debit=debit, credit=credit))
                acc.balance += Decimal(str(debit)) - Decimal(str(credit))
    log_record(db, "Production", "create_spoilage",
               f"Spoilage {ref} — {product.name} — qty: {data.qty}"
               + (f" — {data.reason}" if data.reason else ""),
               user=current_user, ref_type="spoilage", ref_id=spoilage_rec.id)
    await db.commit()
    return {"id": spoilage_rec.id, "ref_number": ref, "qty": data.qty, "product": product.name}

@router.delete("/api/spoilage/{record_id}")
async def delete_spoilage(record_id: int, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    rec_r = await db.execute(select(SpoilageRecord).where(SpoilageRecord.id == record_id))
    spoilage_rec = rec_r.scalar_one_or_none()
    if not spoilage_rec:
        raise HTTPException(status_code=404, detail="Record not found")
    prod_r = await db.execute(select(Product).where(Product.id == spoilage_rec.product_id))
    product = prod_r.scalar_one_or_none()
    if product:
        before = float(product.stock); after = before + float(spoilage_rec.qty); product.stock = after
        db.add(StockMove(product_id=product.id, type="in", qty=float(spoilage_rec.qty), qty_before=before, qty_after=after, ref_type="spoilage_reversal", ref_id=spoilage_rec.id, note=f"Spoilage deleted — {spoilage_rec.ref_number}"))
    log_record(db, "Production", "delete_spoilage",
               f"Deleted spoilage {spoilage_rec.ref_number} — stock restored",
               ref_type="spoilage", ref_id=record_id)
    await db.delete(spoilage_rec); await db.commit()
    return {"ok": True}


@router.get("/api/products-list")
async def products_list(db: AsyncSession = Depends(get_async_session)):
    prod_r = await db.execute(select(Product).where(Product.is_active == True).order_by(Product.name))
    products = prod_r.scalars().all()
    return [{"id": p.id, "sku": p.sku, "name": p.name, "stock": float(p.stock), "unit": p.unit} for p in products]

@router.get("/api/farms-list")
async def farms_list(db: AsyncSession = Depends(get_async_session)):
    from app.models.farm import Farm
    farm_r = await db.execute(select(Farm).where(Farm.is_active == 1))
    farms = farm_r.scalars().all()
    return [{"id": f.id, "name": f.name} for f in farms]


# ── UI ─────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
def production_ui():
    return """<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Production — Thunder ERP</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root{
    --bg:#060810;--surface:#0a0d18;--card:#0f1424;--card2:#151c30;
    --border:rgba(255,255,255,0.06);--border2:rgba(255,255,255,0.11);
    --green:#00ff9d;--blue:#4d9fff;--purple:#a855f7;--orange:#fb923c;--teal:#2dd4bf;
    --danger:#ff4d6d;--warn:#ffb547;--text:#f0f4ff;--sub:#8899bb;--muted:#445066;
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
.account-menu{position:relative;}
.user-pill{display:flex;align-items:center;gap:10px;background:var(--card);border:1px solid var(--border);border-radius:40px;padding:7px 16px 7px 10px;cursor:pointer;transition:all .2s;}
.user-pill:hover,.user-pill.open{border-color:var(--border2);}
.user-avatar{width:28px;height:28px;background:linear-gradient(135deg,#7ecb6f,#d4a256);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;color:#0a0c08;}
.user-name{font-size:13px;font-weight:500;color:var(--sub);}
.menu-caret{font-size:11px;color:var(--muted);}
.account-dropdown{position:absolute;right:0;top:calc(100% + 10px);min-width:220px;background:var(--card);border:1px solid var(--border2);border-radius:14px;padding:8px;box-shadow:0 24px 50px rgba(0,0,0,.35);display:none;z-index:500;}
.account-dropdown.open{display:block;}
.account-head{padding:10px 12px 8px;border-bottom:1px solid var(--border);margin-bottom:6px;}
.account-label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;}
.account-email{font-size:12px;color:var(--sub);margin-top:4px;word-break:break-word;}
.account-item{width:100%;display:flex;align-items:center;gap:10px;padding:10px 12px;border:none;background:transparent;border-radius:10px;color:var(--sub);font-family:var(--sans);font-size:13px;text-decoration:none;cursor:pointer;text-align:left;}
.account-item:hover{background:var(--card2);color:var(--text);}
.account-item.danger:hover{color:#c97a7a;}
.logout-btn{background:transparent;border:1px solid var(--border);color:var(--muted);font-family:var(--sans);font-size:12px;font-weight:500;padding:8px 16px;border-radius:8px;cursor:pointer;transition:all .2s;letter-spacing:.3px;}
.logout-btn:hover{border-color:#c97a7a;color:#c97a7a;}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
body{font-family:var(--sans);background:var(--bg);color:var(--text);min-height:100vh;font-size:14px;}
nav{position:sticky;top:0;z-index:100;display:flex;align-items:center;gap:8px;padding:0 24px;height:58px;background:rgba(10,13,24,.92);backdrop-filter:blur(20px);border-bottom:1px solid var(--border);flex-wrap:wrap;}
.logo{font-size:17px;font-weight:900;background:linear-gradient(135deg,var(--green),var(--blue));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;margin-right:10px;text-decoration:none;display:flex;align-items:center;gap:8px;}
.nav-link{padding:7px 12px;border-radius:8px;color:var(--sub);font-size:12px;font-weight:600;text-decoration:none;transition:all .2s;white-space:nowrap;}
.nav-link:hover{background:rgba(255,255,255,.05);color:var(--text);}
.nav-link.active{background:rgba(251,146,60,.12);color:var(--orange);}
.nav-spacer{flex:1;}
.content{max-width:1300px;margin:0 auto;padding:28px 24px;display:flex;flex-direction:column;gap:20px;}
.page-title{font-size:24px;font-weight:800;letter-spacing:-.5px;}
.page-sub{color:var(--muted);font-size:13px;margin-top:3px;}
.tabs{display:flex;gap:4px;background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:4px;flex-wrap:wrap;}
.tab{padding:8px 16px;border-radius:9px;font-size:13px;font-weight:700;cursor:pointer;border:none;background:transparent;color:var(--muted);transition:all .2s;font-family:var(--sans);}
.tab.active{background:var(--card2);color:var(--text);}
.btn{display:flex;align-items:center;gap:7px;padding:10px 16px;border-radius:var(--r);font-family:var(--sans);font-size:13px;font-weight:700;cursor:pointer;border:none;transition:all .2s;white-space:nowrap;}
.btn-orange{background:linear-gradient(135deg,var(--orange),#f59e0b);color:#1a0800;}
.btn-orange:hover{filter:brightness(1.1);transform:translateY(-1px);}
.btn-teal{background:linear-gradient(135deg,var(--teal),var(--blue));color:#001a18;}
.btn-teal:hover{filter:brightness(1.1);transform:translateY(-1px);}
.btn-blue{background:linear-gradient(135deg,var(--blue),var(--purple));color:white;}
.btn-blue:hover{filter:brightness(1.1);transform:translateY(-1px);}
.btn-danger{background:linear-gradient(135deg,var(--danger),#c0392b);color:white;}
.btn-danger:hover{filter:brightness(1.1);transform:translateY(-1px);}
.recipes-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:14px;}
.recipe-card{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:18px;display:flex;flex-direction:column;gap:12px;position:relative;overflow:hidden;}
.recipe-card.processing::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--orange),transparent);}
.recipe-card.packaging::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--teal),transparent);}
.recipe-name{font-size:15px;font-weight:800;}
.recipe-desc{font-size:12px;color:var(--muted);}
.type-badge{display:inline-flex;padding:2px 8px;border-radius:20px;font-size:10px;font-weight:700;width:fit-content;}
.badge-proc{background:rgba(251,146,60,.1);color:var(--orange);}
.badge-pkg{background:rgba(45,212,191,.1);color:var(--teal);}
.recipe-section{display:flex;flex-direction:column;gap:6px;}
.recipe-section-title{font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);}
.recipe-item{display:flex;justify-content:space-between;align-items:center;font-size:12px;padding:5px 8px;background:var(--card2);border-radius:7px;}
.recipe-actions{display:flex;gap:8px;flex-wrap:wrap;}
.action-btn{background:transparent;border:1px solid var(--border2);color:var(--sub);font-size:12px;font-weight:600;padding:6px 12px;border-radius:7px;cursor:pointer;transition:all .15s;font-family:var(--sans);}
.action-btn:hover{border-color:var(--orange);color:var(--orange);}
.action-btn.teal:hover{border-color:var(--teal);color:var(--teal);}
.action-btn.blue:hover{border-color:var(--blue);color:var(--blue);}
.action-btn.danger:hover{border-color:var(--danger);color:var(--danger);}
.table-wrap{background:var(--card);border:1px solid var(--border);border-radius:var(--r);overflow:hidden;}
table{width:100%;border-collapse:collapse;}
thead{background:var(--card2);}
th{text-align:left;font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);padding:12px 16px;}
td{padding:12px 16px;border-top:1px solid var(--border);color:var(--sub);font-size:13px;}
tr.batch-row{cursor:pointer;}
tr.batch-row:hover td{background:rgba(255,255,255,.02);}
td.name{color:var(--text);font-weight:600;}
.batch-detail{background:var(--card2);border-top:1px solid var(--border);padding:14px 16px;display:none;}
.batch-detail.open{display:block;}
.detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;}
.detail-section-title{font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-bottom:8px;}
.detail-item{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border);font-size:12px;}
.detail-item:last-child{border-bottom:none;}
.pagination{display:flex;align-items:center;justify-content:space-between;padding:14px 16px;border-top:1px solid var(--border);font-size:13px;color:var(--muted);}
.page-btns{display:flex;gap:6px;}
.page-btn{background:var(--card2);border:1px solid var(--border2);color:var(--sub);font-family:var(--sans);font-size:12px;padding:6px 12px;border-radius:7px;cursor:pointer;transition:all .15s;}
.page-btn:hover{border-color:var(--green);color:var(--green);}
.page-btn:disabled{opacity:.3;cursor:not-allowed;}
.modal-bg{position:fixed;inset:0;z-index:500;background:rgba(0,0,0,.75);backdrop-filter:blur(4px);display:none;align-items:center;justify-content:center;}
.modal-bg.open{display:flex;}
.modal{background:var(--card);border:1px solid var(--border2);border-radius:16px;padding:28px;width:660px;max-width:95vw;max-height:90vh;overflow-y:auto;animation:modalIn .2s ease;}
@keyframes modalIn{from{opacity:0;transform:scale(.95)}to{opacity:1;transform:scale(1)}}
.modal-title{font-size:18px;font-weight:800;margin-bottom:4px;}
.modal-sub{font-size:13px;color:var(--muted);margin-bottom:20px;}
.fld{display:flex;flex-direction:column;gap:6px;margin-bottom:14px;}
.fld label{font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);}
.fld input,.fld select{background:var(--card2);border:1px solid var(--border2);border-radius:10px;padding:10px 12px;color:var(--text);font-family:var(--sans);font-size:14px;outline:none;transition:border-color .2s;width:100%;}
.fld input:focus,.fld select:focus{border-color:rgba(251,146,60,.5);}
.modal-actions{display:flex;gap:10px;margin-top:8px;justify-content:flex-end;}
.btn-cancel{background:transparent;border:1px solid var(--border2);color:var(--sub);padding:10px 18px;border-radius:var(--r);font-family:var(--sans);font-size:13px;font-weight:700;cursor:pointer;}
.btn-cancel:hover{border-color:var(--danger);color:var(--danger);}
.section-label{font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;margin-bottom:8px;display:flex;align-items:center;gap:8px;}
.item-row{display:grid;grid-template-columns:1fr 110px 60px 32px;gap:8px;align-items:center;margin-bottom:8px;}
.item-row select,.item-row input{background:var(--card2);border:1px solid var(--border2);border-radius:8px;padding:8px 10px;color:var(--text);font-family:var(--sans);font-size:13px;outline:none;width:100%;}
.item-row select:focus,.item-row input:focus{border-color:rgba(251,146,60,.4);}
.unit-hint{font-size:10px;color:var(--muted);font-family:var(--mono);text-align:center;}
.rm-btn{background:none;border:none;color:var(--muted);font-size:18px;cursor:pointer;padding:0;transition:color .15s;}
.rm-btn:hover{color:var(--danger);}
.add-row-btn{border:1px dashed;font-family:var(--sans);font-size:13px;font-weight:600;padding:8px;border-radius:8px;cursor:pointer;width:100%;transition:all .2s;margin-bottom:16px;background:transparent;}
.add-row-btn.orange-btn{border-color:rgba(251,146,60,.3);color:var(--orange);}
.add-row-btn.orange-btn:hover{background:rgba(251,146,60,.08);}
.add-row-btn.green-btn{border-color:rgba(0,255,157,.3);color:var(--green);}
.add-row-btn.green-btn:hover{background:rgba(0,255,157,.08);}
.add-row-btn.teal-btn{border-color:rgba(45,212,191,.3);color:var(--teal);}
.add-row-btn.teal-btn:hover{background:rgba(45,212,191,.08);}
.loss-bar{background:var(--card2);border:1px solid var(--border2);border-radius:10px;padding:12px 16px;margin-bottom:14px;display:flex;align-items:center;justify-content:space-between;}
.loss-good{color:var(--green);}
.loss-ok{color:var(--warn);}
.loss-high{color:var(--danger);}
.pkg-preview{background:var(--card2);border:1px solid rgba(45,212,191,.2);border-radius:10px;padding:14px 16px;margin-bottom:14px;display:none;}
.pkg-preview-title{font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--teal);margin-bottom:10px;}
.pkg-preview-row{display:flex;justify-content:space-between;font-size:13px;padding:5px 0;border-bottom:1px solid var(--border);}
.pkg-preview-row:last-child{border-bottom:none;}
.stock-info{background:var(--card2);border:1px solid var(--border2);border-radius:10px;padding:10px 14px;margin-bottom:14px;display:flex;justify-content:space-between;font-size:13px;}
.toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%) translateY(16px);background:var(--card2);border:1px solid var(--border2);border-radius:var(--r);padding:12px 20px;font-size:13px;font-weight:600;color:var(--text);box-shadow:0 20px 50px rgba(0,0,0,.5);opacity:0;pointer-events:none;transition:opacity .25s,transform .25s;z-index:999;}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0);}
::-webkit-scrollbar{width:4px;}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:4px;}
</style>
    <script src="/static/auth-guard.js"></script>
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
    <a href="/farm/"      class="nav-link">Farm Intake</a>
    <a href="/inventory/" class="nav-link">Inventory</a>
    <a href="/production/"class="nav-link active">Production</a>
    <a href="/b2b/"       class="nav-link">B2B</a>
    <a href="/accounting/"class="nav-link">Accounting</a>
    <span class="nav-spacer"></span>
    <div class="topbar-right">
        <button class="mode-btn" id="mode-btn" onclick="toggleMode()" title="Toggle color mode">??</button>
        <div class="account-menu">
            <button class="user-pill" id="account-trigger" onclick="toggleAccountMenu(event)" aria-haspopup="menu" aria-expanded="false">
                <div class="user-avatar" id="user-avatar">A</div>
                <span class="user-name" id="user-name">Admin</span>
                <span class="menu-caret">&#9662;</span>
            </button>
            <div class="account-dropdown" id="account-dropdown" role="menu">
                <div class="account-head">
                    <div class="account-label">Signed in as</div>
                    <div class="account-email" id="user-email">&mdash;</div>
                </div>
                <a href="/users/password" class="account-item" role="menuitem">Change Password</a>
                <button class="account-item danger" onclick="logout()" role="menuitem">Sign out</button>
            </div>
        </div>
    </div>
</nav>

<div class="content">
    <div>
        <div class="page-title">Production &amp; Processing</div>
        <div class="page-sub">Process raw materials, package products, track spoilage</div>
    </div>

    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;">
        <div class="tabs">
            <button class="tab active" id="tab-batches"  onclick="switchTab('batches')">All Batches</button>
            <button class="tab"        id="tab-packaging" onclick="switchTab('packaging')">Packaging</button>
            <button class="tab"        id="tab-recipes"   onclick="switchTab('recipes')">Recipes</button>
            <button class="tab"        id="tab-spoilage"  onclick="switchTab('spoilage')">Spoilage</button>
        </div>
        <div style="display:flex;gap:10px;flex-wrap:wrap;">
            <button class="btn btn-orange" id="btn-batch"      onclick="openBatchModal()">New Processing Batch</button>
            <button class="btn btn-teal"   id="btn-pkg"        onclick="openPkgModal()"            style="display:none">New Packaging Run</button>
            <button class="btn btn-blue"   id="btn-recipe"     onclick="openRecipeModal(false)"    style="display:none">+ Processing Recipe</button>
            <button class="btn btn-blue"   id="btn-pkg-recipe" onclick="openRecipeModal(true)"     style="display:none">+ Packaging Recipe</button>
            <button class="btn btn-danger" id="btn-spoilage"   onclick="openSpoilageModal()"       style="display:none">Log Spoilage</button>
        </div>
    </div>

    <!-- BATCHES -->
    <div id="section-batches">
        <div class="table-wrap">
            <table>
                <thead><tr><th>Batch #</th><th>Type</th><th>Recipe</th><th>Inputs</th><th>Outputs</th><th>Loss %</th><th>Date</th><th>Notes</th><th></th></tr></thead>
                <tbody id="batches-body"><tr><td colspan="9" style="text-align:center;color:var(--muted);padding:40px">Loading...</td></tr></tbody>
            </table>
            <div class="pagination">
                <span id="batch-page-info">-</span>
                <div class="page-btns">
                    <button class="page-btn" id="prev-btn" onclick="prevPage()">Prev</button>
                    <button class="page-btn" id="next-btn" onclick="nextPage()">Next</button>
                </div>
            </div>
        </div>
    </div>

    <!-- PACKAGING -->
    <div id="section-packaging" style="display:none">
        <div class="table-wrap">
            <table>
                <thead><tr><th>Batch #</th><th>Recipe</th><th>Materials Used</th><th>Packs Created</th><th>Date</th><th>Notes</th><th></th></tr></thead>
                <tbody id="pkg-body"><tr><td colspan="7" style="text-align:center;color:var(--muted);padding:40px">Loading...</td></tr></tbody>
            </table>
        </div>
    </div>

    <!-- RECIPES -->
    <div id="section-recipes" style="display:none">
        <div class="recipes-grid" id="recipes-grid">
            <div style="color:var(--muted);padding:40px">Loading...</div>
        </div>
    </div>

    <!-- SPOILAGE -->
    <div id="section-spoilage" style="display:none">
        <div class="table-wrap">
            <table>
                <thead><tr><th>Ref #</th><th>Product</th><th>Qty Lost</th><th>Reason</th><th>Farm Source</th><th>Date</th><th>Notes</th><th></th></tr></thead>
                <tbody id="spoilage-body"><tr><td colspan="8" style="text-align:center;color:var(--muted);padding:40px">Loading...</td></tr></tbody>
            </table>
        </div>
    </div>
</div>

<!-- PROCESSING BATCH MODAL -->
<div class="modal-bg" id="batch-modal">
    <div class="modal">
        <div class="modal-title" id="batch-modal-title">New Processing Batch</div>
        <div class="modal-sub">Loss is calculated automatically from input vs output quantities</div>
        <div class="fld"><label>Load from Recipe (optional)</label>
            <select id="batch-recipe-sel" onchange="loadRecipeIntoForm()">
                <option value="">Start blank or select a recipe</option>
            </select>
        </div>
        <div class="fld"><label>Batch Notes</label><input id="b-notes" placeholder="e.g. Morning harvest"></div>
        <div class="loss-bar">
            <span style="color:var(--muted);font-size:12px;font-weight:600">Auto-Calculated Loss</span>
            <span style="font-family:var(--mono);font-size:18px;font-weight:700" id="loss-display" class="loss-good">0.0%</span>
        </div>
        <div class="section-label" style="color:var(--orange)">Raw Materials Used (Inputs)</div>
        <div id="batch-inputs"></div>
        <button class="add-row-btn orange-btn" onclick="addItemRow('batch-inputs',calcLoss)">+ Add Raw Material</button>
        <div class="section-label" style="color:var(--green)">Finished Products Created (Outputs)</div>
        <div id="batch-outputs"></div>
        <button class="add-row-btn green-btn" onclick="addItemRow('batch-outputs',calcLoss)">+ Add Finished Product</button>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeBatchModal()">Cancel</button>
            <button class="btn btn-orange" id="batch-save-btn" onclick="saveBatch()">Run Batch</button>
        </div>
    </div>
</div>

<!-- PACKAGING MODAL -->
<div class="modal-bg" id="pkg-modal">
    <div class="modal">
        <div class="modal-title">New Packaging Run</div>
        <div class="modal-sub">Select a recipe and enter how many packs to produce</div>
        <div class="fld"><label>Packaging Recipe *</label>
            <select id="pkg-recipe-sel" onchange="onPkgRecipeChange()"><option value="">Select packaging recipe</option></select>
        </div>
        <div style="display:grid;grid-template-columns:1fr auto;gap:10px;align-items:end;margin-bottom:14px;">
            <div class="fld" style="margin:0"><label>Number of Packs to Make *</label>
                <input id="pkg-units" type="number" placeholder="e.g. 50" min="1" step="1" oninput="calcPkgPreview()">
            </div>
            <button class="btn btn-teal" onclick="calcPkgPreview()" style="height:42px">Calculate</button>
        </div>
        <div class="pkg-preview" id="pkg-preview">
            <div class="pkg-preview-title">What will happen</div>
            <div id="pkg-preview-body"></div>
        </div>
        <div class="fld"><label>Notes</label><input id="pkg-notes" placeholder="Optional notes"></div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="document.getElementById('pkg-modal').classList.remove('open')">Cancel</button>
            <button class="btn btn-teal" onclick="savePkgBatch()">Run Packaging</button>
        </div>
    </div>
</div>

<!-- RECIPE MODAL -->
<div class="modal-bg" id="recipe-modal">
    <div class="modal">
        <div class="modal-title" id="recipe-modal-title">Save Recipe</div>
        <div class="modal-sub"   id="recipe-modal-sub">Save a reusable formula</div>
        <div class="fld"><label>Recipe Name *</label><input id="r-name" placeholder="e.g. Moringa Powder Processing"></div>
        <div class="fld"><label>Description</label><input id="r-desc" placeholder="e.g. 1kg fresh leaves to 100g powder"></div>
        <div class="section-label" style="color:var(--orange)" id="r-input-label">Inputs per batch</div>
        <div id="recipe-inputs"></div>
        <button class="add-row-btn orange-btn" id="r-add-input" onclick="addItemRow('recipe-inputs',null)">+ Add Input</button>
        <div class="section-label" id="r-output-label" style="color:var(--green)">Outputs per batch</div>
        <div id="recipe-outputs"></div>
        <button class="add-row-btn green-btn" id="r-add-output" onclick="addItemRow('recipe-outputs',null)">+ Add Output</button>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="document.getElementById('recipe-modal').classList.remove('open')">Cancel</button>
            <button class="btn btn-blue" onclick="saveRecipe()">Save Recipe</button>
        </div>
    </div>
</div>

<!-- SPOILAGE MODAL -->
<div class="modal-bg" id="spoilage-modal">
    <div class="modal" style="width:500px">
        <div class="modal-title">Log Spoilage</div>
        <div class="modal-sub">Record damaged or spoiled products — stock deducted, accounting updated automatically</div>
        <div class="fld"><label>Product *</label>
            <input id="spl-product" list="product-datalist" placeholder="Search by name or SKU..."
                style="background:var(--card2);border:1px solid var(--border2);border-radius:10px;padding:10px 12px;color:var(--text);font-family:var(--sans);font-size:14px;outline:none;width:100%"
                oninput="onSplProduct()" autocomplete="off">
            <input type="hidden" id="spl-product-id">
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
            <div class="fld"><label>Quantity Lost *</label>
                <input id="spl-qty" type="number" placeholder="0" min="0.001" step="any">
            </div>
            <div class="fld"><label>Unit</label>
                <input id="spl-unit" readonly placeholder="—" style="opacity:.6;cursor:default">
            </div>
        </div>
        <div class="stock-info">
            <span style="color:var(--muted)">Current Stock</span>
            <span style="font-family:var(--mono);color:var(--warn)" id="spl-stock">—</span>
        </div>
        <div class="fld"><label>Date *</label>
            <input id="spl-date" type="date">
        </div>
        <div class="fld"><label>Reason</label>
            <select id="spl-reason">
                <option value="">Select reason...</option>
                <option value="mold">Mold</option>
                <option value="overripe">Overripe</option>
                <option value="damaged">Damaged</option>
                <option value="pest">Pest</option>
                <option value="heat">Heat damage</option>
                <option value="water">Water damage</option>
                <option value="expired">Expired</option>
                <option value="other">Other</option>
            </select>
        </div>
        <div class="fld"><label>Farm Source (optional)</label>
            <select id="spl-farm">
                <option value="">Not from a specific farm</option>
            </select>
        </div>
        <div class="fld"><label>Notes</label>
            <input id="spl-notes" placeholder="Any additional details...">
        </div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeSpoilageModal()">Cancel</button>
            <button class="btn btn-danger" onclick="saveSpoilage()">Log Spoilage</button>
        </div>
    </div>
</div>

<div class="toast" id="toast"></div>

<script>
  // Auth guard: redirect to login if the readable session cookie is absent
  function _hasAuthCookie() {
      return document.cookie.split(";").some(c => c.trim().startsWith("logged_in="));
  }
  if (!_hasAuthCookie()) { _redirectToLogin(); }

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
        if (!r.ok) { _redirectToLogin(); return; }
        const u = await r.json();
        const nameEl = document.getElementById("user-name");
        const avatarEl = document.getElementById("user-avatar");
        const emailEl = document.getElementById("user-email");
        if (nameEl) nameEl.innerText = u.name;
        if (avatarEl) avatarEl.innerText = u.name.charAt(0).toUpperCase();
        if (emailEl) emailEl.innerText = u.email;
        return u;
    } catch(e) { _redirectToLogin(); }
}
function toggleAccountMenu(event){
    event.stopPropagation();
    const trigger = document.getElementById("account-trigger");
    const dropdown = document.getElementById("account-dropdown");
    const open = dropdown.classList.toggle("open");
    trigger.classList.toggle("open", open);
    trigger.setAttribute("aria-expanded", open ? "true" : "false");
}
document.addEventListener("click", e => {
    const menu = document.getElementById("account-dropdown");
    const trigger = document.getElementById("account-trigger");
    if(!menu || !trigger) return;
    if(menu.contains(e.target) || trigger.contains(e.target)) return;
    menu.classList.remove("open");
    trigger.classList.remove("open");
    trigger.setAttribute("aria-expanded", "false");
});
async function logout(){
    await fetch("/auth/logout", { method: "POST" });
    window.location.href = "/";
}
  function hasPermission(permission, u){
      const role = u ? (u.role || "") : currentUserRole;
      const perms = u
          ? new Set(typeof u.permissions === "string" ? u.permissions.split(",").map(v => v.trim()).filter(Boolean) : (u.permissions || []))
          : currentUserPermissions;
      return role === "admin" || perms.has(permission);
  }
  function configureProductionPermissions(u){
      const tabMap = [
          {id:"tab-batches", permission:"tab_production_batches", tab:"batches"},
          {id:"tab-packaging", permission:"tab_production_packaging", tab:"packaging"},
          {id:"tab-recipes", permission:"tab_production_recipes", tab:"recipes"},
          {id:"tab-spoilage", permission:"tab_production_spoilage", tab:"spoilage"},
      ];
      let firstAllowed = null;
      tabMap.forEach(conf => {
          let el = document.getElementById(conf.id);
          if(!el) return;
          if(!hasPermission(conf.permission, u)) el.style.display = "none";
          else if(!firstAllowed) firstAllowed = conf.tab;
      });
      if(firstAllowed) setTimeout(() => switchTab(firstAllowed), 0);
  }
  initializeColorMode();
  initUser().then(u => {
      if(!u) return;
      isAdmin = (u.role === "admin");
      currentUserRole = u.role || "";
      currentUserPermissions = new Set((u.permissions || "").split(",").map(v => v.trim()).filter(Boolean));
      configureProductionPermissions(u);
  });
  let allProducts   = [];
let allRecipes    = [];
let pkgRecipes    = [];
let procRecipes   = [];
let allFarms      = [];
let batchPage     = 0;
let pageSize      = 20;
let totalBatches  = 0;
let isPackagingRecipe = false;
let editingBatchId    = null;
let isAdmin = false;
let currentUserRole = "";
let currentUserPermissions = new Set();
const WEIGHT_UNITS = ["gram","g","kg","ltr","ml","liter","litre"];

async function init(){
    allProducts = await (await fetch("/production/api/products-list")).json();
    allRecipes  = await (await fetch("/production/api/recipes")).json();
    allFarms    = await (await fetch("/production/api/farms-list")).json();
    buildProductDatalist();
    splitRecipes();
    fillSel("batch-recipe-sel", procRecipes);
    fillSel("pkg-recipe-sel",   pkgRecipes);
    loadBatches();
}

function splitRecipes(){
    pkgRecipes  = allRecipes.filter(r => r.description && r.description.startsWith("[PKG]"));
    procRecipes = allRecipes.filter(r => !pkgRecipes.includes(r));
}

function fillSel(selId, recipes){
    let sel = document.getElementById(selId);
    if(!sel) return;
    let first = sel.options[0].outerHTML;
    sel.innerHTML = first + recipes.map(r => `<option value="${r.id}">${r.name}</option>`).join("");
}

/* ── TABS ── */
function switchTab(tab){
    const required = {
        batches: "tab_production_batches",
        packaging: "tab_production_packaging",
        recipes: "tab_production_recipes",
        spoilage: "tab_production_spoilage",
    };
    if(required[tab] && !hasPermission(required[tab])) return;
    ["batches","packaging","recipes","spoilage"].forEach(t => {
        document.getElementById("section-"+t).style.display = t===tab ? "" : "none";
        document.getElementById("tab-"+t).classList.toggle("active", t===tab);
    });
    document.getElementById("btn-batch").style.display      = tab==="batches"  ? "" : "none";
    document.getElementById("btn-pkg").style.display        = tab==="packaging" ? "" : "none";
    document.getElementById("btn-recipe").style.display     = tab==="recipes"  ? "" : "none";
    document.getElementById("btn-pkg-recipe").style.display = tab==="recipes"  ? "" : "none";
    document.getElementById("btn-spoilage").style.display   = tab==="spoilage" ? "" : "none";
    if(tab==="packaging") loadPkgBatches();
    if(tab==="recipes")   loadRecipes();
    if(tab==="spoilage")  loadSpoilage();
}

/* ── ITEM ROWS ── */
function productOptions(selectedId){
    return allProducts.map(p =>
        `<option value="${p.id}" data-unit="${p.unit}" data-stock="${p.stock}" ${p.id==selectedId?"selected":""}>
            ${p.name} (${p.stock.toFixed(0)} ${p.unit})
        </option>`).join("");
}

/* Build datalist of all products for search */
function buildProductDatalist(){
    let dl = document.getElementById("product-datalist");
    if(!dl){
        dl = document.createElement("datalist");
        dl.id = "product-datalist";
        document.body.appendChild(dl);
    }
    dl.innerHTML = allProducts.map(p =>
        `<option data-id="${p.id}" value="${p.sku} — ${p.name}" data-unit="${p.unit}" data-stock="${p.stock}">`
    ).join("");
}

function resolveProduct(inputEl){
    let val = inputEl.value.trim().toLowerCase();
    // match by SKU prefix or name
    let match = allProducts.find(p =>
        (p.sku + " — " + p.name).toLowerCase() === val ||
        p.sku.toLowerCase() === val ||
        p.name.toLowerCase() === val
    );
    if(!match){
        // partial match
        match = allProducts.find(p =>
            p.sku.toLowerCase().startsWith(val) ||
            p.name.toLowerCase().includes(val)
        );
    }
    return match || null;
}

function addItemRow(containerId, callback){
    let div = document.createElement("div");
    div.className = "item-row";
    div.innerHTML = `
        <div style="position:relative;flex:1;">
            <input type="text" list="product-datalist"
                placeholder="Search by name or SKU..."
                class="product-search-input"
                style="width:100%;background:var(--card2);border:1px solid var(--border2);border-radius:8px;padding:8px 10px;color:var(--text);font-family:var(--sans);font-size:13px;outline:none;"
                autocomplete="off">
            <input type="hidden" class="product-id-hidden" value="">
            <span style="font-size:10px;color:var(--muted);position:absolute;right:8px;top:50%;transform:translateY(-50%)" class="stock-hint"></span>
        </div>
        <input type="number" placeholder="0" min="0.001" step="any"
            style="background:var(--card2);border:1px solid var(--border2);border-radius:8px;padding:8px 10px;color:var(--text);font-family:var(--mono);font-size:13px;outline:none;width:90px;">
        <span class="unit-hint" style="font-size:12px;color:var(--muted);min-width:32px;text-align:center;">-</span>
        <button class="rm-btn" onclick="this.closest('.item-row').remove();${callback?callback.name+'()':''}">×</button>
    `;
    let searchInp = div.querySelector(".product-search-input");
    let hiddenId  = div.querySelector(".product-id-hidden");
    let unitHint  = div.querySelector(".unit-hint");
    let stockHint = div.querySelector(".stock-hint");

    searchInp.addEventListener("input", function(){
        let p = resolveProduct(this);
        if(p){
            hiddenId.value    = p.id;
            unitHint.innerText  = p.unit || "-";
            stockHint.innerText = `stock: ${p.stock.toFixed(0)}`;
            this.style.borderColor = "rgba(0,255,157,.4)";
        } else {
            hiddenId.value    = "";
            unitHint.innerText  = "-";
            stockHint.innerText = "";
            this.style.borderColor = "";
        }
        if(callback) callback();
    });
    searchInp.addEventListener("blur", function(){
        let p = resolveProduct(this);
        if(!p && this.value.trim()){
            this.style.borderColor = "rgba(255,77,109,.5)";
        }
    });

    document.getElementById(containerId).appendChild(div);
    buildProductDatalist();
}

/* Helper to read item rows — replaces select-based reading */
function readItemRows(containerId){
    let rows  = document.querySelectorAll(`#${containerId} .item-row`);
    let items = [];
    for(let row of rows){
        let pid = row.querySelector(".product-id-hidden")?.value;
        let qty = parseFloat(row.querySelectorAll("input[type=number]")[0]?.value)||0;
        if(!pid || qty <= 0) continue;
        items.push({product_id: parseInt(pid), qty});
    }
    return items;
}

function getRows(containerId){
    let result = [];
    document.querySelectorAll(`#${containerId} .item-row`).forEach(row => {
        let pid = parseInt(row.querySelector(".product-id-hidden")?.value||0);
        let qty = parseFloat(row.querySelector("input[type=number]").value)||0;
        if(pid && qty > 0) result.push({product_id:pid, qty});
    });
    return result;
}

function setRow(containerId, items){
    document.getElementById(containerId).innerHTML = "";
    items.forEach(item => {
        addItemRow(containerId, containerId.includes("batch") ? calcLoss : null);
        let rows = document.querySelectorAll(`#${containerId} .item-row`);
        let row  = rows[rows.length-1];
        let p    = allProducts.find(x => x.id === item.product_id);
        if(p){
            let inp = row.querySelector(".product-search-input");
            let hid = row.querySelector(".product-id-hidden");
            inp.value = `${p.sku} — ${p.name}`;
            hid.value = p.id;
            row.querySelector(".unit-hint").innerText  = p.unit || "-";
            row.querySelector(".stock-hint").innerText = `stock: ${p.stock.toFixed(0)}`;
            inp.style.borderColor = "rgba(0,255,157,.4)";
        }
        row.querySelector("input[type=number]").value = item.qty;
        calcLoss();
    });
}

/* ── AUTO LOSS ── */
function calcLoss(){
    let totalIn=0, totalOut=0;
    document.querySelectorAll("#batch-inputs .item-row").forEach(row => {
        let pid  = row.querySelector(".product-id-hidden")?.value;
        let p    = allProducts.find(x => x.id == pid);
        if(p && WEIGHT_UNITS.includes((p.unit||"").toLowerCase()))
            totalIn += parseFloat(row.querySelector("input[type=number]").value)||0;
    });
    document.querySelectorAll("#batch-outputs .item-row").forEach(row => {
        let pid  = row.querySelector(".product-id-hidden")?.value;
        let p    = allProducts.find(x => x.id == pid);
        if(p && WEIGHT_UNITS.includes((p.unit||"").toLowerCase()))
            totalOut += parseFloat(row.querySelector("input[type=number]").value)||0;
    });
    let loss = totalIn > 0 ? ((totalIn - totalOut) / totalIn * 100) : 0;
    let el = document.getElementById("loss-display");
    if(!el) return;
    el.innerText = loss.toFixed(1) + "%";
    el.className = loss < 10 ? "loss-good" : loss < 25 ? "loss-ok" : "loss-high";
}

function loadRecipeIntoForm(){
    let id = parseInt(document.getElementById("batch-recipe-sel").value);
    let recipe = procRecipes.find(r => r.id===id);
    if(!recipe) return;
    setRow("batch-inputs",  recipe.inputs.map(i => ({product_id:i.product_id, qty:i.qty})));
    setRow("batch-outputs", recipe.outputs.map(o => ({product_id:o.product_id, qty:o.qty})));
}

/* ── BATCH MODAL ── */
function openBatchModal(){
    editingBatchId = null;
    document.getElementById("batch-modal-title").innerText = "New Processing Batch";
    document.getElementById("batch-save-btn").innerText    = "Run Batch";
    document.getElementById("batch-inputs").innerHTML  = "";
    document.getElementById("batch-outputs").innerHTML = "";
    document.getElementById("b-notes").value = "";
    document.getElementById("batch-recipe-sel").value = "";
    document.getElementById("loss-display").innerText = "0.0%";
    document.getElementById("loss-display").className = "loss-good";
    addItemRow("batch-inputs", calcLoss);
    addItemRow("batch-outputs", calcLoss);
    document.getElementById("batch-modal").classList.add("open");
}

function closeBatchModal(){
    editingBatchId = null;
    document.getElementById("batch-modal-title").innerText = "New Processing Batch";
    document.getElementById("batch-save-btn").innerText    = "Run Batch";
    document.getElementById("batch-modal").classList.remove("open");
}

async function openEditBatch(id){
    let data  = await (await fetch("/production/api/batches?limit=1000")).json();
    let batch = data.batches.find(b => b.id===id);
    if(!batch){ showToast("Could not load batch"); return; }
    editingBatchId = id;
    document.getElementById("batch-modal-title").innerText = `Edit Batch - ${batch.batch_number}`;
    document.getElementById("batch-save-btn").innerText    = "Save Changes";
    document.getElementById("b-notes").value = batch.notes;
    document.getElementById("batch-recipe-sel").value = batch.recipe_id||"";
    setRow("batch-inputs",  batch.inputs.map(i => ({product_id:i.product_id, qty:i.qty})));
    setRow("batch-outputs", batch.outputs.map(o => ({product_id:o.product_id, qty:o.qty})));
    document.getElementById("batch-modal").classList.add("open");
}

async function deleteBatch(id, number){
    if(!confirm(`Delete batch ${number}? This will reverse all stock changes.`)) return;
    let res  = await fetch(`/production/api/batches/${id}`, {method:"DELETE"});
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    showToast(`${number} deleted - stock reversed`);
    allProducts = await (await fetch("/production/api/products-list")).json();
    loadBatches();
}

async function saveBatch(){
    let inputs  = getRows("batch-inputs");
    let outputs = getRows("batch-outputs");
    if(!inputs.length)  { showToast("Add at least one raw material"); return; }
    if(!outputs.length) { showToast("Add at least one finished product"); return; }
    let url    = editingBatchId ? `/production/api/batches/${editingBatchId}` : "/production/api/batches";
    let method = editingBatchId ? "PUT" : "POST";
    let res    = await fetch(url, {
        method, headers:{"Content-Type":"application/json"},
        body:JSON.stringify({
            recipe_id:  parseInt(document.getElementById("batch-recipe-sel").value)||null,
            batch_type: "processing", waste_pct:0,
            notes:      document.getElementById("b-notes").value.trim()||null,
            inputs, outputs,
        }),
    });
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    closeBatchModal();
    let lossMsg = data.waste_pct > 0 ? ` | Loss: ${data.waste_pct.toFixed(1)}%` : "";
    showToast(`${data.batch_number} ${editingBatchId?"updated":"completed"}${lossMsg}`);
    allProducts = await (await fetch("/production/api/products-list")).json();
    loadBatches();
}

/* ── LOAD BATCHES ── */
async function loadBatches(){
    let data = await (await fetch(`/production/api/batches?skip=${batchPage*pageSize}&limit=${pageSize}`)).json();
    totalBatches = data.total;
    document.getElementById("batch-page-info").innerText =
        `${Math.min(batchPage*pageSize+1,totalBatches)}-${Math.min((batchPage+1)*pageSize,totalBatches)} of ${totalBatches}`;
    document.getElementById("prev-btn").disabled = batchPage===0;
    document.getElementById("next-btn").disabled = (batchPage+1)*pageSize>=totalBatches;
    if(!data.batches.length){
        document.getElementById("batches-body").innerHTML = `<tr><td colspan="9" style="text-align:center;color:var(--muted);padding:60px">No batches yet.</td></tr>`;
        return;
    }
    let html="";
    data.batches.forEach(b => {
        let isPkg = b.batch_number.startsWith("PKG");
        let lossColor = b.waste_pct<10?"var(--green)":b.waste_pct<25?"var(--warn)":"var(--danger)";
        let inSum  = b.inputs.slice(0,2).map(i=>`${i.qty.toFixed(0)}${i.unit} ${i.product.split(" ")[0]}`).join(", ")+(b.inputs.length>2?"...":"");
        let outSum = b.outputs.slice(0,2).map(o=>`${o.qty.toFixed(0)}${o.unit} ${o.product.split(" ")[0]}`).join(", ")+(b.outputs.length>2?"...":"");
        let adminBtns = isAdmin && !isPkg
            ? `<div style="display:flex;gap:6px">
                <button class="action-btn blue" onclick="event.stopPropagation();openEditBatch(${b.id})">Edit</button>
                <button class="action-btn danger" onclick="event.stopPropagation();deleteBatch(${b.id},'${b.batch_number}')">Delete</button>
               </div>`
            : (isAdmin ? `<button class="action-btn danger" onclick="event.stopPropagation();deleteBatch(${b.id},'${b.batch_number}')">Delete</button>` : "");
        html += `<tr class="batch-row" onclick="toggleDet('det-${b.id}')">
            <td style="font-family:var(--mono);font-size:12px;color:${isPkg?"var(--teal)":"var(--orange)"}">${b.batch_number}</td>
            <td><span style="font-size:11px;font-weight:700;padding:2px 8px;border-radius:20px;background:${isPkg?"rgba(45,212,191,.1)":"rgba(251,146,60,.1)"};color:${isPkg?"var(--teal)":"var(--orange)"}">
                ${isPkg?"Packaging":"Processing"}
            </span></td>
            <td class="name">${b.recipe}</td>
            <td style="font-size:12px;color:var(--sub)">${inSum||"-"}</td>
            <td style="font-size:12px;color:var(--green)">${outSum||"-"}</td>
            <td style="font-family:var(--mono);color:${lossColor}">${b.waste_pct.toFixed(1)}%</td>
            <td style="font-size:12px;color:var(--muted)">${b.created_at}</td>
            <td style="font-size:12px;color:var(--muted);max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${b.notes||"-"}</td>
            <td>${adminBtns}</td>
        </tr>
        <tr><td colspan="9" style="padding:0;border:none">
            <div class="batch-detail" id="det-${b.id}">
                <div class="detail-grid">
                    <div><div class="detail-section-title" style="color:var(--orange)">Inputs Used</div>
                        ${b.inputs.map(i=>`<div class="detail-item"><span style="color:var(--sub)">${i.product}</span><span style="font-family:var(--mono);color:var(--orange)">-${i.qty.toFixed(2)} ${i.unit}</span></div>`).join("")}
                    </div>
                    <div><div class="detail-section-title" style="color:var(--green)">Outputs Created</div>
                        ${b.outputs.map(o=>`<div class="detail-item"><span style="color:var(--sub)">${o.product}</span><span style="font-family:var(--mono);color:var(--green)">+${o.qty.toFixed(2)} ${o.unit}</span></div>`).join("")}
                    </div>
                </div>
            </div>
        </td></tr>`;
    });
    document.getElementById("batches-body").innerHTML = html;
}

function toggleDet(id){ let el=document.getElementById(id); if(el) el.classList.toggle("open"); }
function prevPage(){ if(batchPage>0){ batchPage--; loadBatches(); } }
function nextPage(){ if((batchPage+1)*pageSize<totalBatches){ batchPage++; loadBatches(); } }

/* ── PACKAGING ── */
function openPkgModal(){
    splitRecipes();
    fillSel("pkg-recipe-sel", pkgRecipes);
    document.getElementById("pkg-recipe-sel").value = "";
    document.getElementById("pkg-units").value = "";
    document.getElementById("pkg-notes").value = "";
    document.getElementById("pkg-preview").style.display = "none";
    document.getElementById("pkg-modal").classList.add("open");
}
function onPkgRecipeChange(){ document.getElementById("pkg-preview").style.display="none"; }

function calcPkgPreview(){
    let recipeId = parseInt(document.getElementById("pkg-recipe-sel").value);
    let units    = parseFloat(document.getElementById("pkg-units").value)||0;
    let recipe   = pkgRecipes.find(r => r.id===recipeId);
    if(!recipe || units<=0){ document.getElementById("pkg-preview").style.display="none"; return; }
    let rows = recipe.inputs.map(inp => {
        let total = inp.qty * units;
        let prod  = allProducts.find(p => p.id===inp.product_id);
        let avail = prod ? prod.stock : 0;
        let ok    = avail >= total;
        return `<div class="pkg-preview-row"><span style="color:var(--sub)">${inp.product_name}</span>
            <span style="font-family:var(--mono);color:${ok?"var(--green)":"var(--danger)"}">-${total.toFixed(2)} ${inp.unit}${!ok?` (only ${avail.toFixed(0)} available)`:""}</span></div>`;
    }).join("");
    let outRows = recipe.outputs.map(out =>
        `<div class="pkg-preview-row"><span style="color:var(--sub)">${out.product_name}</span>
        <span style="font-family:var(--mono);color:var(--teal)">+${(out.qty*units).toFixed(0)} packs</span></div>`).join("");
    document.getElementById("pkg-preview-body").innerHTML =
        `<div style="font-size:10px;color:var(--orange);font-weight:700;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">Materials needed</div>${rows}
        <div style="height:1px;background:var(--border);margin:8px 0"></div>
        <div style="font-size:10px;color:var(--teal);font-weight:700;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">Packs to create</div>${outRows}`;
    document.getElementById("pkg-preview").style.display = "block";
}

async function savePkgBatch(){
    let recipeId = parseInt(document.getElementById("pkg-recipe-sel").value);
    let units    = parseFloat(document.getElementById("pkg-units").value)||0;
    let notes    = document.getElementById("pkg-notes").value.trim();
    let recipe   = pkgRecipes.find(r => r.id===recipeId);
    if(!recipe)   { showToast("Select a packaging recipe"); return; }
    if(units <= 0){ showToast("Enter number of packs to make"); return; }
    let inputs  = recipe.inputs.map(i => ({product_id:i.product_id, qty:i.qty*units}));
    let outputs = recipe.outputs.map(o => ({product_id:o.product_id, qty:o.qty*units}));
    let res  = await fetch("/production/api/batches",{
        method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({recipe_id:recipeId, batch_type:"packaging", waste_pct:0, notes:`${units} packs. ${notes}`.trim(), inputs, outputs}),
    });
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    document.getElementById("pkg-modal").classList.remove("open");
    showToast(`${data.batch_number} - ${units} packs created!`);
    allProducts = await (await fetch("/production/api/products-list")).json();
    loadPkgBatches();
    switchTab("packaging");
}

async function loadPkgBatches(){
    let data = await (await fetch("/production/api/batches?limit=200")).json();
    let pkgB = data.batches.filter(b => b.batch_number.startsWith("PKG"));
    if(!pkgB.length){
        document.getElementById("pkg-body").innerHTML=`<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:60px">No packaging runs yet.</td></tr>`;
        return;
    }
    document.getElementById("pkg-body").innerHTML = pkgB.map(b => `
        <tr class="batch-row" onclick="toggleDet('pdet-${b.id}')">
            <td style="font-family:var(--mono);font-size:12px;color:var(--teal)">${b.batch_number}</td>
            <td class="name">${b.recipe}</td>
            <td style="font-size:12px;color:var(--sub)">${b.inputs.map(i=>`${i.qty.toFixed(0)}${i.unit} ${i.product.split(" ")[0]}`).join(", ")}</td>
            <td style="font-size:12px;color:var(--teal)">${b.outputs.map(o=>`${o.qty.toFixed(0)} x ${o.product.split(" ")[0]}`).join(", ")}</td>
            <td style="font-size:12px;color:var(--muted)">${b.created_at}</td>
            <td style="font-size:12px;color:var(--muted)">${b.notes||"-"}</td>
            <td>${isAdmin?`<button class="action-btn danger" onclick="event.stopPropagation();deleteBatch(${b.id},'${b.batch_number}')">Delete</button>`:""}</td>
        </tr>
        <tr><td colspan="7" style="padding:0;border:none">
            <div class="batch-detail" id="pdet-${b.id}">
                <div class="detail-grid">
                    <div><div class="detail-section-title" style="color:var(--orange)">Materials Used</div>
                        ${b.inputs.map(i=>`<div class="detail-item"><span style="color:var(--sub)">${i.product}</span><span style="font-family:var(--mono);color:var(--orange)">-${i.qty.toFixed(2)} ${i.unit}</span></div>`).join("")}
                    </div>
                    <div><div class="detail-section-title" style="color:var(--teal)">Packs Created</div>
                        ${b.outputs.map(o=>`<div class="detail-item"><span style="color:var(--sub)">${o.product}</span><span style="font-family:var(--mono);color:var(--teal)">+${o.qty.toFixed(0)} packs</span></div>`).join("")}
                    </div>
                </div>
            </div>
        </td></tr>`).join("");
}

/* ── RECIPES ── */
async function loadRecipes(){
    allRecipes = await (await fetch("/production/api/recipes")).json();
    splitRecipes();
    if(!allRecipes.length){
        document.getElementById("recipes-grid").innerHTML=`<div style="color:var(--muted);font-size:13px;padding:40px 0">No recipes saved yet.</div>`;
        return;
    }
    document.getElementById("recipes-grid").innerHTML = allRecipes.map(r => {
        let isPkg = pkgRecipes.includes(r);
        let desc  = (r.description||"").replace("[PKG]","").trim();
        return `<div class="recipe-card ${isPkg?"packaging":"processing"}">
            <div>
                <span class="type-badge ${isPkg?"badge-pkg":"badge-proc"}">${isPkg?"Packaging":"Processing"}</span>
                <div class="recipe-name" style="margin-top:8px">${r.name}</div>
                ${desc?`<div class="recipe-desc">${desc}</div>`:""}
            </div>
            <div class="recipe-section">
                <div class="recipe-section-title">Inputs${isPkg?" per 1 pack":""}</div>
                ${r.inputs.map(i=>`<div class="recipe-item"><span style="color:var(--sub)">${i.product_name}</span><span style="font-family:var(--mono);font-weight:700;color:var(--orange)">${i.qty} ${i.unit}</span></div>`).join("")}
            </div>
            <div class="recipe-section">
                <div class="recipe-section-title" style="color:${isPkg?"var(--teal)":"var(--green)"}">Outputs${isPkg?" per 1 pack":""}</div>
                ${r.outputs.map(o=>`<div class="recipe-item"><span style="color:var(--sub)">${o.product_name}</span><span style="font-family:var(--mono);font-weight:700;color:${isPkg?"var(--teal)":"var(--green)"}">${o.qty} ${o.unit}</span></div>`).join("")}
            </div>
            <div class="recipe-actions">
                ${isPkg
                    ? `<button class="action-btn teal" onclick="quickUsePkg(${r.id})">Use for Packaging</button>`
                    : `<button class="action-btn" onclick="quickUseProc(${r.id})">Use in Batch</button>`}
                <button class="action-btn danger" onclick="deleteRecipe(${r.id},'${r.name.replace(/'/g,"\\'")}')">Delete</button>
            </div>
        </div>`;
    }).join("");
}

function openRecipeModal(isPkg){
    isPackagingRecipe = isPkg;
    document.getElementById("recipe-inputs").innerHTML  = "";
    document.getElementById("recipe-outputs").innerHTML = "";
    document.getElementById("r-name").value = "";
    document.getElementById("r-desc").value = "";
    if(isPkg){
        document.getElementById("recipe-modal-title").innerText = "New Packaging Recipe";
        document.getElementById("recipe-modal-sub").innerText   = "Define inputs and outputs PER 1 PACK";
        document.getElementById("r-input-label").innerText  = "Inputs per 1 pack";
        document.getElementById("r-output-label").innerText = "Output per 1 pack";
        document.getElementById("r-add-input").className  = "add-row-btn teal-btn";
        document.getElementById("r-add-output").className = "add-row-btn teal-btn";
    } else {
        document.getElementById("recipe-modal-title").innerText = "Processing Recipe";
        document.getElementById("recipe-modal-sub").innerText   = "Save a standard processing formula";
        document.getElementById("r-input-label").innerText  = "Inputs per batch";
        document.getElementById("r-output-label").innerText = "Outputs per batch";
        document.getElementById("r-add-input").className  = "add-row-btn orange-btn";
        document.getElementById("r-add-output").className = "add-row-btn green-btn";
    }
    addItemRow("recipe-inputs", null);
    addItemRow("recipe-outputs", null);
    document.getElementById("recipe-modal").classList.add("open");
}

async function saveRecipe(){
    let name    = document.getElementById("r-name").value.trim();
    let rawDesc = document.getElementById("r-desc").value.trim();
    let desc    = isPackagingRecipe ? `[PKG] ${rawDesc}` : (rawDesc || null);
    let inputs  = getRows("recipe-inputs");
    let outputs = getRows("recipe-outputs");
    if(!name)           { showToast("Recipe name is required"); return; }
    if(!inputs.length)  { showToast("Add at least one input"); return; }
    if(!outputs.length) { showToast("Add at least one output"); return; }
    let res  = await fetch("/production/api/recipes",{
        method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({name, description:desc, inputs, outputs}),
    });
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    document.getElementById("recipe-modal").classList.remove("open");
    showToast("Recipe saved!");
    allRecipes = await (await fetch("/production/api/recipes")).json();
    splitRecipes();
    fillSel("batch-recipe-sel", procRecipes);
    fillSel("pkg-recipe-sel",   pkgRecipes);
    if(document.getElementById("section-recipes").style.display!=="none") loadRecipes();
}

function quickUseProc(id){ switchTab("batches"); openBatchModal(); document.getElementById("batch-recipe-sel").value=id; loadRecipeIntoForm(); }
function quickUsePkg(id)  { switchTab("packaging"); openPkgModal(); setTimeout(()=>{ document.getElementById("pkg-recipe-sel").value=id; },100); }

async function deleteRecipe(id,name){
    if(!confirm(`Delete recipe "${name}"?`)) return;
    await fetch(`/production/api/recipes/${id}`,{method:"DELETE"});
    showToast("Recipe deleted");
    allRecipes = await (await fetch("/production/api/recipes")).json();
    splitRecipes();
    fillSel("batch-recipe-sel", procRecipes);
    fillSel("pkg-recipe-sel",   pkgRecipes);
    loadRecipes();
}

/* ── SPOILAGE ── */
function openSpoilageModal(){
    buildProductDatalist();
    // Fill farm dropdown
    let fsel = document.getElementById("spl-farm");
    fsel.innerHTML = '<option value="">Not from a specific farm</option>' +
        allFarms.map(f => `<option value="${f.id}">${f.name}</option>`).join("");

    document.getElementById("spl-product").value    = "";
    document.getElementById("spl-product-id").value = "";
    document.getElementById("spl-qty").value    = "";
    document.getElementById("spl-unit").value   = "";
    document.getElementById("spl-stock").innerText = "—";
    document.getElementById("spl-date").value   = new Date().toISOString().split("T")[0];
    document.getElementById("spl-reason").value = "";
    document.getElementById("spl-notes").value  = "";
    document.getElementById("spoilage-modal").classList.add("open");
}

function closeSpoilageModal(){
    document.getElementById("spoilage-modal").classList.remove("open");
}

function onSplProduct(){
    let p = resolveProduct(document.getElementById("spl-product"));
    if(p){
        document.getElementById("spl-product-id").value   = p.id;
        document.getElementById("spl-unit").value         = p.unit || "";
        document.getElementById("spl-stock").innerText    = `${p.stock.toFixed(2)} ${p.unit}`;
        document.getElementById("spl-product").style.borderColor = "rgba(0,255,157,.4)";
    } else {
        document.getElementById("spl-product-id").value   = "";
        document.getElementById("spl-unit").value         = "";
        document.getElementById("spl-stock").innerText    = "—";
        document.getElementById("spl-product").style.borderColor = "";
    }
}

async function saveSpoilage(){
    let product_id = parseInt(document.getElementById("spl-product-id").value)||0;
    let qty        = parseFloat(document.getElementById("spl-qty").value)||0;
    let date_val   = document.getElementById("spl-date").value;
    let reason     = document.getElementById("spl-reason").value||null;
    let farm_id    = parseInt(document.getElementById("spl-farm").value)||null;
    let notes      = document.getElementById("spl-notes").value.trim()||null;

    if(!product_id){ showToast("Select a product"); return; }
    if(qty <= 0)   { showToast("Enter a quantity greater than 0"); return; }
    if(!date_val)  { showToast("Select a date"); return; }

    let res  = await fetch("/production/api/spoilage", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({product_id, qty, spoilage_date:date_val, reason, farm_id, notes}),
    });
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    closeSpoilageModal();
    showToast(`Spoilage logged: ${data.ref_number} — ${data.qty} ${data.product}`);
    allProducts = await (await fetch("/production/api/products-list")).json();
    loadSpoilage();
}

async function loadSpoilage(){
    let data = await (await fetch("/production/api/spoilage")).json();
    const reasonLabel = {
        mold:"Mold", overripe:"Overripe", damaged:"Damaged",
        pest:"Pest", heat:"Heat damage", water:"Water damage",
        expired:"Expired", other:"Other"
    };
    if(!data.records.length){
        document.getElementById("spoilage-body").innerHTML =
            `<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:60px">No spoilage recorded yet. Click "Log Spoilage" to start.</td></tr>`;
        return;
    }
    document.getElementById("spoilage-body").innerHTML = data.records.map(r => {
        let deleteBtn = isAdmin
            ? `<button class="action-btn danger" onclick="deleteSpoilage(${r.id},'${r.ref_number}')">Delete</button>`
            : "";
        return `<tr>
            <td style="font-family:var(--mono);font-size:12px;color:var(--danger)">${r.ref_number}</td>
            <td class="name">${r.product}</td>
            <td style="font-family:var(--mono);color:var(--danger)">-${r.qty.toFixed(2)} ${r.unit}</td>
            <td style="font-size:12px">${reasonLabel[r.reason]||r.reason}</td>
            <td style="font-size:12px;color:var(--muted)">${r.farm}</td>
            <td style="font-family:var(--mono);font-size:12px">${r.spoilage_date}</td>
            <td style="font-size:12px;color:var(--muted);max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${r.notes}</td>
            <td>${deleteBtn}</td>
        </tr>`;
    }).join("");
}

async function deleteSpoilage(id, ref){
    if(!confirm(`Delete ${ref}? Stock will be restored.`)) return;
    let res  = await fetch(`/production/api/spoilage/${id}`, {method:"DELETE"});
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    showToast(`${ref} deleted — stock restored`);
    allProducts = await (await fetch("/production/api/products-list")).json();
    loadSpoilage();
}

["batch-modal","pkg-modal","recipe-modal","spoilage-modal"].forEach(id => {
    document.getElementById(id).addEventListener("click", function(e){ if(e.target===this) this.classList.remove("open"); });
});

let toastTimer=null;
function showToast(msg){
    let t=document.getElementById("toast");
    t.innerText=msg; t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer=setTimeout(()=>t.classList.remove("show"),4500);
}

init();
</script>
</body>
</html>"""
