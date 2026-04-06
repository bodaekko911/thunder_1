from fastapi import APIRouter, Depends, HTTPException, Header
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from decimal import Decimal
from datetime import datetime

from app.database import get_db
from app.models.product import Product
from app.models.customer import Customer
from app.models.invoice import Invoice, InvoiceItem
from app.schemas.invoice import InvoiceCreate
from app.services.pos_service import create_invoice
from app.core.security import decode_token

router = APIRouter(tags=["POS"])


def get_current_user(authorization: str = Header(None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="Not logged in")
    try:
        parts = authorization.strip().split(" ")
        token = parts[-1]
        return decode_token(token)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")


@router.get("/products-cache")
def products_cache(db: Session = Depends(get_db)):
    products = db.query(Product).filter(Product.is_active == True).all()
    return [
        {"sku": p.sku, "name": p.name, "price": float(p.price), "stock": float(p.stock)}
        for p in products
    ]


@router.get("/search-products")
def search_products(q: str = "", db: Session = Depends(get_db)):
    results = (
        db.query(Product)
        .filter(
            Product.is_active == True,
            (Product.name.ilike(f"%{q}%")) | (Product.sku.ilike(f"%{q}%"))
        )
        .limit(40).all()
    )
    return [
        {"sku": p.sku, "name": p.name, "price": float(p.price), "stock": float(p.stock)}
        for p in results
    ]


@router.get("/customers")
def list_customers(db: Session = Depends(get_db)):
    customers = db.query(Customer).order_by(Customer.name).all()
    return [{"id": c.id, "name": c.name, "phone": c.phone} for c in customers]


@router.post("/invoice")
def checkout(
    data: InvoiceCreate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    user_id = int(user.get("sub"))
    return create_invoice(db=db, data=data, user_id=user_id)


@router.get("/unpaid-invoices")
def get_unpaid_invoices(db: Session = Depends(get_db)):
    invoices = (
        db.query(Invoice)
        .filter(Invoice.status == "unpaid")
        .order_by(Invoice.created_at.desc())
        .limit(50).all()
    )
    result = []
    for i in invoices:
        customer = db.query(Customer).filter(Customer.id == i.customer_id).first()
        items    = db.query(InvoiceItem).filter(InvoiceItem.invoice_id == i.id).all()
        result.append({
            "id":             i.id,
            "invoice_number": i.invoice_number,
            "customer":       customer.name if customer else "—",
            "total":          float(i.total),
            "subtotal":       float(i.subtotal),
            "discount":       float(i.discount),
            "created_at":     i.created_at.strftime("%Y-%m-%d %H:%M") if i.created_at else "—",
            "items": [
                {
                    "name":       it.name,
                    "qty":        float(it.qty),
                    "unit_price": float(it.unit_price),
                    "total":      float(it.total),
                }
                for it in items
            ],
        })
    return result


@router.post("/invoice/{invoice_id}/collect")
def collect_payment(invoice_id: int, data: dict, db: Session = Depends(get_db)):
    from app.models.accounting import Account, Journal, JournalEntry

    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.status == "paid":
        raise HTTPException(status_code=400, detail="Invoice already paid")

    payment_method         = data.get("payment_method", "cash")
    invoice.status         = "paid"
    invoice.payment_method = payment_method

    total   = float(invoice.total)
    journal = Journal(ref_type="payment",
                      description=f"Payment collected - {invoice.invoice_number}")
    db.add(journal); db.flush()

    for code, debit, credit in [("1000", total, 0), ("1100", 0, total)]:
        acc = db.query(Account).filter(Account.code == code).first()
        if acc:
            db.add(JournalEntry(
                journal_id=journal.id,
                account_id=acc.id,
                debit=debit,
                credit=credit,
            ))
            acc.balance += Decimal(str(debit)) - Decimal(str(credit))

    db.commit()
    return {"ok": True, "invoice_number": invoice.invoice_number}


@router.get("/invoice/{invoice_id}", response_class=HTMLResponse)
def view_invoice(invoice_id: int, db: Session = Depends(get_db)):
    inv      = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    customer = db.query(Customer).filter(Customer.id == inv.customer_id).first()
    items    = db.query(InvoiceItem).filter(InvoiceItem.invoice_id == invoice_id).all()

    rows = ""
    for i in items:
        rows += f"""
        <div class="row">
            <span>{i.name}</span>
            <span>{float(i.qty):.0f} x {float(i.unit_price):.2f}</span>
            <span>{float(i.total):.2f}</span>
        </div>"""

    status_badge = ""
    if inv.status == "unpaid":
        status_badge = '<div style="background:rgba(255,181,71,.15);border:1px solid rgba(255,181,71,.3);color:#ffb547;border-radius:8px;padding:8px 12px;text-align:center;font-weight:700;margin-bottom:10px;">&#9203; UNPAID &#8212; Settle Later</div>'

    return f"""<!DOCTYPE html>
<html>
<head>
<title>Receipt {inv.invoice_number}</title>
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ font-family: monospace; background:#060810; color:white; }}
.r {{ width:320px; margin:30px auto; background:#0f1424; border-radius:16px; padding:20px; }}
.center {{ text-align:center; margin-bottom:12px; }}
.center h3 {{ color:#00ff9d; font-size:18px; }}
.row {{ display:flex; justify-content:space-between; font-size:13px; margin:6px 0; }}
.line {{ border-top:1px dashed #445066; margin:10px 0; }}
.total {{ font-size:20px; color:#00ff9d; font-weight:bold; }}
.btn {{ width:100%; padding:14px; margin-top:10px; background:linear-gradient(135deg,#00ff9d,#00d4ff); border:none; border-radius:10px; color:#021a10; font-size:15px; font-weight:800; cursor:pointer; }}
.btn-back {{ background:#151c30; color:#8899bb; margin-top:6px; }}
@media print {{
    .btn {{ display:none; }}
    body {{ background:white; color:black; }}
    .r {{ background:white; color:black; border:none; }}
    .center h3 {{ color:#000; }}
    .total {{ color:#000; }}
}}
</style>
</head>
<body>
<div class="r">
    <div class="center">
        <img src="/static/logo.png" alt="Habiba" style="height:70px;object-fit:contain;margin-bottom:6px;display:block;margin-left:auto;margin-right:auto">
        <div style="font-size:15px;font-weight:900;color:#2a7a2a;margin-bottom:2px">Habiba Organic Farm</div>
        <div style="color:#445066;font-size:12px">{inv.invoice_number}</div>
    </div>
    <div class="line"></div>
    {status_badge}
    <div class="row"><span>Customer</span><span>{customer.name if customer else '-'}</span></div>
    <div class="row"><span>Date</span><span>{inv.created_at.strftime('%Y-%m-%d %H:%M')}</span></div>
    <div class="row"><span>Payment</span><span>{inv.payment_method}</span></div>
    <div class="line"></div>
    {rows}
    <div class="line"></div>
    <div class="row"><span>Subtotal</span><span>{float(inv.subtotal):.2f}</span></div>
    <div class="row"><span>Discount</span><span>{float(inv.discount):.2f}</span></div>
    <div class="row total"><span>Total</span><span>{float(inv.total):.2f}</span></div>
    <button class="btn" onclick="window.print()">&#128424; Print Receipt</button>
    <button class="btn btn-back" onclick="window.location.href='/pos'">&#8592; Back to POS</button>
</div>
</body>
</html>"""


@router.get("/pos", response_class=HTMLResponse)
def pos_ui():
    return """<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>POS — Thunder ERP</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root {
    --bg:#060810;--surface:#0a0d18;--card:#0f1424;--card2:#151c30;
    --border:rgba(255,255,255,0.06);--border2:rgba(255,255,255,0.11);
    --green:#00ff9d;--blue:#4d9fff;--danger:#ff4d6d;--warn:#ffb547;
    --text:#f0f4ff;--sub:#8899bb;--muted:#445066;
    --sans:'Outfit',sans-serif;--mono:'JetBrains Mono',monospace;--r:13px;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
body{font-family:var(--sans);background:var(--bg);color:var(--text);height:100vh;overflow:hidden;display:grid;grid-template-columns:1fr 430px;grid-template-rows:58px 1fr;font-size:14px;}
body>*{position:relative;z-index:1;}

/* TOPBAR */
#topbar{grid-column:1/-1;display:flex;align-items:center;gap:10px;padding:0 18px;background:rgba(10,13,24,.9);backdrop-filter:blur(20px);border-bottom:1px solid var(--border);overflow:visible;z-index:100;}
.logo{font-size:17px;font-weight:900;background:linear-gradient(135deg,#f59e0b,#fbbf24);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;margin-right:6px;text-decoration:none;display:flex;align-items:center;gap:8px;}
.tb-field{display:flex;align-items:center;gap:9px;background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:0 13px;transition:border-color .2s;}
.tb-field:focus-within{border-color:rgba(0,255,157,.3);}
.tb-field svg{color:var(--muted);flex-shrink:0;}
.tb-field input{background:transparent;border:none;outline:none;color:var(--text);font-family:var(--sans);font-size:14px;font-weight:500;padding:11px 0;width:100%;}
.tb-field input::placeholder{color:var(--muted);}
#barcode_wrap{flex:0 0 200px;}
#barcode_wrap input{font-family:var(--mono);font-size:13px;}
#search_wrap{flex:1;}
.tb-spacer{flex:1;}

/* CUSTOMER */
#cust_wrap{flex:0 0 240px;position:relative;}
#cust_input_box{display:flex;align-items:center;gap:9px;background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:0 13px;transition:border-color .2s;cursor:text;}
#cust_input_box:focus-within{border-color:rgba(0,255,157,.3);}
#cust_input_box svg{color:var(--muted);flex-shrink:0;}
#cust_search{background:transparent;border:none;outline:none;color:var(--text);font-family:var(--sans);font-size:14px;font-weight:500;padding:11px 0;width:100%;}
#cust_search::placeholder{color:var(--muted);}
#cust_dropdown{display:none;position:fixed;width:260px;max-height:280px;overflow-y:auto;background:#0d1220;border:1px solid rgba(255,255,255,.12);border-radius:12px;z-index:99999;box-shadow:0 24px 60px rgba(0,0,0,.95);}
.cust-row{display:flex;align-items:center;gap:10px;padding:11px 14px;font-size:13px;font-weight:500;color:var(--sub);cursor:pointer;border-bottom:1px solid rgba(255,255,255,.05);transition:all .1s;}
.cust-row:last-child{border-bottom:none;}
.cust-row:hover{background:rgba(0,255,157,.08);color:var(--green);}
#selected_badge{display:none;align-items:center;gap:8px;background:rgba(0,255,157,.08);border:1px solid rgba(0,255,157,.25);color:var(--green);font-size:13px;font-weight:700;padding:8px 13px;border-radius:var(--r);white-space:nowrap;flex-shrink:0;}
#selected_badge.show{display:flex;}
#xcust{background:none;border:none;color:var(--green);opacity:.5;font-size:17px;cursor:pointer;padding:0;transition:all .15s;}
#xcust:hover{opacity:1;transform:rotate(90deg);}
#logout_btn{display:flex;align-items:center;gap:7px;background:transparent;border:1px solid var(--border);color:var(--sub);font-family:var(--sans);font-size:13px;font-weight:600;padding:8px 14px;border-radius:var(--r);cursor:pointer;transition:all .2s;}
#logout_btn:hover{border-color:var(--danger);color:var(--danger);}

/* LEFT */
#left{overflow-y:auto;padding:18px;display:flex;flex-direction:column;gap:14px;scrollbar-width:thin;scrollbar-color:var(--border2) transparent;}
.panel-title{font-size:11px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--muted);display:flex;align-items:center;gap:8px;}
.panel-title::after{content:'';flex:1;height:1px;background:linear-gradient(90deg,var(--border2),transparent);}
#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(145px,1fr));gap:10px;}
.product{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:16px 13px 13px;cursor:pointer;display:flex;flex-direction:column;gap:5px;position:relative;overflow:hidden;transition:border-color .2s,box-shadow .2s,transform .15s;}
.product:hover{border-color:rgba(0,255,157,.5);box-shadow:0 8px 30px rgba(0,255,157,.12);transform:translateY(-3px);}
.product:active{transform:translateY(-1px);}
.p-name{font-size:13px;font-weight:700;color:var(--text);line-height:1.35;}
.p-sku{font-family:var(--mono);font-size:10px;color:var(--muted);}
.p-price{font-family:var(--mono);font-size:16px;font-weight:700;color:var(--green);margin-top:4px;}
.p-stock{font-size:10px;margin-top:2px;}
@keyframes ripple{0%{transform:scale(0);opacity:.5}100%{transform:scale(4);opacity:0}}
.ripple-dot{position:absolute;width:40px;height:40px;border-radius:50%;background:radial-gradient(circle,rgba(0,255,157,.5),transparent 70%);transform:scale(0);pointer-events:none;animation:ripple .5s ease-out forwards;}
@keyframes cardFlash{0%{background:rgba(0,255,157,.2);border-color:var(--green);}100%{background:var(--card);border-color:var(--border);}}
.flash{animation:cardFlash .45s ease;}
#no_results{display:none;flex-direction:column;align-items:center;gap:12px;padding:70px 0;color:var(--muted);font-size:14px;}

/* RIGHT */
#right{background:rgba(10,13,24,.9);backdrop-filter:blur(20px);border-left:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;}
#cart_header{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--border);flex-shrink:0;gap:8px;}
#cart_count{background:linear-gradient(135deg,var(--green),var(--blue));color:#000;font-size:11px;font-weight:800;padding:2px 7px;border-radius:20px;display:none;}
#clear_btn{display:flex;align-items:center;gap:6px;background:rgba(255,77,109,.08);border:1px solid rgba(255,77,109,.2);color:var(--danger);font-family:var(--sans);font-size:12px;font-weight:700;padding:7px 13px;border-radius:9px;cursor:pointer;transition:all .2s;}
#clear_btn:hover{background:rgba(255,77,109,.18);border-color:var(--danger);}

/* CART BODY */
#cart_scroll{flex:1;overflow-y:auto;padding:12px 16px;display:flex;flex-direction:column;gap:8px;scrollbar-width:thin;scrollbar-color:var(--border2) transparent;}
#cart_empty{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;color:var(--muted);font-size:14px;padding:40px 0;}
#cart_empty svg{opacity:.25;animation:emptyFloat 3s ease-in-out infinite;}
@keyframes emptyFloat{0%,100%{transform:translateY(0)}50%{transform:translateY(-8px)}}
.cart-item{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:11px 13px;display:grid;grid-template-columns:1fr auto;grid-template-rows:auto auto;column-gap:10px;row-gap:7px;align-items:center;animation:itemIn .2s ease;}
@keyframes itemIn{from{opacity:0;transform:translateX(12px)}to{opacity:1;transform:translateX(0)}}
.ci-name{font-size:13px;font-weight:700;color:var(--text);}
.ci-subtotal{font-family:var(--mono);font-size:14px;font-weight:700;color:var(--green);text-align:right;}
.ci-controls{display:flex;align-items:center;gap:5px;grid-column:1/-1;}
.qty-btn{width:28px;height:28px;display:flex;align-items:center;justify-content:center;background:var(--card2);border:1px solid var(--border2);border-radius:8px;color:var(--text);font-size:18px;font-weight:700;font-family:var(--sans);cursor:pointer;transition:all .15s;flex-shrink:0;}
.qty-btn:hover{border-color:var(--green);color:var(--green);transform:scale(1.1);}
.qty-input{width:38px;height:28px;background:var(--card2);border:1px solid var(--border2);border-radius:8px;color:var(--text);font-family:var(--mono);font-size:13px;text-align:center;outline:none;}
.qty-input:focus{border-color:var(--blue);}
.ci-unit{font-family:var(--mono);font-size:11px;color:var(--muted);margin-left:3px;flex:1;}
.rm-btn{width:28px;height:28px;display:flex;align-items:center;justify-content:center;background:transparent;border:1px solid transparent;border-radius:8px;color:var(--muted);font-size:13px;cursor:pointer;transition:all .15s;margin-left:auto;}
.rm-btn:hover{border-color:var(--danger);color:var(--danger);background:rgba(255,77,109,.08);}

/* TOTALS */
#totals{border-top:1px solid var(--border);padding:12px 16px;display:flex;flex-direction:column;gap:8px;flex-shrink:0;}
.inputs-row{display:grid;grid-template-columns:1fr 1fr;gap:8px;}
.fld{display:flex;flex-direction:column;gap:4px;}
.fld-label{font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);}
.fld-input{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:10px 12px;color:var(--text);font-family:var(--mono);font-size:14px;text-align:center;outline:none;width:100%;transition:all .18s;}
.fld-input:focus{border-color:rgba(77,159,255,.5);}
.total-row{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;background:var(--card);border:1px solid var(--border);border-radius:var(--r);}
.total-label{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:var(--sub);}
#total{font-family:var(--mono);font-size:28px;font-weight:700;color:var(--green);}
.change-row{display:flex;align-items:center;justify-content:space-between;padding:8px 14px;background:var(--card);border:1px solid var(--border);border-radius:var(--r);}
.change-label{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:var(--sub);}
#change{font-family:var(--mono);font-size:15px;font-weight:500;color:var(--muted);}

/* PAYMENT TOGGLE */
.pay-toggle{display:grid;grid-template-columns:1fr 1fr;gap:8px;}
.pay-btn{padding:10px;border-radius:10px;border:2px solid var(--border2);background:transparent;color:var(--sub);font-family:var(--sans);font-size:13px;font-weight:700;cursor:pointer;transition:all .2s;}
.pay-btn.active-cash{border-color:var(--green);background:rgba(0,255,157,.1);color:var(--green);}
.pay-btn.active-visa{border-color:var(--blue);background:rgba(77,159,255,.1);color:var(--blue);}

/* CHECKOUT BUTTONS */
#checkout_btn{display:flex;align-items:center;justify-content:center;gap:9px;width:100%;padding:15px;background:linear-gradient(135deg,var(--green),#00d4ff);border:none;border-radius:var(--r);color:#021a10;font-family:var(--sans);font-size:15px;font-weight:900;cursor:pointer;transition:transform .15s,box-shadow .2s;box-shadow:0 4px 20px rgba(0,255,157,.25);}
#checkout_btn:hover:not(:disabled){transform:translateY(-2px);box-shadow:0 8px 30px rgba(0,255,157,.4);}
#checkout_btn:disabled{opacity:.35;cursor:not-allowed;}
#settle_btn{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;padding:12px;background:rgba(255,181,71,.08);border:2px solid rgba(255,181,71,.4);color:var(--warn);font-family:var(--sans);font-size:13px;font-weight:700;border-radius:var(--r);cursor:pointer;transition:all .2s;}
#settle_btn:hover:not(:disabled){background:rgba(255,181,71,.15);border-color:var(--warn);}
#settle_btn:disabled{opacity:.35;cursor:not-allowed;}

/* UNPAID PANEL */
#unpaid-panel{display:none;flex-direction:column;flex:1;overflow-y:auto;padding:14px 16px;gap:8px;scrollbar-width:thin;scrollbar-color:var(--border2) transparent;}
.unpaid-card{background:var(--card);border:1px solid rgba(255,181,71,.2);border-radius:var(--r);padding:14px;cursor:pointer;transition:border-color .2s;}
.unpaid-card:hover{border-color:rgba(255,181,71,.5);}
.unpaid-num{font-family:var(--mono);font-size:11px;color:var(--warn);}
.unpaid-name{font-weight:700;font-size:14px;margin:4px 0 2px;}
.unpaid-date{font-size:11px;color:var(--muted);margin-bottom:8px;}
.unpaid-total{font-family:var(--mono);font-size:20px;font-weight:700;color:var(--warn);}
.collect-row{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:10px;}
.collect-btn{padding:8px;border-radius:8px;font-family:var(--sans);font-size:12px;font-weight:700;cursor:pointer;transition:all .15s;}
.collect-cash{border:1px solid var(--green);background:rgba(0,255,157,.08);color:var(--green);}
.collect-cash:hover{background:rgba(0,255,157,.18);}
.collect-visa{border:1px solid var(--blue);background:rgba(77,159,255,.08);color:var(--blue);}
.collect-visa:hover{background:rgba(77,159,255,.18);}

/* INVOICE DETAIL MODAL */
.modal-bg{position:fixed;inset:0;z-index:500;background:rgba(0,0,0,.8);backdrop-filter:blur(4px);display:none;align-items:center;justify-content:center;}
.modal-bg.open{display:flex;}
.inv-modal{background:var(--card);border:1px solid var(--border2);border-radius:16px;padding:24px;width:380px;max-width:95vw;max-height:85vh;overflow-y:auto;animation:modalIn .2s ease;}
@keyframes modalIn{from{opacity:0;transform:scale(.95)}to{opacity:1;transform:scale(1)}}
.inv-modal-header{text-align:center;margin-bottom:14px;}
.inv-modal-num{font-family:var(--mono);font-size:12px;color:var(--muted);}
.inv-modal-title{font-size:18px;font-weight:800;color:var(--green);margin-bottom:4px;}
.inv-divider{border:none;border-top:1px dashed var(--border2);margin:12px 0;}
.inv-row{display:flex;justify-content:space-between;font-size:13px;padding:4px 0;}
.inv-row .label{color:var(--muted);}
.inv-row .val{font-weight:600;}
.inv-item-row{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border);font-size:13px;}
.inv-item-row:last-child{border-bottom:none;}
.inv-item-name{color:var(--text);font-weight:600;}
.inv-item-meta{font-size:11px;color:var(--muted);margin-top:2px;}
.inv-item-total{font-family:var(--mono);color:var(--green);font-weight:700;}
.inv-total-row{display:flex;justify-content:space-between;font-size:18px;font-weight:800;padding:10px 0 0;}
.inv-modal-actions{display:flex;gap:8px;margin-top:14px;}
.inv-print-btn{flex:1;padding:12px;background:linear-gradient(135deg,var(--green),#00d4ff);border:none;border-radius:10px;color:#021a10;font-family:var(--sans);font-size:13px;font-weight:800;cursor:pointer;}
.inv-close-btn{flex:1;padding:12px;background:var(--card2);border:1px solid var(--border2);border-radius:10px;color:var(--sub);font-family:var(--sans);font-size:13px;font-weight:700;cursor:pointer;}
.inv-close-btn:hover{border-color:var(--danger);color:var(--danger);}
.unpaid-badge{background:rgba(255,181,71,.12);border:1px solid rgba(255,181,71,.3);color:var(--warn);border-radius:8px;padding:8px;text-align:center;font-weight:700;font-size:12px;margin-bottom:8px;}

/* NOTIFICATION BADGE */
.notif-badge{display:none;background:var(--danger);color:white;font-size:10px;font-weight:800;padding:1px 6px;border-radius:20px;margin-left:4px;vertical-align:middle;}
.notif-badge.show{display:inline;}

/* TOAST */
.toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%) translateY(16px);background:#0f1424;border:1px solid var(--border2);border-radius:var(--r);padding:12px 18px;display:flex;align-items:center;gap:12px;font-size:13px;font-weight:600;color:var(--text);box-shadow:0 20px 50px rgba(0,0,0,.5);opacity:0;pointer-events:none;transition:opacity .25s,transform .25s;z-index:999;}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0);pointer-events:auto;}
.toast-undo{background:linear-gradient(135deg,var(--green),var(--blue));color:#021a10;border:none;border-radius:7px;padding:5px 11px;font-family:var(--sans);font-size:12px;font-weight:800;cursor:pointer;}
::-webkit-scrollbar{width:4px;}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:4px;}

@media print{
    .inv-modal-actions{display:none;}
    .modal-bg{position:static;background:white;display:block;}
    .inv-modal{box-shadow:none;border:none;background:white;color:black;max-height:none;}
    .inv-modal-title{color:black;}
    .inv-row .label,.inv-total-row{color:black;}
    .inv-item-name{color:black;}
    .inv-item-total{color:black;}
    #right,#left,#topbar{display:none!important;}
}
</style>
</head>
<body>

<!-- TOPBAR -->
<div id="topbar">
    <a href="/home" class="logo">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
            <polygon points="13,2 4,14 11,14 11,22 20,10 13,10" fill="#f59e0b"/>
        </svg>
        Thunder ERP
    </a>

    <div class="tb-field" id="barcode_wrap">
        <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
            <path d="M3 5v14M7 5v14M11 5v14M15 5v9M19 5v9M15 17v2M19 17v2"/>
        </svg>
        <input id="barcode" placeholder="Scan / SKU…" autocomplete="off">
    </div>

    <div class="tb-field" id="search_wrap">
        <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24">
            <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
        </svg>
        <input id="search" placeholder="Search products…" autocomplete="off">
    </div>

    <span class="tb-spacer"></span>

    <div id="cust_wrap">
        <div id="cust_input_box">
            <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                <circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 3.6-7 8-7s8 3 8 7"/>
            </svg>
            <input id="cust_search" placeholder="Select customer…" autocomplete="off">
        </div>
        <div id="cust_dropdown"></div>
    </div>

    <div id="selected_badge">
        <svg width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
            <circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 3.6-7 8-7s8 3 8 7"/>
        </svg>
        <span id="sel_name"></span>
        <button id="xcust" onclick="clearCustomer()">×</button>
    </div>

    <button id="logout_btn" onclick="logout()">
        <svg width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24">
            <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
            <polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>
        </svg>
        Sign out
    </button>
</div>

<!-- LEFT: PRODUCTS -->
<div id="left">
    <div class="panel-title">Products</div>
    <div id="grid"></div>
    <div id="no_results">
        <svg width="40" height="40" fill="none" stroke="currentColor" stroke-width="1.2" viewBox="0 0 24 24">
            <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
        </svg>
        No products found
    </div>
</div>

<!-- RIGHT: CART + UNPAID -->
<div id="right">

    <!-- HEADER TABS -->
    <div id="cart_header">
        <div style="display:flex;gap:6px;align-items:center;">
            <button onclick="switchPosTab('cart')" id="tab-cart"
                style="padding:6px 14px;border-radius:8px;border:none;font-family:var(--sans);font-size:12px;font-weight:700;cursor:pointer;background:rgba(0,255,157,.15);color:var(--green);">
                🛒 Cart <span id="cart_count"></span>
            </button>
            <button onclick="switchPosTab('unpaid')" id="tab-unpaid"
                style="padding:6px 14px;border-radius:8px;border:1px solid var(--border2);font-family:var(--sans);font-size:12px;font-weight:700;cursor:pointer;background:transparent;color:var(--muted);display:flex;align-items:center;gap:4px;">
                ⏳ Unpaid <span class="notif-badge" id="unpaid-badge"></span>
            </button>
        </div>
        <button id="clear_btn" onclick="clearCart()">
            <svg width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24">
                <polyline points="3 6 5 6 21 6"/>
                <path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/>
            </svg>
            Clear
        </button>
    </div>

    <!-- CART BODY -->
    <div id="cart_scroll">
        <div id="cart_empty">
            <svg width="44" height="44" fill="none" stroke="currentColor" stroke-width="1.2" viewBox="0 0 24 24">
                <path d="M6 2L3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z"/>
                <line x1="3" y1="6" x2="21" y2="6"/>
                <path d="M16 10a4 4 0 0 1-8 0"/>
            </svg>
            Cart is empty
        </div>
        <div id="cart"></div>
    </div>

    <!-- TOTALS -->
    <div id="totals">
        <div class="inputs-row">
            <div class="fld">
                <span class="fld-label">Discount %</span>
                <input id="discount" class="fld-input" type="number" placeholder="0" min="0" max="100">
            </div>
            <div class="fld">
                <span class="fld-label">Cash Received</span>
                <input id="cash" class="fld-input" type="number" placeholder="0.00" min="0">
            </div>
        </div>

        <div class="total-row">
            <span class="total-label">Total</span>
            <span id="total">0.00</span>
        </div>

        <div class="change-row">
            <span class="change-label">Change</span>
            <span id="change">—</span>
        </div>

        <div class="pay-toggle">
            <button class="pay-btn active-cash" id="pay-cash" onclick="setPayMethod('cash')">💵 Cash</button>
            <button class="pay-btn" id="pay-visa" onclick="setPayMethod('visa')">💳 Visa</button>
        </div>

        <button id="checkout_btn" onclick="checkout(false)">
            <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24">
                <polyline points="20 6 9 17 4 12"/>
            </svg>
            Confirm Order
        </button>

        <button id="settle_btn" onclick="checkout(true)">
            ⏳ Settle Later (requires customer)
        </button>
    </div>

    <!-- UNPAID PANEL -->
    <div id="unpaid-panel">
        <div style="font-size:10px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--muted);margin-bottom:4px;">Unpaid Invoices</div>
        <div id="unpaid-list">
            <div style="color:var(--muted);font-size:13px;text-align:center;padding:30px 0">Loading...</div>
        </div>
    </div>
</div>

<!-- INVOICE DETAIL MODAL -->
<div class="modal-bg" id="inv-modal">
    <div class="inv-modal" id="inv-modal-content">
        <div class="inv-modal-header">
            <div style="font-size:16px;font-weight:800;color:var(--green);margin-bottom:2px;">🌿 Habiba Organic Farm</div>
            <div class="inv-modal-num" id="modal-inv-num">—</div>
        </div>
        <hr class="inv-divider">
        <div class="unpaid-badge">⏳ UNPAID — Settle Later</div>
        <div id="modal-meta"></div>
        <hr class="inv-divider">
        <div id="modal-items"></div>
        <hr class="inv-divider">
        <div id="modal-totals"></div>
        <div class="inv-modal-actions">
            <button class="inv-print-btn" onclick="printInvoice()">🖨 Print Receipt</button>
            <button class="inv-close-btn" onclick="closeInvModal()">Close</button>
        </div>
    </div>
</div>

<!-- TOAST -->
<div class="toast" id="toast">
    <span id="toast_msg"></span>
    <button class="toast-undo" id="toast_undo" style="display:none" onclick="undoCart()">UNDO</button>
</div>

<script>
let beep = new Audio("/static/sounds/beep.wav");
let customers=[], products=[], cart=[], lastCart=[];
let selectedCustomer = null;
let selectedPayMethod = "cash";
let token = localStorage.getItem("token");
let toastTimer = null;
let currentInvoiceData = null;

/* ── INIT ── */
async function load(){
    if(!token){ window.location.href="/"; return; }
    try {
        customers = await (await fetch("/customers")).json();
        products  = await (await fetch("/products-cache")).json();
        draw(products.slice(0,40));
        checkUnpaidCount();
    } catch(e){ console.error("Load error:", e); }
}

/* ── CHECK UNPAID COUNT (for notification badge) ── */
async function checkUnpaidCount(){
    try {
        let data = await (await fetch("/unpaid-invoices")).json();
        let badge = document.getElementById("unpaid-badge");
        if(data.length > 0){
            badge.innerText = data.length;
            badge.classList.add("show");
        } else {
            badge.classList.remove("show");
        }
    } catch(e){}
}

/* ── POS TABS ── */
function switchPosTab(tab){
    let isCart = tab === "cart";

    let cartTab   = document.getElementById("tab-cart");
    let unpaidTab = document.getElementById("tab-unpaid");

    cartTab.style.background = isCart ? "rgba(0,255,157,.15)" : "transparent";
    cartTab.style.color      = isCart ? "var(--green)" : "var(--muted)";
    cartTab.style.border     = isCart ? "none" : "1px solid var(--border2)";

    unpaidTab.style.background = !isCart ? "rgba(255,181,71,.15)" : "transparent";
    unpaidTab.style.color      = !isCart ? "var(--warn)" : "var(--muted)";
    unpaidTab.style.border     = !isCart ? "none" : "1px solid var(--border2)";

    document.getElementById("cart_scroll").style.display  = isCart ? "" : "none";
    document.getElementById("totals").style.display       = isCart ? "" : "none";
    document.getElementById("unpaid-panel").style.display = isCart ? "none" : "flex";
    document.getElementById("clear_btn").style.display    = isCart ? "" : "none";

    if(!isCart) loadUnpaidInvoices();
}

/* ── PAYMENT METHOD ── */
function setPayMethod(method){
    selectedPayMethod = method;
    document.getElementById("pay-cash").className = "pay-btn" + (method==="cash"?" active-cash":"");
    document.getElementById("pay-visa").className = "pay-btn" + (method==="visa"?" active-visa":"");
}

/* ── CUSTOMER SEARCH ── */
document.getElementById("cust_search").addEventListener("input", function(){
    let v  = this.value.toLowerCase().trim();
    let dd = document.getElementById("cust_dropdown");
    if(!v){ dd.style.display="none"; dd.innerHTML=""; return; }
    let f = customers.filter(c=>c.name.toLowerCase().includes(v)||(c.phone||"").includes(v)).slice(0,10);
    if(!f.length){ dd.style.display="none"; return; }
    let rect = this.getBoundingClientRect();
    dd.style.top=  (rect.bottom+4)+"px";
    dd.style.left=  rect.left+"px";
    dd.style.width= "260px";
    dd.innerHTML = f.map(c=>`
        <div class="cust-row" onclick="selectCustomer(${c.id},'${c.name.replace(/'/g,"\\'")}')">
            <svg width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                <circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 3.6-7 8-7s8 3 8 7"/>
            </svg>
            ${c.name}
        </div>`).join("");
    dd.style.display = "block";
});

function selectCustomer(id, name){
    selectedCustomer = id;
    document.getElementById("cust_search").value = name;
    document.getElementById("cust_dropdown").style.display = "none";
    document.getElementById("sel_name").innerText = name;
    document.getElementById("selected_badge").classList.add("show");
    document.getElementById("cust_wrap").style.display = "none";
}

function clearCustomer(){
    selectedCustomer = null;
    document.getElementById("selected_badge").classList.remove("show");
    document.getElementById("cust_wrap").style.display = "";
    document.getElementById("cust_search").value = "";
}

document.addEventListener("click", e=>{
    if(!e.target.closest("#cust_wrap"))
        document.getElementById("cust_dropdown").style.display = "none";
});

/* ── BARCODE ── */
document.getElementById("barcode").addEventListener("keydown", function(e){
    if(e.key!=="Enter") return;
    let v = this.value.trim(); if(!v) return;
    let p = products.find(p=>String(p.sku).toLowerCase()===v.toLowerCase());
    if(p){
        add(p.sku, p.name, p.price);
        let card = document.querySelector(`[data-sku="${p.sku}"]`);
        if(card){ card.classList.add("flash"); setTimeout(()=>card.classList.remove("flash"),450); }
    } else {
        showToast("SKU not found: "+v);
    }
    this.value = "";
});

/* ── PRODUCT GRID ── */
function draw(list){
    let nr = document.getElementById("no_results");
    if(!list.length){ document.getElementById("grid").innerHTML=""; nr.style.display="flex"; return; }
    nr.style.display="none";
    document.getElementById("grid").innerHTML = list.map(p=>`
        <div class="product" data-sku="${p.sku}"
             onclick="addWithRipple(event,'${p.sku}','${p.name.replace(/'/g,"\\'")}',${p.price})">
            <div class="p-name">${p.name}</div>
            <div class="p-sku">${p.sku}</div>
            <div class="p-price">${parseFloat(p.price).toFixed(2)}</div>
            <div class="p-stock" style="color:${p.stock>0?'#445066':'#ff4d6d'}">Stock: ${parseFloat(p.stock).toFixed(0)}</div>
        </div>`).join("");
}

document.getElementById("search").addEventListener("input", async function(){
    let v = this.value.trim();
    if(!v){ draw(products.slice(0,40)); return; }
    let data = await (await fetch("/search-products?q="+encodeURIComponent(v))).json();
    draw(data);
});

/* ── CART ── */
function addWithRipple(e, sku, name, price){
    let card=e.currentTarget, rect=card.getBoundingClientRect(), dot=document.createElement("div");
    dot.className="ripple-dot";
    dot.style.left=(e.clientX-rect.left-20)+"px";
    dot.style.top=(e.clientY-rect.top-20)+"px";
    card.appendChild(dot); setTimeout(()=>dot.remove(),500);
    add(sku,name,price);
}

function add(sku,name,price){
    let ex=cart.find(c=>c.sku===sku);
    ex?ex.qty++:cart.push({sku,name,price:parseFloat(price),qty:1});
    beep.currentTime=0; beep.play().catch(()=>{});
    drawCart();
}
function inc(sku){ cart.find(c=>c.sku===sku).qty++; drawCart(); }
function dec(sku){ let i=cart.find(c=>c.sku===sku); if(--i.qty<=0) cart=cart.filter(c=>c.sku!==sku); drawCart(); }
function updateQty(sku,val){ cart.find(c=>c.sku===sku).qty=Math.max(1,parseFloat(val)||1); drawCart(); }
function removeItem(sku){ cart=cart.filter(c=>c.sku!==sku); drawCart(); }
function clearCart(){
    if(!cart.length) return;
    if(!confirm("Clear all items?")) return;
    lastCart=[...cart]; cart=[]; drawCart(); showToast("Cart cleared",true,true);
}
function undoCart(){ cart=[...lastCart]; drawCart(); hideToast(); }
function logout(){ localStorage.removeItem("token"); window.location.href="/"; }

function drawCart(){
    let empty=document.getElementById("cart_empty"), cartEl=document.getElementById("cart"), countEl=document.getElementById("cart_count"), total=0;
    if(!cart.length){ cartEl.innerHTML=""; empty.style.display="flex"; countEl.style.display="none"; }
    else { empty.style.display="none"; countEl.style.display=""; countEl.innerText=cart.reduce((s,c)=>s+c.qty,0); }
    cartEl.innerHTML=cart.map(c=>{ let t=c.qty*c.price; total+=t; return `
        <div class="cart-item">
            <div class="ci-name">${c.name}</div>
            <div class="ci-subtotal">${t.toFixed(2)}</div>
            <div class="ci-controls">
                <button class="qty-btn" onclick="dec('${c.sku}')">−</button>
                <input class="qty-input" value="${c.qty}" onchange="updateQty('${c.sku}',this.value)">
                <button class="qty-btn" onclick="inc('${c.sku}')">+</button>
                <span class="ci-unit">× ${c.price.toFixed(2)}</span>
                <button class="rm-btn" onclick="removeItem('${c.sku}')">✕</button>
            </div>
        </div>`;}).join("");

    let disc=parseFloat(document.getElementById("discount").value)||0;
    let final=total-(total*disc/100);
    let cash=parseFloat(document.getElementById("cash").value)||0;
    document.getElementById("total").innerText=final.toFixed(2);
    let changeEl=document.getElementById("change");
    if(cash>0){ let ch=cash-final; changeEl.innerText=ch.toFixed(2); changeEl.style.color=ch>=0?"var(--green)":"var(--danger)"; }
    else { changeEl.innerText="—"; changeEl.style.color="var(--muted)"; }
}

document.getElementById("cash").addEventListener("input",drawCart);
document.getElementById("discount").addEventListener("input",drawCart);

/* ── TOAST ── */
function showToast(msg,autoHide=true,undo=false){
    document.getElementById("toast_msg").innerText=msg;
    document.getElementById("toast_undo").style.display=undo?"":"none";
    document.getElementById("toast").classList.add("show");
    if(toastTimer) clearTimeout(toastTimer);
    if(autoHide) toastTimer=setTimeout(hideToast,4000);
}
function hideToast(){ document.getElementById("toast").classList.remove("show"); }

/* ── CHECKOUT ── */
async function checkout(settleLater=false){
    if(!cart.length){ showToast("Cart is empty"); return; }
    if(settleLater && !selectedCustomer){ showToast("Select a customer to settle later"); return; }
    if(!token){ window.location.href="/"; return; }

    let btn=document.getElementById(settleLater?"settle_btn":"checkout_btn");
    btn.disabled=true; btn.innerText="Processing…";

    try {
        let res=await fetch("/invoice",{
            method:"POST",
            headers:{"Content-Type":"application/json","Authorization":"Bearer "+token},
            body:JSON.stringify({
                customer_id:      selectedCustomer?parseInt(selectedCustomer):null,
                items:            cart.map(c=>({sku:c.sku,name:c.name,price:c.price,qty:c.qty})),
                discount_percent: parseFloat(document.getElementById("discount").value)||0,
                notes:            "",
                payment_method:   settleLater?"unpaid":selectedPayMethod,
                settle_later:     settleLater,
            }),
        });
        let data=await res.json();
        if(data.detail){ showToast("Error: "+data.detail); return; }
        if(settleLater){
            showToast(`⏳ ${data.invoice_number} saved — settle later`);
            clearCustomer(); cart=[]; drawCart();
            checkUnpaidCount();
        } else {
            window.location.href="/invoice/"+data.id;
        }
    } catch(e){ showToast("Network error"); }
    finally { btn.disabled=false; btn.innerText=settleLater?"⏳ Settle Later (requires customer)":"Confirm Order"; }
}

/* ── UNPAID INVOICES ── */
async function loadUnpaidInvoices(){
    let list=document.getElementById("unpaid-list");
    list.innerHTML=`<div style="color:var(--muted);font-size:13px;text-align:center;padding:30px 0">Loading...</div>`;
    try {
        let data=await (await fetch("/unpaid-invoices")).json();

        // Update badge
        let badge=document.getElementById("unpaid-badge");
        if(data.length>0){ badge.innerText=data.length; badge.classList.add("show"); }
        else { badge.classList.remove("show"); }

        if(!data.length){
            list.innerHTML=`<div style="color:var(--muted);font-size:13px;text-align:center;padding:30px 0">✓ No unpaid invoices</div>`;
            return;
        }

        list.innerHTML=data.map(inv=>`
            <div class="unpaid-card" onclick="openInvModal(${JSON.stringify(inv).replace(/"/g,'&quot;')})">
                <div style="display:flex;justify-content:space-between;align-items:flex-start;">
                    <div>
                        <div class="unpaid-num">${inv.invoice_number}</div>
                        <div class="unpaid-name">${inv.customer}</div>
                        <div class="unpaid-date">${inv.created_at}</div>
                    </div>
                    <div class="unpaid-total">${inv.total.toFixed(2)}</div>
                </div>
                <div class="collect-row" onclick="event.stopPropagation()">
                    <button class="collect-btn collect-cash"
                        onclick="collectPayment(${inv.id},'${inv.invoice_number}','cash')">
                        💵 Cash
                    </button>
                    <button class="collect-btn collect-visa"
                        onclick="collectPayment(${inv.id},'${inv.invoice_number}','visa')">
                        💳 Visa
                    </button>
                </div>
            </div>`).join("");
    } catch(e){
        list.innerHTML=`<div style="color:var(--danger);font-size:13px;text-align:center;padding:20px 0">Error loading invoices</div>`;
    }
}

async function collectPayment(invoiceId, invoiceNumber, method){
    let res=await fetch(`/invoice/${invoiceId}/collect`,{
        method:"POST",
        headers:{"Content-Type":"application/json","Authorization":"Bearer "+token},
        body:JSON.stringify({payment_method:method}),
    });
    let data=await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    showToast(`✅ ${invoiceNumber} collected via ${method}!`);
    closeInvModal();
    loadUnpaidInvoices();
}

/* ── INVOICE DETAIL MODAL ── */
function openInvModal(inv){
    if(typeof inv === "string") inv = JSON.parse(inv);
    currentInvoiceData = inv;

    document.getElementById("modal-inv-num").innerText = inv.invoice_number;

    document.getElementById("modal-meta").innerHTML = `
        <div class="inv-row"><span class="label">Customer</span><span class="val">${inv.customer}</span></div>
        <div class="inv-row"><span class="label">Date</span><span class="val">${inv.created_at}</span></div>
    `;

    document.getElementById("modal-items").innerHTML = inv.items.map(item=>`
        <div class="inv-item-row">
            <div>
                <div class="inv-item-name">${item.name}</div>
                <div class="inv-item-meta">${item.qty.toFixed(0)} × ${item.unit_price.toFixed(2)}</div>
            </div>
            <div class="inv-item-total">${item.total.toFixed(2)}</div>
        </div>`).join("");

    document.getElementById("modal-totals").innerHTML = `
        <div class="inv-row"><span class="label">Subtotal</span><span>${inv.subtotal.toFixed(2)}</span></div>
        ${inv.discount>0?`<div class="inv-row"><span class="label">Discount</span><span style="color:var(--danger)">-${inv.discount.toFixed(2)}</span></div>`:""}
        <div class="inv-total-row"><span>Total</span><span style="font-family:var(--mono);color:var(--warn)">${inv.total.toFixed(2)} EGP</span></div>
    `;

    document.getElementById("inv-modal").classList.add("open");
}

function closeInvModal(){
    document.getElementById("inv-modal").classList.remove("open");
    currentInvoiceData = null;
}

function printInvoice(){
    window.print();
}

document.getElementById("inv-modal").addEventListener("click", function(e){
    if(e.target===this) closeInvModal();
});

load();
</script>
</body>
</html>
"""