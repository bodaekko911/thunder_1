from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import Optional, List
from pydantic import BaseModel

from app.database import get_db
from app.models.accounting import Account, Journal, JournalEntry
from app.models.b2b import B2BClient, B2BInvoice, B2BInvoiceItem, Consignment
from app.models.product import Product
from app.models.inventory import StockMove
from decimal import Decimal

router = APIRouter(prefix="/accounting", tags=["Accounting"])


# ── Schemas ────────────────────────────────────────────
class AccountCreate(BaseModel):
    code:      str
    name:      str
    type:      str
    parent_id: Optional[int] = None

class JournalEntryIn(BaseModel):
    account_id: int
    debit:      float = 0
    credit:     float = 0
    note:       Optional[str] = None

class JournalCreate(BaseModel):
    ref_type:    Optional[str] = None
    description: Optional[str] = None
    entries:     List[JournalEntryIn]


# ── ACCOUNTS API ───────────────────────────────────────
@router.get("/api/accounts")
def get_accounts(db: Session = Depends(get_db)):
    accounts = db.query(Account).order_by(Account.code).all()
    return [
        {
            "id":      a.id,
            "code":    a.code,
            "name":    a.name,
            "type":    a.type,
            "balance": float(a.balance),
        }
        for a in accounts
    ]

@router.post("/api/accounts")
def create_account(data: AccountCreate, db: Session = Depends(get_db)):
    if db.query(Account).filter(Account.code == data.code).first():
        raise HTTPException(status_code=400, detail="Account code already exists")
    a = Account(**data.model_dump())
    db.add(a); db.commit(); db.refresh(a)
    return {"id": a.id, "code": a.code, "name": a.name}

@router.delete("/api/accounts/{account_id}")
def delete_account(account_id: int, db: Session = Depends(get_db)):
    a = db.query(Account).filter(Account.id == account_id).first()
    if not a:
        raise HTTPException(status_code=404, detail="Account not found")
    if a.entries:
        raise HTTPException(status_code=400, detail="Cannot delete account with journal entries")
    db.delete(a); db.commit()
    return {"ok": True}

@router.post("/api/accounts/seed")
def seed_accounts(db: Session = Depends(get_db)):
    """Create a standard chart of accounts if none exist."""
    if db.query(Account).count() > 0:
        return {"message": "Accounts already exist"}

    defaults = [
        # Assets
        ("1000", "Cash",                  "asset"),
        ("1100", "Accounts Receivable",   "asset"),
        ("1200", "Inventory",             "asset"),
        ("1300", "Prepaid Expenses",      "asset"),
        # Liabilities
        ("2000", "Accounts Payable",      "liability"),
        ("2100", "Salaries Payable",      "liability"),
        ("2200", "Tax Payable",           "liability"),
        # Equity
        ("3000", "Owner Equity",          "equity"),
        ("3100", "Retained Earnings",     "equity"),
        # Revenue
        ("4000", "Sales Revenue",         "revenue"),
        ("4100", "Other Income",          "revenue"),
        # Expenses
        ("5000", "Cost of Goods Sold",    "expense"),
        ("5100", "Salaries Expense",      "expense"),
        ("5200", "Rent Expense",          "expense"),
        ("5300", "Utilities Expense",     "expense"),
        ("5400", "Marketing Expense",     "expense"),
        ("5500", "Other Expenses",        "expense"),
    ]

    for code, name, atype in defaults:
        db.add(Account(code=code, name=name, type=atype, balance=0))
    db.commit()
    return {"message": f"Created {len(defaults)} accounts"}


# ── JOURNALS API ───────────────────────────────────────
@router.get("/api/journals")
def get_journals(skip: int = 0, limit: int = 50, db: Session = Depends(get_db)):
    total    = db.query(Journal).count()
    journals = db.query(Journal).order_by(Journal.created_at.desc()).offset(skip).limit(limit).all()
    return {
        "total": total,
        "journals": [
            {
                "id":          j.id,
                "ref_type":    j.ref_type or "manual",
                "description": j.description or "—",
                "created_at":  j.created_at.strftime("%Y-%m-%d %H:%M") if j.created_at else "—",
                "entries_count": len(j.entries),
                "total_debit": sum(float(e.debit) for e in j.entries),
            }
            for j in journals
        ],
    }

@router.get("/api/journals/{journal_id}")
def get_journal(journal_id: int, db: Session = Depends(get_db)):
    j = db.query(Journal).filter(Journal.id == journal_id).first()
    if not j:
        raise HTTPException(status_code=404, detail="Journal not found")
    return {
        "id":          j.id,
        "ref_type":    j.ref_type or "manual",
        "description": j.description or "—",
        "created_at":  j.created_at.strftime("%Y-%m-%d %H:%M") if j.created_at else "—",
        "entries": [
            {
                "account_code": e.account.code if e.account else "—",
                "account_name": e.account.name if e.account else "—",
                "debit":        float(e.debit),
                "credit":       float(e.credit),
                "note":         e.note or "",
            }
            for e in j.entries
        ],
    }

@router.post("/api/journals")
def create_journal(data: JournalCreate, db: Session = Depends(get_db)):
    total_debit  = sum(e.debit  for e in data.entries)
    total_credit = sum(e.credit for e in data.entries)
    if round(total_debit, 2) != round(total_credit, 2):
        raise HTTPException(
            status_code=400,
            detail=f"Journal not balanced. Debits: {total_debit:.2f}, Credits: {total_credit:.2f}"
        )

    journal = Journal(
        ref_type=data.ref_type or "manual",
        description=data.description,
    )
    db.add(journal); db.flush()

    for entry in data.entries:
        acc = db.query(Account).filter(Account.id == entry.account_id).first()
        if not acc:
            raise HTTPException(status_code=404, detail=f"Account ID not found: {entry.account_id}")

        je = JournalEntry(
            journal_id=journal.id,
            account_id=entry.account_id,
            debit=entry.debit,
            credit=entry.credit,
            note=entry.note,
        )
        db.add(je)

        # Update account balance
        acc.balance += entry.debit - entry.credit

    db.commit(); db.refresh(journal)
    return {"id": journal.id, "ok": True}


# ── REPORTS API ────────────────────────────────────────
@router.get("/api/trial-balance")
def trial_balance(db: Session = Depends(get_db)):
    accounts = db.query(Account).order_by(Account.code).all()
    rows = []
    total_debit  = 0
    total_credit = 0

    for a in accounts:
        bal = float(a.balance)
        if bal > 0:
            total_debit  += bal
        else:
            total_credit += abs(bal)
        rows.append({
            "code":   a.code,
            "name":   a.name,
            "type":   a.type,
            "debit":  bal  if bal > 0 else 0,
            "credit": abs(bal) if bal < 0 else 0,
        })

    return {
        "rows":         rows,
        "total_debit":  total_debit,
        "total_credit": total_credit,
    }

@router.get("/api/profit-loss")
def profit_loss(db: Session = Depends(get_db)):
    revenue_accounts = db.query(Account).filter(Account.type == "revenue").order_by(Account.code).all()
    expense_accounts = db.query(Account).filter(Account.type == "expense").order_by(Account.code).all()

    revenues = [{"code": a.code, "name": a.name, "amount": abs(float(a.balance))} for a in revenue_accounts]
    expenses = [{"code": a.code, "name": a.name, "amount": abs(float(a.balance))} for a in expense_accounts]

    total_revenue = sum(r["amount"] for r in revenues)
    total_expense = sum(e["amount"] for e in expenses)
    net_profit    = total_revenue - total_expense

    return {
        "revenues":      revenues,
        "expenses":      expenses,
        "total_revenue": total_revenue,
        "total_expense": total_expense,
        "net_profit":    net_profit,
    }



# ── B2B INVOICES (for Accounting tab) ─────────────────
@router.get("/api/b2b-invoices")
def get_b2b_invoices(invoice_type: str = None, status: str = None, db: Session = Depends(get_db)):
    query = db.query(B2BInvoice)
    if invoice_type: query = query.filter(B2BInvoice.invoice_type == invoice_type)
    if status:       query = query.filter(B2BInvoice.status == status)
    invoices = query.order_by(B2BInvoice.created_at.desc()).all()
    return [
        {
            "id":             i.id,
            "invoice_number": i.invoice_number,
            "client":         i.client.name if i.client else "—",
            "client_id":      i.client_id,
            "invoice_type":   i.invoice_type,
            "status":         i.status,
            "subtotal":       float(i.subtotal),
            "discount":       float(i.discount),
            "total":          float(i.total),
            "amount_paid":    float(i.amount_paid),
            "balance_due":    round(float(i.total) - float(i.amount_paid), 2),
            "created_at":     i.created_at.strftime("%Y-%m-%d") if i.created_at else "—",
            "items": [
                {
                    "product":    it.product.name if it.product else "—",
                    "qty":        float(it.qty),
                    "unit_price": float(it.unit_price),
                    "total":      float(it.total),
                }
                for it in i.items
            ],
        }
        for i in invoices
    ]

@router.post("/api/b2b-invoices/{invoice_id}/collect")
def collect_b2b_payment(invoice_id: int, data: dict, db: Session = Depends(get_db)):
    invoice = db.query(B2BInvoice).filter(B2BInvoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.invoice_type not in ("cash", "full_payment"):
        raise HTTPException(status_code=400, detail="Use consignment-payment endpoint for consignment invoices")
    amount  = round(float(data.get("amount", 0)), 2)
    balance = round(float(invoice.total) - float(invoice.amount_paid), 2)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than 0")
    if amount > balance + 0.01:
        raise HTTPException(status_code=400, detail=f"Amount exceeds balance: {balance:.2f}")
    invoice.amount_paid = Decimal(str(float(invoice.amount_paid) + amount))
    invoice.status = "paid" if float(invoice.amount_paid) >= float(invoice.total) else "partial"
    client = invoice.client
    if client:
        client.outstanding = Decimal(str(max(0, float(client.outstanding) - amount)))
    # Journal: Cash in, AR out, Deferred → Revenue
    journal = Journal(ref_type="b2b_collection", description=f"B2B payment collected - {invoice.invoice_number}")
    db.add(journal); db.flush()
    for code, debit, credit in [
        ("1000", amount, 0),
        ("1100", 0, amount),
        ("2200", amount, 0),
        ("4000", 0, amount),
    ]:
        acc = db.query(Account).filter(Account.code == code).first()
        if acc:
            db.add(JournalEntry(journal_id=journal.id, account_id=acc.id, debit=debit, credit=credit))
            acc.balance += Decimal(str(debit)) - Decimal(str(credit))
    db.commit()
    return {"ok": True, "status": invoice.status, "invoice_number": invoice.invoice_number}

@router.post("/api/b2b-invoices/{invoice_id}/consignment-payment")
def consignment_b2b_payment(invoice_id: int, data: dict, db: Session = Depends(get_db)):
    invoice = db.query(B2BInvoice).filter(B2BInvoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    amount      = round(float(data.get("amount", 0)), 2)
    month_label = data.get("month_label", "")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than 0")
    balance = round(float(invoice.total) - float(invoice.amount_paid), 2)
    if amount > balance + 0.01:
        raise HTTPException(status_code=400, detail=f"Amount exceeds balance: {balance:.2f}")
    invoice.amount_paid = Decimal(str(float(invoice.amount_paid) + amount))
    if float(invoice.amount_paid) >= float(invoice.total):
        invoice.status = "paid"
    client = invoice.client
    if client:
        client.outstanding = Decimal(str(max(0, float(client.outstanding) - amount)))
    note = f"Consignment payment - {invoice.invoice_number}"
    if month_label: note += f" ({month_label})"
    journal = Journal(ref_type="consignment_payment", description=note)
    db.add(journal); db.flush()
    for code, debit, credit in [
        ("1000", amount, 0),
        ("1100", 0, amount),
        ("2200", amount, 0),
        ("4000", 0, amount),
    ]:
        acc = db.query(Account).filter(Account.code == code).first()
        if acc:
            db.add(JournalEntry(journal_id=journal.id, account_id=acc.id, debit=debit, credit=credit))
            acc.balance += Decimal(str(debit)) - Decimal(str(credit))
    db.commit()
    return {"ok": True, "status": invoice.status, "invoice_number": invoice.invoice_number}


# ── UI ─────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
def accounting_ui():
    return """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Accounting</title>
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
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: var(--sans); background: var(--bg); color: var(--text); min-height: 100vh; font-size: 14px; }
nav {
    position: sticky; top: 0; z-index: 100;
    display: flex; align-items: center; gap: 8px;
    padding: 0 24px; height: 58px;
    background: rgba(10,13,24,.92); backdrop-filter: blur(20px);
    border-bottom: 1px solid var(--border); flex-wrap: wrap;
}
.logo { font-size: 18px; font-weight: 900; background: linear-gradient(135deg,#f59e0b,#fbbf24); -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text; margin-right:10px; text-decoration:none; display:flex; align-items:center; gap:8px; cursor:pointer; }
.nav-link { padding:7px 12px; border-radius:8px; color:var(--sub); font-size:12px; font-weight:600; text-decoration:none; transition:all .2s; white-space:nowrap; }
.nav-link:hover { background:rgba(255,255,255,.05); color:var(--text); }
.nav-link.active { background:rgba(0,255,157,.1); color:var(--green); }
.nav-spacer { flex:1; }
.content { max-width:1300px; margin:0 auto; padding:28px 24px; display:flex; flex-direction:column; gap:20px; }
.page-title { font-size:24px; font-weight:800; letter-spacing:-.5px; }
.page-sub   { color:var(--muted); font-size:13px; margin-top:3px; }
.tabs { display:flex; gap:4px; background:var(--card); border:1px solid var(--border); border-radius:var(--r); padding:4px; width:fit-content; flex-wrap:wrap; }
.tab { padding:8px 18px; border-radius:9px; font-size:13px; font-weight:700; cursor:pointer; border:none; background:transparent; color:var(--muted); transition:all .2s; font-family:var(--sans); }
.tab.active { background:var(--card2); color:var(--text); }
.toolbar { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
.btn { display:flex; align-items:center; gap:7px; padding:10px 16px; border-radius:var(--r); font-family:var(--sans); font-size:13px; font-weight:700; cursor:pointer; border:none; transition:all .2s; white-space:nowrap; }
.btn-green  { background:linear-gradient(135deg,var(--green),#00d4ff); color:#021a10; }
.btn-green:hover  { filter:brightness(1.1); transform:translateY(-1px); }
.btn-blue   { background:linear-gradient(135deg,var(--blue),var(--purple)); color:white; }
.btn-blue:hover   { filter:brightness(1.1); transform:translateY(-1px); }
.btn-outline { background:transparent; border:1px solid var(--border2); color:var(--sub); }
.btn-outline:hover { border-color:var(--green); color:var(--green); }
.table-wrap { background:var(--card); border:1px solid var(--border); border-radius:var(--r); overflow:hidden; }
table { width:100%; border-collapse:collapse; }
thead { background:var(--card2); }
th { text-align:left; font-size:10px; font-weight:700; letter-spacing:1px; text-transform:uppercase; color:var(--muted); padding:12px 16px; }
td { padding:12px 16px; border-top:1px solid var(--border); color:var(--sub); font-size:13px; }
tr:hover td { background:rgba(255,255,255,.02); }
td.name { color:var(--text); font-weight:600; }
td.mono { font-family:var(--mono); }
td.dr { font-family:var(--mono); color:var(--green); }
td.cr { font-family:var(--mono); color:var(--blue); }
.type-badge { display:inline-flex; padding:2px 8px; border-radius:20px; font-size:11px; font-weight:700; }
.type-asset     { background:rgba(0,255,157,.1);  color:var(--green);  }
.type-liability { background:rgba(255,77,109,.1); color:var(--danger); }
.type-equity    { background:rgba(168,85,247,.1); color:var(--purple); }
.type-revenue   { background:rgba(77,159,255,.1); color:var(--blue);   }
.type-expense   { background:rgba(255,181,71,.1); color:var(--warn);   }
.action-btn { background:transparent; border:1px solid var(--border2); color:var(--sub); font-size:12px; font-weight:600; padding:5px 10px; border-radius:7px; cursor:pointer; transition:all .15s; font-family:var(--sans); }
.action-btn:hover { border-color:var(--blue); color:var(--blue); }
.action-btn.danger:hover { border-color:var(--danger); color:var(--danger); }
.action-btn.green:hover  { border-color:var(--green); color:var(--green); }

/* PL REPORT */
.pl-section { background:var(--card); border:1px solid var(--border); border-radius:var(--r); overflow:hidden; margin-bottom:14px; }
.pl-header  { background:var(--card2); padding:12px 16px; font-size:11px; font-weight:700; letter-spacing:1.5px; text-transform:uppercase; color:var(--muted); }
.pl-row     { display:flex; justify-content:space-between; padding:11px 16px; border-top:1px solid var(--border); font-size:13px; }
.pl-row:hover { background:rgba(255,255,255,.02); }
.pl-total   { display:flex; justify-content:space-between; padding:14px 16px; border-top:1px solid var(--border2); font-size:15px; font-weight:800; }
.pl-net     { display:flex; justify-content:space-between; padding:16px; background:var(--card2); border:1px solid var(--border2); border-radius:var(--r); font-size:18px; font-weight:800; }

/* MODAL */
.modal-bg { position:fixed; inset:0; z-index:500; background:rgba(0,0,0,.7); backdrop-filter:blur(4px); display:none; align-items:center; justify-content:center; }
.modal-bg.open { display:flex; }
.modal { background:var(--card); border:1px solid var(--border2); border-radius:16px; padding:28px; width:600px; max-width:95vw; max-height:90vh; overflow-y:auto; animation:modalIn .2s ease; }
@keyframes modalIn { from{opacity:0;transform:scale(.95)} to{opacity:1;transform:scale(1)} }
.modal-title { font-size:18px; font-weight:800; margin-bottom:20px; }
.fld { display:flex; flex-direction:column; gap:6px; margin-bottom:14px; }
.fld label { font-size:11px; font-weight:700; letter-spacing:1px; text-transform:uppercase; color:var(--muted); }
.fld input, .fld select { background:var(--card2); border:1px solid var(--border2); border-radius:10px; padding:10px 12px; color:var(--text); font-family:var(--sans); font-size:14px; outline:none; transition:border-color .2s; width:100%; }
.fld input:focus, .fld select:focus { border-color:rgba(0,255,157,.4); }
.modal-actions { display:flex; gap:10px; margin-top:6px; justify-content:flex-end; }
.btn-cancel { background:transparent; border:1px solid var(--border2); color:var(--sub); padding:10px 18px; border-radius:var(--r); font-family:var(--sans); font-size:13px; font-weight:700; cursor:pointer; }
.btn-cancel:hover { border-color:var(--danger); color:var(--danger); }

/* JOURNAL ENTRY ROWS */
.entry-row { display:grid; grid-template-columns:2fr 1fr 1fr 30px; gap:8px; align-items:center; margin-bottom:8px; }
.entry-row select, .entry-row input { background:var(--card2); border:1px solid var(--border2); border-radius:8px; padding:8px 10px; color:var(--text); font-family:var(--sans); font-size:13px; outline:none; width:100%; }
.entry-row select:focus, .entry-row input:focus { border-color:rgba(0,255,157,.4); }
.rm-btn { background:none; border:none; color:var(--muted); font-size:18px; cursor:pointer; padding:0; transition:color .15s; }
.rm-btn:hover { color:var(--danger); }
.add-entry-btn { background:rgba(77,159,255,.1); border:1px dashed rgba(77,159,255,.3); color:var(--blue); font-family:var(--sans); font-size:13px; font-weight:600; padding:9px; border-radius:8px; cursor:pointer; width:100%; transition:all .2s; margin-bottom:14px; }
.add-entry-btn:hover { background:rgba(77,159,255,.2); }
.balance-display { display:flex; justify-content:space-between; background:var(--card2); border:1px solid var(--border2); border-radius:10px; padding:12px 14px; margin-bottom:14px; }
.balance-ok   { color:var(--green); font-family:var(--mono); font-weight:700; }
.balance-fail { color:var(--danger); font-family:var(--mono); font-weight:700; }

/* SIDE PANEL */
.side-bg { position:fixed; inset:0; z-index:400; background:rgba(0,0,0,.5); display:none; }
.side-bg.open { display:block; }
.side-panel { position:fixed; right:0; top:0; bottom:0; width:460px; max-width:95vw; background:var(--card); border-left:1px solid var(--border2); display:flex; flex-direction:column; transform:translateX(100%); transition:transform .3s ease; z-index:401; }
.side-panel.open { transform:translateX(0); }
.side-header { padding:20px; border-bottom:1px solid var(--border); display:flex; align-items:center; justify-content:space-between; }
.side-header h3 { font-size:16px; font-weight:800; }
.close-btn { background:none; border:none; color:var(--muted); font-size:22px; cursor:pointer; padding:0; transition:color .15s; }
.close-btn:hover { color:var(--danger); }
.side-body { flex:1; overflow-y:auto; padding:16px 20px; }

.toast { position:fixed; bottom:22px; left:50%; transform:translateX(-50%) translateY(16px); background:var(--card2); border:1px solid var(--border2); border-radius:var(--r); padding:12px 20px; font-size:13px; font-weight:600; color:var(--text); box-shadow:0 20px 50px rgba(0,0,0,.5); opacity:0; pointer-events:none; transition:opacity .25s,transform .25s; z-index:999; }
.toast.show { opacity:1; transform:translateX(-50%) translateY(0); }
::-webkit-scrollbar { width:4px; }
::-webkit-scrollbar-thumb { background:var(--border2); border-radius:4px; }
</style>
</head>
<body>
<nav>
    <a href="/home" class="logo" style="text-decoration:none;display:flex;align-items:center;gap:8px;">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none"><polygon points="13,2 4,14 11,14 11,22 20,10 13,10" fill="#f59e0b"/></svg>
        Thunder ERP
    </a>
    <a href="/dashboard"       class="nav-link">Dashboard</a>
    <a href="/pos"             class="nav-link">POS</a>
    <a href="/products/"       class="nav-link">Products</a>
    <a href="/customers-mgmt/" class="nav-link">Customers</a>
    <a href="/suppliers/"      class="nav-link">Suppliers</a>
    <a href="/inventory/"      class="nav-link">Inventory</a>
    <a href="/hr/"             class="nav-link">HR</a>
    <a href="/accounting/"     class="nav-link active">Accounting</a>
    <span class="nav-spacer"></span>
</nav>

<div class="content">
    <div>
        <div class="page-title">Accounting</div>
        <div class="page-sub">Chart of accounts, journal entries and financial reports</div>
    </div>

    <!-- TABS -->
    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;">
        <div class="tabs">
            <button class="tab active" id="tab-accounts" onclick="switchTab('accounts')">Chart of Accounts</button>
            <button class="tab"        id="tab-journals" onclick="switchTab('journals')">Journal Entries</button>
            <button class="tab"        id="tab-pl"       onclick="switchTab('pl')">Profit & Loss</button>
            <button class="tab"        id="tab-tb"       onclick="switchTab('tb')">Trial Balance</button>
            <button class="tab"        id="tab-b2b"      onclick="switchTab('b2b')">B2B Invoices</button>
        </div>
        <div style="display:flex;gap:10px;">
            <button class="btn btn-outline" id="btn-seed"   onclick="seedAccounts()" style="display:none">⚡ Setup Default Accounts</button>
            <button class="btn btn-green"   id="btn-add-acc" onclick="openAddAccModal()">+ Add Account</button>
            <button class="btn btn-blue"    id="btn-add-je"  onclick="openJEModal()" style="display:none">+ New Journal Entry</button>
        </div>
    </div>

    <!-- ACCOUNTS -->
    <div id="section-accounts">
        <div class="table-wrap">
            <table>
                <thead><tr><th>Code</th><th>Name</th><th>Type</th><th>Balance</th><th>Actions</th></tr></thead>
                <tbody id="accounts-body"><tr><td colspan="5" style="text-align:center;color:var(--muted);padding:40px">Loading…</td></tr></tbody>
            </table>
        </div>
    </div>

    <!-- JOURNALS -->
    <div id="section-journals" style="display:none">
        <div class="table-wrap">
            <table>
                <thead><tr><th>ID</th><th>Type</th><th>Description</th><th>Entries</th><th>Total Debit</th><th>Date</th><th>Actions</th></tr></thead>
                <tbody id="journals-body"><tr><td colspan="7" style="text-align:center;color:var(--muted);padding:40px">Loading…</td></tr></tbody>
            </table>
        </div>
    </div>

    <!-- P&L -->
    <div id="section-pl" style="display:none">
        <div id="pl-content"><div style="color:var(--muted);padding:40px;text-align:center">Loading…</div></div>
    </div>

    <!-- TRIAL BALANCE -->
    <div id="section-tb" style="display:none">
        <div class="table-wrap">
            <table>
                <thead><tr><th>Code</th><th>Account</th><th>Type</th><th>Debit</th><th>Credit</th></tr></thead>
                <tbody id="tb-body"><tr><td colspan="5" style="text-align:center;color:var(--muted);padding:40px">Loading…</td></tr></tbody>
                <tfoot id="tb-foot"></tfoot>
            </table>
        </div>
    </div>

    <!-- B2B INVOICES -->
    <div id="section-b2b" style="display:none">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px;">
            <select id="b2b-type-filter" onchange="loadB2BInvoices()" style="background:var(--card2);border:1px solid var(--border2);border-radius:var(--r);padding:9px 13px;color:var(--text);font-family:var(--sans);font-size:13px;outline:none;">
                <option value="">All Types</option>
                <option value="cash">💵 Cash</option>
                <option value="full_payment">📋 Full Payment</option>
                <option value="consignment">🔄 Consignment</option>
            </select>
            <select id="b2b-status-filter" onchange="loadB2BInvoices()" style="background:var(--card2);border:1px solid var(--border2);border-radius:var(--r);padding:9px 13px;color:var(--text);font-family:var(--sans);font-size:13px;outline:none;">
                <option value="">All Statuses</option>
                <option value="paid">Paid</option>
                <option value="unpaid">Unpaid</option>
                <option value="partial">Partial</option>
            </select>
        </div>
        <div class="table-wrap">
            <table>
                <thead><tr><th>Invoice #</th><th>Client</th><th>Type</th><th>Total</th><th>Paid</th><th>Balance</th><th>Status</th><th>Date</th><th>Actions</th></tr></thead>
                <tbody id="b2b-invoices-body"><tr><td colspan="9" style="text-align:center;color:var(--muted);padding:40px">Loading…</td></tr></tbody>
            </table>
        </div>

        <!-- CONSIGNMENT PAYMENTS SUB-SECTION -->
        <div id="cons-payment-section" style="display:none;margin-top:20px">
            <div style="font-size:11px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--muted);margin-bottom:12px;display:flex;align-items:center;gap:8px;">
                Consignment Payment History
                <span style="flex:1;height:1px;background:linear-gradient(90deg,var(--border2),transparent)"></span>
            </div>
            <div id="cons-payment-list"></div>
        </div>
    </div>
</div>

<!-- INVOICE DETAIL MODAL -->
<div class="modal-bg" id="inv-detail-modal">
    <div class="modal" style="width:520px">
        <div style="text-align:center;margin-bottom:16px">
            <img src="/static/logo.png" style="height:60px;object-fit:contain;margin-bottom:6px">
            <div style="font-size:16px;font-weight:900;color:#2a7a2a">Habiba Organic Farm</div>
            <div style="font-size:12px;color:var(--muted);margin-top:2px" id="inv-detail-num"></div>
        </div>
        <div style="border-top:1px dashed var(--border2);margin:12px 0"></div>
        <div id="inv-detail-meta" style="margin-bottom:12px"></div>
        <div style="border-top:1px dashed var(--border2);margin:12px 0"></div>
        <table style="width:100%;border-collapse:collapse;margin-bottom:12px">
            <thead><tr>
                <th style="text-align:left;font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);padding:8px 0">Product</th>
                <th style="text-align:center;font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);padding:8px 0">QTY</th>
                <th style="text-align:right;font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);padding:8px 0">Price</th>
                <th style="text-align:right;font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);padding:8px 0">Total</th>
            </tr></thead>
            <tbody id="inv-detail-items"></tbody>
        </table>
        <div style="border-top:1px dashed var(--border2);margin:12px 0"></div>
        <div id="inv-detail-totals"></div>
        <div style="display:flex;gap:8px;margin-top:16px;justify-content:flex-end">
            <button onclick="printInvDetail()" style="background:linear-gradient(135deg,#2a7a2a,#217346);color:white;border:none;padding:10px 18px;border-radius:var(--r);font-family:var(--sans);font-size:13px;font-weight:700;cursor:pointer;">🖨 Print</button>
            <button onclick="document.getElementById('inv-detail-modal').classList.remove('open')" style="background:var(--card2);border:1px solid var(--border2);color:var(--sub);padding:10px 18px;border-radius:var(--r);font-family:var(--sans);font-size:13px;font-weight:700;cursor:pointer;">Close</button>
        </div>
    </div>
</div>

<!-- COLLECT PAYMENT MODAL (cash / full_payment) -->
<div class="modal-bg" id="collect-modal">
    <div class="modal" style="width:420px">
        <div class="modal-title">Collect Payment</div>
        <div class="modal-sub" id="collect-modal-sub"></div>
        <div style="background:rgba(0,255,157,.06);border:1px solid rgba(0,255,157,.15);border-radius:10px;padding:10px 14px;margin-bottom:14px;font-size:12px;color:var(--green)">
            Recording payment moves: <b>Deferred Revenue → Sales Revenue</b>
        </div>
        <div style="display:flex;flex-direction:column;gap:6px;margin-bottom:14px">
            <label style="font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted)">Confirm Invoice Number *</label>
            <input id="collect-inv-num" placeholder="e.g. B2B-00012"
                style="background:var(--card2);border:1px solid var(--border2);border-radius:10px;padding:10px 12px;color:var(--text);font-family:var(--mono);font-size:14px;outline:none;width:100%">
            <span style="font-size:11px;color:var(--muted)">Type the invoice number to confirm you're collecting the right payment</span>
        </div>
        <div style="display:flex;flex-direction:column;gap:6px;margin-bottom:14px">
            <label style="font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted)">Amount *</label>
            <input id="collect-amount" type="number" placeholder="0.00" min="0.01" step="any"
                style="background:var(--card2);border:1px solid var(--border2);border-radius:10px;padding:10px 12px;color:var(--text);font-family:var(--sans);font-size:14px;outline:none;width:100%">
        </div>
        <div style="display:flex;gap:10px;justify-content:flex-end">
            <button onclick="document.getElementById('collect-modal').classList.remove('open')" style="background:transparent;border:1px solid var(--border2);color:var(--sub);padding:10px 18px;border-radius:var(--r);font-family:var(--sans);font-size:13px;font-weight:700;cursor:pointer;">Cancel</button>
            <button onclick="saveCollect()" style="background:linear-gradient(135deg,#00ff9d,#00d4ff);border:none;color:#021a10;padding:12px 28px;border-radius:var(--r);font-family:var(--sans);font-size:14px;font-weight:800;cursor:pointer;letter-spacing:.3px;box-shadow:0 4px 20px rgba(0,255,157,.3);">✓ Confirm Payment</button>
        </div>
    </div>
</div>

<!-- CONSIGNMENT PAYMENT MODAL -->
<div class="modal-bg" id="cons-modal">
    <div class="modal" style="width:440px">
        <div class="modal-title">💰 Record Consignment Payment</div>
        <div class="modal-sub" id="cons-modal-sub" style="color:var(--muted);font-size:13px;margin-bottom:16px"></div>
        <div style="background:rgba(45,212,191,.06);border:1px solid rgba(45,212,191,.15);border-radius:10px;padding:10px 14px;margin-bottom:16px;font-size:12px;color:var(--teal)">
            Amount moves from <b>Deferred Revenue → Sales Revenue</b>
        </div>
        <div style="display:flex;flex-direction:column;gap:6px;margin-bottom:14px">
            <label style="font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted)">Amount Paid *</label>
            <input id="cons-amount" type="number" placeholder="0.00" min="0.01" step="any"
                style="background:var(--card2);border:1px solid var(--border2);border-radius:10px;padding:10px 12px;color:var(--text);font-family:var(--mono);font-size:16px;outline:none;width:100%">
        </div>
        <div style="display:flex;flex-direction:column;gap:6px;margin-bottom:16px">
            <label style="font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted)">For which month's sales?</label>
            <select id="cons-month" style="background:var(--card2);border:1px solid var(--border2);border-radius:10px;padding:10px 12px;color:var(--text);font-family:var(--sans);font-size:14px;outline:none;width:100%">
                <option value="">General payment (no specific month)</option>
            </select>
        </div>
        <div style="display:flex;gap:10px;justify-content:flex-end;margin-top:8px">
            <button onclick="document.getElementById('cons-modal').classList.remove('open')"
                style="background:transparent;border:1px solid var(--border2);color:var(--sub);padding:10px 18px;border-radius:var(--r);font-family:var(--sans);font-size:13px;font-weight:700;cursor:pointer;">Cancel</button>
            <button onclick="saveConsPayment()"
                style="background:linear-gradient(135deg,#2dd4bf,#4d9fff);border:none;color:#001a18;padding:12px 28px;border-radius:var(--r);font-family:var(--sans);font-size:14px;font-weight:800;cursor:pointer;letter-spacing:.3px;box-shadow:0 4px 20px rgba(45,212,191,.35);">💰 Record Payment</button>
        </div>
    </div>
</div>

<!-- ADD ACCOUNT MODAL -->
<div class="modal-bg" id="acc-modal">
    <div class="modal" style="width:420px">
        <div class="modal-title">Add Account</div>
        <div class="fld"><label>Account Code *</label><input id="ac-code" placeholder="e.g. 1010"></div>
        <div class="fld"><label>Account Name *</label><input id="ac-name" placeholder="e.g. Petty Cash"></div>
        <div class="fld"><label>Type *</label>
            <select id="ac-type">
                <option value="asset">Asset</option>
                <option value="liability">Liability</option>
                <option value="equity">Equity</option>
                <option value="revenue">Revenue</option>
                <option value="expense">Expense</option>
            </select>
        </div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeAccModal()">Cancel</button>
            <button class="btn btn-green" onclick="saveAccount()">Add Account</button>
        </div>
    </div>
</div>

<!-- JOURNAL ENTRY MODAL -->
<div class="modal-bg" id="je-modal">
    <div class="modal">
        <div class="modal-title">New Journal Entry</div>
        <div class="fld"><label>Description</label><input id="je-desc" placeholder="e.g. Monthly rent payment"></div>

        <div style="display:grid;grid-template-columns:2fr 1fr 1fr 30px;gap:8px;margin-bottom:6px;">
            <span style="font-size:10px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:1px">Account</span>
            <span style="font-size:10px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:1px">Debit</span>
            <span style="font-size:10px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:1px">Credit</span>
            <span></span>
        </div>

        <div id="je-entries"></div>
        <button class="add-entry-btn" onclick="addEntryRow()">+ Add Line</button>

        <div class="balance-display">
            <span style="color:var(--muted);font-size:13px;font-weight:600">Balance Check</span>
            <span id="balance-check" class="balance-ok">Debit 0.00 = Credit 0.00 ✓</span>
        </div>

        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeJEModal()">Cancel</button>
            <button class="btn btn-blue" onclick="saveJournal()">Post Journal Entry</button>
        </div>
    </div>
</div>

<!-- JOURNAL DETAIL SIDE PANEL -->
<div class="side-bg" id="side-bg" onclick="closeSide()"></div>
<div class="side-panel" id="side-panel">
    <div class="side-header">
        <h3 id="side-title">Journal Entry</h3>
        <button class="close-btn" onclick="closeSide()">×</button>
    </div>
    <div class="side-body" id="side-body"></div>
</div>

<div class="toast" id="toast"></div>

<script>
let accounts    = [];
let currentTab  = "accounts";

async function init(){
    await loadAccounts();
}

/* ── TABS ── */
function switchTab(tab){
    currentTab = tab;
    ["accounts","journals","pl","tb","b2b"].forEach(t=>{
        document.getElementById("section-"+t).style.display = t===tab?"":"none";
        document.getElementById("tab-"+t).classList.toggle("active", t===tab);
    });
    document.getElementById("btn-add-acc").style.display  = tab==="accounts"?"":"none";
    document.getElementById("btn-seed").style.display     = tab==="accounts"?"":"none";
    document.getElementById("btn-add-je").style.display   = tab==="journals"?"":"none";

    if(tab==="journals") loadJournals();
    if(tab==="pl")       loadPL();
    if(tab==="tb")       loadTB();
    if(tab==="b2b")      loadB2BInvoices();
}

/* ── ACCOUNTS ── */
async function loadAccounts(){
    accounts = await (await fetch("/accounting/api/accounts")).json();
    if(!accounts.length){
        document.getElementById("accounts-body").innerHTML =
            `<tr><td colspan="5" style="text-align:center;color:var(--muted);padding:40px">
                No accounts yet. Click "⚡ Setup Default Accounts" to get started.
            </td></tr>`;
        document.getElementById("btn-seed").style.display="";
        return;
    }
    document.getElementById("btn-seed").style.display="none";
    document.getElementById("accounts-body").innerHTML = accounts.map(a=>`
        <tr>
            <td style="font-family:var(--mono);font-size:12px;color:var(--blue)">${a.code}</td>
            <td class="name">${a.name}</td>
            <td><span class="type-badge type-${a.type}">${a.type}</span></td>
            <td class="${a.balance>=0?'dr':'cr'}">${Math.abs(a.balance).toFixed(2)}</td>
            <td><button class="action-btn danger" onclick="deleteAccount(${a.id},'${a.name.replace(/'/g,"\\'")}')">Delete</button></td>
        </tr>`).join("");
}

async function seedAccounts(){
    let res  = await fetch("/accounting/api/accounts/seed",{method:"POST"});
    let data = await res.json();
    showToast(data.message);
    loadAccounts();
}

function openAddAccModal(){ document.getElementById("acc-modal").classList.add("open"); }
function closeAccModal()  { document.getElementById("acc-modal").classList.remove("open"); }

async function saveAccount(){
    let code = document.getElementById("ac-code").value.trim();
    let name = document.getElementById("ac-name").value.trim();
    if(!code||!name){ showToast("Code and name are required"); return; }
    let res  = await fetch("/accounting/api/accounts",{
        method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({code, name, type:document.getElementById("ac-type").value}),
    });
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    closeAccModal();
    showToast("Account added ✓");
    loadAccounts();
}

async function deleteAccount(id,name){
    if(!confirm(`Delete account "${name}"?`)) return;
    let res  = await fetch(`/accounting/api/accounts/${id}`,{method:"DELETE"});
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    showToast("Account deleted ✓");
    loadAccounts();
}

/* ── JOURNALS ── */
async function loadJournals(){
    let data = await (await fetch("/accounting/api/journals")).json();
    if(!data.journals.length){
        document.getElementById("journals-body").innerHTML =
            `<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:40px">No journal entries yet</td></tr>`;
        return;
    }
    document.getElementById("journals-body").innerHTML = data.journals.map(j=>`
        <tr>
            <td style="font-family:var(--mono);color:var(--muted);font-size:12px">#${j.id}</td>
            <td><span class="type-badge type-${j.ref_type==='manual'?'equity':'revenue'}">${j.ref_type}</span></td>
            <td class="name">${j.description}</td>
            <td style="color:var(--sub)">${j.entries_count} lines</td>
            <td class="dr">${j.total_debit.toFixed(2)}</td>
            <td style="font-size:12px;color:var(--muted)">${j.created_at}</td>
            <td><button class="action-btn green" onclick="viewJournal(${j.id})">View</button></td>
        </tr>`).join("");
}

function openJEModal(){
    document.getElementById("je-desc").value="";
    document.getElementById("je-entries").innerHTML="";
    addEntryRow(); addEntryRow();
    updateBalanceCheck();
    document.getElementById("je-modal").classList.add("open");
}
function closeJEModal(){ document.getElementById("je-modal").classList.remove("open"); }

function addEntryRow(){
    let div = document.createElement("div");
    div.className = "entry-row";
    div.innerHTML = `
        <select onchange="updateBalanceCheck()">
            <option value="">Select account…</option>
            ${accounts.map(a=>`<option value="${a.id}">${a.code} — ${a.name}</option>`).join("")}
        </select>
        <input type="number" placeholder="0.00" min="0" step="any" oninput="updateBalanceCheck()">
        <input type="number" placeholder="0.00" min="0" step="any" oninput="updateBalanceCheck()">
        <button class="rm-btn" onclick="this.parentElement.remove();updateBalanceCheck()">×</button>
    `;
    document.getElementById("je-entries").appendChild(div);
}

function updateBalanceCheck(){
    let rows   = document.querySelectorAll("#je-entries .entry-row");
    let totalD = 0, totalC = 0;
    rows.forEach(row=>{
        let inputs = row.querySelectorAll("input");
        totalD += parseFloat(inputs[0].value)||0;
        totalC += parseFloat(inputs[1].value)||0;
    });
    let el = document.getElementById("balance-check");
    let ok = Math.abs(totalD-totalC) < 0.01;
    el.className = ok ? "balance-ok" : "balance-fail";
    el.innerText  = ok
        ? `Debit ${totalD.toFixed(2)} = Credit ${totalC.toFixed(2)} ✓`
        : `Debit ${totalD.toFixed(2)} ≠ Credit ${totalC.toFixed(2)} ✗`;
}

async function saveJournal(){
    let desc = document.getElementById("je-desc").value.trim();
    let rows = document.querySelectorAll("#je-entries .entry-row");
    let entries = [];
    for(let row of rows){
        let acc_id = parseInt(row.querySelector("select").value);
        let debit  = parseFloat(row.querySelectorAll("input")[0].value)||0;
        let credit = parseFloat(row.querySelectorAll("input")[1].value)||0;
        if(!acc_id) continue;
        entries.push({account_id:acc_id, debit, credit});
    }
    if(!entries.length){ showToast("Add at least one entry"); return; }

    let res  = await fetch("/accounting/api/journals",{
        method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({description:desc, entries}),
    });
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    closeJEModal();
    showToast("Journal entry posted ✓");
    loadJournals(); loadAccounts();
}

async function viewJournal(id){
    document.getElementById("side-body").innerHTML=`<div style="color:var(--muted);font-size:13px">Loading…</div>`;
    document.getElementById("side-bg").classList.add("open");
    document.getElementById("side-panel").classList.add("open");
    let j = await (await fetch(`/accounting/api/journals/${id}`)).json();
    document.getElementById("side-title").innerText = `Journal #${j.id}`;
    document.getElementById("side-body").innerHTML = `
        <div style="display:flex;flex-direction:column;gap:14px">
            <div style="background:var(--card2);border:1px solid var(--border2);border-radius:10px;padding:14px">
                <div style="display:flex;justify-content:space-between;margin-bottom:6px">
                    <span style="color:var(--muted);font-size:12px">Type</span>
                    <span style="font-weight:700;text-transform:capitalize">${j.ref_type}</span>
                </div>
                <div style="display:flex;justify-content:space-between;margin-bottom:6px">
                    <span style="color:var(--muted);font-size:12px">Description</span>
                    <span style="font-size:13px">${j.description}</span>
                </div>
                <div style="display:flex;justify-content:space-between">
                    <span style="color:var(--muted);font-size:12px">Date</span>
                    <span style="font-size:12px">${j.created_at}</span>
                </div>
            </div>
            <table style="width:100%;border-collapse:collapse">
                <thead><tr>
                    <th style="text-align:left;font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);padding:8px 0">Account</th>
                    <th style="text-align:right;font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);padding:8px 0">Debit</th>
                    <th style="text-align:right;font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);padding:8px 0">Credit</th>
                </tr></thead>
                <tbody>
                ${j.entries.map(e=>`
                    <tr>
                        <td style="padding:9px 0;border-top:1px solid var(--border);font-size:13px">
                            <div style="color:var(--text);font-weight:600">${e.account_name}</div>
                            <div style="font-family:var(--mono);font-size:10px;color:var(--muted)">${e.account_code}</div>
                        </td>
                        <td style="padding:9px 0;border-top:1px solid var(--border);text-align:right;font-family:var(--mono);color:var(--green)">${e.debit>0?e.debit.toFixed(2):""}</td>
                        <td style="padding:9px 0;border-top:1px solid var(--border);text-align:right;font-family:var(--mono);color:var(--blue)">${e.credit>0?e.credit.toFixed(2):""}</td>
                    </tr>`).join("")}
                </tbody>
            </table>
        </div>`;
}

function closeSide(){
    document.getElementById("side-bg").classList.remove("open");
    document.getElementById("side-panel").classList.remove("open");
}

/* ── P&L ── */
async function loadPL(){
    let d = await (await fetch("/accounting/api/profit-loss")).json();
    let profitColor = d.net_profit>=0?"var(--green)":"var(--danger)";
    document.getElementById("pl-content").innerHTML = `
        <div class="pl-section">
            <div class="pl-header">Revenue</div>
            ${d.revenues.map(r=>`
                <div class="pl-row">
                    <span style="color:var(--sub)">${r.code} — ${r.name}</span>
                    <span style="font-family:var(--mono);color:var(--green)">${Math.abs(r.amount).toFixed(2)}</span>
                </div>`).join("") || `<div class="pl-row" style="color:var(--muted)">No revenue recorded yet</div>`}
            <div class="pl-total">
                <span>Total Revenue</span>
                <span style="font-family:var(--mono);color:var(--green)">${Math.abs(d.total_revenue).toFixed(2)}</span>
            </div>
        </div>

        <div class="pl-section">
            <div class="pl-header">Expenses</div>
            ${d.expenses.map(e=>`
                <div class="pl-row">
                    <span style="color:var(--sub)">${e.code} — ${e.name}</span>
                    <span style="font-family:var(--mono);color:var(--warn)">${Math.abs(e.amount).toFixed(2)}</span>
                </div>`).join("") || `<div class="pl-row" style="color:var(--muted)">No expenses recorded yet</div>`}
            <div class="pl-total">
                <span>Total Expenses</span>
                <span style="font-family:var(--mono);color:var(--warn)">${Math.abs(d.total_expense).toFixed(2)}</span>
            </div>
        </div>

        <div class="pl-net">
            <span>${d.net_profit>=0?"Net Profit":"Net Loss"}</span>
            <span style="font-family:var(--mono);color:${profitColor}">${Math.abs(d.net_profit).toFixed(2)}</span>
        </div>`;
}

/* ── TRIAL BALANCE ── */
async function loadTB(){
    let d = await (await fetch("/accounting/api/trial-balance")).json();
    document.getElementById("tb-body").innerHTML = d.rows.map(r=>`
        <tr>
            <td style="font-family:var(--mono);font-size:12px;color:var(--blue)">${r.code}</td>
            <td class="name">${r.name}</td>
            <td><span class="type-badge type-${r.type}">${r.type}</span></td>
            <td class="dr">${r.debit>0?r.debit.toFixed(2):""}</td>
            <td class="cr">${r.credit>0?r.credit.toFixed(2):""}</td>
        </tr>`).join("");
    let balanced = Math.abs(d.total_debit-d.total_credit)<0.01;
    document.getElementById("tb-foot").innerHTML = `
        <tr style="background:var(--card2)">
            <td colspan="3" style="padding:12px 16px;font-weight:800;color:var(--sub)">
                Total ${balanced?"✓ Balanced":"✗ Not Balanced"}
            </td>
            <td style="padding:12px 16px;font-family:var(--mono);font-size:14px;font-weight:800;color:var(--green)">${d.total_debit.toFixed(2)}</td>
            <td style="padding:12px 16px;font-family:var(--mono);font-size:14px;font-weight:800;color:var(--blue)">${d.total_credit.toFixed(2)}</td>
        </tr>`;
}

["acc-modal","je-modal"].forEach(id=>{
    document.getElementById(id).addEventListener("click",function(e){ if(e.target===this) this.classList.remove("open"); });
});

let toastTimer=null;
function showToast(msg){
    let t=document.getElementById("toast");
    t.innerText=msg; t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer=setTimeout(()=>t.classList.remove("show"),3500);
}

/* ── B2B INVOICES ── */
let allB2BInvoices  = [];
let collectInvoiceId = null;
let consInvoiceId    = null;
let currentInvDetail = null;

async function loadB2BInvoices(){
    let type   = document.getElementById("b2b-type-filter").value;
    let status = document.getElementById("b2b-status-filter").value;
    let url    = `/accounting/api/b2b-invoices?${type?"invoice_type="+type:""}${status?"&status="+status:""}`;
    allB2BInvoices = await (await fetch(url)).json();
    renderB2BInvoices(allB2BInvoices);

    // Show consignment payment history section if filtering consignment
    document.getElementById("cons-payment-section").style.display = type==="consignment"?"":"none";
}

function renderB2BInvoices(invoices){
    if(!invoices.length){
        document.getElementById("b2b-invoices-body").innerHTML =
            `<tr><td colspan="9" style="text-align:center;color:var(--muted);padding:40px">No invoices found</td></tr>`;
        return;
    }
    const typeLabel = {cash:"💵 Cash", full_payment:"📋 Full Payment", consignment:"🔄 Consignment"};
    const typeBadge = {cash:"badge-cash", full_payment:"badge-full_payment", consignment:"badge-consignment"};
    const statusColor = {paid:"var(--green)", unpaid:"var(--warn)", partial:"var(--blue)"};

    document.getElementById("b2b-invoices-body").innerHTML = invoices.map(i=>{
        let isCons   = i.invoice_type === "consignment";
        let isPaid   = i.status === "paid";
        let hasBalance = i.balance_due > 0.01;

        let actions = `<div style="display:flex;gap:5px;flex-wrap:wrap">
            <button style="background:transparent;border:1px solid var(--border2);color:var(--sub);font-size:12px;font-weight:600;padding:5px 10px;border-radius:7px;cursor:pointer;font-family:var(--sans);"
                onmouseenter="this.style.borderColor='var(--blue)';this.style.color='var(--blue)'"
                onmouseleave="this.style.borderColor='var(--border2)';this.style.color='var(--sub)'"
                onclick="openInvDetail(${i.id})">View</button>
            ${!isCons && hasBalance
                ? `<button style="background:transparent;border:1px solid var(--border2);color:var(--sub);font-size:12px;font-weight:600;padding:5px 10px;border-radius:7px;cursor:pointer;font-family:var(--sans);"
                    onmouseenter="this.style.borderColor='var(--warn)';this.style.color='var(--warn)'"
                    onmouseleave="this.style.borderColor='var(--border2)';this.style.color='var(--sub)'"
                    onclick="openCollectModal(${i.id},'${i.invoice_number}',${i.balance_due})">💵 Collect</button>`
                : ""}
            ${isCons && !isPaid
                ? `<button style="background:transparent;border:1px solid var(--border2);color:var(--sub);font-size:12px;font-weight:600;padding:5px 10px;border-radius:7px;cursor:pointer;font-family:var(--sans);"
                    onmouseenter="this.style.borderColor='var(--teal)';this.style.color='var(--teal)'"
                    onmouseleave="this.style.borderColor='var(--border2)';this.style.color='var(--sub)'"
                    onclick="openConsModal(${i.id},'${i.invoice_number}',${i.balance_due})">💰 Record Payment</button>`
                : ""}
        </div>`;

        return `<tr>
            <td style="font-family:var(--mono);font-size:12px;color:var(--blue)">${i.invoice_number}</td>
            <td style="color:var(--text);font-weight:600">${i.client}</td>
            <td><span style="font-size:11px;font-weight:700;padding:2px 9px;border-radius:20px;background:${isCons?"rgba(45,212,191,.1)":i.invoice_type==="cash"?"rgba(0,255,157,.1)":"rgba(77,159,255,.1)"};color:${isCons?"var(--teal)":i.invoice_type==="cash"?"var(--green)":"var(--blue)"}">${typeLabel[i.invoice_type]||i.invoice_type}</span></td>
            <td style="font-family:var(--mono);font-weight:700">${i.total.toFixed(2)}</td>
            <td style="font-family:var(--mono);color:var(--green)">${i.amount_paid.toFixed(2)}</td>
            <td style="font-family:var(--mono);color:${i.balance_due>0?"var(--warn)":"var(--muted)"};font-weight:${i.balance_due>0?"700":"400"}">${i.balance_due>0?i.balance_due.toFixed(2):"—"}</td>
            <td><span style="font-size:11px;font-weight:700;padding:2px 9px;border-radius:20px;background:rgba(0,0,0,.2);color:${statusColor[i.status]||"var(--muted)"}">${i.status}</span></td>
            <td style="font-size:12px;color:var(--muted)">${i.created_at}</td>
            <td>${actions}</td>
        </tr>`;
    }).join("");
}

/* ── INVOICE DETAIL ── */
function openInvDetail(id){
    let inv = allB2BInvoices.find(i=>i.id===id);
    if(!inv) return;
    currentInvDetail = inv;

    document.getElementById("inv-detail-num").innerText = inv.invoice_number + " — " + inv.created_at;
    document.getElementById("inv-detail-meta").innerHTML = `
        <div style="display:flex;justify-content:space-between;font-size:13px;padding:4px 0"><span style="color:var(--muted)">Client</span><span style="font-weight:700">${inv.client}</span></div>
        <div style="display:flex;justify-content:space-between;font-size:13px;padding:4px 0"><span style="color:var(--muted)">Type</span><span>${inv.invoice_type.split("_").map(w=>w.charAt(0).toUpperCase()+w.slice(1)).join(" ")}</span></div>
        <div style="display:flex;justify-content:space-between;font-size:13px;padding:4px 0"><span style="color:var(--muted)">Status</span><span style="font-weight:700;color:${inv.status==="paid"?"var(--green)":"var(--warn)"}">${inv.status.toUpperCase()}</span></div>`;

    document.getElementById("inv-detail-items").innerHTML = inv.items.map(item=>`
        <tr>
            <td style="font-size:13px;padding:8px 0;border-bottom:1px solid var(--border);color:var(--text)">${item.product}</td>
            <td style="text-align:center;font-family:var(--mono);padding:8px 0;border-bottom:1px solid var(--border)">${item.qty.toFixed(0)}</td>
            <td style="text-align:right;font-family:var(--mono);padding:8px 0;border-bottom:1px solid var(--border)">${item.unit_price.toFixed(2)}</td>
            <td style="text-align:right;font-family:var(--mono);font-weight:700;padding:8px 0;border-bottom:1px solid var(--border);color:var(--green)">${item.total.toFixed(2)}</td>
        </tr>`).join("");

    document.getElementById("inv-detail-totals").innerHTML = `
        <div style="display:flex;justify-content:space-between;font-size:13px;padding:4px 0"><span style="color:var(--muted)">Subtotal</span><span style="font-family:var(--mono)">${inv.subtotal.toFixed(2)}</span></div>
        ${inv.discount>0?`<div style="display:flex;justify-content:space-between;font-size:13px;padding:4px 0"><span style="color:var(--muted)">Discount</span><span style="font-family:var(--mono);color:var(--danger)">-${inv.discount.toFixed(2)}</span></div>`:""}
        <div style="display:flex;justify-content:space-between;font-size:16px;font-weight:800;padding:8px 0;border-top:1px solid var(--border2);margin-top:6px"><span>Total</span><span style="font-family:var(--mono);color:var(--green)">${inv.total.toFixed(2)} EGP</span></div>
        ${inv.balance_due>0?`<div style="display:flex;justify-content:space-between;font-size:13px;padding:4px 0"><span style="color:var(--muted)">Balance Due</span><span style="font-family:var(--mono);font-weight:700;color:var(--warn)">${inv.balance_due.toFixed(2)} EGP</span></div>`:""}`;

    document.getElementById("inv-detail-modal").classList.add("open");
}

function printInvDetail(){
    let inv = currentInvDetail;
    if(!inv) return;
    let rows = inv.items.map(item=>`
        <tr>
            <td style="padding:8px 12px;border-bottom:1px solid #eee">${item.product}</td>
            <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:center">${item.qty.toFixed(0)}</td>
            <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right">${item.unit_price.toFixed(2)}</td>
            <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;font-weight:700">${item.total.toFixed(2)}</td>
        </tr>`).join("");
    let win = window.open("","_blank","width=650,height=900");
    win.document.write(`<!DOCTYPE html><html><head><title>${inv.invoice_number}</title>
    <style>body{font-family:Arial,sans-serif;padding:30px;color:#111;max-width:600px;margin:0 auto}
    .header{text-align:center;margin-bottom:20px;padding-bottom:16px;border-bottom:2px solid #2a7a2a}
    .logo{max-height:70px;margin-bottom:6px}
    .company{font-size:18px;font-weight:900;color:#2a7a2a;margin-bottom:4px}
    .meta{display:flex;justify-content:space-between;font-size:13px;margin-bottom:16px}
    .meta-label{color:#555}
    table{width:100%;border-collapse:collapse;margin-bottom:16px}
    thead{background:#f0f0f0}
    th{padding:8px 12px;text-align:left;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.5px}
    .totals{text-align:right}
    .total-final{font-size:18px;font-weight:900;color:#2a7a2a;border-top:2px solid #2a7a2a;padding-top:8px;margin-top:8px}
    .footer{text-align:center;margin-top:30px;font-size:11px;color:#888;border-top:1px solid #eee;padding-top:10px;font-style:italic}
    @media print{button{display:none}}
    </style></head><body>
    <div class="header">
        <img src="/static/logo.png" class="logo"><br>
        <div class="company">Habiba Organic Farm</div>
        <div style="font-size:12px;color:#555">Commercial registry: 126278 | Tax ID: 560042604</div>
    </div>
    <div class="meta">
        <div><div class="meta-label">Invoice #</div><b>${inv.invoice_number}</b></div>
        <div><div class="meta-label">Client</div><b>${inv.client}</b></div>
        <div><div class="meta-label">Date</div>${inv.created_at}</div>
        <div><div class="meta-label">Type</div>${inv.invoice_type.split("_").map(w=>w.charAt(0).toUpperCase()+w.slice(1)).join(" ")}</div>
    </div>
    <table><thead><tr><th>Product</th><th>QTY</th><th style="text-align:right">Price</th><th style="text-align:right">Total</th></tr></thead>
    <tbody>${rows}</tbody></table>
    <div class="totals">
        <div style="font-size:13px">Subtotal: ${inv.subtotal.toFixed(2)}</div>
        ${inv.discount>0?`<div style="font-size:13px;color:#c0392b">Discount: -${inv.discount.toFixed(2)}</div>`:""}
        <div class="total-final">Total: ${inv.total.toFixed(2)} EGP</div>
        ${inv.balance_due>0?`<div style="color:#c0392b;font-size:13px;margin-top:6px">Balance Due: ${inv.balance_due.toFixed(2)} EGP</div>`:""}
    </div>
    <div style="margin-top:40px;display:flex;justify-content:space-between;font-size:12px;color:#555;border-top:1px solid #ddd;padding-top:16px">
        <div><div style="border-bottom:1px solid #aaa;width:160px;margin-bottom:4px;padding-bottom:20px"></div>Received by</div>
        <div><div style="border-bottom:1px solid #aaa;width:160px;margin-bottom:4px;padding-bottom:20px"></div>Receipt Date</div>
    </div>
    <div class="footer">Desert going green | habibaorganicfarm | habibacommunity.com</div>
    <br><button onclick="window.print()">🖨 Print</button>
    </body></html>`);
    win.document.close();
}

/* ── COLLECT PAYMENT (cash / full_payment) ── */
let collectInvoiceNum = null;

function openCollectModal(id, num, balance){
    collectInvoiceId  = id;
    collectInvoiceNum = num;
    document.getElementById("collect-modal-sub").innerText = `${num} — Balance: ${balance.toFixed(2)} EGP`;
    document.getElementById("collect-amount").value  = balance.toFixed(2);
    document.getElementById("collect-inv-num").value = "";
    document.getElementById("collect-modal").classList.add("open");
}

async function saveCollect(){
    let typed  = document.getElementById("collect-inv-num").value.trim();
    let amount = parseFloat(document.getElementById("collect-amount").value)||0;
    if(!typed){ showToast("Please enter the invoice number to confirm"); return; }
    if(typed !== collectInvoiceNum){
        showToast(`Invoice number doesn't match — expected: ${collectInvoiceNum}`);
        document.getElementById("collect-inv-num").style.border = "1px solid var(--danger)";
        return;
    }
    document.getElementById("collect-inv-num").style.border = "";
    if(amount<=0){ showToast("Enter a valid amount"); return; }
    let res  = await fetch(`/accounting/api/b2b-invoices/${collectInvoiceId}/collect`,{
        method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({amount}),
    });
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    document.getElementById("collect-modal").classList.remove("open");
    showToast(`✓ Payment collected — Revenue recognized! Status: ${data.status}`);
    loadB2BInvoices();
}

/* ── CONSIGNMENT PAYMENT ── */
function openConsModal(id, num, balance){
    consInvoiceId = id;
    document.getElementById("cons-modal-sub").innerText = `${num} — Balance due: ${balance.toFixed(2)} EGP`;
    document.getElementById("cons-amount").value = "";
    document.getElementById("cons-amount").placeholder = "0.00";
    // Fill month selector
    let sel = document.getElementById("cons-month");
    sel.innerHTML = '<option value="">General payment (no specific month)</option>';
    let d = new Date();
    for(let i=0;i<12;i++){
        let label = d.toLocaleDateString("en-GB",{month:"long",year:"numeric"});
        sel.innerHTML += `<option value="${label}">${label}</option>`;
        d.setMonth(d.getMonth()-1);
    }
    document.getElementById("cons-modal").classList.add("open");
    setTimeout(()=>document.getElementById("cons-amount").focus(), 100);
}

async function saveConsPayment(){
    let amount = parseFloat(document.getElementById("cons-amount").value)||0;
    if(amount<=0){ showToast("Enter a valid amount"); return; }
    let month  = document.getElementById("cons-month").value;
    let res    = await fetch(`/accounting/api/b2b-invoices/${consInvoiceId}/consignment-payment`,{
        method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({amount, month_label:month||null}),
    });
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    document.getElementById("cons-modal").classList.remove("open");
    showToast(`✓ ${amount.toFixed(2)} EGP recorded${month?" ("+month+")":""} — Revenue recognized!`);
    loadB2BInvoices();
}

["inv-detail-modal","collect-modal","cons-modal"].forEach(id=>{
    let el = document.getElementById(id);
    if(el) el.addEventListener("click",function(e){ if(e.target===this) this.classList.remove("open"); });
});

init();
</script>
</body>
</html>
"""