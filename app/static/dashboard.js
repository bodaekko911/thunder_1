/* Thunder ERP Dashboard v3 — executive/calm redesign */

// ── State ─────────────────────────────────────────────────────────────────
let _currentRange  = localStorage.getItem("dashboard:range") || "mtd";
let _customStart   = null;
let _customEnd     = null;
let _lastData      = null;
let _refreshTimer  = null;
let _insightsTimer = null;
let _elapsedTimer  = null;
let _lastUpdated   = null;
let _firstLoad     = true;
let _revenueChart  = null;
let _heroType      = "admin";
let _topTab        = "rev";
let _drawerOpen    = false;
let _allInsights   = [];
let _insightsExpanded = false;
const _sparkCharts = {};

// ── Formatters ────────────────────────────────────────────────────────────
function fmt(val) {
  return "EGP\u00a0" + Number(val || 0).toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtHero(val) {
  return "EGP\u00a0" + Math.round(Number(val || 0)).toLocaleString("en-GB");
}
function fmtNum(val) {
  return Number(val || 0).toLocaleString("en-GB");
}
function pctStr(v) {
  if (v === null || v === undefined) return "—";
  return (v > 0 ? "+" : "") + v + "%";
}
function relativeTime(isoStr) {
  if (!isoStr) return "—";
  const s = Math.floor((Date.now() - new Date(isoStr).getTime()) / 1000);
  if (s < 60)              return s + "s ago";
  if (s < 3600)            return Math.floor(s / 60) + "m ago";
  if (s < 86400)           return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}
function escHtml(str) {
  return String(str || "").replace(/[&<>"']/g, c =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function truncate(str, n) {
  return str.length > n ? str.slice(0, n) + "…" : str;
}

// ── Theme ─────────────────────────────────────────────────────────────────
function toggleMode() {
  const html = document.documentElement;
  const isDark = html.dataset.theme === "dark";
  html.dataset.theme = isDark ? "light" : "dark";
  localStorage.setItem("dashboard:theme", html.dataset.theme);
  document.getElementById("mode-btn").textContent = isDark ? "\u2600" : "\u263E";
  if (_revenueChart) _revenueChart.update();
}

function initTheme() {
  const saved = localStorage.getItem("dashboard:theme") || "light";
  document.documentElement.dataset.theme = saved;
  document.getElementById("mode-btn").textContent = saved === "dark" ? "\u263E" : "\u2600";
}

// ── Account menu ──────────────────────────────────────────────────────────
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

// ── User init ─────────────────────────────────────────────────────────────
async function initUser() {
  try {
    const r = await fetch("/auth/me");
    if (!r.ok) return;
    const u = await r.json();
    const name = u.name || "there";
    document.getElementById("user-name").textContent   = name;
    document.getElementById("user-avatar").textContent = name[0].toUpperCase();
    document.getElementById("greeting").textContent    = buildGreeting(name);
    if (u.role === "cashier" && _firstLoad) _currentRange = "today";
    updateRangePickerActive();
  } catch {}
}

function buildGreeting(name) {
  const h = new Date().getHours();
  const g = h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
  return g + ", " + name.split(" ")[0];
}

function updateDateDisplay() {
  const el = document.getElementById("date-display");
  if (el) el.textContent = new Date().toLocaleDateString("en-GB", {
    weekday: "long", year: "numeric", month: "long", day: "numeric",
  });
}

// ── Elapsed timer ─────────────────────────────────────────────────────────
function markUpdated() {
  clearInterval(_elapsedTimer);
  _lastUpdated = Date.now();
  const el = document.getElementById("last-updated");
  function tick() {
    const sec = Math.round((Date.now() - _lastUpdated) / 1000);
    if (el) el.textContent = "\u21bb Updated " + sec + "s ago";
  }
  tick();
  _elapsedTimer = setInterval(tick, 5000);
}

// ── Range picker ──────────────────────────────────────────────────────────
function updateRangePickerActive() {
  document.querySelectorAll(".range-btn").forEach(b => {
    b.classList.toggle("active", b.dataset.range === _currentRange);
  });
}

function onRangeChange(range) {
  if (range === "custom") { openCustomRangePicker(); return; }
  _currentRange = range;
  localStorage.setItem("dashboard:range", range);
  updateRangePickerActive();
  loadDashboard();
}

document.querySelectorAll(".range-btn").forEach(b =>
  b.addEventListener("click", () => onRangeChange(b.dataset.range))
);

function openCustomRangePicker() {
  const modal = document.getElementById("custom-range-modal");
  if (!modal) return;
  document.getElementById("custom-range-start").value = _customStart || "";
  document.getElementById("custom-range-end").value   = _customEnd   || "";
  setCustomRangeError("");
  modal.classList.remove("hidden");
}
function closeCustomRangePicker() {
  document.getElementById("custom-range-modal")?.classList.add("hidden");
  setCustomRangeError("");
}
function setCustomRangeError(msg) {
  const el = document.getElementById("custom-range-error");
  if (!el) return;
  el.textContent = msg || "";
  el.hidden = !msg;
}
function applyCustomRange() {
  const start = document.getElementById("custom-range-start")?.value || "";
  const end   = document.getElementById("custom-range-end")?.value   || "";
  if (!start || !end) { setCustomRangeError("Choose both a start and end date."); return; }
  if (start > end)    { setCustomRangeError("Start must be before or equal to end."); return; }
  _customStart = start; _customEnd = end;
  _currentRange = "custom";
  localStorage.setItem("dashboard:range", "custom");
  updateRangePickerActive();
  closeCustomRangePicker();
  loadDashboard();
}

// ── Count-up animation ────────────────────────────────────────────────────
function countUpMoney(el, target, duration = 400) {
  if (!_firstLoad) { el.textContent = fmtHero(target); return; }
  let start = null;
  const step = ts => {
    if (!start) start = ts;
    const pct  = Math.min((ts - start) / duration, 1);
    const ease = 1 - Math.pow(1 - pct, 2);
    el.textContent = fmtHero(target * ease);
    if (pct < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}
function countUpNum(el, target, suffix, duration = 400) {
  if (!_firstLoad) { el.textContent = fmtNum(Math.round(target)) + (suffix || ""); return; }
  let start = null;
  const step = ts => {
    if (!start) start = ts;
    const pct  = Math.min((ts - start) / duration, 1);
    const ease = 1 - Math.pow(1 - pct, 2);
    el.textContent = fmtNum(Math.round(target * ease)) + (suffix || "");
    if (pct < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}
function flashTint(el) {
  if (_firstLoad || !el) return;
  el.classList.remove("flash-tint");
  void el.offsetWidth;
  el.classList.add("flash-tint");
}

// ── Sparklines ────────────────────────────────────────────────────────────
function drawSparkline(canvasId, data, color) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || !data?.length) return;
  if (_sparkCharts[canvasId]) { _sparkCharts[canvasId].destroy(); }
  _sparkCharts[canvasId] = new Chart(canvas.getContext("2d"), {
    type: "line",
    data: {
      labels: data.map((_, i) => i),
      datasets: [{ data, borderColor: color, borderWidth: 1.5, tension: .3, pointRadius: 0, fill: false }],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { display: false }, tooltip: { enabled: false } },
      scales: { x: { display: false }, y: { display: false } },
    },
  });
}

// ── Hero cards ────────────────────────────────────────────────────────────
function chipClass(dir) {
  return ({ up:"chip-up", down:"chip-down", bad_up:"chip-bad-up", bad_down:"chip-bad-down" })[dir] || "chip-flat";
}
function arrow(dir) {
  return ({ up:"\u2191", down:"\u2193", bad_up:"\u2191", bad_down:"\u2193", flat:"\u2014" })[dir] || "\u2014";
}

function setHeroCard(idx, { label, valueFn, chip, subtitle, sparkline, sparkColor }) {
  const labelEl = document.querySelector(`#hero-${idx} .hero-label`);
  if (labelEl && label) labelEl.textContent = label;

  const valEl = document.getElementById(`hero-${idx}-value`);
  if (valEl) {
    const prev = valEl.dataset.raw !== undefined ? parseFloat(valEl.dataset.raw) : undefined;
    valueFn(valEl);
    if (prev !== undefined) flashTint(valEl);
  }

  const chipEl = document.getElementById(`hero-${idx}-chip`);
  if (chipEl) {
    if (chip && chip.pct !== null && chip.pct !== undefined) {
      chipEl.className = "hero-chip " + chipClass(chip.dir);
      chipEl.textContent = arrow(chip.dir) + " " + pctStr(chip.pct);
      chipEl.setAttribute("aria-label", Math.abs(chip.pct) + "% vs prior period");
    } else {
      chipEl.className = "hero-chip chip-flat";
      chipEl.textContent = "\u2014";
    }
  }

  const subEl = document.getElementById(`hero-${idx}-sub`);
  if (subEl && subtitle !== undefined) subEl.textContent = subtitle;

  if (sparkline?.length && sparkColor) drawSparkline(`spark-${idx}`, sparkline, sparkColor);
}

function accentColor() {
  return getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#1f7a4d";
}

function renderHeroAdmin(hero) {
  if (!hero) return;
  const green = accentColor();

  setHeroCard(0, {
    label:      "Revenue",
    valueFn:    el => { countUpMoney(el, hero.revenue?.value || 0); el.dataset.raw = hero.revenue?.value || 0; },
    chip:       { dir: hero.revenue?.direction || "flat", pct: hero.revenue?.delta_pct },
    subtitle:   hero.revenue?.prior !== undefined ? "Prior: " + fmtHero(hero.revenue.prior) : "",
    sparkline:  hero.revenue?.sparkline,
    sparkColor: green,
  });

  setHeroCard(1, {
    label:      "Gross Profit",
    valueFn:    el => { countUpMoney(el, hero.gross_profit?.value || 0); el.dataset.raw = hero.gross_profit?.value || 0; },
    chip:       { dir: hero.gross_profit?.direction || "flat", pct: hero.gross_profit?.delta_pct },
    subtitle:   "Margin: " + (hero.gross_profit?.margin_pct ?? "\u2014") + "%",
    sparkline:  hero.gross_profit?.sparkline,
    sparkColor: green,
  });

  setHeroCard(2, {
    label:    "Cash Position",
    valueFn:  el => { countUpMoney(el, hero.cash_position?.value || 0); el.dataset.raw = hero.cash_position?.value || 0; },
    chip:     { dir: "flat", pct: null },
    subtitle: hero.cash_position?.ar !== undefined ? "AR: " + fmtHero(hero.cash_position.ar) : "",
  });

  setHeroCard(3, {
    label:    "New Customers",
    valueFn:  el => { countUpNum(el, hero.customer_growth?.value || 0); el.dataset.raw = hero.customer_growth?.value || 0; },
    chip:     { dir: hero.customer_growth?.direction || "flat", pct: hero.customer_growth?.delta_pct },
    subtitle: "Active total: " + fmtNum(hero.customer_growth?.total_active),
  });
}

function renderHeroCashier(hero) {
  if (!hero) return;
  setHeroCard(0, { label: "Shift Sales",   valueFn: el => { countUpMoney(el, hero.shift_sales?.value || 0); el.dataset.raw = 0; }, subtitle: (hero.shift_sales?.count || 0) + " invoices" });
  setHeroCard(1, { label: "Avg Basket",    valueFn: el => { countUpMoney(el, hero.avg_basket?.value   || 0); el.dataset.raw = 0; }, subtitle: "" });
  setHeroCard(2, { label: "Top Item",      valueFn: el => { el.textContent = hero.top_item?.name || "\u2014"; el.dataset.raw = 0; }, subtitle: "Most sold today" });
  setHeroCard(3, { label: "Refunds Today", valueFn: el => { countUpMoney(el, hero.refunds?.value || 0); el.dataset.raw = 0; }, chip: { dir: hero.refunds?.direction || "flat", pct: null }, subtitle: (hero.refunds?.count || 0) + " refund(s)" });
}

function renderHeroFarm(hero) {
  if (!hero) return;
  setHeroCard(0, { label: "Deliveries",  valueFn: el => { countUpNum(el, hero.deliveries?.value || 0); el.dataset.raw = 0; },         subtitle: "This period" });
  setHeroCard(1, { label: "Spoilage",    valueFn: el => { el.textContent = fmtNum(hero.spoilage?.qty || 0) + " kg"; el.dataset.raw = 0; }, subtitle: "This period" });
  setHeroCard(2, { label: "Batches",     valueFn: el => { countUpNum(el, hero.production_batches?.value || 0); el.dataset.raw = 0; }, subtitle: "" });
  setHeroCard(3, { label: "Upcoming",    valueFn: el => { el.textContent = "\u2014"; el.dataset.raw = 0; }, subtitle: hero.upcoming_deliveries?.note || "" });
}

// ── Chart ─────────────────────────────────────────────────────────────────
function fmtLabel(dateStr, granularity) {
  const d = new Date(dateStr + "T12:00:00");
  if (granularity === "month") return d.toLocaleString("en-GB", { month: "short", year: "2-digit" });
  return d.toLocaleString("en-GB", { day: "numeric", month: "short" });
}

function renderChart(chartData, rangeLabel) {
  const ctx = document.getElementById("main-chart");
  if (!ctx || !chartData) return;

  const isDark    = document.documentElement.dataset.theme === "dark";
  const tickColor = isDark ? "#636863" : "#9a9a92";
  const gran      = chartData.granularity || "day";
  const buckets   = chartData.buckets || [];
  const labels    = buckets.map(b => fmtLabel(b.date, gran));

  const title = document.getElementById("chart-title");
  if (title) title.textContent = "Revenue \u2014 " + (rangeLabel || "");

  if (_revenueChart) { _revenueChart.destroy(); _revenueChart = null; }
  _revenueChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [
        { label: "POS",     data: buckets.map(b => b.pos),     backgroundColor: "rgba(31,122,77,.75)", stack: "r", order: 2 },
        { label: "B2B",     data: buckets.map(b => b.b2b),     backgroundColor: "rgba(59,95,138,.70)", stack: "r", order: 2 },
        { label: "Refunds", data: buckets.map(b => b.refunds), backgroundColor: "rgba(181,64,64,.55)", stack: "r", order: 2 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      animation: { duration: _firstLoad ? 400 : 0 },
      plugins: {
        legend: { display: false },
        tooltip: {
          mode: "index", intersect: false,
          callbacks: {
            title: items => items[0]?.label || "",
            label: item => " " + item.dataset.label + ": " + fmt(item.raw),
            afterBody: items => {
              const b = buckets[items[0]?.dataIndex];
              return b ? ["Orders: " + (b.orders || 0)] : [];
            },
          },
        },
      },
      scales: {
        x: { stacked: true, grid: { display: false }, ticks: { color: tickColor, maxTicksLimit: 12, font: { size: 11 } } },
        y: { stacked: true, grid: { display: false }, ticks: { display: false } },
      },
      onClick: (_e, elements) => {
        if (!elements.length) return;
        const b = buckets[elements[0].index];
        if (b) { openDrawer(); setTimeout(() => askAssistant("show me sales on " + b.date), 60); }
      },
    },
  });

  // Custom inline legend
  const legendEl = document.getElementById("chart-legend");
  if (legendEl) {
    legendEl.innerHTML = [
      { color: "rgba(31,122,77,.75)", label: "POS" },
      { color: "rgba(59,95,138,.70)", label: "B2B" },
      { color: "rgba(181,64,64,.55)", label: "Refunds" },
    ].map(s =>
      `<span class="legend-item"><span class="legend-dot" style="background:${s.color}"></span>${s.label}</span>`
    ).join("");
  }

  // SR fallback table
  const tbl = document.getElementById("chart-table");
  if (tbl) {
    tbl.innerHTML = "<tr><th>Date</th><th>POS</th><th>B2B</th><th>Refunds</th><th>Orders</th></tr>"
      + buckets.map(b =>
        `<tr><td>${b.date}</td><td>${fmt(b.pos)}</td><td>${fmt(b.b2b)}</td><td>${fmt(b.refunds)}</td><td>${b.orders}</td></tr>`
      ).join("");
  }
}

// ── Top Products panel ────────────────────────────────────────────────────
let _topData = { by_revenue: [], by_qty: [] };

function renderTopProductsPanel(topProducts) {
  if (!topProducts) return;
  _topData = {
    by_revenue: topProducts.by_revenue || [],
    by_qty:     topProducts.by_qty     || [],
  };
  _renderTopList();
}

function _renderTopList() {
  const data = _topTab === "rev" ? _topData.by_revenue : _topData.by_qty;
  const container = document.getElementById("top-products-body");
  if (!container) return;

  if (!data.length) {
    container.innerHTML = `<div class="panel-empty">No sales data for this period.</div>`;
    return;
  }

  const maxVal = _topTab === "rev" ? (data[0]?.revenue || 1) : (data[0]?.qty || 1);
  container.innerHTML = `<div class="product-list">${data.slice(0, 8).map(r => {
    const val     = _topTab === "rev" ? (r.revenue || 0) : (r.qty || 0);
    const display = _topTab === "rev" ? fmt(r.revenue) : fmtNum(r.qty);
    const pct     = maxVal > 0 ? Math.min(val / maxVal * 100, 100) : 0;
    const safeName = escHtml(r.name || "");
    return `<div class="product-row" onclick="askAssistant('show product details for ${safeName}')"
        tabindex="0" role="button" aria-label="${safeName}: ${escHtml(display)}"
        onkeydown="if(event.key==='Enter'||event.key===' ')this.click()">
      <div class="product-info">
        <div class="product-name" title="${safeName}">${escHtml(truncate(r.name || "", 40))}</div>
        <div class="product-bar"><div class="product-bar-fill" style="width:${pct.toFixed(1)}%"></div></div>
      </div>
      <div class="product-value">${escHtml(display)}</div>
    </div>`;
  }).join("")}</div>`;
}

document.querySelectorAll(".tab-btn[data-tab]").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn[data-tab]").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    _topTab = btn.dataset.tab;
    _renderTopList();
  });
});

// ── Needs Attention panel ─────────────────────────────────────────────────
function renderNeedsAttention(cards) {
  _allInsights      = cards || [];
  _insightsExpanded = false;
  _renderAttention();
}

function _renderAttention() {
  const container = document.getElementById("needs-attention-body");
  if (!container) return;

  if (!_allInsights.length) {
    container.innerHTML = `<div class="panel-empty">Everything looks healthy.</div>`;
    return;
  }

  const shown = _insightsExpanded ? _allInsights : _allInsights.slice(0, 5);
  const extra = _allInsights.length - 5;

  container.innerHTML = shown.map(c => {
    const text   = c.text || "";
    const parts  = text.split("**");
    const title  = parts.length >= 2 ? parts[1] : parts[0];
    const rest   = parts.length >= 3 ? parts.slice(2).join("").replace(/^\s*[-\u2014]\s*/, "").trim() : "";
    const link   = c.action_url
      ? `<a class="attention-link" href="${escHtml(c.action_url)}">View \u2192</a>`
      : "";
    return `<div class="attention-item">
      <span class="attention-icon" aria-hidden="true">\u26a0</span>
      <div class="attention-content">
        <div class="attention-title">${escHtml(title)}</div>
        ${rest ? `<div class="attention-desc">${escHtml(rest)}</div>` : ""}
        ${link}
      </div>
    </div>`;
  }).join("") + (!_insightsExpanded && extra > 0
    ? `<button class="attention-more" onclick="_insightsExpanded=true;_renderAttention()">+${extra} more</button>`
    : "");
}

// ── Recent Activity ───────────────────────────────────────────────────────
function renderRecentActivity(items) {
  const tbody = document.getElementById("recent-activity");
  if (!tbody) return;

  if (!items?.length) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="5">No activity in this period.</td></tr>`;
    return;
  }

  tbody.innerHTML = items.slice(0, 10).map(r => {
    const isRef = r.type === "refund";
    const tc    = isRef ? "td-neg" : "td-pos";
    const val   = isRef ? "\u2212" + fmt(Math.abs(r.total)) : fmt(r.total);
    const ref   = escHtml(r.ref || r.invoice_number || "\u2014");
    const tag   = isRef ? `<span class="refund-tag">REFUND</span>` : "";
    const time  = relativeTime(r.at);
    return `<tr>
      <td class="td-mono td-bold">${ref}${tag}</td>
      <td>${escHtml(r.customer || "\u2014")}</td>
      <td class="${tc}">${val}</td>
      <td class="text-muted">${escHtml(r.method || "\u2014")}</td>
      <td class="text-muted">${time}</td>
    </tr>`;
  }).join("");
}

// ── Insights fetch ────────────────────────────────────────────────────────
async function loadInsights() {
  try {
    const r = await fetch("/dashboard/insights");
    if (!r.ok) { renderNeedsAttention([]); return; }
    const data = await r.json();
    renderNeedsAttention(data.cards || []);
    renderChips(data.suggested_chips || []);
  } catch {
    renderNeedsAttention([]);
  }
}

function renderChips(chips) {
  const el = document.getElementById("preset-chips");
  if (!el || !chips.length) return;
  el.innerHTML = chips.map(c =>
    `<button class="preset-chip" onclick="askAssistant(${JSON.stringify(c)})">${escHtml(c)}</button>`
  ).join("");
}

// ── Main load ─────────────────────────────────────────────────────────────
async function loadDashboard() {
  let url = `/dashboard/summary?range=${_currentRange}`;
  if (_currentRange === "custom" && _customStart && _customEnd) {
    url += `&start=${_customStart}&end=${_customEnd}`;
  }

  let res;
  try {
    res = await fetch(url, { credentials: "same-origin" });
  } catch (err) {
    showLoadError("Can't reach server", "Check your connection and try again.", err);
    return;
  }

  if (res.status === 401) {
    window.location.href = "/?next=" + encodeURIComponent(location.pathname);
    return;
  }
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    let d = null; try { d = JSON.parse(body); } catch {}
    const msg = d?.detail?.message || (typeof d?.detail === "string" ? d.detail : null)
      || body.slice(0, 300) || "Unknown server error";
    showLoadError(`Server error (HTTP ${res.status})`, msg, null);
    return;
  }

  let data;
  try { data = await res.json(); }
  catch (err) { showLoadError("Bad server response", "Response wasn't valid JSON.", err); return; }

  try {
    clearPartialErrorBanner();
    _heroType = data.hero_type || "admin";
    const hero = data.hero || {};
    if      (_heroType === "cashier")      renderHeroCashier(hero);
    else if (_heroType === "farm_manager") renderHeroFarm(hero);
    else                                   renderHeroAdmin(hero);

    renderChart(data.chart || {}, data.range?.label);
    renderTopProductsPanel(data.panels?.top_products || {});
    renderRecentActivity(data.panels?.recent_activity || []);

    _lastData  = data;
    _firstLoad = false;
    markUpdated();
    document.getElementById("loading")?.remove();

    if (data._errors?.length) renderPartialErrorBanner(data._errors);
  } catch (err) {
    showLoadError("Render error", "Data loaded but couldn't be displayed: " + err.message, err);
  }
}

// ── Error helpers ─────────────────────────────────────────────────────────
function showLoadError(title, detail, err) {
  if (err) console.error("[dashboard]", title, err);
  const el = document.getElementById("loading");
  if (!el) return;
  el.innerHTML = `
    <div style="max-width:520px;margin:80px auto;padding:24px;border:1px solid rgba(181,64,64,.3);border-radius:14px;background:rgba(181,64,64,.04)">
      <div style="color:var(--negative);font-weight:600;font-size:15px;margin-bottom:8px">\u26a0 ${escHtml(title)}</div>
      <div style="color:var(--text-sub);font-size:13px;line-height:1.6;margin-bottom:16px">${escHtml(detail)}</div>
      <div style="display:flex;gap:10px;flex-wrap:wrap">
        <button onclick="loadDashboard()" style="background:var(--accent);color:#fff;border:none;padding:9px 18px;border-radius:var(--radius);font-weight:600;cursor:pointer;font-size:13px">Retry</button>
        <a href="/home" style="color:var(--text-sub);padding:9px 16px;border:1px solid var(--border);border-radius:var(--radius);text-decoration:none;font-size:13px">Back to home</a>
      </div>
    </div>`;
}

function renderPartialErrorBanner(errors) {
  clearPartialErrorBanner();
  const sections = errors.map(e => e.section).join(", ");
  const banner = document.createElement("div");
  banner.className = "partial-error-banner";
  Object.assign(banner.style, {
    background: "rgba(166,116,24,.08)", border: "1px solid rgba(166,116,24,.3)",
    color: "var(--warning)", padding: "10px 16px", borderRadius: "var(--radius)",
    marginBottom: "4px", fontSize: "13px",
  });
  banner.textContent = `\u26a0 Some sections couldn't load: ${sections}. Shown data may be partial.`;
  document.querySelector(".content")?.prepend(banner);
}
function clearPartialErrorBanner() {
  document.querySelector(".partial-error-banner")?.remove();
}

// ── Assistant drawer ──────────────────────────────────────────────────────
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
  const typing = appendChatMsg("assistant", "\u2026");
  try {
    const r = await fetch("/dashboard/assistant", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q }),
    });
    const raw = await r.text();
    let data = null; try { data = raw ? JSON.parse(raw) : null; } catch {}
    if (!r.ok) { typing.textContent = data?.message || `Request failed (HTTP ${r.status}).`; return; }
    typing.textContent = data?.answer || data?.message || "I couldn't find a response for that.";
  } catch {
    typing.textContent = "Couldn't reach the assistant. Please try again.";
  }
}
function appendChatMsg(role, text) {
  const body = document.getElementById("chat-body");
  const el   = document.createElement("div");
  el.className   = "chat-msg " + role;
  el.textContent = text;
  body.appendChild(el);
  body.scrollTop = body.scrollHeight;
  return el;
}

// ── Auto-refresh ──────────────────────────────────────────────────────────
function startAutoRefresh() {
  _refreshTimer  = setInterval(() => { if (!document.hidden) loadDashboard(); }, 60_000);
  _insightsTimer = setInterval(() => { if (!document.hidden) loadInsights();  }, 300_000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && _lastUpdated && Date.now() - _lastUpdated > 60_000) loadDashboard();
  });
}

// ── Init ──────────────────────────────────────────────────────────────────
async function initDashboard() {
  initTheme();

  if (document.cookie.includes("drawer_open=1")) openDrawer();

  document.getElementById("custom-range-modal")?.addEventListener("click", e => {
    if (e.target?.id === "custom-range-modal") closeCustomRangePicker();
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") closeCustomRangePicker();
  });

  updateDateDisplay();
  updateRangePickerActive();

  await initUser();
  updateRangePickerActive();

  await Promise.all([loadDashboard(), loadInsights()]);

  startAutoRefresh();

  document.getElementById("chat-send")?.addEventListener("click", () => sendChat());
  document.getElementById("chat-input")?.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
  });
}

window.addEventListener("load", initDashboard);
