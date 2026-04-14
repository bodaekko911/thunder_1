from typing import Optional
from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import or_, select, func

from app.core.log import ActivityLog
from app.core.permissions import require_admin
from app.database import get_async_session

router = APIRouter(prefix="/audit-log", tags=["Audit Log"])


# ── Data API ──────────────────────────────────────────────────────────────────

@router.get("/data")
async def audit_log_data(
    module:     Optional[str] = None,
    action:     Optional[str] = None,
    user_name:  Optional[str] = None,
    date_from:  Optional[str] = None,
    date_to:    Optional[str] = None,
    search:     Optional[str] = None,
    page:       int = Query(1, ge=1),
    page_size:  int = Query(50, ge=1, le=500),
    db:         AsyncSession = Depends(get_async_session),
    _=Depends(require_admin),
):
    conditions = []
    if module:    conditions.append(ActivityLog.module    == module)
    if action:    conditions.append(ActivityLog.action    == action)
    if user_name: conditions.append(ActivityLog.user_name == user_name)
    if date_from: conditions.append(ActivityLog.created_at >= date_from)
    if date_to:   conditions.append(ActivityLog.created_at <= date_to + " 23:59:59")
    if search:
        like = f"%{search}%"
        conditions.append(or_(
            ActivityLog.description.ilike(like),
            ActivityLog.user_name.ilike(like),
            ActivityLog.ref_id.ilike(like),
        ))

    cnt_result = await db.execute(
        select(func.count()).select_from(ActivityLog).where(*conditions)
    )
    total = cnt_result.scalar()

    result = await db.execute(
        select(ActivityLog).where(*conditions)
        .order_by(ActivityLog.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = result.scalars().all()

    return {
        "total": total,
        "page":  page,
        "pages": max(1, -(-total // page_size)),
        "rows": [
            {
                "id":          r.id,
                "user_name":   r.user_name,
                "user_role":   r.user_role,
                "action":      r.action,
                "module":      r.module,
                "description": r.description,
                "ref_type":    r.ref_type  or "",
                "ref_id":      r.ref_id    or "",
                "created_at":  r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "—",
            }
            for r in rows
        ],
    }


@router.get("/meta")
async def audit_log_meta(db: AsyncSession = Depends(get_async_session), _=Depends(require_admin)):
    """Return distinct values for filter dropdowns."""
    res_mod  = await db.execute(select(ActivityLog.module).distinct().order_by(ActivityLog.module))
    res_act  = await db.execute(select(ActivityLog.action).distinct().order_by(ActivityLog.action))
    res_usr  = await db.execute(select(ActivityLog.user_name).distinct().order_by(ActivityLog.user_name))
    modules  = [r[0] for r in res_mod.all() if r[0]]
    actions  = [r[0] for r in res_act.all() if r[0]]
    users    = [r[0] for r in res_usr.all() if r[0]]
    return {"modules": modules, "actions": actions, "users": users}


# ── HTML UI ───────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def audit_log_ui():
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Audit Log — Thunder ERP</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,600;1,300;1,400&family=DM+Sans:wght@300;400;500;600&family=DM+Mono:wght@400;500&family=Outfit:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
<style>
:root {
    --bg:      #08090c;
    --card:    #0d1008;
    --card2:   #111408;
    --border:  rgba(255,255,255,0.055);
    --border2: rgba(255,255,255,0.10);
    --green:   #7ecb6f;
    --green2:  #a8d97a;
    --amber:   #d4a256;
    --teal:    #5bbfb5;
    --rose:    #c97a7a;
    --blue:    #6a9fd4;
    --purple:  #9a7ecb;
    --text:    #e8eae0;
    --sub:     #8a9080;
    --muted:   #4a5040;
    --serif:   'Cormorant Garamond', serif;
    --sans:    'DM Sans', sans-serif;
    --mono:    'DM Mono', monospace;
}
body.light {
    --bg:      #f4f5ef; --card:   #eceee6; --card2:  #e4e6de;
    --border:  rgba(0,0,0,0.07); --border2: rgba(0,0,0,0.12);
    --green:   #0f8a43;
    --text:    #1a1e14; --sub:    #4a5040; --muted:  #8a9080;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:var(--sans);background:var(--bg);color:var(--text);min-height:100vh}

/* ── Topbar ── */
.topbar{
    position:sticky;top:0;z-index:100;
    display:flex;align-items:center;justify-content:space-between;gap:10px;
    padding:0 24px;height:58px;
    border-bottom:1px solid var(--border);
    background:rgba(10,13,24,.92);backdrop-filter:blur(20px);
}
.logo{font-family:'Outfit',sans-serif;font-size:17px;font-weight:900;text-decoration:none;display:flex;align-items:center;gap:8px}
.logo-text{background:linear-gradient(135deg,var(--green),var(--blue));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.topbar-right{display:flex;align-items:center;gap:10px}
.mode-btn{background:var(--card);border:1px solid var(--border);color:var(--sub);width:36px;height:36px;border-radius:10px;font-size:16px;cursor:pointer;transition:all .2s;display:flex;align-items:center;justify-content:center}
.mode-btn:hover{border-color:var(--border2);transform:scale(1.08)}
.back-btn{background:var(--card);border:1px solid var(--border);color:var(--sub);font-family:var(--sans);font-size:12px;font-weight:500;padding:8px 14px;border-radius:8px;cursor:pointer;transition:all .2s;text-decoration:none;display:flex;align-items:center;gap:6px}
.back-btn:hover{border-color:var(--border2);color:var(--text)}

/* ── Page header ── */
.page-header{padding:32px 32px 20px;display:flex;align-items:flex-end;justify-content:space-between;flex-wrap:wrap;gap:16px}
.page-title{font-family:var(--serif);font-size:38px;font-weight:300;letter-spacing:-.5px;line-height:1.1}
.page-title em{font-style:italic;color:var(--purple)}
.page-sub{font-size:13px;color:var(--muted);margin-top:4px}
.stat-chips{display:flex;gap:10px;flex-wrap:wrap}
.stat-chip{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:8px 16px;display:flex;flex-direction:column;gap:2px;min-width:90px}
.chip-val{font-family:var(--mono);font-size:20px;font-weight:500;color:var(--text)}
.chip-lbl{font-size:10px;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted)}

/* ── Filters ── */
.filters-bar{
    display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;
    padding:0 32px 20px;
}
.filter-group{display:flex;flex-direction:column;gap:4px;min-width:140px}
.filter-group.wide{min-width:220px}
.filter-label{font-size:10px;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted)}
.filter-input,.filter-select{
    background:var(--card);border:1px solid var(--border);
    color:var(--text);font-family:var(--sans);font-size:13px;
    padding:8px 12px;border-radius:8px;outline:none;transition:border-color .2s;
    height:36px;
}
.filter-input::placeholder{color:var(--muted)}
.filter-input:focus,.filter-select:focus{border-color:var(--border2)}
.filter-select option{background:#1a1e14}
.btn{
    height:36px;padding:0 16px;border-radius:8px;border:1px solid var(--border);
    font-family:var(--sans);font-size:13px;font-weight:500;cursor:pointer;transition:all .2s;
    display:flex;align-items:center;gap:6px;white-space:nowrap;
}
.btn-primary{background:color-mix(in srgb,var(--purple) 15%,transparent);border-color:color-mix(in srgb,var(--purple) 30%,transparent);color:var(--purple)}
.btn-primary:hover{background:color-mix(in srgb,var(--purple) 25%,transparent);border-color:var(--purple)}
.btn-ghost{background:var(--card);color:var(--sub)}
.btn-ghost:hover{color:var(--text);border-color:var(--border2)}

/* ── Table ── */
.table-wrap{padding:0 32px 40px;overflow-x:auto}
.table-inner{min-width:900px}
table{width:100%;border-collapse:collapse}
thead th{
    font-size:10px;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);
    padding:10px 14px;text-align:left;border-bottom:1px solid var(--border);
    white-space:nowrap;font-weight:500;
}
tbody tr{border-bottom:1px solid var(--border);transition:background .15s}
tbody tr:hover{background:rgba(255,255,255,.025)}
tbody td{padding:12px 14px;font-size:13px;vertical-align:top}
.td-time{font-family:var(--mono);font-size:11px;color:var(--sub);white-space:nowrap}
.td-user{font-weight:500}
.td-desc{color:var(--sub);font-size:12px;max-width:380px;line-height:1.5}
.td-ref{font-family:var(--mono);font-size:11px;color:var(--muted)}

/* ── Badges ── */
.badge{display:inline-block;font-size:10px;font-weight:600;letter-spacing:.8px;text-transform:uppercase;padding:3px 8px;border-radius:6px}
/* module colours */
.m-pos       {background:color-mix(in srgb,#7ecb6f 12%,transparent);color:#7ecb6f;border:1px solid color-mix(in srgb,#7ecb6f 25%,transparent)}
.m-b2b       {background:color-mix(in srgb,#6ab5d4 12%,transparent);color:#6ab5d4;border:1px solid color-mix(in srgb,#6ab5d4 25%,transparent)}
.m-inventory {background:color-mix(in srgb,#5bbfb5 12%,transparent);color:#5bbfb5;border:1px solid color-mix(in srgb,#5bbfb5 25%,transparent)}
.m-production{background:color-mix(in srgb,#d4a256 12%,transparent);color:#d4a256;border:1px solid color-mix(in srgb,#d4a256 25%,transparent)}
.m-farm      {background:color-mix(in srgb,#a8d97a 12%,transparent);color:#a8d97a;border:1px solid color-mix(in srgb,#a8d97a 25%,transparent)}
.m-hr        {background:color-mix(in srgb,#9a7ecb 12%,transparent);color:#9a7ecb;border:1px solid color-mix(in srgb,#9a7ecb 25%,transparent)}
.m-accounting{background:color-mix(in srgb,#d4a256 12%,transparent);color:#d4a256;border:1px solid color-mix(in srgb,#d4a256 25%,transparent)}
.m-users     {background:color-mix(in srgb,#c97a7a 12%,transparent);color:#c97a7a;border:1px solid color-mix(in srgb,#c97a7a 25%,transparent)}
.m-expenses  {background:color-mix(in srgb,#e8c07a 12%,transparent);color:#e8c07a;border:1px solid color-mix(in srgb,#e8c07a 25%,transparent)}
.m-default   {background:color-mix(in srgb,#8a9080 12%,transparent);color:#8a9080;border:1px solid color-mix(in srgb,#8a9080 25%,transparent)}

/* action colours */
.a-create{background:color-mix(in srgb,#7ecb6f 12%,transparent);color:#7ecb6f;border:1px solid color-mix(in srgb,#7ecb6f 20%,transparent)}
.a-update{background:color-mix(in srgb,#6a9fd4 12%,transparent);color:#6a9fd4;border:1px solid color-mix(in srgb,#6a9fd4 20%,transparent)}
.a-delete{background:color-mix(in srgb,#c97a7a 12%,transparent);color:#c97a7a;border:1px solid color-mix(in srgb,#c97a7a 20%,transparent)}
.a-login {background:color-mix(in srgb,#9a7ecb 12%,transparent);color:#9a7ecb;border:1px solid color-mix(in srgb,#9a7ecb 20%,transparent)}
.a-export{background:color-mix(in srgb,#5bbfb5 12%,transparent);color:#5bbfb5;border:1px solid color-mix(in srgb,#5bbfb5 20%,transparent)}
.a-default{background:color-mix(in srgb,#8a9080 12%,transparent);color:#8a9080;border:1px solid color-mix(in srgb,#8a9080 20%,transparent)}

/* role badge */
.role-admin{color:var(--rose);font-size:11px;font-weight:600}
.role-other{color:var(--muted);font-size:11px}

/* ── Pagination ── */
.pagination{display:flex;align-items:center;gap:8px;padding:0 32px 32px;flex-wrap:wrap}
.page-info{font-size:12px;color:var(--muted);font-family:var(--mono)}
.page-btn{height:32px;min-width:32px;padding:0 10px;border-radius:7px;border:1px solid var(--border);background:var(--card);color:var(--sub);font-size:12px;cursor:pointer;transition:all .15s}
.page-btn:hover:not(:disabled){border-color:var(--border2);color:var(--text)}
.page-btn:disabled{opacity:.35;cursor:not-allowed}
.page-btn.active{background:color-mix(in srgb,var(--purple) 20%,transparent);border-color:var(--purple);color:var(--purple)}

/* ── Empty / loading ── */
.empty{padding:60px 32px;text-align:center;color:var(--muted)}
.empty-icon{font-size:40px;margin-bottom:12px}
.empty-txt{font-size:14px}
.loading-row td{text-align:center;padding:40px;color:var(--muted);font-size:13px}

@media(max-width:700px){
    .page-header{padding:20px 16px 12px}
    .filters-bar,.table-wrap,.pagination{padding-left:16px;padding-right:16px}
    .topbar{padding:0 16px}
}
</style>
</head>
<body>

<header class="topbar">
    <a href="/home" class="logo">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
            <polygon points="13,2 4,14 11,14 11,22 20,10 13,10" fill="#f59e0b"/>
        </svg>
        <span class="logo-text">Thunder ERP</span>
    </a>
    <div class="topbar-right">
        <button class="mode-btn" id="mode-btn" onclick="toggleMode()">&#127769;</button>
        <a href="/home" class="back-btn">&#8592; Home</a>
    </div>
</header>

<div class="page-header">
    <div>
        <div class="page-title">Audit <em>Log</em></div>
        <div class="page-sub">Full history of every action taken across the system</div>
    </div>
    <div class="stat-chips">
        <div class="stat-chip"><span class="chip-val" id="stat-total">—</span><span class="chip-lbl">Total entries</span></div>
        <div class="stat-chip"><span class="chip-val" id="stat-pages">—</span><span class="chip-lbl">Pages</span></div>
    </div>
</div>

<div class="filters-bar">
    <div class="filter-group wide">
        <span class="filter-label">Search</span>
        <input class="filter-input" id="f-search" type="text" placeholder="Description, user, ref ID…" oninput="debounceLoad()">
    </div>
    <div class="filter-group">
        <span class="filter-label">Module</span>
        <select class="filter-select" id="f-module" onchange="loadPage(1)">
            <option value="">All modules</option>
        </select>
    </div>
    <div class="filter-group">
        <span class="filter-label">Action</span>
        <select class="filter-select" id="f-action" onchange="loadPage(1)">
            <option value="">All actions</option>
        </select>
    </div>
    <div class="filter-group">
        <span class="filter-label">User</span>
        <select class="filter-select" id="f-user" onchange="loadPage(1)">
            <option value="">All users</option>
        </select>
    </div>
    <div class="filter-group">
        <span class="filter-label">From</span>
        <input class="filter-input" id="f-from" type="date" onchange="loadPage(1)">
    </div>
    <div class="filter-group">
        <span class="filter-label">To</span>
        <input class="filter-input" id="f-to" type="date" onchange="loadPage(1)">
    </div>
    <button class="btn btn-ghost" onclick="clearFilters()">&#10005; Clear</button>
    <button class="btn btn-primary" onclick="loadPage(1)">&#128269; Search</button>
</div>

<div class="table-wrap">
    <div class="table-inner">
        <table>
            <thead>
                <tr>
                    <th>Time</th>
                    <th>User</th>
                    <th>Module</th>
                    <th>Action</th>
                    <th>Description</th>
                    <th>Ref</th>
                </tr>
            </thead>
            <tbody id="log-tbody">
                <tr class="loading-row"><td colspan="6">Loading…</td></tr>
            </tbody>
        </table>
    </div>
</div>

<div class="pagination" id="pagination"></div>

<script>
// Auth guard: redirect to login if the readable session cookie is absent
function _hasAuthCookie() {
    return document.cookie.split(";").some(c => c.trim().startsWith("logged_in="));
}
if (!_hasAuthCookie()) { window.location.href = "/"; }

// ── colour mode ──
if (localStorage.getItem("colorMode") === "light") {
    document.body.classList.add("light");
    document.getElementById("mode-btn").innerHTML = "&#9728;&#65039;";
}
function toggleMode(){
    const isLight = document.body.classList.toggle("light");
    document.getElementById("mode-btn").innerHTML = isLight ? "&#9728;&#65039;" : "&#127769;";
    localStorage.setItem("colorMode", isLight ? "light" : "dark");
}

// ── state ──
let currentPage = 1;
const PAGE_SIZE = 50;
let debounceTimer;

// ── init ──
async function init(){
    await loadMeta();
    loadPage(1);
}

async function loadMeta(){
    try {
        const r = await fetch("/audit-log/meta");
        if (!r.ok) return;
        const d = await r.json();
        fillSelect("f-module", d.modules);
        fillSelect("f-action", d.actions);
        fillSelect("f-user",   d.users);
    } catch(e) {}
}

function fillSelect(id, items){
    const sel = document.getElementById(id);
    items.forEach(v => {
        const o = document.createElement("option");
        o.value = v; o.textContent = v;
        sel.appendChild(o);
    });
}

function debounceLoad(){
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => loadPage(1), 400);
}

function clearFilters(){
    ["f-search","f-from","f-to"].forEach(id => document.getElementById(id).value = "");
    ["f-module","f-action","f-user"].forEach(id => document.getElementById(id).value = "");
    loadPage(1);
}

async function loadPage(page){
    currentPage = page;
    document.getElementById("log-tbody").innerHTML = '<tr class="loading-row"><td colspan="6">Loading…</td></tr>';

    const params = new URLSearchParams({
        page,
        page_size: PAGE_SIZE,
    });
    const search = document.getElementById("f-search").value.trim();
    const module = document.getElementById("f-module").value;
    const action = document.getElementById("f-action").value;
    const user   = document.getElementById("f-user").value;
    const from   = document.getElementById("f-from").value;
    const to     = document.getElementById("f-to").value;

    if (search) params.set("search",    search);
    if (module) params.set("module",    module);
    if (action) params.set("action",    action);
    if (user)   params.set("user_name", user);
    if (from)   params.set("date_from", from);
    if (to)     params.set("date_to",   to);

    try {
        const r = await fetch("/audit-log/data?" + params.toString());
        if (r.status === 401 || r.status === 403) {
            document.getElementById("log-tbody").innerHTML =
                '<tr class="loading-row"><td colspan="6">Access denied — admin only.</td></tr>';
            return;
        }
        const d = await r.json();
        renderTable(d);
        renderPagination(d);
        document.getElementById("stat-total").textContent = d.total.toLocaleString();
        document.getElementById("stat-pages").textContent = d.pages;
    } catch(e) {
        document.getElementById("log-tbody").innerHTML =
            '<tr class="loading-row"><td colspan="6">Error loading data.</td></tr>';
    }
}

// ── module badge class ──
function modClass(m){
    const map = {
        pos:"m-pos", b2b:"m-b2b", inventory:"m-inventory",
        production:"m-production", farm:"m-farm", hr:"m-hr",
        accounting:"m-accounting", users:"m-users", expenses:"m-expenses",
    };
    return map[(m||"").toLowerCase()] || "m-default";
}

// ── action badge class ──
function actClass(a){
    const s = (a||"").toLowerCase();
    if (s.includes("create") || s.includes("add") || s.includes("new")) return "a-create";
    if (s.includes("update") || s.includes("edit") || s.includes("change")) return "a-update";
    if (s.includes("delete") || s.includes("remove") || s.includes("void")) return "a-delete";
    if (s.includes("login")  || s.includes("auth"))   return "a-login";
    if (s.includes("export") || s.includes("print"))  return "a-export";
    return "a-default";
}

function renderTable(d){
    const tbody = document.getElementById("log-tbody");
    if (!d.rows.length){
        tbody.innerHTML = `<tr><td colspan="6">
            <div class="empty">
                <div class="empty-icon">&#128220;</div>
                <div class="empty-txt">No log entries match your filters</div>
            </div>
        </td></tr>`;
        return;
    }

    tbody.innerHTML = d.rows.map(r => {
        const roleClass = r.user_role === "admin" ? "role-admin" : "role-other";
        const ref = r.ref_type ? `<span class="td-ref">${esc(r.ref_type)} #${esc(r.ref_id)}</span>` : `<span class="td-ref" style="color:var(--muted)">—</span>`;
        return `<tr>
            <td class="td-time">${esc(r.created_at)}</td>
            <td class="td-user">
                ${esc(r.user_name)}<br>
                <span class="${roleClass}">${esc(r.user_role)}</span>
            </td>
            <td><span class="badge ${modClass(r.module)}">${esc(r.module)}</span></td>
            <td><span class="badge ${actClass(r.action)}">${esc(r.action)}</span></td>
            <td class="td-desc">${esc(r.description)}</td>
            <td>${ref}</td>
        </tr>`;
    }).join("");
}

function renderPagination(d){
    const el = document.getElementById("pagination");
    if (d.pages <= 1){ el.innerHTML = ""; return; }

    let html = `<span class="page-info">Page ${d.page} of ${d.pages} &nbsp;·&nbsp; ${d.total.toLocaleString()} entries</span>`;

    html += `<button class="page-btn" onclick="loadPage(1)" ${d.page===1?"disabled":""}>&#171;</button>`;
    html += `<button class="page-btn" onclick="loadPage(${d.page-1})" ${d.page===1?"disabled":""}>&#8249;</button>`;

    // window of pages
    const lo = Math.max(1, d.page - 3);
    const hi = Math.min(d.pages, d.page + 3);
    if (lo > 1) html += `<span class="page-info">…</span>`;
    for (let p = lo; p <= hi; p++){
        html += `<button class="page-btn${p===d.page?" active":""}" onclick="loadPage(${p})">${p}</button>`;
    }
    if (hi < d.pages) html += `<span class="page-info">…</span>`;

    html += `<button class="page-btn" onclick="loadPage(${d.page+1})" ${d.page===d.pages?"disabled":""}>&#8250;</button>`;
    html += `<button class="page-btn" onclick="loadPage(${d.pages})" ${d.page===d.pages?"disabled":""}>&#187;</button>`;

    el.innerHTML = html;
}

function esc(s){
    return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

init();
</script>
</body>
</html>"""
