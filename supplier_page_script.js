
  // Auth guard: redirect to login if the readable session cookie is absent
  function _hasAuthCookie() {
      return document.cookie.split(";").some(c => c.trim().startsWith("logged_in="));
  }
  if (!_hasAuthCookie()) { window.location.href = "/"; }

  // Cookie is sent automatically — authHeaders just passes through any extra headers
  function authHeaders(extraHeaders = {}){ return { ...extraHeaders }; }

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
        if (!r.ok) { window.location.href = "/"; return; }
        const u = await r.json();
        const nameEl = document.getElementById("user-name");
        const avatarEl = document.getElementById("user-avatar");
        if (nameEl) nameEl.innerText = u.name;
        if (avatarEl) avatarEl.innerText = u.name.charAt(0).toUpperCase();
        currentUserRole = u.role || "";
        currentUserPermissions = new Set(
            (typeof u.permissions === "string" ? u.permissions.split(",") : (u.permissions || []))
                .map(v => String(v).trim())
                .filter(Boolean)
        );
        configureSupplierPermissions();
        return u;
    } catch(e) { window.location.href = "/"; }
}
async function logout(){
    await fetch("/auth/logout", { method: "POST" });
    window.location.href = "/";
}
  initializeColorMode();
  initUser();
  let suppliers  = [];
let allProducts = [];
let currentTab  = "suppliers";
let editingSupplierId = null;
let searchTimer = null;
let currentUserRole = "";
let currentUserPermissions = new Set();
const supplierTabPermissions = {
    suppliers: "tab_suppliers_directory",
    purchases: "tab_suppliers_purchases",
};

/* ── INIT ── */
async function init(){
    try {
        await loadSuppliers();
        allProducts = await (await fetch("/suppliers/api/products-list")).json();
    } catch (error) {
        console.error("Suppliers page init failed", error);
        showToast("Couldn't load supplier data");
    }
}

function hasPermission(permission){
    return currentUserRole === "admin" || currentUserPermissions.has(permission);
}

function hasExplicitSupplierTabPermissions(){
    return Object.values(supplierTabPermissions).some(permission => currentUserPermissions.has(permission));
}

function canAccessSupplierTab(tab){
    const permission = supplierTabPermissions[tab];
    if(!permission) return false;
    if(hasPermission(permission)) return true;
    return hasPermission("page_suppliers") && !hasExplicitSupplierTabPermissions();
}

function configureSupplierPermissions(){
    const tabMap = [
        { id: "tab-suppliers", tab: "suppliers" },
        { id: "tab-purchases", tab: "purchases" },
    ];
    let firstAllowed = null;
    tabMap.forEach(conf => {
        const el = document.getElementById(conf.id);
        if(!el) return;
        const allowed = canAccessSupplierTab(conf.tab);
        el.style.display = allowed ? "" : "none";
        if(allowed && !firstAllowed) firstAllowed = conf.tab;
    });
    if(firstAllowed && !canAccessSupplierTab(currentTab)){
        switchTab(firstAllowed);
    } else {
        updateTabActionButtons();
    }
}

function updateTabActionButtons(){
    document.getElementById("add-supplier-btn").style.display =
        currentTab === "suppliers" && canAccessSupplierTab("suppliers") ? "" : "none";
    document.getElementById("new-po-btn").style.display =
        currentTab === "purchases" && canAccessSupplierTab("purchases") ? "" : "none";
}

/* ── TABS ── */
function switchTab(tab){
    if(!canAccessSupplierTab(tab)) return;
    currentTab = tab;
    document.getElementById("tab-suppliers").classList.toggle("active", tab==="suppliers");
    document.getElementById("tab-purchases").classList.toggle("active", tab==="purchases");
    document.getElementById("suppliers-section").style.display = tab==="suppliers" ? "" : "none";
    document.getElementById("purchases-section").style.display = tab==="purchases" ? "" : "none";
    updateTabActionButtons();
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

function escapeJsString(value){
    return String(value ?? "")
        .replace(/\\/g, "\\\\")
        .replace(/'/g, "\\'")
        .replace(/\r/g, "\\r")
        .replace(/\n/g, "\\n");
}

/* ── SUPPLIERS ── */
async function loadSuppliers(){
    try {
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
                    <button class="action-btn" onclick="openEditSupplierModal(${s.id},'${escapeJsString(s.name)}','${escapeJsString(s.phone)}','${escapeJsString(s.email)}','${escapeJsString(s.address)}')">Edit</button>
                    <button class="action-btn danger" onclick="deleteSupplier(${s.id},'${escapeJsString(s.name)}')">Delete</button>
                </td>
            </tr>`).join("");
    } catch (error) {
        console.error("Failed to load suppliers", error);
        document.getElementById("suppliers-body").innerHTML =
            `<tr><td colspan="6" style="text-align:center;color:var(--danger);padding:40px">Failed to load suppliers</td></tr>`;
    }
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
    try {
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
    } catch (error) {
        console.error("Failed to load purchases", error);
        document.getElementById("purchases-body").innerHTML =
            `<tr><td colspan="7" style="text-align:center;color:var(--danger);padding:40px">Failed to load purchase orders</td></tr>`;
    }
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
