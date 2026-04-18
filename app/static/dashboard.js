/* ── Thunder ERP Dashboard v2 ─────────────────────────────────────────
   Vanilla JS + Chart.js (loaded via CDN in the HTML).
   Entry: window.onload → initDashboard()
────────────────────────────────────────────────────────────────────── */

// ── state ──────────────────────────────────────────────────────────────
let _currentRange = "today";
let _customStart  = null;
let _customEnd    = null;
let _lastData     = null;
let _refreshTimer = null;
let _elapsedTimer = null;
let _lastUpdated  = null;
let _firstLoad    = true;
let _revenueChart = null;
let _drawerOpen   = false;
let _heroType     = "admin";

// ── helpers ────────────────────────────────────────────────────────────
function fmt(val, decimals = 2) {
  return Number(val || 0).toLocaleString("en-GB", { minimumFractionDigits: decimals }) + " EGP";
}
function fmtNum(val) {
  return Number(val || 0).toLocaleString("en-GB");
}
function pctStr(v) {
  if (v === null || v === undefined) return "—";
  return (v > 0 ? "+" : "") + v + "%";
}
function directionArrow(dir) {
  return { up:"↑", down:"↓", bad_up:"↑", bad_down:"↓", flat:"—" }[dir] || "—";
}
function chipClass(dir) {
  return "chip chip-" + (dir || "flat").replace("_","-");
}

function setText(id, val) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = val;
}
function setHTML(id, html) {
  const el = document.getElementById(id);
  if (!el) return;
  el.innerHTML = html;
}

// count-up animation (first load only)
function countUp(el, target, prefix, suffix, duration = 600) {
  if (!_firstLoad) { el.textContent = prefix + fmtNum(target) + suffix; return; }
  let start   = null;
  const from  = 0;
  const step  = ts => {
    if (!start) start = ts;
    const pct = Math.min((ts - start) / duration, 1);
    const ease = 1 - Math.pow(1 - pct, 3); // easeOutCubic
    el.textContent = prefix + fmtNum(Math.round(from + (target - from) * ease)) + suffix;
    if (pct < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function flashValue(el, oldVal, newVal) {
  if (_firstLoad) return;
  if (oldVal !== undefined && oldVal !== newVal) {
    const cls = newVal > oldVal ? "flash-green" : "flash-red";
    el.classList.remove("flash-green", "flash-red");
    void el.offsetWidth; // reflow
    el.classList.add(cls);
  }
}

// ── theme ──────────────────────────────────────────────────────────────
function toggleMode() {
  const isLight = document.body.classList.toggle("light");
  localStorage.setItem("colorMode", isLight ? "light" : "dark");
  document.getElementById("mode-btn").textContent = isLight ? "☀️" : "🌙";
  if (_revenueChart) {
    updateChartTheme(_revenueChart);
    _revenueChart.update();
  }
}

function updateChartTheme(chart) {
  const light = document.body.classList.contains("light");
  const gridColor = light ? "rgba(0,0,0,.07)" : "rgba(255,255,255,.06)";
  chart.options.scales.x.grid.color = gridColor;
  chart.options.scales.y.grid.color = gridColor;
  chart.options.scales.x.ticks.color = light ? "#64748b" : "#64748b";
  chart.options.scales.y.ticks.color = light ? "#64748b" : "#64748b";
}

// ── account menu ───────────────────────────────────────────────────────
function toggleAccountMenu(e) {
  e.stopPropagation();
  document.getElementById("account-dropdown").classList.toggle("open");
}
document.addEventListener("click", () => {
  document.getElementById("account-dropdown")?.classList.remove("open");
});

async function logout() {
  await fetch("/auth/logout", { method: "POST" });
  window.location.href = "/";
}

// ── user init ──────────────────────────────────────────────────────────
async function initUser() {
  try {
    const r = await fetch("/auth/me");
    if (!r.ok) return;
    const u = await r.json();
    document.getElementById("user-name").textContent   = u.name || "User";
    document.getElementById("user-avatar").textContent = (u.name || "U")[0].toUpperCase();
    document.getElementById("greeting").textContent    = greeting(u.name);

    // Default range by role
    const role = u.role || "admin";
    if (role === "cashier" && _firstLoad) {
      _currentRange = "today";
    } else if (_firstLoad) {
      _currentRange = "mtd";
    }
    setActiveRange(_currentRange);
  } catch {}
}

function greeting(name) {
  const h = new Date().getHours();
  const part = h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
  return part + (name ? ", " + name.split(" ")[0] : "");
}

// ── date display ───────────────────────────────────────────────────────
function updateDateDisplay() {
  const el = document.getElementById("date-display");
  if (el) el.textContent = new Date().toLocaleDateString("en-GB",
    { weekday:"long", year:"numeric", month:"long", day:"numeric" });
}

function startElapsedTimer() {
  clearInterval(_elapsedTimer);
  _lastUpdated = Date.now();
  _elapsedTimer = setInterval(() => {
    const sec = Math.round((Date.now() - _lastUpdated) / 1000);
    const el  = document.getElementById("last-updated");
    if (el) el.textContent = "Last updated " + sec + "s ago";
  }, 1000);
}

// ── range picker ───────────────────────────────────────────────────────
function setActiveRange(range) {
  _currentRange = range;
  document.querySelectorAll(".range-btn").forEach(b => {
    b.classList.toggle("active", b.dataset.range === range);
  });
}

function onRangeClick(e) {
  const btn = e.currentTarget;
  const range = btn.dataset.range;
  if (range === "custom") {
    const s = prompt("Start date (YYYY-MM-DD):");
    if (!s) return;
    const en = prompt("End date (YYYY-MM-DD):");
    if (!en) return;
    _customStart = s; _customEnd = en;
  }
  setActiveRange(range);
  loadSummary();
}

document.querySelectorAll(".range-btn").forEach(b =>
  b.addEventListener("click", onRangeClick)
);

// ── tabs ───────────────────────────────────────────────────────────────
function initTabs() {
  document.querySelectorAll(".tab-bar").forEach(bar => {
    bar.querySelectorAll(".tab-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        const pane = btn.dataset.pane;
        const parent = btn.closest(".panel");
        parent.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
        parent.querySelectorAll(".tab-pane").forEach(p => p.classList.remove("active"));
        btn.classList.add("active");
        parent.querySelector("#" + pane)?.classList.add("active");
      });
    });
  });
}

// ── insights strip ─────────────────────────────────────────────────────
async function loadInsights() {
  try {
    const r = await fetch("/dashboard/insights");
    if (!r.ok) return;
    const data = await r.json();
    renderInsights(data.cards || [], data.suggested_chips || []);
  } catch {}
}

function iconChar(icon) {
  return { up:"↑", down:"↓", flat:"→", warning:"⚠" }[icon] || "•";
}

function renderInsights(cards, chips) {
  const el = document.getElementById("insights-strip");
  if (!el) return;

  if (!cards.length) {
    el.innerHTML = '<span class="insights-empty">No significant changes detected. ✓</span>';
    renderChips(chips);
    return;
  }

  el.innerHTML = cards.map(c => `
    <div class="insight-card" role="region" aria-label="Insight: ${escHtml(c.text.replace(/\*\*/g,''))}"
         tabindex="0">
      <span class="insight-icon ${c.icon}" aria-hidden="true">${iconChar(c.icon)}</span>
      <p class="insight-text">${mdBold(escHtml(c.text))}</p>
      <button class="insight-action" onclick="askAssistant(${JSON.stringify(c.suggested_question)})">
        See details
      </button>
    </div>
  `).join("");

  renderChips(chips);
}

function renderChips(chips) {
  const el = document.getElementById("preset-chips");
  if (!el || !chips.length) return;
  el.innerHTML = chips.map(c =>
    `<button class="preset-chip" onclick="askAssistant(${JSON.stringify(c)})">${escHtml(c)}</button>`
  ).join("");
}

function mdBold(str) {
  return str.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
}
function escHtml(str) {
  return String(str).replace(/[&<>"']/g, c =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

// ── sparkline chart (mini, no axes, 40px) ─────────────────────────────
function drawSparkline(canvasId, data, color) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || !data || data.length === 0) return;
  const ctx = canvas.getContext("2d");
  new Chart(ctx, {
    type: "line",
    data: {
      labels: data.map((_,i) => i),
      datasets:[{ data, borderColor: color || "#4d9fff", borderWidth:1.5, tension:.3,
                  pointRadius:0, fill:false }]
    },
    options: {
      responsive:true, maintainAspectRatio:false, animation:false,
      plugins:{ legend:{display:false}, tooltip:{enabled:false} },
      scales:{ x:{display:false}, y:{display:false} },
    }
  });
}

// ── hero section ──────────────────────────────────────────────────────
function renderHeroAdmin(hero) {
  if (!hero) return;

  renderHeroCard("hero-0", {
    label: "Revenue",
    value: hero.revenue?.value,
    prefix: "", suffix: " EGP",
    delta: hero.revenue?.delta_pct,
    dir: hero.revenue?.direction,
    subtitle: hero.revenue?.prior !== undefined
      ? "Prior period: " + fmt(hero.revenue.prior)
      : "",
    sparklineData: hero.revenue?.sparkline,
    sparklineColor: "#4d9fff",
    higherBetter: true,
  });

  renderHeroCard("hero-1", {
    label: "Gross Profit",
    value: hero.gross_profit?.value,
    prefix: "", suffix: " EGP",
    delta: hero.gross_profit?.delta_pct,
    dir: hero.gross_profit?.direction,
    subtitle: "Margin: " + (hero.gross_profit?.margin_pct ?? 0) + "%",
    sparklineData: hero.gross_profit?.sparkline,
    sparklineColor: "#10b981",
    higherBetter: true,
  });

  renderHeroCard("hero-2", {
    label: "Cash Position",
    value: hero.cash_position?.value,
    prefix: "", suffix: " EGP",
    delta: null,
    dir: "flat",
    subtitle: "AR: " + fmt(hero.cash_position?.ar),
    sparklineData: null,
    higherBetter: true,
  });

  renderHeroCard("hero-3", {
    label: "Customer Growth",
    value: hero.customer_growth?.value,
    prefix: "+", suffix: " new",
    delta: hero.customer_growth?.delta_pct,
    dir: hero.customer_growth?.direction,
    subtitle: "Total active: " + fmtNum(hero.customer_growth?.total_active),
    sparklineData: null,
    higherBetter: true,
  });
}

function renderHeroCashier(hero) {
  if (!hero) return;

  renderHeroCard("hero-0", {
    label: "My Shift Sales", value: hero.shift_sales?.value,
    prefix:"", suffix:" EGP", delta: null, dir:"flat",
    subtitle: hero.shift_sales?.count + " invoice(s)", sparklineData: null,
  });
  renderHeroCard("hero-1", {
    label: "Avg Basket", value: hero.avg_basket?.value,
    prefix:"", suffix:" EGP", delta: null, dir:"flat",
    subtitle: "", sparklineData: null,
  });
  renderHeroCard("hero-2", {
    label: "Top Item Today", value: null, rawText: hero.top_item?.name,
    prefix:"", suffix:"", delta: null, dir:"flat",
    subtitle: "Most sold SKU", sparklineData: null,
  });
  renderHeroCard("hero-3", {
    label: "Refunds Today", value: hero.refunds?.value,
    prefix:"", suffix:" EGP", delta: null, dir: hero.refunds?.direction,
    subtitle: hero.refunds?.count + " refund(s)", sparklineData: null,
    higherBetter: false,
  });
}

function renderHeroFarm(hero) {
  if (!hero) return;
  renderHeroCard("hero-0", { label:"Deliveries", value:hero.deliveries?.value, prefix:"", suffix:"", delta:null, dir:"flat", subtitle:"This period", sparklineData:null });
  renderHeroCard("hero-1", { label:"Spoilage", value:hero.spoilage?.qty, prefix:"", suffix:" kg", delta:null, dir:"flat", subtitle:"This period", sparklineData:null });
  renderHeroCard("hero-2", { label:"Production Batches", value:hero.production_batches?.value, prefix:"", suffix:"", delta:null, dir:"flat", subtitle:"", sparklineData:null });
  renderHeroCard("hero-3", { label:"Upcoming Deliveries", value:0, prefix:"", suffix:"", delta:null, dir:"flat", subtitle:hero.upcoming_deliveries?.note||"", sparklineData:null });
}

function renderHeroCard(elId, opts) {
  const el = document.getElementById(elId);
  if (!el) return;

  const labelEl = el.querySelector(".hero-label");
  if (labelEl && opts.label) labelEl.textContent = opts.label;

  const valEl = el.querySelector(".hero-value");
  if (valEl) {
    valEl.classList.remove("skeleton");
    valEl.style.removeProperty("height");
    valEl.style.removeProperty("width");
    if (opts.rawText !== undefined) {
      valEl.textContent = opts.rawText || "—";
    } else if (opts.value !== null && opts.value !== undefined) {
      const old = valEl.dataset.rawValue;
      flashValue(valEl, old !== undefined ? parseFloat(old) : undefined, opts.value);
      valEl.dataset.rawValue = opts.value;
      if (opts.suffix === " EGP") {
        countUp(valEl, opts.value, "", " EGP");
      } else {
        valEl.textContent = (opts.prefix || "") + fmtNum(opts.value) + (opts.suffix || "");
      }
    }
  }

  const chipEl = el.querySelector(".hero-chip");
  if (chipEl && opts.delta !== null && opts.delta !== undefined) {
    const dir = opts.dir || "flat";
    chipEl.className = "hero-chip " + chipClass(dir);
    chipEl.textContent = directionArrow(dir) + " " + pctStr(opts.delta);
    chipEl.setAttribute("aria-label",
      (dir === "up" || dir === "bad_down" ? "increased" : "decreased") + " by " + Math.abs(opts.delta) + "%");
  } else if (chipEl) {
    chipEl.className = "hero-chip chip chip-flat";
    chipEl.textContent = "—";
  }

  const subEl = el.querySelector(".hero-subtitle");
  if (subEl && opts.subtitle !== undefined) subEl.textContent = opts.subtitle;

  const canvas = el.querySelector(".hero-sparkline canvas");
  if (canvas && opts.sparklineData?.length) {
    canvas.id = canvas.id || (elId + "-spark");
    drawSparkline(canvas.id, opts.sparklineData, opts.sparklineColor);
  }
}

// ── primary chart ─────────────────────────────────────────────────────
function renderMainChart(chartData, rangeLabel) {
  const ctx = document.getElementById("main-chart");
  if (!ctx) return;

  const light = document.body.classList.contains("light");
  const gridColor  = light ? "rgba(0,0,0,.07)" : "rgba(255,255,255,.06)";
  const tickColor  = "#64748b";
  const buckets    = chartData.buckets || [];
  const labels     = buckets.map(b => b.date);
  const movingAvg  = chartData.moving_avg_7d || [];

  if (_revenueChart) { _revenueChart.destroy(); _revenueChart = null; }

  _revenueChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [
        {
          label: "POS Revenue",
          data: buckets.map(b => b.pos),
          backgroundColor: "rgba(77,159,255,.7)",
          stack: "revenue",
          order: 2,
        },
        {
          label: "B2B Revenue",
          data: buckets.map(b => b.b2b),
          backgroundColor: "rgba(16,185,129,.7)",
          stack: "revenue",
          order: 2,
        },
        {
          label: "Refunds",
          data: buckets.map(b => b.refunds),
          backgroundColor: "rgba(239,68,68,.6)",
          stack: "revenue",
          order: 2,
        },
        {
          label: "7-day Avg",
          data: movingAvg,
          type: "line",
          borderColor: "rgba(245,158,11,.9)",
          borderWidth: 2,
          pointRadius: 0,
          tension: .4,
          fill: false,
          stack: null,
          order: 1,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: _firstLoad ? 500 : 0 },
      plugins: {
        legend: { display: true, position: "top",
          labels: { color: tickColor, font:{ size:11 }, usePointStyle:true } },
        tooltip: {
          mode: "index",
          callbacks: {
            title: items => items[0]?.label || "",
            label: item => {
              const b = buckets[item.dataIndex];
              if (item.dataset.label === "7-day Avg")
                return " 7d avg: " + fmtNum(Math.round(item.raw)) + " EGP";
              return " " + item.dataset.label + ": " + fmtNum(Math.round(item.raw)) + " EGP";
            },
            afterBody: items => {
              const b = buckets[items[0]?.dataIndex];
              return b ? ["Orders: " + (b.orders || 0)] : [];
            },
          },
        },
      },
      scales: {
        x: { stacked:true, grid:{ color:gridColor }, ticks:{ color:tickColor, maxTicksLimit:12 } },
        y: { stacked:true, grid:{ color:gridColor }, ticks:{ color:tickColor,
              callback: v => fmtNum(v) + " EGP" } },
      },
      onClick: (_e, elements) => {
        if (!elements.length) return;
        const b = buckets[elements[0].index];
        if (b) askAssistant("show me sales on " + b.date);
      },
    },
  });

  // Accessibility fallback table
  const tbl = document.getElementById("chart-table");
  if (tbl) {
    tbl.innerHTML = "<tr><th>Date</th><th>POS</th><th>B2B</th><th>Refunds</th><th>Orders</th></tr>"
      + buckets.map(b =>
        `<tr><td>${b.date}</td><td>${fmtNum(b.pos)}</td><td>${fmtNum(b.b2b)}</td>`+
        `<td>${fmtNum(b.refunds)}</td><td>${b.orders}</td></tr>`
      ).join("");
  }
}

// ── panels ────────────────────────────────────────────────────────────
function renderPanels(panels) {
  if (!panels) return;

  // Panel A: Top Products
  renderTopProducts(panels.top_products || {});
  // Panel B: Receivables
  renderReceivables(panels.receivables || {});
  // Panel C: Stock pressure
  renderStockPressure(panels.stock_pressure || {});
  // Panel D: Recent activity
  renderRecentActivity(panels.recent_activity || []);
}

function renderTopProducts(tp) {
  const byRev = tp.by_revenue || [];
  renderTable("top-by-revenue", byRev,
    ["<th>Product</th><th>Revenue</th><th>Share</th>"],
    r => `<td class="td-bold">${escHtml(r.name)}</td>
          <td class="td-mono text-success">${fmt(r.revenue)}</td>
          <td>
            <div class="share-bar-wrap">
              <div class="share-bar"><div class="share-bar-fill" style="width:${Math.min(r.share,100)}%"></div></div>
              <span class="text-muted" style="font-size:11px">${r.share}%</span>
            </div>
          </td>`,
    r => `onclick="askAssistant('show product details for ${escHtml(r.name)}')" style="cursor:pointer"`,
  );

  const byQty = tp.by_qty || [];
  renderTable("top-by-qty", byQty,
    ["<th>Product</th><th>Qty Sold</th><th>Revenue</th>"],
    r => `<td class="td-bold">${escHtml(r.name)}</td>
          <td class="td-mono">${fmtNum(r.qty)}</td>
          <td class="td-mono text-success">${fmt(r.revenue)}</td>`,
  );

  const byMar = tp.by_margin || [];
  renderTable("top-by-margin", byMar,
    ["<th>Product</th><th>Margin</th><th>%</th>"],
    r => `<td class="td-bold">${escHtml(r.name)}</td>
          <td class="td-mono text-success">${fmt(r.margin)}</td>
          <td class="td-mono">${r.margin_pct}%</td>`,
  );
}

function renderReceivables(rec) {
  const b2b = rec.b2b || [];
  renderTable("recv-b2b", b2b,
    ["<th>Client</th><th>Outstanding</th><th>Status</th><th></th>"],
    r => {
      const d = r.days_overdue;
      const cls = d > 60 ? "badge-red" : d > 30 ? "badge-yellow" : "badge-green";
      const label = d > 0 ? d + "d overdue" : "Current";
      return `<td class="td-bold">${escHtml(r.name)}</td>
              <td class="td-mono text-error">${fmt(r.outstanding)}</td>
              <td><span class="badge ${cls}" title="${d} days overdue">${label}</span></td>
              <td><a href="/b2b/" class="row-action">Record payment</a></td>`;
    },
  );
  renderTable("recv-retail", [],
    ["<th>Customer</th><th>Balance</th>"],
    () => "",
    null, "No retail credit data."
  );
}

function renderStockPressure(sp) {
  const sor = sp.stockout_risk || [];
  renderTable("stock-risk", sor,
    ["<th>Product</th><th>Stock</th><th>Days Left</th><th></th>"],
    r => `<td class="td-bold">${escHtml(r.name)}</td>
          <td class="td-mono">${fmtNum(r.stock)}</td>
          <td class="td-mono text-error">${r.days_left}d</td>
          <td><a href="/receive/" class="row-action">Reorder</a></td>`,
  );

  const ls = sp.low_stock || [];
  renderTable("stock-low", ls,
    ["<th>Product</th><th>Stock</th><th>Min Stock</th><th></th>"],
    r => `<td class="td-bold">${escHtml(r.name)}</td>
          <td class="td-mono text-warning">${fmtNum(r.stock)}</td>
          <td class="td-mono">${fmtNum(r.min_stock)}</td>
          <td><a href="/receive/" class="row-action">Reorder</a></td>`,
  );

  const ds = sp.dead_stock || [];
  renderTable("stock-dead", ds,
    ["<th>Product</th><th>Stock</th><th>Note</th>"],
    r => `<td class="td-bold">${escHtml(r.name)}</td>
          <td class="td-mono">${fmtNum(r.stock)}</td>
          <td class="text-muted" style="font-size:11px">No sales in 60d</td>`,
  );
}

function renderRecentActivity(items) {
  renderTable("recent-activity", items,
    ["<th>Ref</th><th>Customer</th><th>Total</th><th>Method</th><th>Time</th>"],
    r => {
      const tc = r.total >= 0 ? "text-success" : "text-error";
      const bc = r.type === "refund" ? "badge-refund" : "badge-sale";
      const t  = r.at ? new Date(r.at).toLocaleTimeString("en-GB",{hour:"2-digit",minute:"2-digit"}) : "—";
      return `<td class="td-mono td-bold">${escHtml(r.ref || "—")}</td>
              <td>${escHtml(r.customer)}</td>
              <td class="td-mono ${tc}">${fmt(Math.abs(r.total))}</td>
              <td><span class="badge ${bc}">${escHtml(r.method)}</span></td>
              <td class="td-mono text-muted">${t}</td>`;
    },
  );
}

function renderTable(tbodyId, rows, headers, rowFn, trAttrFn, emptyMsg) {
  const tbody = document.getElementById(tbodyId);
  if (!tbody) return;
  const thead = tbody.previousElementSibling;

  if (thead && thead.tagName === "THEAD") {
    thead.innerHTML = "<tr>" + headers.join("") + "</tr>";
  }

  if (!rows.length) {
    const cols = headers.length;
    tbody.innerHTML = `<tr class="empty-row"><td colspan="${cols}">${emptyMsg || "No data for this period."}</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map(r => {
    const attr = trAttrFn ? trAttrFn(r) : "";
    return `<tr ${attr}>${rowFn(r)}</tr>`;
  }).join("");
}

// ── assistant drawer ───────────────────────────────────────────────────
function openDrawer() {
  _drawerOpen = true;
  document.getElementById("assistant-drawer").classList.add("open");
  document.cookie = "drawer_open=1;path=/;max-age=86400";
}
function closeDrawer() {
  _drawerOpen = false;
  document.getElementById("assistant-drawer").classList.remove("open");
  document.cookie = "drawer_open=0;path=/;max-age=86400";
}

function askAssistant(question) {
  openDrawer();
  document.getElementById("chat-input").value = question;
  sendChat(question);
}

async function sendChat(questionOverride) {
  const input = document.getElementById("chat-input");
  const q     = questionOverride || input.value.trim();
  if (!q) return;
  input.value = "";

  appendChatMsg("user", q);

  const typing = appendChatMsg("assistant", "…");
  try {
    const r = await fetch("/dashboard/assistant", {
      method:  "POST",
      headers: {"Content-Type":"application/json"},
      body:    JSON.stringify({ question: q }),
    });
    const data = await r.json();
    typing.textContent = data.answer || data.message || JSON.stringify(data);
  } catch (err) {
    typing.textContent = "Error: " + err.message;
  }
}

function appendChatMsg(role, text) {
  const body = document.getElementById("chat-body");
  const el   = document.createElement("div");
  el.className = "chat-msg " + role;
  el.textContent = text;
  body.appendChild(el);
  body.scrollTop = body.scrollHeight;
  return el;
}

// ── data fetch + render ────────────────────────────────────────────────
async function loadSummary() {
  let url = `/dashboard/summary?range=${_currentRange}`;
  if (_currentRange === "custom" && _customStart && _customEnd) {
    url += `&start=${_customStart}&end=${_customEnd}`;
  }

  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();

    _heroType = data.hero_type || "admin";
    const hero = data.hero || {};
    if (_heroType === "cashier")      renderHeroCashier(hero);
    else if (_heroType === "farm_manager") renderHeroFarm(hero);
    else renderHeroAdmin(hero);

    renderMainChart(data.chart || {}, data.range?.label);
    renderPanels(data.panels || {});

    // Range label
    const rl = document.getElementById("range-label");
    if (rl) rl.textContent = data.range?.label || "";

    _lastData  = data;
    _firstLoad = false;
    startElapsedTimer();

    document.getElementById("loading")?.remove();
  } catch (err) {
    console.error("Dashboard load error:", err);
    const l = document.getElementById("loading");
    if (l) l.innerHTML = `<div style="color:var(--error)">Failed to load dashboard. Please refresh.</div>`;
  }
}

// ── 60s auto-refresh ───────────────────────────────────────────────────
function startAutoRefresh() {
  _refreshTimer = setInterval(() => {
    if (!document.hidden) loadSummary();
  }, 60_000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && _lastUpdated && Date.now() - _lastUpdated > 60_000) {
      loadSummary();
    }
  });
}

// ── init ───────────────────────────────────────────────────────────────
async function initDashboard() {
  // Restore color mode
  const isLight = localStorage.getItem("colorMode") === "light";
  document.body.classList.toggle("light", isLight);
  document.getElementById("mode-btn").textContent = isLight ? "☀️" : "🌙";

  // Restore drawer state
  if (document.cookie.includes("drawer_open=1")) openDrawer();

  updateDateDisplay();
  initTabs();

  await initUser();
  await Promise.all([loadSummary(), loadInsights()]);

  startAutoRefresh();

  // Chat send button
  document.getElementById("chat-send")?.addEventListener("click", () => sendChat());
  document.getElementById("chat-input")?.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
  });
}

window.addEventListener("load", initDashboard);
