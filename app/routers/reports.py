from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, date, timedelta
from typing import Optional
import io

from app.core.permissions import require_permission
from app.database import get_db
from app.models.product import Product
from app.models.invoice import Invoice, InvoiceItem
from app.models.b2b import B2BClient, B2BInvoice, B2BInvoiceItem
from app.models.inventory import StockMove
from app.models.farm import Farm, FarmDelivery, FarmDeliveryItem
from app.models.spoilage import SpoilageRecord
from app.models.refund import RetailRefund
from app.models.production import ProductionBatch, BatchInput, BatchOutput
from app.models.accounting import Account, Journal, JournalEntry

router = APIRouter(
    prefix="/reports",
    tags=["Reports"],
    dependencies=[Depends(require_permission("page_reports"))],
)


# ── EXCEL HELPER ───────────────────────────────────────
def to_xlsx(headers, rows, sheet_name="Report"):
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = sheet_name
        hfill  = PatternFill("solid", fgColor="2a7a2a")
        hfont  = Font(bold=True, color="FFFFFF", size=11)
        thin   = Side(style="thin", color="DDDDDD")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)
        for col, h in enumerate(headers, 1):
            c = ws.cell(row=1, column=col, value=h)
            c.fill = hfill; c.font = hfont
            c.alignment = Alignment(horizontal="center", vertical="center")
            c.border = border
        for ri, row in enumerate(rows, 2):
            for ci, val in enumerate(row, 1):
                c = ws.cell(row=ri, column=ci, value=val)
                c.border = border
                c.alignment = Alignment(vertical="center")
                if ri % 2 == 0:
                    c.fill = PatternFill("solid", fgColor="F5FAF5")
        for col in ws.columns:
            mx = max((len(str(c.value or "")) for c in col), default=10)
            ws.column_dimensions[col[0].column_letter].width = min(mx + 4, 40)
        ws.row_dimensions[1].height = 20
        buf = io.BytesIO()
        wb.save(buf); buf.seek(0)
        return buf
    except ImportError:
        raise Exception("Run: pip install openpyxl --break-system-packages")


def parse_dates(date_from, date_to):
    now = datetime.utcnow()
    if date_from and date_to:
        try:
            d_from = datetime.fromisoformat(date_from)
            d_to   = datetime.fromisoformat(date_to).replace(hour=23, minute=59, second=59)
        except Exception:
            d_from = now.replace(day=1, hour=0, minute=0, second=0)
            d_to   = now
    else:
        d_from = now.replace(day=1, hour=0, minute=0, second=0)
        d_to   = now
    return d_from, d_to


# ── SALES ──────────────────────────────────────────────
@router.get("/api/sales")
def sales_report(date_from: Optional[str] = None, date_to: Optional[str] = None, db: Session = Depends(get_db)):
    d_from, d_to = parse_dates(date_from, date_to)
    pos_invoices = db.query(Invoice).filter(Invoice.created_at >= d_from, Invoice.created_at <= d_to, Invoice.status == "paid").all()
    b2b_invoices = db.query(B2BInvoice).filter(B2BInvoice.created_at >= d_from, B2BInvoice.created_at <= d_to).all()
    refunds      = db.query(RetailRefund).filter(RetailRefund.created_at >= d_from, RetailRefund.created_at <= d_to).all()

    pos_total    = sum(float(i.total) for i in pos_invoices)
    b2b_total    = sum(float(i.amount_paid) for i in b2b_invoices)
    refund_total = sum(float(r.total) for r in refunds)
    pos_total    = max(0, pos_total - refund_total)

    daily = {}
    for i in pos_invoices:
        d = i.created_at.strftime("%Y-%m-%d")
        daily.setdefault(d, {"pos": 0, "b2b": 0, "refunds": 0})
        daily[d]["pos"] += float(i.total)
    for i in b2b_invoices:
        d = i.created_at.strftime("%Y-%m-%d")
        daily.setdefault(d, {"pos": 0, "b2b": 0, "refunds": 0})
        daily[d]["b2b"] += float(i.amount_paid)
    for r in refunds:
        d = r.created_at.strftime("%Y-%m-%d")
        daily.setdefault(d, {"pos": 0, "b2b": 0, "refunds": 0})
        daily[d]["refunds"] += float(r.total)
    daily_list = [{"date": k, "pos": round(max(0, v["pos"] - v["refunds"]), 2), "b2b": round(v["b2b"], 2), "refunds": round(v["refunds"], 2), "total": round(max(0, v["pos"] - v["refunds"]) + v["b2b"], 2)} for k, v in sorted(daily.items())]

    product_sales = {}
    for inv in pos_invoices:
        for item in db.query(InvoiceItem).filter(InvoiceItem.invoice_id == inv.id).all():
            product_sales.setdefault(item.name, {"qty": 0, "revenue": 0})
            product_sales[item.name]["qty"]     += float(item.qty)
            product_sales[item.name]["revenue"] += float(item.total)
    top = sorted(product_sales.items(), key=lambda x: x[1]["revenue"], reverse=True)[:10]

    # Detailed POS records
    pos_records = []
    for inv in sorted(pos_invoices, key=lambda x: x.created_at, reverse=True):
        items = db.query(InvoiceItem).filter(InvoiceItem.invoice_id == inv.id).all()
        pos_records.append({
            "invoice_number": inv.invoice_number or f"POS-{inv.id}",
            "datetime": inv.created_at.strftime("%Y-%m-%d %H:%M") if inv.created_at else "—",
            "user_name": inv.user.name if inv.user else "—",
            "payment": inv.payment_method or "—",
            "items": [{"name": it.name, "qty": float(it.qty), "unit_price": float(it.unit_price), "total": float(it.total)} for it in items],
            "total": float(inv.total),
        })

    # Detailed B2B records
    b2b_records = []
    for inv in sorted(b2b_invoices, key=lambda x: x.created_at, reverse=True):
        items_data = []
        for it in inv.items:
            items_data.append({"name": it.product.name if it.product else "—", "qty": float(it.qty), "unit_price": float(it.unit_price), "total": float(it.total)})
        b2b_records.append({
            "invoice_number": inv.invoice_number,
            "client": inv.client.name if inv.client else "—",
            "datetime": inv.created_at.strftime("%Y-%m-%d %H:%M") if inv.created_at else "—",
            "user_name": inv.user.name if inv.user else "—",
            "invoice_type": inv.invoice_type,
            "status": inv.status,
            "items": items_data,
            "total": float(inv.total),
            "amount_paid": float(inv.amount_paid),
            "balance_due": float(inv.total) - float(inv.amount_paid),
        })

    # Detailed refund records
    refund_records = []
    for r in sorted(refunds, key=lambda x: x.created_at, reverse=True):
        refund_records.append({
            "refund_number":  r.refund_number,
            "customer":       r.customer.name if r.customer else "—",
            "datetime":       r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "—",
            "processed_by":   r.user.name if r.user else "—",
            "reason":         r.reason or "—",
            "refund_method":  r.refund_method,
            "total":          float(r.total),
        })

    return {"pos_total": round(pos_total, 2), "b2b_total": round(b2b_total, 2),
            "refund_total": round(refund_total, 2),
            "grand_total": round(pos_total + b2b_total, 2),
            "pos_count": len(pos_invoices), "b2b_count": len(b2b_invoices), "refund_count": len(refunds),
            "daily": daily_list, "top_products": [{"name": k, "qty": round(v["qty"], 2), "revenue": round(v["revenue"], 2)} for k, v in top],
            "pos_records": pos_records, "b2b_records": b2b_records, "refund_records": refund_records,
            "date_from": d_from.strftime("%Y-%m-%d"), "date_to": d_to.strftime("%Y-%m-%d")}

@router.get("/export/sales", dependencies=[Depends(require_permission("action_export_excel"))])
def export_sales(date_from: str = None, date_to: str = None, db: Session = Depends(get_db)):
    data = sales_report(date_from=date_from, date_to=date_to, db=db)
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

        wb = openpyxl.Workbook()

        green_fill  = PatternFill("solid", fgColor="2a7a2a")
        blue_fill   = PatternFill("solid", fgColor="1a4a8a")
        orange_fill = PatternFill("solid", fgColor="8a4a00")
        white_font  = Font(bold=True, color="FFFFFF", size=11)
        thin  = Side(style="thin", color="CCCCCC")
        bord  = Border(left=thin, right=thin, top=thin, bottom=thin)
        alt   = PatternFill("solid", fgColor="F5FAF5")
        total_fill = PatternFill("solid", fgColor="E8F5E8")
        total_font = Font(bold=True, size=11)

        def style_header(ws, headers, fill, font=white_font):
            for ci, h in enumerate(headers, 1):
                c = ws.cell(row=1, column=ci, value=h)
                c.fill = fill; c.font = font
                c.alignment = Alignment(horizontal="center", vertical="center")
                c.border = bord
            ws.row_dimensions[1].height = 22

        def auto_width(ws):
            for ci, col in enumerate(ws.columns, 1):
                mx = max((len(str(c.value or "")) for c in col), default=10)
                ws.column_dimensions[get_column_letter(ci)].width = min(mx + 4, 50)

        def add_row(ws, ri, values, fill=None, font=None, bold=False):
            for ci, val in enumerate(values, 1):
                c = ws.cell(row=ri, column=ci, value=val)
                c.border = bord
                c.alignment = Alignment(vertical="center", wrap_text=True)
                if fill: c.fill = fill
                if font: c.font = font
                elif bold: c.font = Font(bold=True)
                elif ri % 2 == 0: c.fill = alt

        # ── Sheet 1: Summary ──
        ws1 = wb.active
        ws1.title = "Summary"
        style_header(ws1, ["Period","Grand Total (EGP)","POS Revenue","B2B Revenue","POS Orders","B2B Invoices"], green_fill)
        add_row(ws1, 2, [
            f"{data['date_from']} → {data['date_to']}",
            data["grand_total"], data["pos_total"], data["b2b_total"],
            data["pos_count"], data["b2b_count"]
        ], fill=total_fill, font=total_font)
        ws1.append([])
        style_header_row = ws1.max_row + 1
        for ci, h in enumerate(["Date","POS (EGP)","B2B (EGP)","Total (EGP)"], 1):
            c = ws1.cell(row=style_header_row, column=ci, value=h)
            c.fill = green_fill; c.font = white_font; c.border = bord
            c.alignment = Alignment(horizontal="center")
        for ri, d in enumerate(data["daily"], style_header_row + 1):
            add_row(ws1, ri, [d["date"], d["pos"], d["b2b"], d["total"]])
        total_ri = ws1.max_row + 1
        add_row(ws1, total_ri, ["TOTAL", data["pos_total"], data["b2b_total"], data["grand_total"]], fill=total_fill, bold=True)
        auto_width(ws1)

        # ── Sheet 2: POS Invoices ──
        ws2 = wb.create_sheet("POS Invoices")
        style_header(ws2, ["Invoice #","Date / Time","User","Payment","Product","Qty","Unit Price (EGP)","Line Total (EGP)","Invoice Total (EGP)"], blue_fill)
        ri = 2
        for inv in data["pos_records"]:
            for i, item in enumerate(inv["items"]):
                add_row(ws2, ri, [
                    inv["invoice_number"] if i == 0 else "",
                    inv["datetime"] if i == 0 else "",
                    inv["user_name"] if i == 0 else "",
                    inv["payment"] if i == 0 else "",
                    item["name"], item["qty"], item["unit_price"], item["total"],
                    inv["total"] if i == 0 else ""
                ])
                ri += 1
            if not inv["items"]:
                add_row(ws2, ri, [inv["invoice_number"], inv["datetime"], inv["user_name"], inv["payment"], "—", "", "", "", inv["total"]])
                ri += 1
        auto_width(ws2)

        # ── Sheet 3: B2B Invoices ──
        ws3 = wb.create_sheet("B2B Invoices")
        style_header(ws3, ["Invoice #","Client","Date / Time","User","Type","Product","Qty","Unit Price (EGP)","Line Total (EGP)","Invoice Total","Amount Paid","Balance Due"], orange_fill)
        ri = 2
        for inv in data["b2b_records"]:
            for i, item in enumerate(inv["items"]):
                add_row(ws3, ri, [
                    inv["invoice_number"] if i == 0 else "",
                    inv["client"] if i == 0 else "",
                    inv["datetime"] if i == 0 else "",
                    inv["user_name"] if i == 0 else "",
                    inv["invoice_type"] if i == 0 else "",
                    item["name"], item["qty"], item["unit_price"], item["total"],
                    inv["total"] if i == 0 else "",
                    inv["amount_paid"] if i == 0 else "",
                    inv["balance_due"] if i == 0 else ""
                ])
                ri += 1
            if not inv["items"]:
                add_row(ws3, ri, [inv["invoice_number"], inv["client"], inv["datetime"], inv["user_name"], inv["invoice_type"], "—", "", "", "", inv["total"], inv["amount_paid"], inv["balance_due"]])
                ri += 1
        auto_width(ws3)

        # ── Sheet 4: Top Products ──
        ws4 = wb.create_sheet("Top Products")
        style_header(ws4, ["Product","Qty Sold","Revenue (EGP)"], green_fill)
        for ri, p in enumerate(data["top_products"], 2):
            add_row(ws4, ri, [p["name"], p["qty"], p["revenue"]])
        auto_width(ws4)

        buf = io.BytesIO()
        wb.save(buf); buf.seek(0)
        return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=sales_report_{date.today()}.xlsx"})
    except ImportError:
        raise Exception("Run: pip install openpyxl --break-system-packages")


# ── B2B STATEMENT ──────────────────────────────────────
@router.get("/api/b2b-statement")
def b2b_statement(date_from: Optional[str] = None, date_to: Optional[str] = None, db: Session = Depends(get_db)):
    d_from, d_to = parse_dates(date_from, date_to)
    clients = db.query(B2BClient).filter(B2BClient.is_active == True).order_by(B2BClient.name).all()
    result = []
    for c in clients:
        invoices = db.query(B2BInvoice).filter(B2BInvoice.client_id == c.id, B2BInvoice.created_at >= d_from, B2BInvoice.created_at <= d_to).all()
        if not invoices: continue
        total_invoiced = sum(float(i.total) for i in invoices)
        total_paid     = sum(float(i.amount_paid) for i in invoices)
        result.append({"id":c.id,"name":c.name,"phone":c.phone or "—","payment_terms":c.payment_terms,
            "total_invoiced":round(total_invoiced,2),"total_paid":round(total_paid,2),
            "outstanding":round(total_invoiced-total_paid,2),"invoice_count":len(invoices)})
    return result

@router.get("/export/b2b-statement", dependencies=[Depends(require_permission("action_export_excel"))])
def export_b2b(date_from: str = None, date_to: str = None, db: Session = Depends(get_db)):
    data = b2b_statement(date_from=date_from, date_to=date_to, db=db)
    rows = [[d["name"],d["phone"],d["payment_terms"],d["total_invoiced"],d["total_paid"],d["outstanding"],d["invoice_count"]] for d in data]
    buf = to_xlsx(["Client","Phone","Payment Terms","Total Invoiced","Total Paid","Outstanding","Invoices"], rows, "B2B Statement")
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=b2b_statement_{date.today()}.xlsx"})


# ── INVENTORY ──────────────────────────────────────────
@router.get("/api/inventory")
def inventory_report(db: Session = Depends(get_db)):
    products = db.query(Product).filter(Product.is_active == True).order_by(Product.name).all()
    rows = []
    for p in products:
        total_in  = float(db.query(func.sum(StockMove.qty)).filter(StockMove.product_id==p.id, StockMove.type=="in").scalar()  or 0)
        total_out = abs(float(db.query(func.sum(StockMove.qty)).filter(StockMove.product_id==p.id, StockMove.type=="out").scalar() or 0))
        rows.append({"sku":p.sku,"name":p.name,"stock":float(p.stock),"unit":p.unit,"price":float(p.price),
            "value":round(float(p.stock)*float(p.price),2),"total_in":round(total_in,2),"total_out":round(total_out,2),"low_stock":float(p.stock)<=5})
    return {"products":rows,"total_value":round(sum(r["value"] for r in rows),2),"low_count":sum(1 for r in rows if r["low_stock"]),"total_products":len(rows)}

@router.get("/export/inventory", dependencies=[Depends(require_permission("action_export_excel"))])
def export_inventory(db: Session = Depends(get_db)):
    data = inventory_report(db=db)
    rows = [[p["sku"],p["name"],p["stock"],p["unit"],p["price"],p["value"],p["total_in"],p["total_out"],"YES" if p["low_stock"] else ""] for p in data["products"]]
    buf = to_xlsx(["SKU","Product","Stock","Unit","Price (EGP)","Stock Value","Total In","Total Out","Low Stock"], rows, "Inventory")
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=inventory_{date.today()}.xlsx"})


# ── FARM INTAKE ────────────────────────────────────────
@router.get("/api/farm-intake")
def farm_intake_report(date_from: Optional[str] = None, date_to: Optional[str] = None, db: Session = Depends(get_db)):
    d_from, d_to = parse_dates(date_from, date_to)
    farms = db.query(Farm).filter(Farm.is_active == 1).all()
    # Auto-fix unnamed farms
    default_names = ["Organic Farm", "Regenerative Farm"]
    for i, farm in enumerate(farms):
        if not farm.name or str(farm.name).strip().lower() in ("none", ""):
            farm.name = default_names[i] if i < len(default_names) else f"Farm {farm.id}"
    try: db.commit()
    except Exception: db.rollback()
    result = []
    delivery_rows = []
    for farm in farms:
        deliveries = db.query(FarmDelivery).filter(FarmDelivery.farm_id==farm.id, FarmDelivery.delivery_date>=d_from.date(), FarmDelivery.delivery_date<=d_to.date()).all()
        product_totals = {}
        for d in deliveries:
            delivery_rows.append({
                "delivery_number": d.delivery_number,
                "farm": farm.name or f"Farm {farm.id}",
                "delivery_date": str(d.delivery_date),
                "received_by": d.received_by or "—",
                "user_name": d.user.name if d.user else "—",
                "total_items": len(d.items),
                "total_qty": round(sum(float(item.qty) for item in d.items), 2),
                "notes": d.notes or "",
            })
            for item in d.items:
                name = item.product.name if item.product else "—"
                unit = item.unit or ""
                key  = f"{name}|{unit}"
                product_totals[key] = product_totals.get(key, 0) + float(item.qty)
        products = [{"name":k.split("|")[0],"unit":k.split("|")[1],"total_qty":round(v,2)} for k,v in sorted(product_totals.items(), key=lambda x: x[1], reverse=True)]
        result.append({"name": farm.name or f"Farm {farm.id}", "delivery_count":len(deliveries), "products":products, "total_qty":round(sum(p["total_qty"] for p in products),2)})
    delivery_rows.sort(key=lambda row: (row["delivery_date"], row["delivery_number"]), reverse=True)
    return {"farms": result, "deliveries": delivery_rows}

@router.get("/export/farm-intake", dependencies=[Depends(require_permission("action_export_excel"))])
def export_farm(date_from: str = None, date_to: str = None, db: Session = Depends(get_db)):
    data = farm_intake_report(date_from=date_from, date_to=date_to, db=db)
    rows = []
    for farm in data["farms"]:
        for p in farm["products"]:
            rows.append([farm["name"],p["name"],p["total_qty"],p["unit"],farm["delivery_count"]])
        if not farm["products"]:
            rows.append([farm["name"],"No deliveries",0,"",0])
    for delivery in data["deliveries"]:
        rows.append([delivery["farm"], "Delivery " + delivery["delivery_number"], delivery["total_qty"], "", delivery["total_items"], delivery["user_name"]])
    buf = to_xlsx(["Farm","Product / Record","Total Qty","Unit","Deliveries / Items","Performed By"], rows, "Farm Intake")
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=farm_intake_{date.today()}.xlsx"})


# ── SPOILAGE ───────────────────────────────────────────
@router.get("/api/spoilage")
def spoilage_report(date_from: Optional[str] = None, date_to: Optional[str] = None, db: Session = Depends(get_db)):
    d_from, d_to = parse_dates(date_from, date_to)
    records = db.query(SpoilageRecord).filter(SpoilageRecord.spoilage_date>=d_from.date(), SpoilageRecord.spoilage_date<=d_to.date()).order_by(SpoilageRecord.spoilage_date.desc()).all()
    by_product, by_reason, rows = {}, {}, []
    for r in records:
        name = r.product.name if r.product else "—"; unit = r.product.unit if r.product else ""; reason = r.reason or "—"
        by_product[name]  = by_product.get(name, 0)  + float(r.qty)
        by_reason[reason] = by_reason.get(reason, 0) + float(r.qty)
        rows.append({"ref":r.ref_number,"product":name,"qty":float(r.qty),"unit":unit,"reason":reason,"farm":r.farm.name if r.farm else "—","date":str(r.spoilage_date),"user_name":r.user.name if r.user else "—","notes":r.notes or ""})
    return {"records":rows,"total_qty":round(sum(float(r.qty) for r in records),2),"total_count":len(records),
            "by_product":[{"name":k,"qty":round(v,2)} for k,v in sorted(by_product.items(),key=lambda x:x[1],reverse=True)[:8]],
            "by_reason": [{"reason":k,"qty":round(v,2)} for k,v in sorted(by_reason.items(), key=lambda x:x[1],reverse=True)]}

@router.get("/export/spoilage", dependencies=[Depends(require_permission("action_export_excel"))])
def export_spoilage(date_from: str = None, date_to: str = None, db: Session = Depends(get_db)):
    data = spoilage_report(date_from=date_from, date_to=date_to, db=db)
    rows = [[r["ref"],r["product"],r["qty"],r["unit"],r["reason"],r["farm"],r["date"],r["user_name"],r["notes"]] for r in data["records"]]
    buf = to_xlsx(["Ref #","Product","Qty","Unit","Reason","Farm","Date","Performed By","Notes"], rows, "Spoilage")
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=spoilage_{date.today()}.xlsx"})


# ── PRODUCTION ─────────────────────────────────────────
@router.get("/api/production")
def production_report(date_from: Optional[str] = None, date_to: Optional[str] = None, db: Session = Depends(get_db)):
    d_from, d_to = parse_dates(date_from, date_to)
    batches = db.query(ProductionBatch).filter(ProductionBatch.created_at>=d_from, ProductionBatch.created_at<=d_to).order_by(ProductionBatch.created_at.desc()).all()
    rows, losses, total_proc, total_pkg = [], [], 0, 0
    for b in batches:
        is_pkg = b.batch_number.startswith("PKG")
        inputs_str  = ", ".join(f"{float(i.qty):.0f}{i.product.unit if i.product else ''} {i.product.name if i.product else '—'}" for i in b.inputs)
        outputs_str = ", ".join(f"{float(o.qty):.0f}{o.product.unit if o.product else ''} {o.product.name if o.product else '—'}" for o in b.outputs)
        rows.append({"batch_number":b.batch_number,"type":"Packaging" if is_pkg else "Processing",
            "recipe":b.recipe.name if b.recipe else "Custom","waste_pct":float(b.waste_pct),
            "notes":b.notes or "","date":b.created_at.strftime("%Y-%m-%d") if b.created_at else "—",
            "inputs_str":inputs_str,"outputs_str":outputs_str,"user_name":b.user.name if b.user else "—"})
        if is_pkg: total_pkg  += 1
        else:      total_proc += 1; losses.append(float(b.waste_pct))
    return {"batches":rows,"total_processing":total_proc,"total_packaging":total_pkg,
            "avg_loss_pct":round(sum(losses)/len(losses),2) if losses else 0,"total_batches":len(rows)}

@router.get("/export/production", dependencies=[Depends(require_permission("action_export_excel"))])
def export_production(date_from: str = None, date_to: str = None, db: Session = Depends(get_db)):
    data = production_report(date_from=date_from, date_to=date_to, db=db)
    rows = [[b["batch_number"],b["type"],b["recipe"],b["inputs_str"],b["outputs_str"],b["waste_pct"],b["date"],b["user_name"],b["notes"]] for b in data["batches"]]
    buf = to_xlsx(["Batch #","Type","Recipe","Inputs","Outputs","Loss %","Date","Performed By","Notes"], rows, "Production")
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=production_{date.today()}.xlsx"})


# ── P&L ────────────────────────────────────────────────
@router.get("/api/pl")
def pl_report(date_from: Optional[str] = None, date_to: Optional[str] = None, db: Session = Depends(get_db)):
    d_from, d_to = parse_dates(date_from, date_to)
    accounts = db.query(Account).all()

    def acc_entries(acc):
        return db.query(JournalEntry).join(Journal).filter(
            JournalEntry.account_id == acc.id,
            Journal.created_at >= d_from,
            Journal.created_at <= d_to
        ).order_by(Journal.created_at.desc()).all()

    def acc_movement(acc):
        entries = acc_entries(acc)
        return sum(float(e.credit) - float(e.debit) for e in entries)

    def entry_details(acc):
        entries = acc_entries(acc)
        result = []
        for e in entries:
            j = e.journal
            result.append({
                "date": j.created_at.strftime("%Y-%m-%d %H:%M") if j.created_at else "—",
                "ref_type": j.ref_type or "manual",
                "description": j.description or "—",
                "debit": float(e.debit),
                "credit": float(e.credit),
                "amount": abs(float(e.credit) - float(e.debit)),
            })
        return result

    revenue_lines = []
    for a in accounts:
        if a.type == "revenue":
            mv = round(acc_movement(a), 2)
            if mv != 0:
                revenue_lines.append({"name": a.name, "code": a.code, "amount": round(abs(mv), 2), "entries": entry_details(a)})

    expense_lines = []
    for a in accounts:
        if a.type == "expense":
            mv = round(acc_movement(a), 2)
            if mv != 0:
                expense_lines.append({"name": a.name, "code": a.code, "amount": round(abs(mv), 2), "entries": entry_details(a)})

    total_revenue = sum(r["amount"] for r in revenue_lines)
    total_expense = sum(e["amount"] for e in expense_lines)
    return {"revenue_lines": revenue_lines, "expense_lines": expense_lines,
            "total_revenue": round(total_revenue, 2), "total_expense": round(total_expense, 2),
            "net_profit": round(total_revenue - total_expense, 2),
            "date_from": d_from.strftime("%Y-%m-%d"), "date_to": d_to.strftime("%Y-%m-%d")}

@router.get("/export/pl", dependencies=[Depends(require_permission("action_export_excel"))])
def export_pl(date_from: str = None, date_to: str = None, db: Session = Depends(get_db)):
    data = pl_report(date_from=date_from, date_to=date_to, db=db)
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

        wb = openpyxl.Workbook()

        green_fill  = PatternFill("solid", fgColor="2a7a2a")
        red_fill    = PatternFill("solid", fgColor="8a1a2a")
        blue_fill   = PatternFill("solid", fgColor="1a3a7a")
        white_font  = Font(bold=True, color="FFFFFF", size=11)
        thin  = Side(style="thin", color="CCCCCC")
        bord  = Border(left=thin, right=thin, top=thin, bottom=thin)
        alt   = PatternFill("solid", fgColor="F5FAF5")
        alt2  = PatternFill("solid", fgColor="FFF5F5")
        total_font  = Font(bold=True, size=11)
        total_green = PatternFill("solid", fgColor="D0F0D0")
        total_red   = PatternFill("solid", fgColor="F0D0D0")
        section_font = Font(bold=True, size=12, color="FFFFFF")

        def add_cell(ws, row, col, value, fill=None, font=None, align="left"):
            c = ws.cell(row=row, column=col, value=value)
            c.border = bord
            c.alignment = Alignment(horizontal=align, vertical="center")
            if fill: c.fill = fill
            if font: c.font = font
            return c

        def auto_width(ws):
            for ci, col in enumerate(ws.columns, 1):
                mx = max((len(str(c.value or "")) for c in col), default=10)
                ws.column_dimensions[get_column_letter(ci)].width = min(mx + 4, 50)

        # ── Sheet 1: P&L Summary ──
        ws1 = wb.active
        ws1.title = "P&L Summary"

        # Title row
        ws1.merge_cells("A1:D1")
        tc = ws1["A1"]
        tc.value = f"Profit & Loss Statement  |  {data['date_from']}  →  {data['date_to']}"
        tc.font = Font(bold=True, size=13, color="FFFFFF")
        tc.fill = green_fill
        tc.alignment = Alignment(horizontal="center", vertical="center")
        ws1.row_dimensions[1].height = 28

        # Headers
        ri = 2
        for ci, h in enumerate(["Category", "Code", "Account", "Amount (EGP)"], 1):
            add_cell(ws1, ri, ci, h, fill=PatternFill("solid", fgColor="4a4a4a"), font=Font(bold=True, color="FFFFFF", size=10), align="center")
        ri += 1

        # Revenue section header
        ws1.merge_cells(f"A{ri}:D{ri}")
        c = ws1.cell(row=ri, column=1, value="REVENUE")
        c.font = section_font; c.fill = green_fill
        c.alignment = Alignment(horizontal="center", vertical="center")
        ri += 1

        for line in data["revenue_lines"]:
            fill = alt if ri % 2 == 0 else None
            add_cell(ws1, ri, 1, "Revenue", fill=fill)
            add_cell(ws1, ri, 2, line["code"], fill=fill)
            add_cell(ws1, ri, 3, line["name"], fill=fill)
            add_cell(ws1, ri, 4, line["amount"], fill=fill, align="right")
            ri += 1

        add_cell(ws1, ri, 1, "", fill=total_green)
        add_cell(ws1, ri, 2, "", fill=total_green)
        add_cell(ws1, ri, 3, "TOTAL REVENUE", fill=total_green, font=total_font)
        add_cell(ws1, ri, 4, data["total_revenue"], fill=total_green, font=Font(bold=True, size=12), align="right")
        ri += 2

        # Expenses section header
        ws1.merge_cells(f"A{ri}:D{ri}")
        c = ws1.cell(row=ri, column=1, value="EXPENSES")
        c.font = section_font; c.fill = red_fill
        c.alignment = Alignment(horizontal="center", vertical="center")
        ri += 1

        for line in data["expense_lines"]:
            fill = alt2 if ri % 2 == 0 else None
            add_cell(ws1, ri, 1, "Expense", fill=fill)
            add_cell(ws1, ri, 2, line["code"], fill=fill)
            add_cell(ws1, ri, 3, line["name"], fill=fill)
            add_cell(ws1, ri, 4, line["amount"], fill=fill, align="right")
            ri += 1

        add_cell(ws1, ri, 1, "", fill=total_red)
        add_cell(ws1, ri, 2, "", fill=total_red)
        add_cell(ws1, ri, 3, "TOTAL EXPENSES", fill=total_red, font=total_font)
        add_cell(ws1, ri, 4, data["total_expense"], fill=total_red, font=Font(bold=True, size=12), align="right")
        ri += 2

        # Net profit/loss
        is_profit = data["net_profit"] >= 0
        net_fill = PatternFill("solid", fgColor="B0E0B0") if is_profit else PatternFill("solid", fgColor="E0B0B0")
        net_font = Font(bold=True, size=13, color="1a5a1a" if is_profit else "8a0000")
        ws1.merge_cells(f"A{ri}:C{ri}")
        c = ws1.cell(row=ri, column=1, value="NET PROFIT" if is_profit else "NET LOSS")
        c.font = net_font; c.fill = net_fill
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = bord
        ws1.cell(row=ri, column=2).border = bord
        ws1.cell(row=ri, column=3).border = bord
        add_cell(ws1, ri, 4, abs(data["net_profit"]), fill=net_fill, font=net_font, align="right")
        ws1.row_dimensions[ri].height = 24

        auto_width(ws1)

        # ── Sheet 2: Revenue Entries ──
        ws2 = wb.create_sheet("Revenue Entries")
        for ci, h in enumerate(["Account Code", "Account Name", "Date", "Type", "Description", "Amount (EGP)"], 1):
            c = ws2.cell(row=1, column=ci, value=h)
            c.fill = green_fill; c.font = white_font; c.border = bord
            c.alignment = Alignment(horizontal="center", vertical="center")
        ws2.row_dimensions[1].height = 22
        ri = 2
        for line in data["revenue_lines"]:
            for entry in line["entries"]:
                fill = alt if ri % 2 == 0 else None
                for ci, val in enumerate([line["code"], line["name"], entry["date"], entry["ref_type"], entry["description"], entry["amount"]], 1):
                    c = ws2.cell(row=ri, column=ci, value=val)
                    c.border = bord
                    c.alignment = Alignment(vertical="center")
                    if fill: c.fill = fill
                ri += 1
        auto_width(ws2)

        # ── Sheet 3: Expense Entries ──
        ws3 = wb.create_sheet("Expense Entries")
        for ci, h in enumerate(["Account Code", "Account Name", "Date", "Type", "Description", "Amount (EGP)"], 1):
            c = ws3.cell(row=1, column=ci, value=h)
            c.fill = red_fill; c.font = white_font; c.border = bord
            c.alignment = Alignment(horizontal="center", vertical="center")
        ws3.row_dimensions[1].height = 22
        ri = 2
        for line in data["expense_lines"]:
            for entry in line["entries"]:
                fill = alt2 if ri % 2 == 0 else None
                for ci, val in enumerate([line["code"], line["name"], entry["date"], entry["ref_type"], entry["description"], entry["amount"]], 1):
                    c = ws3.cell(row=ri, column=ci, value=val)
                    c.border = bord
                    c.alignment = Alignment(vertical="center")
                    if fill: c.fill = fill
                ri += 1
        auto_width(ws3)

        buf = io.BytesIO()
        wb.save(buf); buf.seek(0)
        return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=pl_report_{date.today()}.xlsx"})
    except ImportError:
        raise Exception("Run: pip install openpyxl --break-system-packages")


# ── TRANSACTIONS ────────────────────────────────────────
@router.get("/api/transactions")
def transactions_report(
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    source:    Optional[str] = None,
    db: Session = Depends(get_db)
):
    from app.models.customer import Customer
    d_from = datetime.fromisoformat(date_from) if date_from else datetime(2000,1,1)
    d_to   = datetime.fromisoformat(date_to).replace(hour=23,minute=59) if date_to else datetime.utcnow()
    rows   = []

    # POS
    if not source or source == "pos":
        for inv in db.query(Invoice).filter(Invoice.created_at >= d_from, Invoice.created_at <= d_to).order_by(Invoice.created_at.desc()).all():
            customer = db.query(Customer).filter(Customer.id == inv.customer_id).first()
            cname    = customer.name if customer else "Walk-in"
            items    = db.query(InvoiceItem).filter(InvoiceItem.invoice_id == inv.id).all()
            disc_per = round(float(inv.discount)/len(items),2) if items else 0
            disc_pct = round(float(inv.discount)/float(inv.subtotal)*100,1) if float(inv.subtotal)>0 else 0
            for item in items:
                rows.append({
                    "date":           inv.created_at.strftime("%Y-%m-%d %H:%M") if inv.created_at else "—",
                    "invoice_number": inv.invoice_number,
                    "source":         "POS",
                    "customer":       cname,
                    "sku":            item.sku or "—",
                    "user_name":      inv.user.name if inv.user else "—",
                    "product":        item.name or "—",
                    "qty":            float(item.qty),
                    "unit_price":     float(item.unit_price),
                    "line_total":     float(item.total),
                    "discount":       disc_per,
                    "discount_pct":   disc_pct,
                    "payment_method": inv.payment_method or "cash",
                    "invoice_total":  float(inv.total),
                    "status":         inv.status,
                    "row_type":       "sale",
                })

    # B2B
    if not source or source == "b2b":
        for inv in db.query(B2BInvoice).filter(B2BInvoice.created_at >= d_from, B2BInvoice.created_at <= d_to).order_by(B2BInvoice.created_at.desc()).all():
            client = db.query(B2BClient).filter(B2BClient.id == inv.client_id).first()
            cname  = client.name if client else "—"
            items  = db.query(B2BInvoiceItem).filter(B2BInvoiceItem.invoice_id == inv.id).all()
            disc_per = round(float(inv.discount)/len(items),2) if items else 0
            disc_pct = round(float(inv.discount)/float(inv.subtotal)*100,1) if float(inv.subtotal)>0 else 0
            for item in items:
                product = db.query(Product).filter(Product.id == item.product_id).first()
                rows.append({
                    "date":           inv.created_at.strftime("%Y-%m-%d %H:%M") if inv.created_at else "—",
                    "invoice_number": inv.invoice_number,
                    "user_name":      inv.user.name if inv.user else "—",
                    "source":         f"B2B ({inv.invoice_type.replace('_',' ').title()})",
                    "customer":       cname,
                    "sku":            product.sku if product else "—",
                    "product":        product.name if product else "—",
                    "qty":            float(item.qty),
                    "unit_price":     float(item.unit_price),
                    "line_total":     float(item.total),
                    "discount":       disc_per,
                    "discount_pct":   disc_pct,
                    "payment_method": inv.payment_method or inv.invoice_type,
                    "invoice_total":  float(inv.total),
                    "status":         inv.status,
                    "row_type":       "sale",
                })

    # Refunds
    if not source or source == "refund":
        from app.models.refund import RetailRefund, RetailRefundItem
        from app.models.user import User
        for ref in db.query(RetailRefund).filter(RetailRefund.created_at >= d_from, RetailRefund.created_at <= d_to).order_by(RetailRefund.created_at.desc()).all():
            customer = db.query(Customer).filter(Customer.id == ref.customer_id).first()
            cname    = customer.name if customer else "—"
            user     = db.query(User).filter(User.id == ref.user_id).first() if ref.user_id else None
            items    = db.query(RetailRefundItem).filter(RetailRefundItem.refund_id == ref.id).all()
            for item in items:
                product = db.query(Product).filter(Product.id == item.product_id).first()
                rows.append({
                    "date":           ref.created_at.strftime("%Y-%m-%d %H:%M") if ref.created_at else "—",
                    "invoice_number": ref.refund_number,
                    "user_name":      user.name if user else "—",
                    "source":         "Refund",
                    "customer":       cname,
                    "sku":            product.sku if product else "—",
                    "product":        product.name if product else "—",
                    "qty":            -float(item.qty),
                    "unit_price":     float(item.unit_price),
                    "line_total":     -float(item.total),
                    "discount":       0,
                    "discount_pct":   0,
                    "payment_method": ref.refund_method,
                    "invoice_total":  -float(ref.total),
                    "status":         "refunded",
                    "row_type":       "refund",
                    "reason":         ref.reason or "—",
                })

    rows.sort(key=lambda x: x["date"], reverse=True)
    total_revenue  = sum(r["line_total"] for r in rows)
    total_qty      = sum(r["qty"]        for r in rows)
    total_discount = sum(r["discount"]   for r in rows)
    return {
        "rows":           rows,
        "total_rows":     len(rows),
        "total_revenue":  round(total_revenue,  2),
        "total_qty":      round(total_qty,       2),
        "total_discount": round(total_discount,  2),
    }

@router.get("/export/transactions", dependencies=[Depends(require_permission("action_export_excel"))])
def export_transactions(date_from: str = None, date_to: str = None, source: str = None, db: Session = Depends(get_db)):
    data = transactions_report(date_from=date_from, date_to=date_to, source=source, db=db)
    headers = ["Date","Invoice #","Source","Customer","Performed By","SKU","Product","QTY","Unit Price","Line Total","Discount (EGP)","Discount %","Payment Method","Invoice Total","Status"]
    rows    = [[r["date"],r["invoice_number"],r["source"],r["customer"],r["user_name"],r["sku"],r["product"],r["qty"],r["unit_price"],r["line_total"],r["discount"],r["discount_pct"],r["payment_method"],r["invoice_total"],r["status"]] for r in data["rows"]]
    rows.append(["","","","","","","TOTAL",data["total_qty"],"TOTAL REVENUE","","","","","",""])
    buf = to_xlsx(headers, rows, "Transactions")
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=transactions_{date.today()}.xlsx"})


# ── UI ─────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
def reports_ui():
    return """<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Reports — Thunder ERP</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root{
    --bg:#060810;--card:#0f1424;--card2:#151c30;
    --border:rgba(255,255,255,0.06);--border2:rgba(255,255,255,0.11);
    --green:#00ff9d;--blue:#4d9fff;--orange:#fb923c;--teal:#2dd4bf;
    --danger:#ff4d6d;--warn:#ffb547;--lime:#84cc16;--purple:#a855f7;
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
nav{position:sticky;top:0;z-index:100;display:flex;align-items:center;gap:8px;padding:0 24px;height:58px;background:rgba(10,13,24,.92);backdrop-filter:blur(20px);border-bottom:1px solid var(--border);}
.logo{font-size:17px;font-weight:900;background:linear-gradient(135deg,var(--green),var(--blue));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;text-decoration:none;display:flex;align-items:center;gap:8px;margin-right:10px;}
.nav-link{padding:7px 12px;border-radius:8px;color:var(--sub);font-size:12px;font-weight:600;text-decoration:none;transition:all .2s;}
.nav-link:hover{background:rgba(255,255,255,.05);color:var(--text);}
.nav-link.active{background:rgba(132,204,22,.1);color:var(--lime);}
.nav-spacer{flex:1;}
.content{max-width:1300px;margin:0 auto;padding:28px 24px;display:flex;flex-direction:column;gap:18px;}
.page-title{font-size:24px;font-weight:800;letter-spacing:-.5px;}
.page-sub{color:var(--muted);font-size:13px;margin-top:3px;}
.tabs{display:flex;gap:4px;background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:4px;flex-wrap:wrap;}
.tab{padding:7px 13px;border-radius:9px;font-size:12px;font-weight:700;cursor:pointer;border:none;background:transparent;color:var(--muted);transition:all .2s;font-family:var(--sans);white-space:nowrap;}
.tab.active{background:var(--card2);color:var(--text);}
.section{display:none;flex-direction:column;gap:16px;}
.section.active{display:flex;}
/* FILTER BAR */
.filter-bar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:12px 16px;}
.filter-bar label{font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);white-space:nowrap;}
.filter-bar input[type=date]{background:var(--card2);border:1px solid var(--border2);border-radius:8px;padding:7px 11px;color:var(--text);font-family:var(--sans);font-size:13px;outline:none;}
.filter-bar input[type=date]:focus{border-color:var(--lime);}
.filter-sep{width:1px;height:24px;background:var(--border2);margin:0 4px;}
.btn{display:inline-flex;align-items:center;gap:6px;padding:8px 14px;border-radius:var(--r);font-family:var(--sans);font-size:12px;font-weight:700;cursor:pointer;border:none;transition:all .2s;white-space:nowrap;}
.btn-lime {background:linear-gradient(135deg,var(--lime),var(--green));color:#0a1a00;}
.btn-lime:hover{filter:brightness(1.1);}
.btn-excel{background:linear-gradient(135deg,#217346,#1e6b3f);color:white;}
.btn-excel:hover{filter:brightness(1.1);}
.btn-print{background:var(--card2);border:1px solid var(--border2);color:var(--sub);}
.btn-print:hover{border-color:var(--blue);color:var(--blue);}
/* STATS */
.stats-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;}
.stat-card{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:14px 16px;position:relative;overflow:hidden;}
.stat-card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;}
.sc-green::before {background:linear-gradient(90deg,var(--green),transparent);}
.sc-blue::before  {background:linear-gradient(90deg,var(--blue),transparent);}
.sc-orange::before{background:linear-gradient(90deg,var(--orange),transparent);}
.sc-danger::before{background:linear-gradient(90deg,var(--danger),transparent);}
.sc-teal::before  {background:linear-gradient(90deg,var(--teal),transparent);}
.stat-label{font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-bottom:6px;}
.stat-value{font-family:var(--mono);font-size:22px;font-weight:700;}
.sv-green {color:var(--green);}
.sv-blue  {color:var(--blue);}
.sv-orange{color:var(--orange);}
.sv-danger{color:var(--danger);}
.sv-teal  {color:var(--teal);}
/* TABLE */
.table-wrap{background:var(--card);border:1px solid var(--border);border-radius:var(--r);overflow:hidden;}
.table-title{padding:12px 16px;font-size:11px;font-weight:700;letter-spacing:.5px;text-transform:uppercase;border-bottom:1px solid var(--border);color:var(--muted);display:flex;justify-content:space-between;align-items:center;}
table{width:100%;border-collapse:collapse;}
thead{background:var(--card2);}
th{text-align:left;font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);padding:10px 14px;}
td{padding:10px 14px;border-top:1px solid var(--border);color:var(--sub);font-size:13px;}
tr:hover td{background:rgba(255,255,255,.02);}
td.name{color:var(--text);font-weight:600;}
td.mono{font-family:var(--mono);}
/* CHARTS */
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:14px;}
.chart-card{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:16px;}
.chart-title{font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-bottom:12px;}
.bar-row{display:flex;align-items:center;gap:10px;margin-bottom:8px;}
.bar-label{font-size:12px;color:var(--sub);width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex-shrink:0;}
.bar-track{flex:1;background:var(--card2);border-radius:4px;height:8px;overflow:hidden;}
.bar-fill{height:100%;border-radius:4px;transition:width .5s ease;}
.bar-val{font-family:var(--mono);font-size:11px;width:70px;text-align:right;flex-shrink:0;}
/* BADGES */
.badge{display:inline-flex;padding:2px 8px;border-radius:20px;font-size:11px;font-weight:700;}
.badge-low{background:rgba(255,77,109,.1);color:var(--danger);}
.badge-ok {background:rgba(0,255,157,.1);color:var(--green);}
/* P&L */
.pl-section{background:var(--card);border:1px solid var(--border);border-radius:var(--r);overflow:hidden;margin-bottom:8px;}
.pl-header{background:var(--card2);padding:10px 16px;font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);}
.pl-row{display:flex;justify-content:space-between;padding:9px 16px;border-top:1px solid var(--border);font-size:13px;}
.pl-row:hover{background:rgba(255,255,255,.02);}
.pl-total{font-weight:700;font-size:14px;background:rgba(0,0,0,.15);}
.pl-net{font-size:16px;font-weight:800;padding:12px 16px;}
/* TOAST */
.toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%) translateY(16px);background:var(--card2);border:1px solid var(--border2);border-radius:var(--r);padding:12px 20px;font-size:13px;font-weight:600;color:var(--text);opacity:0;pointer-events:none;transition:opacity .25s,transform .25s;z-index:999;}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0);}
::-webkit-scrollbar{width:4px;}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:4px;}
@media(max-width:700px){.two-col{grid-template-columns:1fr;}}

/* ══════════════ PRINT STYLES ══════════════ */
.print-header{display:none;}
@media print{
    nav,.filter-bar,.tabs,.no-print{display:none!important;}
    body{background:white;color:#111;font-family:Arial,sans-serif;padding:0;}
    .content{padding:10px;max-width:100%;}
    .section{display:flex!important;}
    .section:not(.active){display:none!important;}
    .print-header{display:flex;align-items:center;justify-content:space-between;padding-bottom:14px;margin-bottom:20px;border-bottom:3px solid #2a7a2a;}
    .stat-card{border:1px solid #ddd!important;background:white!important;}
    .stat-label{color:#555!important;}
    .stat-value{color:#111!important;}
    .table-wrap{border:1px solid #ddd!important;background:white!important;}
    table thead{background:#2a7a2a!important;-webkit-print-color-adjust:exact;print-color-adjust:exact;}
    th{color:white!important;}
    td{color:#333!important;border-top:1px solid #eee!important;}
    td.name{color:#111!important;}
    .table-title{color:#555!important;background:white!important;}
    .chart-card{border:1px solid #ddd!important;background:white!important;}
    .chart-title{color:#555!important;}
    .bar-label,.bar-val{color:#333!important;}
    .bar-track{background:#eee!important;}
    .bar-fill{-webkit-print-color-adjust:exact;print-color-adjust:exact;}
    .pl-section{border:1px solid #ddd!important;background:white!important;}
    .pl-header{background:#f0f0f0!important;color:#555!important;-webkit-print-color-adjust:exact;print-color-adjust:exact;}
    .pl-row{color:#333!important;}
    .pl-total{background:#f5f5f5!important;-webkit-print-color-adjust:exact;print-color-adjust:exact;}
    .badge-low{background:#fee!important;color:#c00!important;-webkit-print-color-adjust:exact;print-color-adjust:exact;}
    .badge-ok {background:#efe!important;color:#2a7a2a!important;-webkit-print-color-adjust:exact;print-color-adjust:exact;}
    .two-col{grid-template-columns:1fr 1fr;}
}
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
    <a href="/b2b/"       class="nav-link">B2B</a>
    <a href="/reports/"   class="nav-link active">Reports</a>
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
    <div class="no-print">
        <div class="page-title">📊 Reports</div>
        <div class="page-sub">Filter by date period · Export to Excel · Print with logo</div>
    </div>

    <div class="tabs no-print">
        <button class="tab active" onclick="switchTab('sales')">📈 Sales</button>
        <button class="tab"        onclick="switchTab('transactions')">🧾 Transactions</button>
        <button class="tab"        onclick="switchTab('b2b')">🤝 B2B Statement</button>
        <button class="tab"        onclick="switchTab('inventory')">📦 Inventory</button>
        <button class="tab"        onclick="switchTab('farm')">🌾 Farm Intake</button>
        <button class="tab"        onclick="switchTab('spoilage')">🗑 Spoilage</button>
        <button class="tab"        onclick="switchTab('production')">⚙️ Production</button>
        <button class="tab"        onclick="switchTab('pl')">💰 P&amp;L</button>
    </div>

    <!-- ──────────── SALES ──────────── -->
    <div id="section-sales" class="section active">
        <div class="print-header">
            <div style="display:flex;align-items:center;gap:14px">
                <img src="/static/Logo.png" style="height:120px;object-fit:contain">
                <div>
                    <div style="font-size:16px;font-weight:900;color:#2a7a2a">Habiba Organic Farm</div>
                    <div style="font-size:11px;color:#666;margin-top:2px">Commercial registry: 126278 &nbsp;|&nbsp; Tax ID: 560042604</div>
                </div>
            </div>
            <div style="text-align:right">
                <div style="font-size:18px;font-weight:800;color:#2a7a2a">Sales Report</div>
                <div style="font-size:12px;color:#666;margin-top:4px" id="ph-sales-dates"></div>
            </div>
        </div>
        <div class="filter-bar no-print">
            <label>From</label><input type="date" id="sales-from">
            <label>To</label>  <input type="date" id="sales-to">
            <div class="filter-sep"></div>
            <button class="btn btn-lime"  onclick="loadSales()">Apply</button>
            <button class="btn btn-excel" onclick="exportSection('sales')">⬇ Excel</button>
            <button class="btn btn-print" onclick="window.print()">🖨 Print</button>
        </div>
        <div class="stats-row">
            <div class="stat-card sc-green"><div class="stat-label">Net Revenue</div><div class="stat-value sv-green" id="s-total">—</div></div>
            <div class="stat-card sc-blue" ><div class="stat-label">POS Revenue</div> <div class="stat-value sv-blue"  id="s-pos">—</div></div>
            <div class="stat-card sc-orange"><div class="stat-label">B2B Revenue</div><div class="stat-value sv-orange" id="s-b2b">—</div></div>
            <div class="stat-card" style="border-color:rgba(255,77,109,.3);background:rgba(255,77,109,.04);position:relative;overflow:hidden;">
                <div style="position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,#ff4d6d,transparent)"></div>
                <div class="stat-label" style="color:#ff4d6d">↩ Refunds</div>
                <div class="stat-value" style="color:#ff4d6d;font-family:var(--mono)" id="s-refunds">—</div>
                <div style="font-size:11px;color:rgba(255,77,109,.6);margin-top:4px" id="s-refund-count">— refunds</div>
            </div>
        </div>
        <div class="two-col">
            <div class="table-wrap">
                <div class="table-title">Daily Breakdown</div>
                <table><thead><tr><th>Date</th><th>POS</th><th>B2B</th><th style="color:#ff4d6d">Refunds</th><th>Net Total</th></tr></thead>
                <tbody id="sales-daily"></tbody></table>
            </div>
            <div class="chart-card">
                <div class="chart-title">Top Products by Revenue</div>
                <div id="sales-top"></div>
            </div>
        </div>
        <div id="sales-records"></div>
    </div>

    <!-- ──────────── B2B ──────────── -->
    <!-- ── TRANSACTIONS ── -->
    <div id="section-transactions" class="section">
        <div class="print-header">
            <div style="display:flex;align-items:center;gap:14px">
                <img src="/static/Logo.png" style="height:120px;object-fit:contain">
                <div>
                    <div style="font-size:20px;font-weight:900;color:#2a7a2a">Habiba Organic Farm</div>
                    <div style="font-size:13px;color:#555;margin-top:2px">Transactions Report</div>
                    <div style="font-size:12px;color:#555" id="ph-tx-dates"></div>
                </div>
            </div>
        </div>
        <div class="filter-bar no-print">
            <label>From</label><input type="date" id="tx-from">
            <label>To</label><input type="date" id="tx-to">
            <select id="tx-source">
                <option value="">All Sources</option>
                <option value="pos">POS Only</option>
                <option value="b2b">B2B Only</option>
                <option value="refund">Refunds Only</option>
            </select>
            <button class="btn btn-lime" onclick="loadTransactions()">Apply</button>
            <div class="filter-sep"></div>
            <button class="btn btn-excel no-print" onclick="exportSection('transactions')">Export Excel</button>
            <button class="btn-print no-print" onclick="printSection()">🖨 Print</button>
        </div>
        <div class="stats-row">
            <div class="stat-card lime" ><div class="stat-label">Total Lines</div><div class="stat-value lime"   id="tx-count">—</div></div>
            <div class="stat-card green"><div class="stat-label">Total Revenue</div><div class="stat-value green" id="tx-revenue">—</div></div>
            <div class="stat-card blue" ><div class="stat-label">Total Qty Sold</div><div class="stat-value blue"  id="tx-qty">—</div></div>
            <div class="stat-card warn" ><div class="stat-label">Total Discount</div><div class="stat-value warn"  id="tx-discount">—</div></div>
        </div>
        <div class="table-wrap">
            <div class="table-title">All Transactions</div>
            <div style="overflow-x:auto">
            <table>
                <thead><tr>
                    <th>Date</th><th>Invoice #</th><th>Source</th><th>Customer</th><th>By</th>
                    <th>SKU</th><th>Product</th><th>QTY</th><th>Unit Price</th>
                    <th>Line Total</th><th>Discount</th><th>Disc %</th>
                    <th>Payment</th><th>Inv. Total</th><th>Status</th>
                </tr></thead>
                <tbody id="tx-body"></tbody>
            </table>
            </div>
        </div>
    </div>

    <div id="section-b2b" class="section">
        <div class="print-header">
            <div style="display:flex;align-items:center;gap:14px">
                <img src="/static/Logo.png" style="height:120px;object-fit:contain">
                <div>
                    <div style="font-size:16px;font-weight:900;color:#2a7a2a">Habiba Organic Farm</div>
                    <div style="font-size:11px;color:#666;margin-top:2px">Commercial registry: 126278 &nbsp;|&nbsp; Tax ID: 560042604</div>
                </div>
            </div>
            <div style="text-align:right">
                <div style="font-size:18px;font-weight:800;color:#2a7a2a">B2B Client Statement</div>
                <div style="font-size:12px;color:#666;margin-top:4px" id="ph-b2b-dates"></div>
            </div>
        </div>
        <div class="filter-bar no-print">
            <label>From</label><input type="date" id="b2b-from">
            <label>To</label>  <input type="date" id="b2b-to">
            <div class="filter-sep"></div>
            <button class="btn btn-lime"  onclick="loadB2B()">Apply</button>
            <button class="btn btn-excel" onclick="exportSection('b2b')">⬇ Excel</button>
            <button class="btn btn-print" onclick="window.print()">🖨 Print</button>
        </div>
        <div class="stats-row">
            <div class="stat-card sc-blue"  ><div class="stat-label">Clients</div>      <div class="stat-value sv-blue"   id="b-clients">—</div></div>
            <div class="stat-card sc-green" ><div class="stat-label">Total Invoiced</div><div class="stat-value sv-green"  id="b-invoiced">—</div></div>
            <div class="stat-card sc-danger"><div class="stat-label">Outstanding</div>   <div class="stat-value sv-danger" id="b-outstanding">—</div></div>
        </div>
        <div class="table-wrap">
            <div class="table-title">Client Statements</div>
            <table><thead><tr><th>Client</th><th>Phone</th><th>Terms</th><th>Invoiced</th><th>Paid</th><th>Outstanding</th><th>Invoices</th></tr></thead>
            <tbody id="b2b-body"></tbody></table>
        </div>
    </div>

    <!-- ──────────── INVENTORY ──────────── -->
    <div id="section-inventory" class="section">
        <div class="print-header">
            <div style="display:flex;align-items:center;gap:14px">
                <img src="/static/Logo.png" style="height:120px;object-fit:contain">
                <div>
                    <div style="font-size:16px;font-weight:900;color:#2a7a2a">Habiba Organic Farm</div>
                    <div style="font-size:11px;color:#666;margin-top:2px">Commercial registry: 126278 &nbsp;|&nbsp; Tax ID: 560042604</div>
                </div>
            </div>
            <div style="text-align:right">
                <div style="font-size:18px;font-weight:800;color:#2a7a2a">Inventory Report</div>
                <div style="font-size:12px;color:#666;margin-top:4px" id="ph-inv-dates"></div>
            </div>
        </div>
        <div class="filter-bar no-print">
            <span style="font-size:12px;color:var(--muted)">Current stock snapshot</span>
            <div class="filter-sep"></div>
            <button class="btn btn-lime"  onclick="loadInventory()">Refresh</button>
            <button class="btn btn-excel" onclick="exportSection('inventory')">⬇ Excel</button>
            <button class="btn btn-print" onclick="window.print()">🖨 Print</button>
        </div>
        <div class="stats-row">
            <div class="stat-card sc-blue"  ><div class="stat-label">Products</div>   <div class="stat-value sv-blue"   id="inv-count">—</div></div>
            <div class="stat-card sc-green" ><div class="stat-label">Stock Value</div><div class="stat-value sv-green"  id="inv-value">—</div></div>
            <div class="stat-card sc-danger"><div class="stat-label">Low Stock</div>   <div class="stat-value sv-danger" id="inv-low">—</div></div>
        </div>
        <div class="table-wrap">
            <div class="table-title">Stock Levels</div>
            <table><thead><tr><th>SKU</th><th>Product</th><th>Stock</th><th>Unit</th><th>Price</th><th>Stock Value</th><th>Status</th></tr></thead>
            <tbody id="inv-body"></tbody></table>
        </div>
    </div>

    <!-- ──────────── FARM ──────────── -->
    <div id="section-farm" class="section">
        <div class="print-header">
            <div style="display:flex;align-items:center;gap:14px">
                <img src="/static/Logo.png" style="height:120px;object-fit:contain">
                <div>
                    <div style="font-size:16px;font-weight:900;color:#2a7a2a">Habiba Organic Farm</div>
                    <div style="font-size:11px;color:#666;margin-top:2px">Commercial registry: 126278 &nbsp;|&nbsp; Tax ID: 560042604</div>
                </div>
            </div>
            <div style="text-align:right">
                <div style="font-size:18px;font-weight:800;color:#2a7a2a">Farm Intake Report</div>
                <div style="font-size:12px;color:#666;margin-top:4px" id="ph-farm-dates"></div>
            </div>
        </div>
        <div class="filter-bar no-print">
            <label>From</label><input type="date" id="farm-from">
            <label>To</label>  <input type="date" id="farm-to">
            <div class="filter-sep"></div>
            <button class="btn btn-lime"  onclick="loadFarm()">Apply</button>
            <button class="btn btn-excel" onclick="exportSection('farm')">⬇ Excel</button>
            <button class="btn btn-print" onclick="window.print()">🖨 Print</button>
        </div>
        <div id="farm-content"></div>
    </div>

    <!-- ──────────── SPOILAGE ──────────── -->
    <div id="section-spoilage" class="section">
        <div class="print-header">
            <div style="display:flex;align-items:center;gap:14px">
                <img src="/static/Logo.png" style="height:120px;object-fit:contain">
                <div>
                    <div style="font-size:16px;font-weight:900;color:#2a7a2a">Habiba Organic Farm</div>
                    <div style="font-size:11px;color:#666;margin-top:2px">Commercial registry: 126278 &nbsp;|&nbsp; Tax ID: 560042604</div>
                </div>
            </div>
            <div style="text-align:right">
                <div style="font-size:18px;font-weight:800;color:#2a7a2a">Spoilage Report</div>
                <div style="font-size:12px;color:#666;margin-top:4px" id="ph-spl-dates"></div>
            </div>
        </div>
        <div class="filter-bar no-print">
            <label>From</label><input type="date" id="spl-from">
            <label>To</label>  <input type="date" id="spl-to">
            <div class="filter-sep"></div>
            <button class="btn btn-lime"  onclick="loadSpoilage()">Apply</button>
            <button class="btn btn-excel" onclick="exportSection('spoilage')">⬇ Excel</button>
            <button class="btn btn-print" onclick="window.print()">🖨 Print</button>
        </div>
        <div class="stats-row">
            <div class="stat-card sc-danger"><div class="stat-label">Total Records</div><div class="stat-value sv-danger" id="spl-count">—</div></div>
            <div class="stat-card sc-orange"><div class="stat-label">Total Qty Lost</div><div class="stat-value sv-orange" id="spl-qty">—</div></div>
        </div>
        <div class="two-col">
            <div class="chart-card"><div class="chart-title">By Product</div><div id="spl-by-product"></div></div>
            <div class="chart-card"><div class="chart-title">By Reason</div> <div id="spl-by-reason"></div></div>
        </div>
        <div class="table-wrap">
            <div class="table-title">All Records</div>
            <table><thead><tr><th>Ref #</th><th>Product</th><th>Qty</th><th>Reason</th><th>Farm</th><th>Date</th><th>By</th><th>Notes</th></tr></thead>
            <tbody id="spl-body"></tbody></table>
        </div>
    </div>

    <!-- ──────────── PRODUCTION ──────────── -->
    <div id="section-production" class="section">
        <div class="print-header">
            <div style="display:flex;align-items:center;gap:14px">
                <img src="/static/Logo.png" style="height:120px;object-fit:contain">
                <div>
                    <div style="font-size:16px;font-weight:900;color:#2a7a2a">Habiba Organic Farm</div>
                    <div style="font-size:11px;color:#666;margin-top:2px">Commercial registry: 126278 &nbsp;|&nbsp; Tax ID: 560042604</div>
                </div>
            </div>
            <div style="text-align:right">
                <div style="font-size:18px;font-weight:800;color:#2a7a2a">Production Report</div>
                <div style="font-size:12px;color:#666;margin-top:4px" id="ph-prod-dates"></div>
            </div>
        </div>
        <div class="filter-bar no-print">
            <label>From</label><input type="date" id="prod-from">
            <label>To</label>  <input type="date" id="prod-to">
            <div class="filter-sep"></div>
            <button class="btn btn-lime"  onclick="loadProduction()">Apply</button>
            <button class="btn btn-excel" onclick="exportSection('production')">⬇ Excel</button>
            <button class="btn btn-print" onclick="window.print()">🖨 Print</button>
        </div>
        <div class="stats-row">
            <div class="stat-card sc-orange"><div class="stat-label">Processing Batches</div><div class="stat-value sv-orange" id="prod-proc">—</div></div>
            <div class="stat-card sc-teal"  ><div class="stat-label">Packaging Runs</div>   <div class="stat-value sv-teal"   id="prod-pkg">—</div></div>
            <div class="stat-card sc-danger"><div class="stat-label">Avg Loss %</div>        <div class="stat-value sv-danger" id="prod-loss">—</div></div>
        </div>
        <div class="table-wrap">
            <div class="table-title">All Batches</div>
            <table><thead><tr><th>Batch #</th><th>Type</th><th>Recipe</th><th>Inputs</th><th>Outputs</th><th>Loss %</th><th>Date</th><th>By</th></tr></thead>
            <tbody id="prod-body"></tbody></table>
        </div>
    </div>

    <!-- ──────────── P&L ──────────── -->
    <div id="section-pl" class="section">
        <div class="print-header">
            <div style="display:flex;align-items:center;gap:14px">
                <img src="/static/Logo.png" style="height:120px;object-fit:contain">
                <div>
                    <div style="font-size:16px;font-weight:900;color:#2a7a2a">Habiba Organic Farm</div>
                    <div style="font-size:11px;color:#666;margin-top:2px">Commercial registry: 126278 &nbsp;|&nbsp; Tax ID: 560042604</div>
                </div>
            </div>
            <div style="text-align:right">
                <div style="font-size:18px;font-weight:800;color:#2a7a2a">Profit &amp; Loss Statement</div>
                <div style="font-size:12px;color:#666;margin-top:4px" id="ph-pl-dates"></div>
            </div>
        </div>
        <div class="filter-bar no-print">
            <label>From</label><input type="date" id="pl-from">
            <label>To</label>  <input type="date" id="pl-to">
            <div class="filter-sep"></div>
            <button class="btn btn-lime"  onclick="loadPL()">Apply</button>
            <button class="btn btn-excel" onclick="exportSection('pl')">⬇ Excel</button>
            <button class="btn btn-print" onclick="window.print()">🖨 Print</button>
        </div>
        <div id="pl-content"></div>
    </div>

</div><!-- end .content -->
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
  function hasPermission(permission){
      return __erpUserRole === "admin" || __erpUserPermissions.has(permission);
  }
  function configureReportsPermissions(){
      const tabMap = [
          {tab:"sales", permission:"tab_reports_sales"},
          {tab:"transactions", permission:"tab_reports_transactions"},
          {tab:"inventory", permission:"tab_reports_inventory"},
          {tab:"pl", permission:"tab_reports_pl"},
      ];
      let firstAllowed = null;
      document.querySelectorAll(".tabs .tab").forEach((btn, index) => {
          const conf = tabMap[index];
          if(!conf) return;
          if(!hasPermission(conf.permission)){
              btn.style.display = "none";
          } else if(!firstAllowed) {
              firstAllowed = conf.tab;
          }
      });
      if(!hasPermission(`tab_reports_${currentTab}`) && firstAllowed){
          currentTab = firstAllowed;
      }
      if(!hasPermission("action_export_excel")){
          document.querySelectorAll(".btn-excel").forEach(btn => btn.style.display = "none");
      }
  }
  requirePageAccess("page_reports");
  applyNavPermissions();
  initializeColorMode();
  setUserInfo();
  let currentTab = "sales";
let toastTimer = null;
configureReportsPermissions();

function switchTab(tab){
    const required = {
        sales: "tab_reports_sales",
        transactions: "tab_reports_transactions",
        inventory: "tab_reports_inventory",
        pl: "tab_reports_pl",
    };
    if(required[tab] && !hasPermission(required[tab])) return;
    currentTab = tab;
    const tabs = ["sales","transactions","b2b","inventory","farm","spoilage","production","pl"];
    document.querySelectorAll(".tab").forEach((btn,i) => btn.classList.toggle("active", tabs[i]===tab));
    document.querySelectorAll(".section").forEach(s => s.classList.remove("active"));
    document.getElementById("section-"+tab).classList.add("active");
    const loaders = {sales:loadSales, transactions:loadTransactions, b2b:loadB2B, inventory:loadInventory, farm:loadFarm, spoilage:loadSpoilage, production:loadProduction, pl:loadPL};
    loaders[tab]();
}

function today(){ return new Date().toISOString().split("T")[0]; }
function monthStart(){ let d=new Date(); d.setDate(1); return d.toISOString().split("T")[0]; }
function yearStart() { let d=new Date(); d.setMonth(0); d.setDate(1); return d.toISOString().split("T")[0]; }
function getRange(f, t){ return {from: document.getElementById(f).value, to: document.getElementById(t).value}; }
function setEl(id, v){ document.getElementById(id).value = v; }
function setPrintDates(id, from, to){ let el=document.getElementById(id); if(el) el.innerText=`Period: ${from}  →  ${to}`; }

function showToast(msg){
    let t=document.getElementById("toast"); t.innerText=msg; t.classList.add("show");
    clearTimeout(toastTimer); toastTimer=setTimeout(()=>t.classList.remove("show"),3000);
}

function exportSection(tab){
    const build = {
        sales:      ()=>{ let r=getRange("sales-from","sales-to"); return `/reports/export/sales?date_from=${r.from}&date_to=${r.to}`; },
        b2b:        ()=>{ let r=getRange("b2b-from","b2b-to");     return `/reports/export/b2b-statement?date_from=${r.from}&date_to=${r.to}`; },
        inventory:  ()=> `/reports/export/inventory`,
        farm:       ()=>{ let r=getRange("farm-from","farm-to");   return `/reports/export/farm-intake?date_from=${r.from}&date_to=${r.to}`; },
        spoilage:   ()=>{ let r=getRange("spl-from","spl-to");     return `/reports/export/spoilage?date_from=${r.from}&date_to=${r.to}`; },
        production: ()=>{ let r=getRange("prod-from","prod-to");   return `/reports/export/production?date_from=${r.from}&date_to=${r.to}`; },
        pl:           ()=>{ let r=getRange("pl-from","pl-to");   return `/reports/export/pl?date_from=${r.from}&date_to=${r.to}`; },
        transactions: ()=>{ let r=getRange("tx-from","tx-to"); let s=document.getElementById("tx-source").value; return `/reports/export/transactions?date_from=${r.from}&date_to=${r.to}${s?"&source="+s:""}`; },
    };
    window.location.href = build[tab]();
    showToast("Downloading Excel...");
}

/* ── TRANSACTIONS ── */
async function loadTransactions(){
    let r      = getRange("tx-from","tx-to");
    let source = document.getElementById("tx-source").value;
    let url    = `/reports/api/transactions?date_from=${r.from}&date_to=${r.to}${source?"&source="+source:""}`;
    let data   = await (await fetch(url)).json();

    document.getElementById("tx-count").innerText   = data.total_rows;
    document.getElementById("tx-revenue").innerText = data.total_revenue.toFixed(2);
    document.getElementById("tx-qty").innerText     = data.total_qty.toFixed(2);
    document.getElementById("tx-discount").innerText= data.total_discount.toFixed(2);
    setPrintDates("ph-tx-dates", r.from, r.to);

    const payColor = (m) => {
        if(!m) return "var(--muted)";
        m = m.toLowerCase();
        if(m.includes("visa") || m.includes("card")) return "var(--blue)";
        if(m.includes("cash"))                        return "var(--green)";
        if(m.includes("consign"))                     return "var(--teal)";
        if(m.includes("transfer"))                    return "var(--purple)";
        if(m.includes("credit") || m.includes("exchange") || m.includes("refund")) return "var(--danger)";
        return "var(--sub)";
    };
    const statusColor = (s) => {
        if(s==="paid")      return "var(--green)";
        if(s==="unpaid")    return "var(--warn)";
        if(s==="partial")   return "var(--blue)";
        if(s==="consignment") return "var(--teal)";
        if(s==="refunded")  return "var(--danger)";
        return "var(--muted)";
    };

    document.getElementById("tx-body").innerHTML = data.rows.length
        ? data.rows.map(r => {
            const isRef = r.row_type === "refund";
            const rowStyle = isRef ? 'style="background:rgba(255,77,109,.04);"' : '';
            const numColor = isRef ? "var(--danger)" : "var(--green)";
            const refBadge = isRef ? `<br><span style="font-size:9px;font-weight:800;letter-spacing:.5px;color:var(--danger);background:rgba(255,77,109,.15);padding:1px 5px;border-radius:4px">↩ REFUND</span>` : "";
            return `<tr ${rowStyle}>
                <td class="mono" style="font-size:11px;white-space:nowrap">${r.date}</td>
                <td class="mono" style="font-size:11px;color:${isRef?"var(--danger)":"var(--lime)"}">${r.invoice_number}${refBadge}</td>
                <td style="font-size:11px;color:${isRef?"var(--danger)":"var(--sub)"}">${r.source}</td>
                <td class="name" style="white-space:nowrap">${r.customer}</td>
                <td style="font-size:12px;color:var(--muted);white-space:nowrap">${r.user_name}</td>
                <td class="mono" style="font-size:11px;color:var(--muted)">${r.sku}</td>
                <td style="font-weight:600;white-space:nowrap">${r.product}${isRef&&r.reason?`<br><span style="font-size:10px;color:var(--muted);font-weight:400">${r.reason}</span>`:""}</td>
                <td class="mono" style="color:${isRef?"var(--danger)":"var(--blue)"};font-weight:700">${r.qty.toFixed(2)}</td>
                <td class="mono">${r.unit_price.toFixed(2)}</td>
                <td class="mono" style="color:${numColor};font-weight:700">${isRef?"−":""}${Math.abs(r.line_total).toFixed(2)}</td>
                <td class="mono" style="color:${r.discount>0?"var(--warn)":"var(--muted)"}">${r.discount>0?"-"+r.discount.toFixed(2):"—"}</td>
                <td class="mono" style="color:${r.discount_pct>0?"var(--warn)":"var(--muted)"}">${r.discount_pct>0?r.discount_pct.toFixed(1)+"%":"—"}</td>
                <td style="font-size:12px;font-weight:700;color:${payColor(r.payment_method)}">${r.payment_method}</td>
                <td class="mono" style="font-weight:700;color:${isRef?"var(--danger)":"inherit"}">${isRef?"−":""}${Math.abs(r.invoice_total).toFixed(2)}</td>
                <td><span style="font-size:11px;font-weight:700;padding:2px 8px;border-radius:20px;background:rgba(0,0,0,.2);color:${statusColor(r.status)}">${r.status}</span></td>
              </tr>`;
        }).join("")
        : `<tr><td colspan="15" style="text-align:center;color:var(--muted);padding:40px">No transactions in this period</td></tr>`;
}

/* ── SALES ── */
async function loadSales(){
    let r = getRange("sales-from","sales-to");
    let data = await (await fetch(`/reports/api/sales?date_from=${r.from}&date_to=${r.to}`)).json();
    document.getElementById("s-total").innerText   = data.grand_total.toFixed(2);
    document.getElementById("s-pos").innerText     = data.pos_total.toFixed(2);
    document.getElementById("s-b2b").innerText     = data.b2b_total.toFixed(2);
    document.getElementById("s-refunds").innerText = data.refund_total > 0 ? "−" + data.refund_total.toFixed(2) : "0.00";
    document.getElementById("s-refund-count").innerText = data.refund_count + " refund" + (data.refund_count !== 1 ? "s" : "") + "  ·  " + data.pos_count + " POS orders";
    setPrintDates("ph-sales-dates", data.date_from, data.date_to);

    // Daily breakdown with refund column
    document.getElementById("sales-daily").innerHTML = data.daily.length
        ? data.daily.map(d=>`<tr>
            <td class="mono">${d.date}</td>
            <td class="mono" style="color:var(--blue)">${d.pos.toFixed(2)}</td>
            <td class="mono" style="color:var(--orange)">${d.b2b.toFixed(2)}</td>
            <td class="mono" style="color:#ff4d6d;font-weight:${d.refunds>0?700:400}">${d.refunds>0?"−"+d.refunds.toFixed(2):"—"}</td>
            <td class="mono" style="color:var(--green);font-weight:700">${d.total.toFixed(2)}</td>
          </tr>`).join("")
        : `<tr><td colspan="5" style="text-align:center;color:var(--muted);padding:30px">No sales in this period</td></tr>`;

    // Top products
    let maxR = data.top_products.length ? data.top_products[0].revenue : 1;
    document.getElementById("sales-top").innerHTML = data.top_products.length
        ? data.top_products.map(p=>`<div class="bar-row">
            <div class="bar-label">${p.name}</div>
            <div class="bar-track"><div class="bar-fill" style="width:${(p.revenue/maxR*100).toFixed(1)}%;background:linear-gradient(90deg,var(--green),var(--lime))"></div></div>
            <div class="bar-val" style="color:var(--green)">${p.revenue.toFixed(0)}</div>
          </div>`).join("")
        : `<div style="color:var(--muted);font-size:13px">No data</div>`;

    // Detailed POS invoices
    let posHtml = `
        <div class="table-title" style="margin-top:28px">POS Invoices — ${data.pos_records.length} transactions</div>
        <div class="table-wrap">
        <table><thead><tr><th>Invoice #</th><th>Date / Time</th><th>By</th><th>Payment</th><th>Items</th><th style="text-align:right">Total (EGP)</th></tr></thead><tbody>`;
    if(data.pos_records.length){
        posHtml += data.pos_records.map(inv=>`
            <tr>
                <td class="mono" style="font-size:11px;color:var(--blue)">${inv.invoice_number}</td>
                <td class="mono" style="font-size:12px;color:var(--muted)">${inv.datetime}</td>
                <td style="font-size:12px;color:var(--muted);white-space:nowrap">${inv.user_name}</td>
                <td style="font-size:12px">${inv.payment}</td>
                <td style="font-size:12px;color:var(--sub)">
                    ${inv.items.map(it=>`<span style="display:inline-block;background:var(--card2);border:1px solid var(--border2);border-radius:5px;padding:1px 7px;margin:2px;white-space:nowrap">${it.qty%1===0?it.qty.toFixed(0):it.qty.toFixed(2)} × ${it.name} <span style="color:var(--muted)">${it.total.toFixed(2)}</span></span>`).join("")}
                </td>
                <td class="mono" style="text-align:right;font-weight:700;color:var(--green)">${inv.total.toFixed(2)}</td>
            </tr>`).join("");
    } else {
        posHtml += `<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:24px">No POS invoices</td></tr>`;
    }
    posHtml += `</tbody></table></div>`;

    // Detailed B2B invoices
    const typeLabel = {cash:"💵 Cash", full_payment:"📋 Full Payment", consignment:"🔄 Consignment"};
    let b2bHtml = `
        <div class="table-title" style="margin-top:22px">B2B Invoices — ${data.b2b_records.length} invoices</div>
        <div class="table-wrap">
        <table><thead><tr><th>Invoice #</th><th>Client</th><th>Date / Time</th><th>By</th><th>Type</th><th>Items</th><th style="text-align:right">Total</th><th style="text-align:right">Paid</th><th style="text-align:right">Balance</th></tr></thead><tbody>`;
    if(data.b2b_records.length){
        b2bHtml += data.b2b_records.map(inv=>`
            <tr>
                <td class="mono" style="font-size:11px;color:var(--blue)">${inv.invoice_number}</td>
                <td class="name" style="font-size:13px">${inv.client}</td>
                <td class="mono" style="font-size:12px;color:var(--muted)">${inv.datetime}</td>
                <td style="font-size:12px;color:var(--muted);white-space:nowrap">${inv.user_name}</td>
                <td style="font-size:12px">${typeLabel[inv.invoice_type]||inv.invoice_type}</td>
                <td style="font-size:12px;color:var(--sub)">
                    ${inv.items.map(it=>`<span style="display:inline-block;background:var(--card2);border:1px solid var(--border2);border-radius:5px;padding:1px 7px;margin:2px;white-space:nowrap">${it.qty%1===0?it.qty.toFixed(0):it.qty.toFixed(2)} × ${it.name} <span style="color:var(--muted)">${it.total.toFixed(2)}</span></span>`).join("")}
                </td>
                <td class="mono" style="text-align:right;font-weight:700">${inv.total.toFixed(2)}</td>
                <td class="mono" style="text-align:right;color:var(--green)">${inv.amount_paid.toFixed(2)}</td>
                <td class="mono" style="text-align:right;color:${inv.balance_due>0?"var(--warn)":"var(--muted)"};font-weight:${inv.balance_due>0?700:400}">${inv.balance_due>0?inv.balance_due.toFixed(2):"—"}</td>
            </tr>`).join("");
    } else {
        b2bHtml += `<tr><td colspan="9" style="text-align:center;color:var(--muted);padding:24px">No B2B invoices</td></tr>`;
    }
    b2bHtml += `</tbody></table></div>`;

    // Refund records — red section
    let refHtml = "";
    if(data.refund_records && data.refund_records.length){
        refHtml = `
        <div style="margin-top:28px;display:flex;align-items:center;gap:14px;padding:14px 18px;background:rgba(255,77,109,.06);border:1px solid rgba(255,77,109,.2);border-radius:12px;">
            <span style="font-size:22px">↩</span>
            <div>
                <div style="font-size:13px;font-weight:700;color:#ff4d6d;letter-spacing:.3px">Refunds — ${data.refund_records.length} refund${data.refund_records.length!==1?"s":""}</div>
                <div style="font-size:11px;color:rgba(255,77,109,.6);margin-top:2px">Deducted from POS revenue</div>
            </div>
            <div style="margin-left:auto;font-family:var(--mono);font-size:22px;font-weight:800;color:#ff4d6d">−${data.refund_total.toFixed(2)}</div>
        </div>
        <div class="table-wrap" style="border-color:rgba(255,77,109,.18);">
        <table>
            <thead style="background:rgba(255,77,109,.05)">
                <tr>
                    <th style="color:#ff4d6d">Ref #</th>
                    <th>Customer</th>
                    <th>Date / Time</th>
                    <th>Processed By</th>
                    <th>Reason</th>
                    <th>Method</th>
                    <th style="text-align:right;color:#ff4d6d">Amount</th>
                </tr>
            </thead>
            <tbody>
            ${data.refund_records.map(r=>`
                <tr>
                    <td class="mono" style="font-size:11px;color:#ff4d6d;font-weight:700">${r.refund_number}</td>
                    <td class="name">${r.customer}</td>
                    <td class="mono" style="font-size:12px;color:var(--muted)">${r.datetime}</td>
                    <td style="font-size:12px;color:var(--muted)">${r.processed_by}</td>
                    <td style="font-size:12px;color:var(--sub)">${r.reason}</td>
                    <td style="font-size:12px">${r.refund_method}</td>
                    <td class="mono" style="text-align:right;font-weight:700;color:#ff4d6d">−${r.total.toFixed(2)}</td>
                </tr>`).join("")}
            </tbody>
        </table></div>`;
    }

    document.getElementById("sales-records").innerHTML = posHtml + b2bHtml + refHtml;
}

/* ── B2B ── */
async function loadB2B(){
    let r = getRange("b2b-from","b2b-to");
    let data = await (await fetch(`/reports/api/b2b-statement?date_from=${r.from}&date_to=${r.to}`)).json();
    document.getElementById("b-clients").innerText     = data.length;
    document.getElementById("b-invoiced").innerText    = data.reduce((s,c)=>s+c.total_invoiced,0).toFixed(2);
    document.getElementById("b-outstanding").innerText = data.reduce((s,c)=>s+c.outstanding,0).toFixed(2);
    setPrintDates("ph-b2b-dates", r.from, r.to);
    document.getElementById("b2b-body").innerHTML = data.length
        ? data.map(c=>`<tr>
            <td class="name">${c.name}</td>
            <td style="font-size:12px">${c.phone}</td>
            <td style="font-size:12px">${c.payment_terms.replace("_"," ")}</td>
            <td class="mono">${c.total_invoiced.toFixed(2)}</td>
            <td class="mono" style="color:var(--green)">${c.total_paid.toFixed(2)}</td>
            <td class="mono" style="color:${c.outstanding>0?"var(--warn)":"var(--muted)"};font-weight:${c.outstanding>0?700:400}">${c.outstanding>0?c.outstanding.toFixed(2):"—"}</td>
            <td class="mono" style="color:var(--muted)">${c.invoice_count}</td>
          </tr>`).join("")
        : `<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:30px">No data for this period</td></tr>`;
}

/* ── INVENTORY ── */
async function loadInventory(){
    let data = await (await fetch("/reports/api/inventory")).json();
    document.getElementById("inv-count").innerText = data.total_products;
    document.getElementById("inv-value").innerText = data.total_value.toFixed(2);
    document.getElementById("inv-low").innerText   = data.low_count;
    document.getElementById("ph-inv-dates").innerText = `As of ${today()}`;
    document.getElementById("inv-body").innerHTML = data.products.map(p=>`<tr>
        <td class="mono" style="font-size:11px;color:var(--muted)">${p.sku}</td>
        <td class="name">${p.name}</td>
        <td class="mono" style="color:${p.low_stock?"var(--danger)":"var(--text)"};font-weight:700">${p.stock.toFixed(2)}</td>
        <td style="font-size:12px;color:var(--muted)">${p.unit}</td>
        <td class="mono">${p.price.toFixed(2)}</td>
        <td class="mono" style="color:var(--blue)">${p.value.toFixed(2)}</td>
        <td><span class="badge ${p.low_stock?"badge-low":"badge-ok"}">${p.low_stock?"Low Stock":"OK"}</span></td>
      </tr>`).join("");
}

/* ── FARM ── */
async function loadFarm(){
    let r = getRange("farm-from","farm-to");
    let data = await (await fetch(`/reports/api/farm-intake?date_from=${r.from}&date_to=${r.to}`)).json();
    setPrintDates("ph-farm-dates", r.from, r.to);
    let summaryHtml = data.farms.map((farm,fi)=>{
        let color = fi===0?"var(--lime)":"var(--teal)";
        let maxQty = farm.products.length ? farm.products[0].total_qty : 1;
        return `<div class="table-wrap" style="margin-bottom:12px">
            <div class="table-title">
                <span>${fi===0?"🌿":"♻️"} ${farm.name}</span>
                <span>${farm.delivery_count} deliveries — ${farm.total_qty.toFixed(1)} total</span>
            </div>
            <div style="padding:14px 16px">
                ${farm.products.length
                    ? farm.products.map(p=>`<div class="bar-row">
                        <div class="bar-label">${p.name}</div>
                        <div class="bar-track"><div class="bar-fill" style="width:${(p.total_qty/maxQty*100).toFixed(1)}%;background:linear-gradient(90deg,${color},var(--green))"></div></div>
                        <div class="bar-val" style="color:${color}">${p.total_qty.toFixed(1)} ${p.unit}</div>
                      </div>`).join("")
                    : `<div style="color:var(--muted);font-size:13px">No deliveries in this period.</div>`}
            </div>
        </div>`;
    }).join("");
    let deliveriesHtml = `<div class="table-wrap">
        <div class="table-title">Delivery Records</div>
        <table><thead><tr><th>Delivery #</th><th>Farm</th><th>Date</th><th>Received By</th><th>Qty</th><th>Items</th><th>By</th><th>Notes</th></tr></thead><tbody>`;
    if(data.deliveries.length){
        deliveriesHtml += data.deliveries.map(d=>`<tr>
            <td class="mono" style="font-size:11px;color:var(--lime)">${d.delivery_number}</td>
            <td class="name">${d.farm}</td>
            <td class="mono" style="font-size:12px;color:var(--muted)">${d.delivery_date}</td>
            <td style="font-size:12px">${d.received_by}</td>
            <td class="mono" style="color:var(--green)">${d.total_qty.toFixed(2)}</td>
            <td class="mono">${d.total_items}</td>
            <td style="font-size:12px;color:var(--muted);white-space:nowrap">${d.user_name}</td>
            <td style="font-size:12px;color:var(--muted)">${d.notes||"—"}</td>
        </tr>`).join("");
    } else {
        deliveriesHtml += `<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:30px">No deliveries in this period</td></tr>`;
    }
    deliveriesHtml += `</tbody></table></div>`;
    document.getElementById("farm-content").innerHTML = summaryHtml + deliveriesHtml;
}


/* ── SPOILAGE ── */
async function loadSpoilage(){
    let r = getRange("spl-from","spl-to");
    let data = await (await fetch(`/reports/api/spoilage?date_from=${r.from}&date_to=${r.to}`)).json();
    document.getElementById("spl-count").innerText = data.total_count;
    document.getElementById("spl-qty").innerText   = data.total_qty.toFixed(2);
    setPrintDates("ph-spl-dates", r.from, r.to);
    let maxP = data.by_product.length ? data.by_product[0].qty : 1;
    document.getElementById("spl-by-product").innerHTML = data.by_product.length
        ? data.by_product.map(p=>`<div class="bar-row">
            <div class="bar-label">${p.name}</div>
            <div class="bar-track"><div class="bar-fill" style="width:${(p.qty/maxP*100).toFixed(1)}%;background:linear-gradient(90deg,var(--danger),var(--orange))"></div></div>
            <div class="bar-val" style="color:var(--danger)">${p.qty.toFixed(1)}</div>
          </div>`).join("")
        : `<div style="color:var(--muted);font-size:13px">No data</div>`;
    let maxR = data.by_reason.length ? data.by_reason[0].qty : 1;
    document.getElementById("spl-by-reason").innerHTML = data.by_reason.length
        ? data.by_reason.map(r=>`<div class="bar-row">
            <div class="bar-label">${r.reason}</div>
            <div class="bar-track"><div class="bar-fill" style="width:${(r.qty/maxR*100).toFixed(1)}%;background:linear-gradient(90deg,var(--warn),var(--orange))"></div></div>
            <div class="bar-val" style="color:var(--warn)">${r.qty.toFixed(1)}</div>
          </div>`).join("")
        : `<div style="color:var(--muted);font-size:13px">No data</div>`;
    document.getElementById("spl-body").innerHTML = data.records.length
        ? data.records.map(r=>`<tr>
            <td class="mono" style="font-size:11px;color:var(--danger)">${r.ref}</td>
            <td class="name">${r.product}</td>
            <td class="mono" style="color:var(--danger)">-${r.qty.toFixed(2)} ${r.unit}</td>
            <td style="font-size:12px">${r.reason}</td>
            <td style="font-size:12px;color:var(--muted)">${r.farm}</td>
            <td class="mono" style="font-size:12px">${r.date}</td>
            <td style="font-size:12px;color:var(--muted);white-space:nowrap">${r.user_name}</td>
            <td style="font-size:12px;color:var(--muted)">${r.notes}</td>
          </tr>`).join("")
        : `<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:30px">No spoilage in this period</td></tr>`;
}

/* ── PRODUCTION ── */
async function loadProduction(){
    let r = getRange("prod-from","prod-to");
    let data = await (await fetch(`/reports/api/production?date_from=${r.from}&date_to=${r.to}`)).json();
    document.getElementById("prod-proc").innerText = data.total_processing;
    document.getElementById("prod-pkg").innerText  = data.total_packaging;
    document.getElementById("prod-loss").innerText = data.avg_loss_pct.toFixed(1)+"%";
    setPrintDates("ph-prod-dates", r.from, r.to);
    document.getElementById("prod-body").innerHTML = data.batches.length
        ? data.batches.map(b=>`<tr>
            <td class="mono" style="font-size:12px;color:${b.type==="Packaging"?"var(--teal)":"var(--orange)"}">${b.batch_number}</td>
            <td><span style="font-size:11px;font-weight:700;padding:2px 8px;border-radius:20px;background:${b.type==="Packaging"?"rgba(45,212,191,.1)":"rgba(251,146,60,.1)"};color:${b.type==="Packaging"?"var(--teal)":"var(--orange)"}">${b.type}</span></td>
            <td class="name" style="font-size:12px">${b.recipe}</td>
            <td style="font-size:11px;color:var(--sub);max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${b.inputs_str||"—"}</td>
            <td style="font-size:11px;color:var(--green);max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${b.outputs_str||"—"}</td>
            <td class="mono" style="color:${b.waste_pct<10?"var(--green)":b.waste_pct<25?"var(--warn)":"var(--danger)"}">${b.waste_pct.toFixed(1)}%</td>
            <td class="mono" style="font-size:12px;color:var(--muted)">${b.date}</td>
            <td style="font-size:12px;color:var(--muted);white-space:nowrap">${b.user_name}</td>
          </tr>`).join("")
        : `<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:30px">No batches in this period</td></tr>`;
}


/* ── P&L ── */
async function loadPL(){
    let r = getRange("pl-from","pl-to");
    let data = await (await fetch(`/reports/api/pl?date_from=${r.from}&date_to=${r.to}`)).json();
    setPrintDates("ph-pl-dates", data.date_from, data.date_to);
    let isProfit = data.net_profit >= 0;

    const refLabel = {b2b:"B2B Sale", b2b_payment:"B2B Payment", pos:"POS Sale", consignment:"Consignment", payroll:"Payroll", spoilage:"Spoilage", manual:"Manual Entry"};

    function renderEntries(entries, color){
        if(!entries.length) return "";
        return `<div style="background:var(--bg);border-radius:8px;margin:4px 0 10px 24px;overflow:hidden">
            <table style="width:100%;border-collapse:collapse;font-size:12px">
                <thead><tr style="background:var(--card2)">
                    <th style="padding:6px 12px;text-align:left;color:var(--muted);font-weight:700;letter-spacing:.5px">Date</th>
                    <th style="padding:6px 12px;text-align:left;color:var(--muted);font-weight:700;letter-spacing:.5px">Type</th>
                    <th style="padding:6px 12px;text-align:left;color:var(--muted);font-weight:700;letter-spacing:.5px">Description</th>
                    <th style="padding:6px 12px;text-align:right;color:var(--muted);font-weight:700;letter-spacing:.5px">Amount (EGP)</th>
                </tr></thead>
                <tbody>
                ${entries.map(e=>`<tr style="border-top:1px solid var(--border)">
                    <td style="padding:7px 12px;font-family:var(--mono);color:var(--muted)">${e.date}</td>
                    <td style="padding:7px 12px"><span style="background:var(--card2);border-radius:4px;padding:1px 7px;font-size:11px;color:var(--sub)">${refLabel[e.ref_type]||e.ref_type}</span></td>
                    <td style="padding:7px 12px;color:var(--sub)">${e.description}</td>
                    <td style="padding:7px 12px;text-align:right;font-family:var(--mono);font-weight:700;color:${color}">${e.amount.toFixed(2)}</td>
                </tr>`).join("")}
                </tbody>
            </table>
        </div>`;
    }

    function renderAccountLine(item, color, expanded=false){
        let id = "pl-"+item.code.replace(/\\W/g,"");
        return `
            <div class="pl-row" style="cursor:pointer;user-select:none" onclick="togglePLDetail('${id}')">
                <span style="color:var(--sub);display:flex;align-items:center;gap:8px">
                    <span id="${id}-icon" style="color:var(--muted);font-size:11px;transition:transform .2s">${item.entries.length?"▶":""}</span>
                    ${item.code} — ${item.name}
                    <span style="font-size:11px;color:var(--muted)">(${item.entries.length} entries)</span>
                </span>
                <span class="mono" style="color:${color}">${item.amount.toFixed(2)}</span>
            </div>
            <div id="${id}" style="display:none">${renderEntries(item.entries, color)}</div>`;
    }

    document.getElementById("pl-content").innerHTML = `
        <div class="stats-row">
            <div class="stat-card sc-green"><div class="stat-label">Total Revenue</div><div class="stat-value sv-green">${data.total_revenue.toFixed(2)}</div></div>
            <div class="stat-card sc-danger"><div class="stat-label">Total Expenses</div><div class="stat-value sv-danger">${data.total_expense.toFixed(2)}</div></div>
            <div class="stat-card ${isProfit?"sc-green":"sc-danger"}">
                <div class="stat-label">Net ${isProfit?"Profit":"Loss"}</div>
                <div class="stat-value ${isProfit?"sv-green":"sv-danger"}">${Math.abs(data.net_profit).toFixed(2)}</div>
            </div>
        </div>
        <div style="font-size:12px;color:var(--muted);margin-bottom:12px">💡 Click any account line to expand its journal entries</div>
        <div class="pl-section">
            <div class="pl-header">Revenue</div>
            ${data.revenue_lines.map(r=>renderAccountLine(r,"var(--green)")).join("") || `<div class="pl-row"><span style="color:var(--muted)">No revenue entries</span><span></span></div>`}
            <div class="pl-row pl-total"><span>Total Revenue</span><span class="mono" style="color:var(--green)">${data.total_revenue.toFixed(2)}</span></div>
        </div>
        <div class="pl-section">
            <div class="pl-header">Expenses</div>
            ${data.expense_lines.map(e=>renderAccountLine(e,"var(--danger)")).join("") || `<div class="pl-row"><span style="color:var(--muted)">No expense entries</span><span></span></div>`}
            <div class="pl-row pl-total"><span>Total Expenses</span><span class="mono" style="color:var(--danger)">${data.total_expense.toFixed(2)}</span></div>
        </div>
        <div class="pl-section">
            <div class="pl-row pl-net" style="background:${isProfit?"rgba(0,255,157,.06)":"rgba(255,77,109,.06)"};border-top:2px solid ${isProfit?"var(--green)":"var(--danger)"}">
                <span>Net ${isProfit?"Profit":"Loss"}</span>
                <span class="mono" style="color:${isProfit?"var(--green)":"var(--danger)"};font-size:20px">${Math.abs(data.net_profit).toFixed(2)}</span>
            </div>
        </div>`;
}

function togglePLDetail(id){
    let el   = document.getElementById(id);
    let icon = document.getElementById(id+"-icon");
    if(!el) return;
    let open = el.style.display === "none" || el.style.display === "";
    el.style.display   = open ? "block" : "none";
    if(icon) icon.style.transform = open ? "rotate(90deg)" : "";
}

/* ── INIT: set default dates ── */
(function initDates(){
    let m = monthStart(), y = yearStart(), t = today();
    setEl("sales-from", m); setEl("sales-to", t);
    setEl("tx-from",    m); setEl("tx-to",    t);
    setEl("b2b-from",   m); setEl("b2b-to",   t);
    setEl("farm-from",  m); setEl("farm-to",   t);
    setEl("spl-from",   m); setEl("spl-to",    t);
    setEl("prod-from",  m); setEl("prod-to",   t);
    setEl("pl-from",    y); setEl("pl-to",     t);
})();

loadSales();
</script>
</body>
</html>"""
