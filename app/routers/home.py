from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["Home"])


@router.get("/home", response_class=HTMLResponse)
def home_ui():
    return """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Habiba Organic Farm — ERP</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,600;1,300;1,400&family=DM+Sans:wght@300;400;500;600&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {
    --bg:       #08090c;
    --card:     #0d1008;
    --card2:    #111408;
    --border:   rgba(255,255,255,0.055);
    --border2:  rgba(255,255,255,0.10);
    --green:    #7ecb6f;
    --green2:   #a8d97a;
    --amber:    #d4a256;
    --amber2:   #e8c07a;
    --teal:     #5bbfb5;
    --rose:     #c97a7a;
    --blue:     #6a9fd4;
    --text:     #e8eae0;
    --sub:      #8a9080;
    --muted:    #4a5040;
    --serif:    'Cormorant Garamond', serif;
    --sans:     'DM Sans', sans-serif;
    --mono:     'DM Mono', monospace;
}

body.light {
    --bg:      #f4f5ef;
    --card:    #eceee6;
    --card2:   #e4e6de;
    --border:  rgba(0,0,0,0.07);
    --border2: rgba(0,0,0,0.12);
    --text:    #1a1e14;
    --sub:     #4a5040;
    --muted:   #8a9080;
}
body.light .bg-orb      { opacity: .08; }
body.light .bg-grain    { opacity: .15; }
body.light .topbar      { background: rgba(244,245,239,.85); }
body.light .user-pill   { background: #dddfd7; }
body.light .module-card { background: #eceee6; }
body.light .logout-btn  { color: var(--muted); }

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
    font-family: var(--sans);
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    overflow-x: hidden;
}

.bg-layer {
    position: fixed; inset: 0; z-index: 0;
    overflow: hidden; transform: translateZ(0);
}

.bg-orb {
    position: absolute; border-radius: 50%;
    filter: blur(80px); opacity: .18;
    animation: orbFloat 18s ease-in-out infinite alternate;
    will-change: transform;
    backface-visibility: hidden;
    -webkit-backface-visibility: hidden;
}
.bg-orb:nth-child(1) { width:700px; height:500px; top:-10%; left:-15%; background:radial-gradient(circle,#7ecb6f,transparent 70%); animation-duration:20s; }
.bg-orb:nth-child(2) { width:500px; height:600px; top:30%; right:-10%; background:radial-gradient(circle,#d4a256,transparent 70%); animation-duration:25s; animation-delay:-8s; }
.bg-orb:nth-child(3) { width:400px; height:400px; bottom:-10%; left:30%; background:radial-gradient(circle,#5bbfb5,transparent 70%); animation-duration:22s; animation-delay:-4s; }

@keyframes orbFloat {
    0%   { transform: translate(0, 0); }
    33%  { transform: translate(30px, -20px); }
    66%  { transform: translate(-20px, 30px); }
    100% { transform: translate(10px, 10px); }
}

.bg-grain {
    position: fixed; inset: 0; z-index: 1;
    background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)' opacity='0.04'/%3E%3C/svg%3E");
    pointer-events: none; opacity: .4;
}

.page {
    position: relative; z-index: 2;
    min-height: 100vh; display: flex; flex-direction: column;
}

.topbar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 24px 40px;
    border-bottom: 1px solid var(--border);
    background: rgba(8,9,12,.6);
    backdrop-filter: blur(20px);
    animation: fadeDown .6s ease both;
}

@keyframes fadeDown {
    from { opacity:0; transform:translateY(-12px); }
    to   { opacity:1; transform:translateY(0); }
}

.brand { display: flex; align-items: center; gap: 14px; }

.brand-leaf {
    width: 36px; height: 36px;
    background: linear-gradient(135deg, var(--green), var(--green2));
    border-radius: 50% 12px 50% 12px;
    display: flex; align-items: center; justify-content: center;
    font-size: 18px;
}

.brand-text h1 {
    font-family: var(--serif); font-size: 20px; font-weight: 600;
    letter-spacing: .3px; color: var(--text);
}
.brand-text span {
    font-size: 11px; color: var(--muted);
    letter-spacing: 2px; text-transform: uppercase; font-weight: 500;
}

.topbar-right { display: flex; align-items: center; gap: 12px; }

.mode-btn {
    background: var(--card);
    border: 1px solid var(--border);
    color: var(--sub);
    width: 36px; height: 36px;
    border-radius: 10px;
    font-size: 16px;
    cursor: pointer;
    transition: all .2s;
    display: flex; align-items: center; justify-content: center;
}
.mode-btn:hover { border-color: var(--border2); transform: scale(1.08); }

.user-pill {
    display: flex; align-items: center; gap: 10px;
    background: var(--card); border: 1px solid var(--border);
    border-radius: 40px; padding: 7px 16px 7px 10px;
}

.user-avatar {
    width: 28px; height: 28px;
    background: linear-gradient(135deg, var(--green), var(--amber));
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 12px; font-weight: 700; color: #0a0c08;
}

.user-name { font-size: 13px; font-weight: 500; color: var(--sub); }

.logout-btn {
    background: transparent; border: 1px solid var(--border);
    color: var(--muted); font-family: var(--sans);
    font-size: 12px; font-weight: 500;
    padding: 8px 16px; border-radius: 8px;
    cursor: pointer; transition: all .2s; letter-spacing: .3px;
}
.logout-btn:hover { border-color: var(--rose); color: var(--rose); }

.hero { padding: 60px 40px 40px; animation: fadeUp .7s ease .1s both; }

@keyframes fadeUp {
    from { opacity:0; transform:translateY(16px); }
    to   { opacity:1; transform:translateY(0); }
}

.hero-greeting {
    font-family: var(--serif); font-size: 48px; font-weight: 300;
    letter-spacing: -.5px; line-height: 1.1; color: var(--text); margin-bottom: 10px;
}
.hero-greeting em { font-style: italic; color: var(--green2); }
.hero-sub  { font-size: 14px; color: var(--muted); letter-spacing: .3px; }
.hero-date { font-family: var(--mono); font-size: 12px; color: var(--muted); margin-top: 6px; letter-spacing: .5px; }

.modules-wrap { padding: 0 40px 60px; flex: 1; }

.section-title {
    font-size: 11px; font-weight: 600; letter-spacing: 2.5px;
    text-transform: uppercase; color: var(--muted); margin-bottom: 20px;
    display: flex; align-items: center; gap: 12px;
}
.section-title::after {
    content: ''; flex: 1; height: 1px;
    background: linear-gradient(90deg, var(--border2), transparent);
}

.modules-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 14px; }

.module-card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 16px; padding: 24px; cursor: pointer;
    text-decoration: none; display: flex; flex-direction: column; gap: 14px;
    position: relative; overflow: hidden;
    transition: transform .2s, border-color .2s, box-shadow .2s;
    animation: cardReveal .5s ease both;
}

@keyframes cardReveal {
    from { opacity:0; transform:translateY(20px) scale(.97); }
    to   { opacity:1; transform:translateY(0) scale(1); }
}

.module-card:nth-child(1)  { animation-delay:.08s }
.module-card:nth-child(2)  { animation-delay:.14s }
.module-card:nth-child(3)  { animation-delay:.20s }
.module-card:nth-child(4)  { animation-delay:.26s }
.module-card:nth-child(5)  { animation-delay:.32s }
.module-card:nth-child(6)  { animation-delay:.38s }
.module-card:nth-child(7)  { animation-delay:.44s }
.module-card:nth-child(8)  { animation-delay:.50s }
.module-card:nth-child(9)  { animation-delay:.56s }
.module-card:nth-child(10) { animation-delay:.62s }

.module-card::before {
    content: ''; position: absolute; inset: 0;
    opacity: 0; transition: opacity .3s; border-radius: 16px;
    background: radial-gradient(circle at 0% 0%, color-mix(in srgb, var(--accent, #7ecb6f) 8%, transparent), transparent 60%);
}

.module-card:hover {
    transform: translateY(-4px);
    box-shadow: 0 12px 40px rgba(0,0,0,.4), 0 0 0 1px var(--accent, #7ecb6f);
    border-color: var(--accent, #7ecb6f);
}
.module-card:hover::before { opacity: 1; }
.module-card:active { transform: translateY(-1px); }

.c-green  { --accent: #7ecb6f; }
.c-amber  { --accent: #d4a256; }
.c-teal   { --accent: #5bbfb5; }
.c-blue   { --accent: #6a9fd4; }
.c-rose   { --accent: #c97a7a; }
.c-lime   { --accent: #9ecf5a; }
.c-purple { --accent: #9a7ecb; }
.c-orange { --accent: #cb9c5f; }
.c-sky    { --accent: #6ab5d4; }

.card-icon {
    width: 44px; height: 44px; border-radius: 12px;
    display: flex; align-items: center; justify-content: center; font-size: 20px;
    background: color-mix(in srgb, var(--accent, #7ecb6f) 12%, transparent);
    border: 1px solid color-mix(in srgb, var(--accent, #7ecb6f) 20%, transparent);
    transition: transform .2s;
}
.module-card:hover .card-icon { transform: scale(1.1) rotate(-3deg); }

.card-body { flex: 1; }
.card-name {
    font-family: var(--serif); font-size: 20px; font-weight: 600;
    letter-spacing: -.2px; color: var(--text); margin-bottom: 4px;
}
.card-desc { font-size: 12px; color: var(--muted); line-height: 1.5; font-weight: 300; }
.card-footer { display: flex; align-items: center; justify-content: space-between; }
.card-tag {
    font-size: 10px; font-weight: 600; letter-spacing: 1.5px;
    text-transform: uppercase; color: var(--accent, #7ecb6f); opacity: .7;
}
.card-arrow {
    width: 26px; height: 26px; border-radius: 8px;
    background: color-mix(in srgb, var(--accent, #7ecb6f) 10%, transparent);
    border: 1px solid color-mix(in srgb, var(--accent, #7ecb6f) 20%, transparent);
    display: flex; align-items: center; justify-content: center;
    color: var(--accent, #7ecb6f); font-size: 13px;
    transition: transform .2s, background .2s;
}
.module-card:hover .card-arrow {
    transform: translate(2px,-2px);
    background: color-mix(in srgb, var(--accent, #7ecb6f) 20%, transparent);
}

.group-gap { margin-top: 36px; }

.footer {
    border-top: 1px solid var(--border); padding: 20px 40px;
    display: flex; align-items: center; justify-content: space-between;
    font-size: 12px; color: var(--muted);
    animation: fadeUp .6s ease .6s both;
}
.footer-brand { font-family: var(--serif); font-size: 14px; color: var(--sub); font-style: italic; }

@media (max-width: 700px) {
    .topbar        { padding: 16px 20px; }
    .hero          { padding: 40px 20px 24px; }
    .hero-greeting { font-size: 32px; }
    .modules-wrap  { padding: 0 20px 40px; }
    .modules-grid  { grid-template-columns: 1fr 1fr; gap: 10px; }
    .footer        { padding: 16px 20px; flex-direction:column; gap:8px; text-align:center; }
}
@media (max-width: 420px) {
    .modules-grid { grid-template-columns: 1fr; }
}
</style>
</head>
<body>

<div class="bg-layer">
    <div class="bg-orb"></div>
    <div class="bg-orb"></div>
    <div class="bg-orb"></div>
</div>
<div class="bg-grain"></div>

<div class="page">

    <header class="topbar">
        <div class="brand">
            <div class="brand-leaf">🌿</div>
            <div class="brand-text">
                <h1>Habiba Organic Farm</h1>
                <span>Enterprise Resource System</span>
            </div>
        </div>
        <div class="topbar-right">
            <button class="mode-btn" id="mode-btn" onclick="toggleMode()" title="Toggle light/dark mode">🌙</button>
            <div class="user-pill">
                <div class="user-avatar" id="user-avatar">A</div>
                <span class="user-name" id="user-name">Admin</span>
            </div>
            <button class="logout-btn" onclick="logout()">Sign out</button>
        </div>
    </header>

    <section class="hero">
        <div class="hero-greeting" id="greeting">Good morning, <em>welcome back</em></div>
        <div class="hero-sub">What would you like to work on today?</div>
        <div class="hero-date" id="hero-date"></div>
    </section>

    <main class="modules-wrap">

        <div class="section-title">Core Operations</div>
        <div class="modules-grid">

            <a href="/pos" class="module-card c-green">
                <div class="card-icon">🛒</div>
                <div class="card-body">
                    <div class="card-name">Point of Sale</div>
                    <div class="card-desc">Process sales, scan barcodes, print receipts</div>
                </div>
                <div class="card-footer">
                    <span class="card-tag">Sales</span>
                    <span class="card-arrow">↗</span>
                </div>
            </a>

            <a href="/production/" class="module-card c-amber">
                <div class="card-icon">⚗</div>
                <div class="card-body">
                    <div class="card-name">Production</div>
                    <div class="card-desc">Process raw materials, packaging runs, track loss</div>
                </div>
                <div class="card-footer">
                    <span class="card-tag">Manufacturing</span>
                    <span class="card-arrow">↗</span>
                </div>
            </a>

            <a href="/inventory/" class="module-card c-teal">
                <div class="card-icon">📦</div>
                <div class="card-body">
                    <div class="card-name">Inventory</div>
                    <div class="card-desc">Stock levels, movements, adjustments</div>
                </div>
                <div class="card-footer">
                    <span class="card-tag">Stock</span>
                    <span class="card-arrow">↗</span>
                </div>
            </a>

            <a href="/dashboard" class="module-card c-blue">
                <div class="card-icon">📊</div>
                <div class="card-body">
                    <div class="card-name">Dashboard</div>
                    <div class="card-desc">Sales today, top products, low stock alerts</div>
                </div>
                <div class="card-footer">
                    <span class="card-tag">Reports</span>
                    <span class="card-arrow">↗</span>
                </div>
            </a>

        </div>

        <div class="group-gap"></div>
        <div class="section-title">Management</div>
        <div class="modules-grid">

            <a href="/products/" class="module-card c-lime">
                <div class="card-icon">🌱</div>
                <div class="card-body">
                    <div class="card-name">Products</div>
                    <div class="card-desc">Catalog, pricing, SKUs, categories</div>
                </div>
                <div class="card-footer">
                    <span class="card-tag">Catalog</span>
                    <span class="card-arrow">↗</span>
                </div>
            </a>

            <a href="/customers-mgmt/" class="module-card c-rose">
                <div class="card-icon">👥</div>
                <div class="card-body">
                    <div class="card-name">Customers</div>
                    <div class="card-desc">Customer list, invoice history, balances</div>
                </div>
                <div class="card-footer">
                    <span class="card-tag">CRM</span>
                    <span class="card-arrow">↗</span>
                </div>
            </a>

            <a href="/suppliers/" class="module-card c-orange">
                <div class="card-icon">🚚</div>
                <div class="card-body">
                    <div class="card-name">Suppliers</div>
                    <div class="card-desc">Supplier list, purchase orders, receiving</div>
                </div>
                <div class="card-footer">
                    <span class="card-tag">Purchasing</span>
                    <span class="card-arrow">↗</span>
                </div>
            </a>

            <a href="/import" class="module-card c-sky">
                <div class="card-icon">📥</div>
                <div class="card-body">
                    <div class="card-name">Import Data</div>
                    <div class="card-desc">Import customers and products from Excel</div>
                </div>
                <div class="card-footer">
                    <span class="card-tag">Tools</span>
                    <span class="card-arrow">↗</span>
                </div>
            </a>

        </div>

        <div class="group-gap"></div>
        <div class="section-title">People &amp; Finance</div>
        <div class="modules-grid">

            <a href="/hr/" class="module-card c-purple">
                <div class="card-icon">🧑‍🤝‍🧑</div>
                <div class="card-body">
                    <div class="card-name">HR &amp; Payroll</div>
                    <div class="card-desc">Employees, attendance, salary runs</div>
                </div>
                <div class="card-footer">
                    <span class="card-tag">Human Resources</span>
                    <span class="card-arrow">↗</span>
                </div>
            </a>

            <a href="/accounting/" class="module-card c-amber">
                <div class="card-icon">📒</div>
                <div class="card-body">
                    <div class="card-name">Accounting</div>
                    <div class="card-desc">Ledger, journal entries, P&amp;L report</div>
                </div>
                <div class="card-footer">
                    <span class="card-tag">Finance</span>
                    <span class="card-arrow">↗</span>
                </div>
            </a>

            <a href="/users/" class="module-card c-rose" id="card-users" style="display:none">
                <div class="card-icon">🔐</div>
                <div class="card-body">
                    <div class="card-name">User Management</div>
                    <div class="card-desc">Roles, permissions, passwords, activity log</div>
                </div>
                <div class="card-footer">
                    <span class="card-tag">Admin Only</span>
                    <span class="card-arrow">↗</span>
                </div>
            </a>

        </div>

    </main>

    <footer class="footer">
        <span class="footer-brand">Habiba Organic Farm</span>
        <span id="footer-time"></span>
    </footer>

</div>

<script>
function setGreeting(){
    let name  = localStorage.getItem("user_name") || "there";
    let h     = new Date().getHours();
    let greet = h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
    document.getElementById("greeting").innerHTML   = `${greet}, <em>${name}</em>`;
    document.getElementById("user-avatar").innerText = name.charAt(0).toUpperCase();
    document.getElementById("user-name").innerText   = name;
}

function setDateTime(){
    let now  = new Date();
    let opts = {weekday:"long",year:"numeric",month:"long",day:"numeric"};
    document.getElementById("hero-date").innerText   = now.toLocaleDateString("en-GB", opts);
    document.getElementById("footer-time").innerText = now.toLocaleTimeString("en-GB", {hour:"2-digit",minute:"2-digit"});
}

function logout(){
    localStorage.removeItem("token");
    localStorage.removeItem("user_name");
    localStorage.removeItem("user_role");
    window.location.href = "/";
}

function toggleMode(){
    let isLight = document.body.classList.toggle("light");
    document.getElementById("mode-btn").innerText = isLight ? "☀️" : "🌙";
    localStorage.setItem("colorMode", isLight ? "light" : "dark");
}

// Guard — redirect to login if no token
if(!localStorage.getItem("token")){
    window.location.href = "/";
}

// Show Users card for admin only
if(localStorage.getItem("user_role") === "admin"){
    let c = document.getElementById("card-users");
    if(c) c.style.display = "";
}

// Restore saved colour mode
if(localStorage.getItem("colorMode") === "light"){
    document.body.classList.add("light");
    document.getElementById("mode-btn").innerText = "☀️";
}

setGreeting();
setDateTime();
setInterval(setDateTime, 30000);
</script>
</body>
</html>
"""