ACCOUNT_MENU_CSS = """
.account-menu{position:relative;}
.user-pill{cursor:pointer;transition:all .2s;}
.user-pill:hover,.user-pill.open{border-color:var(--border2);}
.menu-caret{font-size:11px;color:var(--muted);}
.account-dropdown{
    position:absolute;right:0;top:calc(100% + 10px);
    min-width:220px;background:var(--card);
    border:1px solid var(--border2);border-radius:14px;
    padding:8px;box-shadow:0 24px 50px rgba(0,0,0,.35);
    display:none;z-index:500;
}
.account-dropdown.open{display:block;}
.account-head{padding:10px 12px 8px;border-bottom:1px solid var(--border);margin-bottom:6px;}
.account-label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;}
.account-email{font-size:12px;color:var(--sub);margin-top:4px;word-break:break-word;}
.account-item{
    width:100%;display:flex;align-items:center;gap:10px;
    padding:10px 12px;border:none;background:transparent;
    border-radius:10px;color:var(--sub);font-family:var(--sans);
    font-size:13px;text-decoration:none;cursor:pointer;text-align:left;
}
.account-item:hover{background:var(--card2);color:var(--text);}
.account-item.danger:hover{color:var(--danger, #c97a7a);}
"""


ACCOUNT_MENU_HTML = """
<div class="account-menu">
    <button class="user-pill" id="account-trigger" onclick="toggleAccountMenu(event)" aria-haspopup="menu" aria-expanded="false">
        <div class="user-avatar" id="user-avatar">A</div>
        <span class="user-name" id="user-name">Admin</span>
        <span class="menu-caret">▾</span>
    </button>
    <div class="account-dropdown" id="account-dropdown" role="menu">
        <div class="account-head">
            <div class="account-label">Signed in as</div>
            <div class="account-email" id="user-email">—</div>
        </div>
        <a href="/users/password" class="account-item" role="menuitem">Change Password</a>
        <button class="account-item danger" onclick="logout()" role="menuitem">Sign out</button>
    </div>
</div>
"""


ACCOUNT_MENU_SCRIPT = """
function toggleAccountMenu(event){
    event.stopPropagation();
    const trigger = document.getElementById("account-trigger");
    const dropdown = document.getElementById("account-dropdown");
    if(!trigger || !dropdown) return;
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

function populateAccountMenuUser(u){
    const nameEl = document.getElementById("user-name");
    const avatarEl = document.getElementById("user-avatar");
    const emailEl = document.getElementById("user-email");
    if(nameEl) nameEl.innerText = u.name;
    if(avatarEl) avatarEl.innerText = (u.name || "?").charAt(0).toUpperCase();
    if(emailEl) emailEl.innerText = u.email || "—";
}
"""
