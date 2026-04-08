from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import Optional
from pydantic import BaseModel

from app.database import get_db
from app.models.customer import Customer
from app.models.invoice import Invoice

router = APIRouter(prefix="/customers-mgmt", tags=["Customers"])


# ── Schemas ────────────────────────────────────────────
class CustomerCreate(BaseModel):
    name:    str
    phone:   Optional[str] = None
    email:   Optional[str] = None
    address: Optional[str] = None

class CustomerUpdate(BaseModel):
    name:    Optional[str] = None
    phone:   Optional[str] = None
    email:   Optional[str] = None
    address: Optional[str] = None


# ── API ────────────────────────────────────────────────
@router.get("/api/list")
def get_customers(
    q:     str = "",
    skip:  int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    query = db.query(Customer)
    if q:
        query = query.filter(
            Customer.name.ilike(f"%{q}%") |
            Customer.phone.ilike(f"%{q}%") |
            Customer.email.ilike(f"%{q}%")
        )
    total = query.count()
    items = query.order_by(Customer.name).offset(skip).limit(limit).all()

    result = []
    for c in items:
        inv_count = db.query(func.count(Invoice.id)).filter(Invoice.customer_id == c.id).scalar() or 0
        inv_total = db.query(func.sum(Invoice.total)).filter(
            Invoice.customer_id == c.id, Invoice.status == "paid"
        ).scalar() or 0
        result.append({
            "id":        c.id,
            "name":      c.name,
            "phone":     c.phone or "—",
            "email":     c.email or "—",
            "address":   c.address or "—",
            "invoices":  inv_count,
            "total_spent": float(inv_total),
        })

    return {"total": total, "items": result}


@router.get("/api/invoices/{customer_id}")
def get_customer_invoices(customer_id: int, db: Session = Depends(get_db)):
    invoices = (
        db.query(Invoice)
        .filter(Invoice.customer_id == customer_id)
        .order_by(Invoice.created_at.desc())
        .limit(20)
        .all()
    )
    return [
        {
            "id":             i.id,
            "invoice_number": i.invoice_number,
            "total":          float(i.total),
            "status":         i.status,
            "payment_method": i.payment_method,
            "created_at":     i.created_at.strftime("%Y-%m-%d %H:%M") if i.created_at else "—",
        }
        for i in invoices
    ]


@router.post("/api/add")
def add_customer(data: CustomerCreate, db: Session = Depends(get_db)):
    if data.phone and db.query(Customer).filter(Customer.phone == data.phone).first():
        raise HTTPException(status_code=400, detail="Phone number already exists")
    c = Customer(**data.model_dump())
    db.add(c); db.commit(); db.refresh(c)
    return {"id": c.id, "name": c.name}


@router.put("/api/edit/{customer_id}")
def edit_customer(customer_id: int, data: CustomerUpdate, db: Session = Depends(get_db)):
    c = db.query(Customer).filter(Customer.id == customer_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Customer not found")
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(c, k, v)
    db.commit()
    return {"ok": True}


@router.delete("/api/delete/{customer_id}")
def delete_customer(customer_id: int, db: Session = Depends(get_db)):
    c = db.query(Customer).filter(Customer.id == customer_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Customer not found")
    db.delete(c); db.commit()
    return {"ok": True}


# ── UI ─────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
def customers_ui():
    return """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Customers</title>
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
.btn-green { background: linear-gradient(135deg, var(--green), #00d4ff); color: #021a10; }
.btn-green:hover { filter: brightness(1.1); transform: translateY(-1px); }
.count-badge {
    background: var(--card2); border: 1px solid var(--border2);
    color: var(--sub); font-family: var(--mono); font-size: 12px;
    padding: 8px 14px; border-radius: var(--r);
}

.table-wrap {
    background: var(--card); border: 1px solid var(--border);
    border-radius: var(--r); overflow: hidden;
}
table { width: 100%; border-collapse: collapse; }
thead { background: var(--card2); }
th {
    text-align: left; font-size: 10px; font-weight: 700;
    letter-spacing: 1px; text-transform: uppercase;
    color: var(--muted); padding: 12px 16px;
}
td { padding: 13px 16px; border-top: 1px solid var(--border); color: var(--sub); font-size: 13px; }
tr:hover td { background: rgba(255,255,255,.02); cursor: pointer; }
td.name  { color: var(--text); font-weight: 600; }
td.mono  { font-family: var(--mono); color: var(--green); }
td.phone { font-family: var(--mono); font-size: 12px; }

.action-btn {
    background: transparent; border: 1px solid var(--border2);
    color: var(--sub); font-size: 12px; font-weight: 600;
    padding: 5px 10px; border-radius: 7px; cursor: pointer;
    transition: all .15s; font-family: var(--sans);
}
.action-btn:hover { border-color: var(--blue); color: var(--blue); }
.action-btn.danger:hover { border-color: var(--danger); color: var(--danger); }

.pagination {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 16px; border-top: 1px solid var(--border);
    font-size: 13px; color: var(--muted);
}
.page-btns { display: flex; gap: 6px; }
.page-btn {
    background: var(--card2); border: 1px solid var(--border2);
    color: var(--sub); font-family: var(--sans); font-size: 12px;
    padding: 6px 12px; border-radius: 7px; cursor: pointer; transition: all .15s;
}
.page-btn:hover { border-color: var(--green); color: var(--green); }
.page-btn:disabled { opacity: .3; cursor: not-allowed; }

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
    width: 480px; max-width: 95vw;
    animation: modalIn .2s ease;
}
@keyframes modalIn { from{opacity:0;transform:scale(.95)} to{opacity:1;transform:scale(1)} }
.modal-title { font-size: 18px; font-weight: 800; margin-bottom: 20px; }
.fld { display: flex; flex-direction: column; gap: 6px; margin-bottom: 14px; }
.fld label { font-size: 11px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; color: var(--muted); }
.fld input {
    background: var(--card2); border: 1px solid var(--border2);
    border-radius: 10px; padding: 10px 12px;
    color: var(--text); font-family: var(--sans); font-size: 14px;
    outline: none; transition: border-color .2s; width: 100%;
}
.fld input:focus { border-color: rgba(0,255,157,.4); }
.modal-actions { display: flex; gap: 10px; margin-top: 6px; justify-content: flex-end; }
.btn-cancel {
    background: transparent; border: 1px solid var(--border2);
    color: var(--sub); padding: 10px 18px; border-radius: var(--r);
    font-family: var(--sans); font-size: 13px; font-weight: 700; cursor: pointer;
}
.btn-cancel:hover { border-color: var(--danger); color: var(--danger); }

/* SIDE PANEL - invoice history */
.side-bg {
    position: fixed; inset: 0; z-index: 400;
    background: rgba(0,0,0,.5);
    display: none;
}
.side-bg.open { display: block; }
.side-panel {
    position: fixed; right: 0; top: 0; bottom: 0;
    width: 420px; max-width: 95vw;
    background: var(--card);
    border-left: 1px solid var(--border2);
    display: flex; flex-direction: column;
    transform: translateX(100%);
    transition: transform .3s ease;
    z-index: 401;
}
.side-panel.open { transform: translateX(0); }
.side-header {
    padding: 20px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
}
.side-header h3 { font-size: 16px; font-weight: 800; }
.close-btn {
    background: none; border: none; color: var(--muted);
    font-size: 22px; cursor: pointer; padding: 0;
    transition: color .15s;
}
.close-btn:hover { color: var(--danger); }
.side-body { flex: 1; overflow-y: auto; padding: 16px 20px; }
.inv-card {
    background: var(--card2); border: 1px solid var(--border);
    border-radius: 10px; padding: 14px; margin-bottom: 10px;
    display: flex; align-items: center; justify-content: space-between;
    text-decoration: none; transition: border-color .15s;
}
.inv-card:hover { border-color: var(--green); }
.inv-num { font-family: var(--mono); font-size: 12px; color: var(--muted); }
.inv-date { font-size: 12px; color: var(--muted); margin-top: 3px; }
.inv-total { font-family: var(--mono); font-size: 16px; font-weight: 700; color: var(--green); }
.inv-method { font-size: 11px; color: var(--sub); text-transform: capitalize; }
.side-stats {
    padding: 16px 20px; border-top: 1px solid var(--border);
    display: grid; grid-template-columns: 1fr 1fr; gap: 12px;
}
.side-stat { display: flex; flex-direction: column; gap: 4px; }
.side-stat-label { font-size: 10px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; color: var(--muted); }
.side-stat-val { font-family: var(--mono); font-size: 20px; font-weight: 700; color: var(--green); }

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
    <a href="/dashboard"        class="nav-link">Dashboard</a>
    <a href="/pos"              class="nav-link">POS</a>
    <a href="/products/"        class="nav-link">Products</a>
    <a href="/customers-mgmt/"  class="nav-link active">Customers</a>
    <a href="/import"           class="nav-link">Import</a>
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
        <div class="page-title">Customers</div>
        <div class="page-sub">View and manage your customer base</div>
    </div>

    <div class="toolbar">
        <div class="search-box">
            <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24">
                <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
            </svg>
            <input id="search" placeholder="Search by name, phone or email…" oninput="onSearch()">
        </div>
        <span class="count-badge" id="count-badge">— customers</span>
        <button class="btn btn-green" onclick="openAddModal()">+ Add Customer</button>
    </div>

    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>Name</th>
                    <th>Phone</th>
                    <th>Email</th>
                    <th>Address</th>
                    <th>Invoices</th>
                    <th>Total Spent</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody id="table-body">
                <tr><td colspan="7" style="text-align:center;color:var(--muted);padding:40px">Loading…</td></tr>
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

<!-- ADD / EDIT MODAL -->
<div class="modal-bg" id="modal">
    <div class="modal">
        <div class="modal-title" id="modal-title">Add Customer</div>
        <div class="fld">
            <label>Name *</label>
            <input id="f-name" placeholder="Customer name">
        </div>
        <div class="fld">
            <label>Phone</label>
            <input id="f-phone" placeholder="+20 100 000 0000">
        </div>
        <div class="fld">
            <label>Email</label>
            <input id="f-email" placeholder="customer@email.com">
        </div>
        <div class="fld">
            <label>Address</label>
            <input id="f-address" placeholder="City / Area">
        </div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeModal()">Cancel</button>
            <button class="btn btn-green" onclick="saveCustomer()">Save Customer</button>
        </div>
    </div>
</div>

<!-- INVOICE HISTORY SIDE PANEL -->
<div class="side-bg" id="side-bg" onclick="closeSide()"></div>
<div class="side-panel" id="side-panel">
    <div class="side-header">
        <h3 id="side-name">Customer</h3>
        <button class="close-btn" onclick="closeSide()">×</button>
    </div>
    <div class="side-body" id="side-body">
        <div style="color:var(--muted);font-size:13px">Loading…</div>
    </div>
    <div class="side-stats">
        <div class="side-stat">
            <span class="side-stat-label">Total Invoices</span>
            <span class="side-stat-val" id="side-inv-count">—</span>
        </div>
        <div class="side-stat">
            <span class="side-stat-label">Total Spent</span>
            <span class="side-stat-val" id="side-inv-total">—</span>
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
  requirePageAccess("page_customers");
  applyNavPermissions();
  initializeColorMode();
  setUserInfo();
  let currentPage = 0;
let pageSize    = 50;
let totalItems  = 0;
let searchTimer = null;
let editingId   = null;

async function load(){
    let q   = document.getElementById("search").value.trim();
    let url = `/customers-mgmt/api/list?skip=${currentPage*pageSize}&limit=${pageSize}`;
    if(q) url += `&q=${encodeURIComponent(q)}`;

    let data = await (await fetch(url)).json();
    totalItems = data.total;

    document.getElementById("count-badge").innerText = `${totalItems} customers`;
    document.getElementById("page-info").innerText =
        `Showing ${Math.min(currentPage*pageSize+1,totalItems)}–${Math.min((currentPage+1)*pageSize, totalItems)} of ${totalItems}`;

    document.getElementById("prev-btn").disabled = currentPage === 0;
    document.getElementById("next-btn").disabled = (currentPage+1)*pageSize >= totalItems;

    if(!data.items.length){
        document.getElementById("table-body").innerHTML =
            `<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:40px">No customers found</td></tr>`;
        return;
    }

    document.getElementById("table-body").innerHTML = data.items.map(c => `
        <tr onclick="openHistory(${c.id},'${c.name.replace(/'/g,"\\'")}',${c.invoices},${c.total_spent})">
            <td class="name">${c.name}</td>
            <td class="phone">${c.phone}</td>
            <td style="font-size:12px">${c.email}</td>
            <td style="font-size:12px">${c.address}</td>
            <td style="font-family:var(--mono);color:var(--blue)">${c.invoices}</td>
            <td class="mono">${c.total_spent.toFixed(2)}</td>
            <td style="display:flex;gap:6px" onclick="event.stopPropagation()">
                <button class="action-btn" onclick="openEditModal(${c.id},'${c.name.replace(/'/g,"\\'")}','${c.phone}','${c.email}','${c.address}')">Edit</button>
                <button class="action-btn danger" onclick="deleteCustomer(${c.id},'${c.name.replace(/'/g,"\\'")}')">Delete</button>
            </td>
        </tr>`).join("");
}

function onSearch(){
    clearTimeout(searchTimer);
    searchTimer = setTimeout(()=>{ currentPage=0; load(); }, 300);
}
function prevPage(){ if(currentPage>0){ currentPage--; load(); } }
function nextPage(){ if((currentPage+1)*pageSize<totalItems){ currentPage++; load(); } }

/* ── ADD/EDIT MODAL ── */
function openAddModal(){
    editingId = null;
    document.getElementById("modal-title").innerText = "Add Customer";
    ["f-name","f-phone","f-email","f-address"].forEach(id =>
        document.getElementById(id).value = "");
    document.getElementById("modal").classList.add("open");
}

function openEditModal(id, name, phone, email, address){
    editingId = id;
    document.getElementById("modal-title").innerText = "Edit Customer";
    document.getElementById("f-name").value    = name;
    document.getElementById("f-phone").value   = phone === "—" ? "" : phone;
    document.getElementById("f-email").value   = email === "—" ? "" : email;
    document.getElementById("f-address").value = address === "—" ? "" : address;
    document.getElementById("modal").classList.add("open");
}

function closeModal(){
    document.getElementById("modal").classList.remove("open");
}

async function saveCustomer(){
    let name = document.getElementById("f-name").value.trim();
    if(!name){ showToast("Name is required"); return; }

    let body = {
        name,
        phone:   document.getElementById("f-phone").value.trim() || null,
        email:   document.getElementById("f-email").value.trim() || null,
        address: document.getElementById("f-address").value.trim() || null,
    };

    let url    = editingId ? `/customers-mgmt/api/edit/${editingId}` : "/customers-mgmt/api/add";
    let method = editingId ? "PUT" : "POST";

    let res  = await fetch(url, {
        method, headers: {"Content-Type":"application/json"}, body: JSON.stringify(body),
    });
    let data = await res.json();
    if(data.detail){ showToast("Error: " + data.detail); return; }

    closeModal();
    showToast(editingId ? "Customer updated ✓" : "Customer added ✓");
    load();
}

async function deleteCustomer(id, name){
    if(!confirm(`Delete "${name}"? This cannot be undone.`)) return;
    let res = await fetch(`/customers-mgmt/api/delete/${id}`, {method:"DELETE"});
    let data = await res.json();
    if(data.detail){ showToast("Error: " + data.detail); return; }
    showToast("Customer deleted ✓");
    load();
}

/* ── INVOICE HISTORY ── */
async function openHistory(id, name, invCount, totalSpent){
    document.getElementById("side-name").innerText  = name;
    document.getElementById("side-inv-count").innerText = invCount;
    document.getElementById("side-inv-total").innerText = totalSpent.toFixed(2);
    document.getElementById("side-body").innerHTML  = `<div style="color:var(--muted);font-size:13px">Loading…</div>`;
    document.getElementById("side-bg").classList.add("open");
    document.getElementById("side-panel").classList.add("open");

    let invoices = await (await fetch(`/customers-mgmt/api/invoices/${id}`)).json();

    if(!invoices.length){
        document.getElementById("side-body").innerHTML =
            `<div style="color:var(--muted);font-size:13px;padding:20px 0">No invoices yet</div>`;
        return;
    }

    document.getElementById("side-body").innerHTML = invoices.map(i => `
        <a class="inv-card" href="/invoice/${i.id}" target="_blank">
            <div>
                <div class="inv-num">${i.invoice_number || "#"+i.id}</div>
                <div class="inv-date">${i.created_at}</div>
                <div class="inv-method">${i.payment_method}</div>
            </div>
            <div style="text-align:right">
                <div class="inv-total">${i.total.toFixed(2)}</div>
                <div style="font-size:11px;color:${i.status==='paid'?'var(--green)':'var(--warn)'};margin-top:4px">
                    ${i.status}
                </div>
            </div>
        </a>`).join("");
}

function closeSide(){
    document.getElementById("side-bg").classList.remove("open");
    document.getElementById("side-panel").classList.remove("open");
}

document.getElementById("modal").addEventListener("click", function(e){
    if(e.target === this) closeModal();
});

/* ── TOAST ── */
let toastTimer = null;
function showToast(msg){
    let t = document.getElementById("toast");
    t.innerText = msg; t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(()=>t.classList.remove("show"), 3000);
}

load();
</script>
</body>
</html>
"""


