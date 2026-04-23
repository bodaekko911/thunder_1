from __future__ import annotations

from html import escape

from app.core.permissions import get_effective_permissions
from app.models.user import User


NAV_GROUPS = [
    {
        "label": "Work",
        "items": [
            {"label": "Dashboard", "href": "/dashboard", "permission": "page_dashboard"},
            {"label": "POS", "href": "/pos", "permission": "page_pos"},
            {"label": "B2B", "href": "/b2b/", "permission": "page_b2b"},
            {"label": "Reports", "href": "/reports/", "permission": "page_reports"},
        ],
    },
    {
        "label": "Stock",
        "items": [
            {"label": "Products", "href": "/products/", "permission": "page_products"},
            {"label": "Inventory", "href": "/inventory/", "permission": "page_inventory"},
            {"label": "Receive", "href": "/receive/", "permission": "page_receive_products"},
            {"label": "Import", "href": "/import", "permission": "page_import"},
            {"label": "Farm", "href": "/farm/", "permission": "page_farm"},
            {"label": "Production", "href": "/production/", "permission": "page_production"},
        ],
    },
    {
        "label": "Finance",
        "items": [
            {"label": "Accounting", "href": "/accounting/", "permission": "page_accounting"},
            {"label": "Expenses", "href": "/expenses/", "permission": "page_expenses"},
            {"label": "Customers", "href": "/customers-mgmt/", "permission": "page_customers"},
            {"label": "Suppliers", "href": "/suppliers/", "permission": "page_suppliers"},
        ],
    },
    {
        "label": "People",
        "items": [
            {"label": "HR", "href": "/hr/", "permission": "page_hr"},
            {"label": "Users", "href": "/users/", "admin_only": True},
        ],
    },
]


def _user_permissions(user: User) -> set[str]:
    return get_effective_permissions(user.role, getattr(user, "permissions", None))


def _can_see_item(user: User, permissions: set[str], item: dict) -> bool:
    if item.get("admin_only"):
        return user.role == "admin"
    return "*" in permissions or item["permission"] in permissions


def _is_active(item: dict, active_permission: str | None) -> bool:
    if item.get("admin_only"):
        return active_permission == "admin_users"
    return item.get("permission") == active_permission


def _render_group(user: User, permissions: set[str], group: dict, active_permission: str | None) -> str:
    visible_items = [
        item for item in group["items"]
        if _can_see_item(user, permissions, item)
    ]
    if not visible_items:
        return ""

    is_group_active = any(_is_active(item, active_permission) for item in visible_items)
    links = []
    for item in visible_items:
        active = _is_active(item, active_permission)
        links.append(
            f'<a class="app-nav-menu-item{" active" if active else ""}" '
            f'href="{escape(item["href"])}" role="menuitem"'
            f'{" aria-current=\"page\"" if active else ""}>{escape(item["label"])}</a>'
        )
    return (
        f'<details class="app-nav-group{" active" if is_group_active else ""}"'
        f'{" open" if is_group_active else ""}>'
        f'<summary>{escape(group["label"])}</summary>'
        f'<div class="app-nav-menu" role="menu">{"".join(links)}</div>'
        f'</details>'
    )


def app_nav_styles() -> str:
    return """
<style>
.app-nav{grid-column:1/-1;position:sticky;top:0;z-index:300;display:flex;align-items:center;gap:12px;min-height:64px;padding:10px 24px;background:rgba(10,13,24,.94);backdrop-filter:blur(20px);border-bottom:1px solid var(--border,rgba(255,255,255,.08));color:var(--text,#f0f4ff);}
body.light .app-nav,[data-theme="light"] .app-nav{background:rgba(244,245,239,.94);}
.app-nav-brand{display:flex;align-items:center;gap:9px;min-width:max-content;text-decoration:none;font-size:17px;font-weight:900;color:var(--text,#f0f4ff);}
.app-nav-brand svg{flex:0 0 auto}.app-nav-brand span{background:linear-gradient(135deg,var(--green,#00ff9d),var(--blue,#4d9fff));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;}
.app-nav-main{display:flex;align-items:center;gap:6px;flex:1;min-width:0;}
.app-nav-group{position:relative}.app-nav-group summary{list-style:none;display:flex;align-items:center;gap:7px;padding:8px 12px;border-radius:8px;color:var(--sub,#8899bb);font-size:12px;font-weight:800;cursor:pointer;white-space:nowrap;transition:background .16s,color .16s;}
.app-nav-group summary::-webkit-details-marker{display:none}.app-nav-group summary:after{content:"";width:6px;height:6px;border-right:1.5px solid currentColor;border-bottom:1.5px solid currentColor;transform:rotate(45deg) translateY(-2px);opacity:.75;}
.app-nav-group:hover summary,.app-nav-group[open] summary{background:rgba(255,255,255,.06);color:var(--text,#f0f4ff)}body.light .app-nav-group:hover summary,body.light .app-nav-group[open] summary,[data-theme="light"] .app-nav-group:hover summary,[data-theme="light"] .app-nav-group[open] summary{background:rgba(0,0,0,.05);}
.app-nav-group.active summary{background:rgba(77,159,255,.14);color:var(--blue,#4d9fff);box-shadow:inset 0 -2px 0 var(--blue,#4d9fff);}
.app-nav-menu{position:absolute;left:0;top:calc(100% + 8px);min-width:190px;padding:8px;background:var(--card,#0f1424);border:1px solid var(--border2,rgba(255,255,255,.11));border-radius:12px;box-shadow:0 22px 50px rgba(0,0,0,.34);}
.app-nav-menu-item{display:flex;align-items:center;padding:10px 12px;border-radius:8px;color:var(--sub,#8899bb);font-size:13px;font-weight:700;text-decoration:none;white-space:nowrap;}
.app-nav-menu-item:hover,.app-nav-menu-item:focus-visible{background:var(--card2,#151c30);color:var(--text,#f0f4ff);outline:none}.app-nav-menu-item.active{background:rgba(77,159,255,.14);color:var(--blue,#4d9fff);}
.app-nav-actions{display:flex;align-items:center;gap:10px;margin-left:auto}.app-nav .topbar-right{margin-left:auto}
.app-nav-mobile-toggle{display:none;align-items:center;justify-content:center;width:38px;height:38px;border-radius:10px;border:1px solid var(--border,rgba(255,255,255,.08));background:var(--card,#0f1424);color:var(--sub,#8899bb);font-size:18px;cursor:pointer;}
.app-nav .mode-btn{flex:0 0 auto}.app-nav .account-menu{position:relative}.app-nav .account-dropdown{z-index:600}
@media(max-width:900px){.app-nav{flex-wrap:wrap;padding:10px 14px}.app-nav-mobile-toggle{display:flex}.app-nav-main{display:none;order:3;flex-basis:100%;flex-direction:column;align-items:stretch;gap:6px;padding-top:8px}.app-nav.open .app-nav-main{display:flex}.app-nav-group{width:100%}.app-nav-group summary{justify-content:space-between;padding:12px 13px;background:rgba(255,255,255,.04)}.app-nav-menu{position:static;box-shadow:none;margin-top:6px;width:100%}.app-nav-actions{margin-left:auto}.app-nav .user-name{display:none}}
@media(max-width:520px){.app-nav{gap:8px}.app-nav-brand span{max-width:116px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.app-nav-actions{gap:6px}.app-nav .user-pill{padding:6px 9px}.app-nav .menu-caret{display:none}}
</style>
"""


def app_nav_script() -> str:
    return """
<script>
(function(){
  function closeOtherGroups(current){
    document.querySelectorAll(".app-nav-group[open]").forEach(function(group){
      if(group !== current) group.removeAttribute("open");
    });
  }
  window.__appNav = {
    toggleAccount: function(event){
      if(event) event.stopPropagation();
      var trigger = document.getElementById("account-trigger");
      var dropdown = document.getElementById("account-dropdown");
      if(!trigger || !dropdown) return;
      var open = dropdown.classList.toggle("open");
      trigger.classList.toggle("open", open);
      trigger.setAttribute("aria-expanded", open ? "true" : "false");
    },
    signOut: async function(){
      await fetch("/auth/logout", {method:"POST"});
      window.location.href = "/";
    },
    toggleTheme: function(){
      if(typeof window.toggleMode === "function") return window.toggleMode();
      if(typeof window.toggleTheme === "function") return window.toggleTheme();
      var next = document.body.classList.contains("light") ? "dark" : "light";
      document.body.classList.toggle("light", next === "light");
      document.documentElement.dataset.theme = next;
      localStorage.setItem("colorMode", next);
    }
  };
  document.addEventListener("click", function(event){
    var account = document.getElementById("account-dropdown");
    var trigger = document.getElementById("account-trigger");
    if(account && trigger && !account.contains(event.target) && !trigger.contains(event.target)){
      account.classList.remove("open");
      trigger.classList.remove("open");
      trigger.setAttribute("aria-expanded", "false");
    }
    if(!event.target.closest(".app-nav-group")) closeOtherGroups(null);
  });
  document.addEventListener("toggle", function(event){
    if(event.target.classList && event.target.classList.contains("app-nav-group") && event.target.open){
      closeOtherGroups(event.target);
    }
  }, true);
})();
</script>
"""


def render_app_header(user: User, active_permission: str | None = None) -> str:
    permissions = _user_permissions(user)
    groups = "".join(
        _render_group(user, permissions, group, active_permission)
        for group in NAV_GROUPS
    )
    name = escape(getattr(user, "name", None) or "User")
    email = escape(getattr(user, "email", None) or "")
    avatar = escape((name.strip()[:1] or "U").upper())
    return f"""
{app_nav_styles()}
<nav class="app-nav" id="app-nav" aria-label="Primary navigation">
  <a href="/home" class="app-nav-brand">
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true"><polygon points="13,2 4,14 11,14 11,22 20,10 13,10" fill="#f59e0b"/></svg>
    <span>Thunder ERP</span>
  </a>
  <button class="app-nav-mobile-toggle" type="button" aria-label="Toggle navigation" onclick="document.getElementById('app-nav').classList.toggle('open')">&#9776;</button>
  <div class="app-nav-main">{groups}</div>
  <div class="app-nav-actions topbar-right">
    <button class="mode-btn" id="mode-btn" type="button" title="Toggle color mode" aria-label="Toggle color mode" onclick="window.__appNav.toggleTheme()">&#9790;</button>
    <div class="account-menu">
      <button class="user-pill" id="account-trigger" type="button" onclick="window.__appNav.toggleAccount(event)" aria-haspopup="menu" aria-expanded="false">
        <div class="user-avatar" id="user-avatar">{avatar}</div>
        <span class="user-name" id="user-name">{name}</span>
        <span class="menu-caret">&#9662;</span>
      </button>
      <div class="account-dropdown" id="account-dropdown" role="menu">
        <div class="account-head">
          <div class="account-label">Signed in as</div>
          <div class="account-email" id="user-email">{email}</div>
        </div>
        <a href="/users/password" class="account-item" role="menuitem">Change Password</a>
        <button class="account-item danger" id="signout-btn" type="button" onclick="window.__appNav.signOut()" role="menuitem">Sign out</button>
      </div>
    </div>
  </div>
</nav>
{app_nav_script()}
"""
