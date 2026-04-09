from fastapi import APIRouter, Depends, HTTPException, Header
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, ForeignKey
from sqlalchemy.sql import func
from pydantic import BaseModel
from typing import Optional

from app.database import get_db, Base
from app.models.user import User
from app.core.security import hash_password, verify_password, decode_token
from app.core.log import ActivityLog, record as log_record

router = APIRouter(prefix="/users", tags=["Users"])


# ── Schemas ─────────────────────────────────────────────
class UserCreate(BaseModel):
    name:        str
    email:       str
    password:    str
    role:        str = "cashier"
    is_active:   bool = True
    permissions: Optional[str] = None

class UserUpdate(BaseModel):
    name:        Optional[str]  = None
    email:       Optional[str]  = None
    role:        Optional[str]  = None
    is_active:   Optional[bool] = None
    permissions: Optional[str]  = None

class ChangePasswordData(BaseModel):
    old_password: str
    new_password: str

class AdminResetPassword(BaseModel):
    new_password: str

class LogCreate(BaseModel):
    action:      str
    module:      str
    description: str
    ref_type:    Optional[str] = None
    ref_id:      Optional[str] = None


# ── Auth helpers ─────────────────────────────────────────
def _extract_user(authorization: str, db: Session):
    """Parse Bearer token and return User or None."""
    if not authorization:
        return None
    try:
        from jose import JWTError
        token = authorization.strip().split(" ")[-1]
        # decode_token raises HTTPException on bad token — catch it
        try:
            payload = decode_token(token)
        except HTTPException:
            return None
        user_id = int(payload.get("sub", 0))
        if not user_id:
            return None
        return db.query(User).filter(User.id == user_id).first()
    except Exception:
        return None

def get_user_from_token(authorization: str = Header(None), db: Session = Depends(get_db)):
    return _extract_user(authorization, db)

def require_admin(authorization: str = Header(None), db: Session = Depends(get_db)):
    user = _extract_user(authorization, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ── Activity Log API ─────────────────────────────────────
@router.post("/api/log")
def log_action(data: LogCreate, authorization: str = Header(None), db: Session = Depends(get_db)):
    user = _extract_user(authorization, db)
    entry = ActivityLog(
        user_id     = user.id   if user else None,
        user_name   = user.name if user else "Unknown",
        user_role   = user.role if user else "unknown",
        action      = data.action,
        module      = data.module,
        description = data.description,
        ref_type    = data.ref_type,
        ref_id      = data.ref_id,
    )
    db.add(entry); db.commit()
    return {"ok": True}

@router.get("/api/logs")
def get_logs(
    module:  Optional[str] = None,
    user_id: Optional[int] = None,
    limit:   int = 300,
    db:      Session = Depends(get_db),
    _=Depends(require_admin),
):
    q = db.query(ActivityLog).order_by(ActivityLog.created_at.desc())
    if module:  q = q.filter(ActivityLog.module == module)
    if user_id: q = q.filter(ActivityLog.user_id == user_id)
    logs = q.limit(limit).all()
    return [
        {
            "id":          l.id,
            "user_name":   l.user_name,
            "user_role":   l.user_role,
            "action":      l.action,
            "module":      l.module,
            "description": l.description,
            "ref_type":    l.ref_type or "",
            "ref_id":      l.ref_id   or "",
            "created_at":  l.created_at.strftime("%Y-%m-%d %H:%M:%S") if l.created_at else "—",
        }
        for l in logs
    ]


# ── User CRUD API ────────────────────────────────────────
@router.get("/api/users")
def get_users(db: Session = Depends(get_db), _=Depends(require_admin)):
    users = db.query(User).order_by(User.id).all()
    return [
        {
            "id":          u.id,
            "name":        u.name,
            "email":       u.email,
            "role":        u.role,
            "is_active":   u.is_active,
            "permissions": getattr(u, "permissions", None) or "",
            "created_at":  u.created_at.strftime("%Y-%m-%d") if u.created_at else "—",
        }
        for u in users
    ]

@router.post("/api/users")
def create_user(data: UserCreate, db: Session = Depends(get_db), admin=Depends(require_admin)):
    if db.query(User).filter(User.email == data.email).first():
        raise HTTPException(status_code=400, detail="Email already exists")
    if len(data.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    u = User(
        name=data.name, email=data.email,
        password=hash_password(data.password),
        role=data.role, is_active=data.is_active,
    )
    if hasattr(u, "permissions"):
        u.permissions = data.permissions
    db.add(u); db.commit(); db.refresh(u)
    # log
    log = ActivityLog(user_id=admin.id, user_name=admin.name, user_role=admin.role,
        action="CREATE_USER", module="USERS",
        description=f"Created user {u.name} ({u.email}) with role {u.role}",
        ref_type="user", ref_id=str(u.id))
    db.add(log); db.commit()
    return {"id": u.id, "name": u.name, "email": u.email, "role": u.role}

@router.put("/api/users/{user_id}")
def update_user(user_id: int, data: UserUpdate, db: Session = Depends(get_db), admin=Depends(require_admin)):
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    if data.name      is not None: u.name      = data.name
    if data.role      is not None: u.role      = data.role
    if data.is_active is not None: u.is_active = data.is_active
    if data.email     is not None:
        if db.query(User).filter(User.email == data.email, User.id != user_id).first():
            raise HTTPException(status_code=400, detail="Email already in use")
        u.email = data.email
    if data.permissions is not None and hasattr(u, "permissions"):
        u.permissions = data.permissions
    db.commit()
    log = ActivityLog(user_id=admin.id, user_name=admin.name, user_role=admin.role,
        action="UPDATE_USER", module="USERS",
        description=f"Updated user {u.name} — role: {u.role}, active: {u.is_active}",
        ref_type="user", ref_id=str(u.id))
    db.add(log); db.commit()
    return {"ok": True, "id": u.id, "name": u.name, "role": u.role}

@router.delete("/api/users/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db), admin=Depends(require_admin)):
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    name = u.name
    db.delete(u); db.commit()
    log = ActivityLog(user_id=admin.id, user_name=admin.name, user_role=admin.role,
        action="DELETE_USER", module="USERS",
        description=f"Deleted user {name}", ref_type="user", ref_id=str(user_id))
    db.add(log); db.commit()
    return {"ok": True}

@router.post("/api/users/{user_id}/reset-password")
def admin_reset_password(user_id: int, data: AdminResetPassword,
    db: Session = Depends(get_db), admin=Depends(require_admin)):
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    if len(data.new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    u.password = hash_password(data.new_password)
    db.commit()
    log = ActivityLog(user_id=admin.id, user_name=admin.name, user_role=admin.role,
        action="RESET_PASSWORD", module="USERS",
        description=f"Admin reset password for {u.name}",
        ref_type="user", ref_id=str(user_id))
    db.add(log); db.commit()
    return {"ok": True}

@router.post("/api/change-password")
def change_password(data: ChangePasswordData,
    authorization: str = Header(None), db: Session = Depends(get_db)):
    user = _extract_user(authorization, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not verify_password(data.old_password, user.password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if len(data.new_password) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters")
    if data.old_password == data.new_password:
        raise HTTPException(status_code=400, detail="New password must be different")
    user.password = hash_password(data.new_password)
    db.commit()
    log = ActivityLog(user_id=user.id, user_name=user.name, user_role=user.role,
        action="CHANGE_PASSWORD", module="USERS",
        description=f"{user.name} changed their own password",
        ref_type="user", ref_id=str(user.id))
    db.add(log); db.commit()
    return {"ok": True}


# ── UI ────────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
def users_ui():
    return """<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Users — Thunder ERP</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root{
    --bg:#060810;--card:#0f1424;--card2:#151c30;
    --border:rgba(255,255,255,0.06);--border2:rgba(255,255,255,0.11);
    --green:#00ff9d;--blue:#4d9fff;--purple:#a855f7;
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
nav{position:sticky;top:0;z-index:100;display:flex;align-items:center;gap:8px;padding:0 24px;height:58px;background:rgba(10,13,24,.92);backdrop-filter:blur(20px);border-bottom:1px solid var(--border);flex-wrap:wrap;}
.logo{font-size:17px;font-weight:900;text-decoration:none;display:flex;align-items:center;gap:8px;margin-right:10px;}
.logo svg polygon{fill:#f59e0b;}
.logo-txt{background:linear-gradient(135deg,var(--green),var(--blue));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;}
.nav-link{padding:7px 12px;border-radius:8px;color:var(--sub);font-size:12px;font-weight:600;text-decoration:none;transition:all .2s;white-space:nowrap;}
.nav-link:hover{background:rgba(255,255,255,.05);color:var(--text);}
.nav-link.active{background:rgba(168,85,247,.1);color:var(--purple);}
.nav-spacer{flex:1;}
.content{max-width:1200px;margin:0 auto;padding:28px 24px;display:flex;flex-direction:column;gap:20px;}
.page-title{font-size:24px;font-weight:800;letter-spacing:-.5px;}
.page-sub{color:var(--muted);font-size:13px;margin-top:3px;}
.tabs{display:flex;gap:4px;background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:4px;width:fit-content;flex-wrap:wrap;}
.tab{padding:8px 18px;border-radius:9px;font-size:13px;font-weight:700;cursor:pointer;border:none;background:transparent;color:var(--muted);transition:all .2s;font-family:var(--sans);}
.tab.active{background:var(--card2);color:var(--text);}
.section{display:none;flex-direction:column;gap:16px;}
.section.active{display:flex;}
.toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;}
.search-box{display:flex;align-items:center;gap:9px;background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:0 14px;min-width:220px;}
.search-box input{background:transparent;border:none;outline:none;color:var(--text);font-family:var(--sans);font-size:14px;padding:11px 0;width:100%;}
.search-box input::placeholder{color:var(--muted);}
.sel{background:var(--card);border:1px solid var(--border2);border-radius:var(--r);padding:10px 14px;color:var(--text);font-family:var(--sans);font-size:13px;outline:none;}
.btn{display:flex;align-items:center;gap:7px;padding:10px 16px;border-radius:var(--r);font-family:var(--sans);font-size:13px;font-weight:700;cursor:pointer;border:none;transition:all .2s;white-space:nowrap;}
.btn-purple{background:linear-gradient(135deg,var(--purple),var(--blue));color:white;}
.btn-purple:hover{filter:brightness(1.1);transform:translateY(-1px);}
.btn-green{background:linear-gradient(135deg,var(--green),#00d4ff);color:#021a10;}
.btn-green:hover{filter:brightness(1.1);transform:translateY(-1px);}
.table-wrap{background:var(--card);border:1px solid var(--border);border-radius:var(--r);overflow:hidden;}
table{width:100%;border-collapse:collapse;}
thead{background:var(--card2);}
th{text-align:left;font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);padding:12px 16px;}
td{padding:11px 16px;border-top:1px solid var(--border);color:var(--sub);font-size:13px;}
tr:hover td{background:rgba(255,255,255,.02);}
td.name{color:var(--text);font-weight:600;}
.role-badge{display:inline-flex;align-items:center;gap:5px;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:700;}
.role-admin{background:rgba(255,77,109,.15);color:#ff4d6d;}
.role-manager{background:rgba(255,181,71,.15);color:#ffb547;}
.role-cashier{background:rgba(0,255,157,.12);color:#00ff9d;}
.role-accountant{background:rgba(77,159,255,.15);color:#4d9fff;}
.role-hr{background:rgba(168,85,247,.15);color:#a855f7;}
.role-viewer{background:rgba(100,100,120,.15);color:#8899bb;}
.status-badge{display:inline-flex;padding:2px 9px;border-radius:20px;font-size:11px;font-weight:700;}
.status-active{background:rgba(0,255,157,.1);color:var(--green);}
.status-inactive{background:rgba(255,77,109,.1);color:var(--danger);}
.action-btn{background:transparent;border:1px solid var(--border2);color:var(--sub);font-size:12px;font-weight:600;padding:5px 10px;border-radius:7px;cursor:pointer;transition:all .15s;font-family:var(--sans);}
.action-btn:hover{border-color:var(--blue);color:var(--blue);}
.action-btn.danger:hover{border-color:var(--danger);color:var(--danger);}
.action-btn.warn:hover{border-color:var(--warn);color:var(--warn);}
.log-module{display:inline-flex;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700;font-family:var(--mono);}
.lm-POS{background:rgba(0,255,157,.1);color:var(--green);}
.lm-B2B{background:rgba(77,159,255,.1);color:var(--blue);}
.lm-HR{background:rgba(168,85,247,.1);color:var(--purple);}
.lm-Accounting{background:rgba(255,181,71,.1);color:var(--warn);}
.lm-ACCOUNTING{background:rgba(255,181,71,.1);color:var(--warn);}
.lm-Inventory{background:rgba(45,212,191,.1);color:var(--teal);}
.lm-INVENTORY{background:rgba(45,212,191,.1);color:var(--teal);}
.lm-Production{background:rgba(132,204,22,.1);color:var(--lime);}
.lm-PRODUCTION{background:rgba(132,204,22,.1);color:var(--lime);}
.lm-Users{background:rgba(255,77,109,.1);color:var(--danger);}
.lm-USERS{background:rgba(255,77,109,.1);color:var(--danger);}
.lm-Products{background:rgba(255,181,71,.1);color:var(--warn);}
.lm-PRODUCTS{background:rgba(255,181,71,.1);color:var(--warn);}
.lm-Suppliers{background:rgba(251,146,60,.1);color:#fb923c;}
.lm-Customers{background:rgba(236,72,153,.1);color:#ec4899;}
.lm-Farm{background:rgba(34,197,94,.1);color:#22c55e;}
.lm-Refunds{background:rgba(255,77,109,.15);color:var(--danger);}
.lm-Auth{background:rgba(99,102,241,.1);color:#818cf8;}
.modal-bg{position:fixed;inset:0;z-index:500;background:rgba(0,0,0,.75);backdrop-filter:blur(4px);display:none;align-items:center;justify-content:center;}
.modal-bg.open{display:flex;}
.modal{background:var(--card);border:1px solid var(--border2);border-radius:16px;padding:28px;width:620px;max-width:95vw;max-height:90vh;overflow-y:auto;animation:modalIn .2s ease;}
@keyframes modalIn{from{opacity:0;transform:scale(.95)}to{opacity:1;transform:scale(1)}}
.modal-title{font-size:18px;font-weight:800;margin-bottom:4px;}
.modal-sub{font-size:13px;color:var(--muted);margin-bottom:20px;}
.fld{display:flex;flex-direction:column;gap:6px;margin-bottom:14px;}
.fld label{font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);}
.fld input,.fld select{background:var(--card2);border:1px solid var(--border2);border-radius:10px;padding:10px 12px;color:var(--text);font-family:var(--sans);font-size:14px;outline:none;transition:border-color .2s;width:100%;}
.fld input:focus,.fld select:focus{border-color:rgba(168,85,247,.5);}
.fld input::placeholder{color:var(--muted);}
.role-info{font-size:12px;color:var(--muted);padding:8px 12px;background:var(--card2);border-radius:8px;border-left:3px solid var(--purple);margin-top:6px;}
.perms-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-top:6px;}
.perm-section{background:var(--card2);border:1px solid var(--border2);border-radius:10px;padding:12px 14px;}
.perm-section-title{font-size:11px;font-weight:800;letter-spacing:.5px;color:var(--sub);margin-bottom:8px;text-transform:uppercase;}
/* Page chips */
.page-chip{display:inline-flex;align-items:center;gap:6px;padding:6px 13px;border-radius:20px;border:1.5px solid var(--border2);background:var(--card2);color:var(--sub);font-size:12px;font-weight:700;cursor:pointer;transition:all .15s;user-select:none;}
.page-chip:hover{border-color:var(--purple);color:var(--purple);}
.page-chip.selected{border-color:var(--purple);background:rgba(168,85,247,.12);color:var(--purple);}
.page-chip.selected::before{content:"✓ ";}
.perm-item{display:flex;align-items:center;gap:8px;background:var(--card2);border:1px solid var(--border2);border-radius:8px;padding:8px 12px;cursor:pointer;transition:border-color .15s;}
.perm-item:hover{border-color:var(--purple);}
.perm-item input{accent-color:var(--purple);width:14px;height:14px;cursor:pointer;flex-shrink:0;}
.perm-item span{font-size:12px;color:var(--sub);}
.pwd-strength{height:3px;border-radius:2px;margin-top:5px;transition:all .3s;width:0;}
.pwd-card{max-width:440px;background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:24px;}
.modal-actions{display:flex;gap:10px;margin-top:12px;justify-content:flex-end;}
.btn-cancel{background:transparent;border:1px solid var(--border2);color:var(--sub);padding:10px 18px;border-radius:var(--r);font-family:var(--sans);font-size:13px;font-weight:700;cursor:pointer;}
.btn-cancel:hover{border-color:var(--danger);color:var(--danger);}
.toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%) translateY(16px);background:var(--card2);border:1px solid var(--border2);border-radius:var(--r);padding:12px 20px;font-size:13px;font-weight:600;color:var(--text);box-shadow:0 20px 50px rgba(0,0,0,.5);opacity:0;pointer-events:none;transition:opacity .25s,transform .25s;z-index:999;}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0);}
</style>
</head>
<body>
<nav>
    <a href="/home" class="logo">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none"><polygon points="13,2 4,14 11,14 11,22 20,10 13,10" fill="#f59e0b"/></svg>
        <span class="logo-txt">Thunder ERP</span>
    </a>
    <a href="/dashboard"   class="nav-link">Dashboard</a>
    <a href="/accounting/" class="nav-link">Accounting</a>
    <a href="/hr/"         class="nav-link">HR</a>
    <a href="/users/"      class="nav-link active">Users</a>
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
    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px">
        <div>
            <div class="page-title">👥 User Management</div>
            <div class="page-sub">Create users, assign roles, manage permissions, and track all system activity</div>
        </div>
    </div>

    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px">
        <div class="tabs">
            <button class="tab active" id="tab-users"  onclick="switchTab('users')">Users</button>
            <button class="tab"        id="tab-logs"   onclick="switchTab('logs')">Activity Log</button>
            <button class="tab"        id="tab-mypass" onclick="switchTab('mypass')">My Password</button>
        </div>
        <div id="add-btn-wrap">
            <button class="btn btn-purple" onclick="openAddModal()">+ Add User</button>
        </div>
    </div>

    <!-- USERS -->
    <div class="section active" id="section-users">
        <div class="toolbar">
            <div class="search-box">
                <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
                <input id="user-search" placeholder="Search name, email, role..." oninput="filterUsers()">
            </div>
        </div>
        <div class="table-wrap"><table>
            <thead><tr>
                <th>Name</th><th>Email</th><th>Role</th>
                <th>Permissions</th><th>Status</th><th>Created</th><th>Actions</th>
            </tr></thead>
            <tbody id="users-body">
                <tr><td colspan="7" style="text-align:center;color:var(--muted);padding:40px">Loading…</td></tr>
            </tbody>
        </table></div>
    </div>

    <!-- ACTIVITY LOG -->
    <div class="section" id="section-logs">
        <div class="toolbar">
            <div class="search-box">
                <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
                <input id="log-search" placeholder="Search logs..." oninput="filterLogs()">
            </div>
            <select class="sel" id="log-module" onchange="loadLogs()">
                <option value="">All Modules</option>
                <option value="Auth">Auth</option>
                <option value="POS">POS</option>
                <option value="Refunds">Refunds</option>
                <option value="B2B">B2B</option>
                <option value="Customers">Customers</option>
                <option value="Suppliers">Suppliers</option>
                <option value="Products">Products</option>
                <option value="Inventory">Inventory</option>
                <option value="Production">Production</option>
                <option value="Farm">Farm</option>
                <option value="HR">HR</option>
                <option value="Accounting">Accounting</option>
                <option value="Users">Users</option>
            </select>
            <select class="sel" id="log-user" onchange="loadLogs()">
                <option value="">All Users</option>
            </select>
        </div>
        <div class="table-wrap"><table>
            <thead><tr>
                <th>Date / Time</th><th>User</th><th>Role</th>
                <th>Module</th><th>Action</th><th>Description</th><th>Reference</th>
            </tr></thead>
            <tbody id="logs-body">
                <tr><td colspan="7" style="text-align:center;color:var(--muted);padding:40px">Loading…</td></tr>
            </tbody>
        </table></div>
    </div>

    <!-- MY PASSWORD -->
    <div class="section" id="section-mypass">
        <div class="pwd-card">
            <div style="font-size:16px;font-weight:800;margin-bottom:4px">Change Your Password</div>
            <div style="font-size:13px;color:var(--muted);margin-bottom:20px">You must enter your current password to set a new one.</div>
            <div class="fld"><label>Current Password *</label>
                <input id="cp-old" type="password" placeholder="Enter your current password">
            </div>
            <div class="fld"><label>New Password *</label>
                <input id="cp-new" type="password" placeholder="Minimum 6 characters" oninput="pwdStrength('cp-new','cp-bar')">
                <div class="pwd-strength" id="cp-bar"></div>
            </div>
            <div class="fld"><label>Confirm New Password *</label>
                <input id="cp-confirm" type="password" placeholder="Repeat new password">
            </div>
            <button class="btn btn-green" style="width:100%;justify-content:center;margin-top:4px" onclick="changeMyPassword()">
                🔒 Update Password
            </button>
        </div>
    </div>
</div>

<!-- ADD / EDIT MODAL -->
<div class="modal-bg" id="user-modal">
    <div class="modal">
        <div class="modal-title" id="modal-title">Add User</div>
        <div class="modal-sub"   id="modal-sub">Create a new system user</div>
        <div class="fld"><label>Full Name *</label>
            <input id="u-name" placeholder="e.g. Ahmed Hassan">
        </div>
        <div class="fld"><label>Email *</label>
            <input id="u-email" type="email" placeholder="ahmed@habiba.com">
        </div>
        <div class="fld"><label id="pass-label">Password *</label>
            <input id="u-pass" type="password" placeholder="Min 6 characters" oninput="pwdStrength('u-pass','u-bar')">
            <div class="pwd-strength" id="u-bar"></div>
        </div>
        <div class="fld"><label>Role *</label>
            <select id="u-role" onchange="updateRoleDesc()">
                <option value="cashier">Cashier</option>
                <option value="manager">Manager</option>
                <option value="accountant">Accountant</option>
                <option value="hr">HR</option>
                <option value="viewer">Viewer</option>
                <option value="admin">Admin</option>
            </select>
            <div id="role-info" class="role-info"></div>
        </div>
        <div class="fld">
            <label>Pages Access <span style="font-weight:400;text-transform:none;letter-spacing:0;font-size:10px;color:var(--muted)">(select pages first — then tabs & actions appear)</span></label>
            <!-- Step 1: Page picker chips -->
            <div id="page-chips" style="display:flex;flex-wrap:wrap;gap:7px;margin-top:8px;"></div>
            <!-- Step 2: Tabs & actions for selected pages -->
            <div id="sub-perms" style="display:flex;flex-direction:column;gap:8px;margin-top:10px;"></div>
        </div>
        <div class="fld">
            <label style="display:flex;align-items:center;gap:10px;cursor:pointer;text-transform:none;letter-spacing:0;font-size:13px;font-weight:600">
                <input type="checkbox" id="u-active" checked style="width:15px;height:15px;accent-color:var(--purple)">
                <span style="color:var(--sub)">Account is active (user can log in)</span>
            </label>
        </div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeModal()">Cancel</button>
            <button class="btn btn-purple" onclick="saveUser()">Save User</button>
        </div>
    </div>
</div>

<!-- RESET PASSWORD MODAL -->
<div class="modal-bg" id="reset-modal">
    <div class="modal" style="width:400px">
        <div class="modal-title">🔑 Reset Password</div>
        <div class="modal-sub" id="reset-sub">Reset password for user</div>
        <div class="fld"><label>New Password *</label>
            <input id="rp-new" type="password" placeholder="Min 6 characters" oninput="pwdStrength('rp-new','rp-bar')">
            <div class="pwd-strength" id="rp-bar"></div>
        </div>
        <div class="fld"><label>Confirm Password *</label>
            <input id="rp-confirm" type="password" placeholder="Repeat new password">
        </div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="document.getElementById('reset-modal').classList.remove('open')">Cancel</button>
            <button class="btn btn-purple" onclick="saveResetPassword()">Reset Password</button>
        </div>
    </div>
</div>

<div class="toast" id="toast"></div>
<script>
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
const token  = localStorage.getItem("token");
const myRole = localStorage.getItem("user_role");

// Guard — admin only
if(!token || myRole !== "admin"){
    document.body.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100vh;flex-direction:column;gap:16px;color:var(--muted);font-family:var(--sans)">
        <div style="font-size:48px">🔒</div>
        <div style="font-size:18px;font-weight:700;color:var(--text)">Admin Access Only</div>
        <div style="font-size:14px">You need administrator privileges to view this page.</div>
        <a href="/home" style="color:var(--purple);text-decoration:none;font-weight:700;margin-top:8px">← Back to Home</a>
    </div>`;
    throw new Error("Access denied");
}

const H = {"Content-Type":"application/json","Authorization":"Bearer "+token};

initializeColorMode();
setUserInfo();

let allUsers = [], allLogs = [], editingId = null, resetUserId = null;

const roleDesc = {
    cashier:    "POS terminal only. Cannot access reports, settings, or financial data.",
    manager:    "Full operations: POS, B2B, inventory, production, farm, reports. No user/accounting management.",
    accountant: "Accounting only: journal entries, P&L, trial balance, financial reports.",
    hr:         "HR module only: employees, attendance, payroll. No financial or sales data.",
    viewer:     "Read-only access to dashboard and reports. Cannot create, edit, or delete.",
    admin:      "⚠️ Full system access including user management, all reports, and all operations.",
};

// ── TABS ──────────────────────────────────────────────
function switchTab(tab){
    ["users","logs","mypass"].forEach(t=>{
        document.getElementById("section-"+t).classList.toggle("active", t===tab);
        document.getElementById("tab-"+t).classList.toggle("active", t===tab);
    });
    document.getElementById("add-btn-wrap").style.display = tab==="users"?"":"none";
    if(tab==="logs") loadLogs();
}

// ── LOAD USERS ─────────────────────────────────────────
async function loadUsers(){
    let res = await fetch("/users/api/users", {headers:H});
    if(!res.ok){ showToast("Failed to load users"); return; }
    allUsers = await res.json();
    renderUsers(allUsers);
    let sel = document.getElementById("log-user");
    sel.innerHTML = `<option value="">All Users</option>` +
        allUsers.map(u=>`<option value="${u.id}">${u.name}</option>`).join("");
}

function filterUsers(){
    let q = document.getElementById("user-search").value.toLowerCase();
    renderUsers(q ? allUsers.filter(u=>
        u.name.toLowerCase().includes(q)||
        u.email.toLowerCase().includes(q)||
        u.role.toLowerCase().includes(q)
    ) : allUsers);
}

function renderUsers(users){
    if(!users.length){
        document.getElementById("users-body").innerHTML =
            `<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:40px">No users found</td></tr>`;
        return;
    }
    document.getElementById("users-body").innerHTML = users.map(u=>`
        <tr>
            <td class="name">${u.name}</td>
            <td style="font-family:var(--mono);font-size:12px;color:var(--muted)">${u.email}</td>
            <td><span class="role-badge role-${u.role}">${u.role}</span></td>
            <td style="font-size:11px;color:var(--muted);max-width:180px">
                ${(()=>{
                    if(!u.permissions) return '<span style="color:var(--muted);font-size:12px">—</span>';
                    let ps = u.permissions.split(",").filter(Boolean);
                    let count = ps.length;
                    let preview = ps.slice(0,2).map(p=>p.replace("page_","").replace("tab_","").replace("action_","").replace(/_/g," ")).join(", ");
                    return `<span style="font-size:11px;color:var(--blue)" title="${ps.join(', ')}">${count} permission${count>1?'s':''}: ${preview}${count>2?" +more":""}</span>`;
                })()}
            </td>
            <td><span class="status-badge status-${u.is_active?"active":"inactive"}">${u.is_active?"✓ Active":"✗ Inactive"}</span></td>
            <td style="font-size:12px;color:var(--muted)">${u.created_at}</td>
            <td style="display:flex;gap:5px;flex-wrap:wrap">
                <button class="action-btn" onclick="openEdit(${u.id})">Edit</button>
                <button class="action-btn warn" onclick="openResetModal(${u.id},'${u.name.replace(/'/g,"\\'")}')">Reset Pwd</button>
                <button class="action-btn danger" onclick="deleteUser(${u.id},'${u.name.replace(/'/g,"\\'")}')">Delete</button>
            </td>
        </tr>`).join("");
}

// ── PERMISSIONS TREE ───────────────────────────────────
const PAGE_TREE = [
    { page: "page_dashboard",  icon: "📊", label: "Dashboard",   children: [] },
    { page: "page_reports",    icon: "📈", label: "Reports",     children: [
        {value:"tab_reports_sales",      label:"Sales tab"},
        {value:"tab_reports_pl",         label:"P&L tab"},
        {value:"tab_reports_inventory",  label:"Inventory tab"},
        {value:"tab_reports_transactions",label:"Transactions tab"},
        {value:"action_export_excel",    label:"Export to Excel"},
    ]},
    { page: "page_pos",        icon: "🛒", label: "POS",         children: [
        {value:"action_pos_delete_invoice", label:"Delete invoices"},
        {value:"action_pos_discount",        label:"Apply discounts"},
        {value:"action_pos_settle_later",    label:"Settle later"},
    ]},
    { page: "page_b2b",        icon: "🤝", label: "B2B",         children: [
        {value:"tab_b2b_clients",      label:"Clients tab"},
        {value:"tab_b2b_invoices",     label:"Invoices tab"},
        {value:"tab_b2b_consignment",  label:"Consignment tab"},
        {value:"action_b2b_delete",    label:"Delete invoices"},
        {value:"action_b2b_collect",   label:"Collect payments"},
    ]},
    { page: "page_inventory",  icon: "📦", label: "Inventory",   children: [
        {value:"action_inventory_adjust", label:"Adjust stock"},
    ]},
    { page: "page_products",   icon: "🏷", label: "Products",    children: [
        {value:"action_products_edit",   label:"Edit products"},
        {value:"action_products_delete", label:"Delete products"},
        {value:"page_import",            label:"Import data"},
    ]},
    { page: "page_production", icon: "⚗️", label: "Production",  children: [
        {value:"tab_production_batches",   label:"Batches tab"},
        {value:"tab_production_packaging", label:"Packaging tab"},
        {value:"tab_production_spoilage",  label:"Spoilage tab"},
        {value:"tab_production_recipes",   label:"Recipes tab"},
    ]},
    { page: "page_farm",       icon: "🌾", label: "Farm Intake", children: [] },
    { page: "page_hr",         icon: "👥", label: "HR & Payroll",children: [
        {value:"tab_hr_employees",     label:"Employees tab"},
        {value:"tab_hr_attendance",    label:"Attendance tab"},
        {value:"tab_hr_payroll",       label:"Payroll tab"},
        {value:"action_hr_run_payroll",label:"Run payroll"},
        {value:"action_hr_mark_paid",  label:"Mark payroll paid"},
    ]},
    { page: "page_accounting", icon: "📒", label: "Accounting",  children: [
        {value:"tab_accounting_pos",       label:"POS invoices tab"},
        {value:"tab_accounting_b2b",       label:"B2B invoices tab"},
        {value:"tab_accounting_journal",   label:"Journal tab"},
        {value:"tab_accounting_pl",        label:"P&L tab"},
        {value:"action_accounting_post_journal", label:"Post journal entries"},
    ]},
    { page: "page_customers",  icon: "👤", label: "Customers",   children: [] },
    { page: "page_suppliers",  icon: "🏭", label: "Suppliers",   children: [] },
];

let selectedPages = new Set();
let selectedSubs  = new Set();

function renderPageChips(){
    let container = document.getElementById("page-chips");
    container.innerHTML = PAGE_TREE.map(p=>`
        <div class="page-chip ${selectedPages.has(p.page)?'selected':''}"
             onclick="togglePage('${p.page}')">
            ${p.icon} ${p.label}
        </div>`).join("");
    renderSubPerms();
}

function togglePage(pageKey){
    if(selectedPages.has(pageKey)){
        selectedPages.delete(pageKey);
        // remove any sub-permissions that belong to this page
        let pg = PAGE_TREE.find(p=>p.page===pageKey);
        if(pg) pg.children.forEach(c=>selectedSubs.delete(c.value));
    } else {
        selectedPages.add(pageKey);
    }
    renderPageChips();
}

function renderSubPerms(){
    let container = document.getElementById("sub-perms");
    let html = "";
    PAGE_TREE.forEach(pg=>{
        if(!selectedPages.has(pg.page) || pg.children.length===0) return;
        html += `<div class="perm-section">
            <div class="perm-section-title">${pg.icon} ${pg.label} — Tabs & Actions</div>
            <div class="perms-grid">
                ${pg.children.map(c=>`
                    <label class="perm-item">
                        <input type="checkbox" value="${c.value}"
                            ${selectedSubs.has(c.value)?'checked':''}
                            onchange="toggleSub('${c.value}',this.checked)">
                        <span>${c.label}</span>
                    </label>`).join("")}
            </div>
        </div>`;
    });
    container.innerHTML = html || (selectedPages.size>0
        ? '<div style="font-size:12px;color:var(--muted);padding:6px 0">No extra tabs/actions for the selected pages.</div>'
        : '');
}

function toggleSub(val, checked){
    if(checked) selectedSubs.add(val);
    else selectedSubs.delete(val);
}

function setPermsFromString(str){
    selectedPages.clear(); selectedSubs.clear();
    if(!str) { renderPageChips(); return; }
    let all = str.split(",").map(s=>s.trim()).filter(Boolean);
    all.forEach(v=>{
        if(PAGE_TREE.find(p=>p.page===v)) selectedPages.add(v);
        else selectedSubs.add(v);
    });
    renderPageChips();
}

function getPermsString(){
    return [...selectedPages, ...selectedSubs].join(",");
}

// ── MODAL ──────────────────────────────────────────────
function openAddModal(){
    editingId = null;
    document.getElementById("modal-title").innerText = "Add User";
    document.getElementById("modal-sub").innerText   = "Create a new system user";
    document.getElementById("pass-label").innerText  = "Password *";
    ["u-name","u-email","u-pass"].forEach(id=>document.getElementById(id).value="");
    document.getElementById("u-role").value  = "cashier";
    document.getElementById("u-active").checked = true;
    document.getElementById("u-bar").style.cssText = "width:0;background:var(--border2)";
    setPermsFromString("");
    updateRoleDesc();
    document.getElementById("user-modal").classList.add("open");
    setTimeout(()=>document.getElementById("u-name").focus(),100);
}

function openEdit(id){
    let u = allUsers.find(x=>x.id===id);
    if(!u) return;
    editingId = id;
    document.getElementById("modal-title").innerText = "Edit User";
    document.getElementById("modal-sub").innerText   = `Editing: ${u.name}`;
    document.getElementById("pass-label").innerText  = "New Password (leave blank to keep current)";
    document.getElementById("u-name").value  = u.name;
    document.getElementById("u-email").value = u.email;
    document.getElementById("u-pass").value  = "";
    document.getElementById("u-role").value  = u.role;
    document.getElementById("u-active").checked = u.is_active;
    document.getElementById("u-bar").style.cssText = "width:0;background:var(--border2)";
    setPermsFromString(u.permissions||"");
    updateRoleDesc();
    document.getElementById("user-modal").classList.add("open");
}

function closeModal(){ document.getElementById("user-modal").classList.remove("open"); }

function updateRoleDesc(){
    let r = document.getElementById("u-role").value;
    document.getElementById("role-info").innerText = roleDesc[r]||"";
}

function pwdStrength(inputId, barId){
    let v = document.getElementById(inputId).value;
    let s = 0;
    if(v.length>=6)  s++;
    if(v.length>=10) s++;
    if(/[A-Z]/.test(v)) s++;
    if(/[0-9]/.test(v)) s++;
    if(/[^A-Za-z0-9]/.test(v)) s++;
    let c = ["","#ff4d6d","#ffb547","#ffb547","#00ff9d","#00ff9d"][s];
    let b = document.getElementById(barId);
    b.style.background = c||"var(--border2)";
    b.style.width = (s*20)+"%";
}

async function saveUser(){
    let name  = document.getElementById("u-name").value.trim();
    let email = document.getElementById("u-email").value.trim();
    let pass  = document.getElementById("u-pass").value;
    let role  = document.getElementById("u-role").value;
    let active = document.getElementById("u-active").checked;
    let perms = getPermsString();

    if(!name)  { showToast("Name is required"); return; }
    if(!email) { showToast("Email is required"); return; }
    if(!editingId && !pass){ showToast("Password is required for new users"); return; }
    if(pass && pass.length < 6){ showToast("Password must be at least 6 characters"); return; }

    let body = {name, email, role, is_active:active, permissions:perms};
    if(pass) body.password = pass;

    let url    = editingId ? `/users/api/users/${editingId}` : "/users/api/users";
    let method = editingId ? "PUT" : "POST";
    let res    = await fetch(url, {method, headers:H, body:JSON.stringify(body)});
    let data   = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    closeModal();
    showToast(editingId ? `✓ ${data.name} updated` : `✓ ${data.name} created — role: ${data.role}`);
    loadUsers();
}

async function deleteUser(id, name){
    if(!confirm(`Delete user "${name}"?\n\nThis cannot be undone.`)) return;
    let res  = await fetch(`/users/api/users/${id}`, {method:"DELETE", headers:H});
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    showToast(`${name} deleted`);
    loadUsers();
}

// ── RESET PASSWORD ─────────────────────────────────────
function openResetModal(id, name){
    resetUserId = id;
    document.getElementById("reset-sub").innerText = `Reset password for: ${name}`;
    document.getElementById("rp-new").value     = "";
    document.getElementById("rp-confirm").value = "";
    document.getElementById("rp-bar").style.cssText = "width:0;background:var(--border2)";
    document.getElementById("reset-modal").classList.add("open");
    setTimeout(()=>document.getElementById("rp-new").focus(),100);
}

async function saveResetPassword(){
    let np  = document.getElementById("rp-new").value;
    let cnf = document.getElementById("rp-confirm").value;
    if(!np || np.length<6){ showToast("Password must be at least 6 characters"); return; }
    if(np !== cnf){ showToast("Passwords do not match"); return; }
    let res  = await fetch(`/users/api/users/${resetUserId}/reset-password`, {
        method:"POST", headers:H, body:JSON.stringify({new_password:np}),
    });
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    document.getElementById("reset-modal").classList.remove("open");
    showToast("✓ Password reset successfully");
}

// ── CHANGE MY PASSWORD ─────────────────────────────────
async function changeMyPassword(){
    let old = document.getElementById("cp-old").value;
    let np  = document.getElementById("cp-new").value;
    let cnf = document.getElementById("cp-confirm").value;
    if(!old){ showToast("Enter your current password"); return; }
    if(!np || np.length<6){ showToast("New password must be at least 6 characters"); return; }
    if(np !== cnf){ showToast("Passwords do not match"); return; }
    let res  = await fetch("/users/api/change-password", {
        method:"POST", headers:H,
        body:JSON.stringify({old_password:old, new_password:np}),
    });
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    ["cp-old","cp-new","cp-confirm"].forEach(id=>document.getElementById(id).value="");
    document.getElementById("cp-bar").style.cssText="width:0;background:var(--border2)";
    showToast("✓ Password changed. Please log in again next time with your new password.");
}

// ── ACTIVITY LOGS ──────────────────────────────────────
async function loadLogs(){
    let module = document.getElementById("log-module").value;
    let uid    = document.getElementById("log-user").value;
    let url    = "/users/api/logs?limit=500";
    if(module) url += `&module=${module}`;
    if(uid)    url += `&user_id=${uid}`;
    let res = await fetch(url, {headers:H});
    if(!res.ok){ showToast("Failed to load logs"); return; }
    allLogs = await res.json();
    filterLogs();
}

function filterLogs(){
    let q = document.getElementById("log-search").value.toLowerCase();
    let filtered = q ? allLogs.filter(l=>
        l.user_name.toLowerCase().includes(q)||
        l.action.toLowerCase().includes(q)||
        l.description.toLowerCase().includes(q)||
        l.module.toLowerCase().includes(q)
    ) : allLogs;
    if(!filtered.length){
        document.getElementById("logs-body").innerHTML =
            `<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:40px">No activity logs found</td></tr>`;
        return;
    }
    document.getElementById("logs-body").innerHTML = filtered.map(l=>`
        <tr>
            <td style="font-family:var(--mono);font-size:11px;color:var(--muted);white-space:nowrap">${l.created_at}</td>
            <td class="name" style="white-space:nowrap;font-size:13px">${l.user_name}</td>
            <td><span class="role-badge role-${l.user_role}" style="font-size:10px">${l.user_role}</span></td>
            <td><span class="log-module lm-${l.module}">${l.module}</span></td>
            <td style="font-size:12px;color:var(--text);font-weight:600;white-space:nowrap">${l.action.replace(/_/g," ")}</td>
            <td style="font-size:12px;color:var(--sub)">${l.description}</td>
            <td style="font-family:var(--mono);font-size:11px;color:var(--muted);white-space:nowrap">${l.ref_id?l.ref_type+" "+l.ref_id:""}</td>
        </tr>`).join("");
}

["user-modal","reset-modal"].forEach(id=>{
    document.getElementById(id).addEventListener("click",function(e){if(e.target===this)this.classList.remove("open");});
});

let toastT=null;
function showToast(msg){
    let t=document.getElementById("toast");
    t.innerText=msg;t.classList.add("show");
    clearTimeout(toastT);toastT=setTimeout(()=>t.classList.remove("show"),4000);
}

loadUsers();
</script>
</body>
</html>"""