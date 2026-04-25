let currentRange = localStorage.getItem("dashboard:range") || "mtd";
let customStart = null;
let customEnd = null;
let lastUpdatedAt = null;
let elapsedTimer = null;
let refreshTimer = null;
let salesChart = null;
let dashboardData = null;
let currentUser = null;
let dashboardAbortController = null;
let dashboardRequestId = 0;
let dashboardHasLoaded = false;

function escHtml(value) {
  return String(value || "").replace(/[&<>"']/g, (char) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]
  ));
}

function formatMoney(value) {
  return `EGP ${Math.round(Number(value || 0)).toLocaleString("en-GB")}`;
}

function formatMoneyPrecise(value) {
  return `EGP ${Number(value || 0).toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function formatNumber(value) {
  return Number(value || 0).toLocaleString("en-GB");
}

function longDateLabel() {
  return new Date().toLocaleDateString("en-GB", {
    weekday: "long",
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}

function greetingForHour(hour) {
  if (hour < 12) return "Good morning";
  if (hour < 17) return "Good afternoon";
  return "Good evening";
}

function setGreeting() {
  const name = (currentUser?.name || "there").split(" ")[0];
  const hour = new Date().getHours();
  document.getElementById("greeting").textContent = `${greetingForHour(hour)}, ${name}`;
  document.getElementById("date-display").textContent = longDateLabel();
}

function setTheme(theme) {
  if (window.__appTheme) {
    window.__appTheme.set(theme);
    return;
  }
  document.documentElement.dataset.theme = theme;
  document.body.classList.toggle("light", theme === "light");
  localStorage.setItem("colorMode", theme);
  document.getElementById("mode-btn").innerHTML = theme === "light" ? "&#9728;&#65039;" : "&#127769;";
  if (salesChart) salesChart.update("none");
}

function toggleTheme() {
  setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
}

function initTheme() {
  if (window.__appTheme) {
    window.__appTheme.sync();
    return;
  }
  setTheme(localStorage.getItem("colorMode") || "dark");
}

function refreshThemeUi() {
  const theme = window.__appTheme ? window.__appTheme.get() : (document.documentElement.dataset.theme || "dark");
  document.getElementById("mode-btn").innerHTML = theme === "light" ? "&#9728;&#65039;" : "&#127769;";
  if (salesChart) salesChart.update("none");
}

function updateRangeButtons() {
  document.querySelectorAll(".range-btn").forEach((button) => {
    button.classList.toggle("active", button.dataset.range === currentRange);
  });
}

function openCustomRangePicker() {
  document.getElementById("custom-range-modal").classList.remove("hidden");
  document.getElementById("custom-range-start").value = customStart || "";
  document.getElementById("custom-range-end").value = customEnd || "";
  setCustomRangeError("");
}

function closeCustomRangePicker() {
  document.getElementById("custom-range-modal").classList.add("hidden");
  setCustomRangeError("");
}

function setCustomRangeError(message) {
  const error = document.getElementById("custom-range-error");
  error.hidden = !message;
  error.textContent = message;
}

function applyCustomRange() {
  const start = document.getElementById("custom-range-start").value;
  const end = document.getElementById("custom-range-end").value;
  if (!start || !end) {
    setCustomRangeError("Choose both dates.");
    return;
  }
  if (start > end) {
    setCustomRangeError("Start date must come first.");
    return;
  }
  customStart = start;
  customEnd = end;
  currentRange = "custom";
  localStorage.setItem("dashboard:range", currentRange);
  updateRangeButtons();
  closeCustomRangePicker();
  loadDashboard();
}

function markUpdated() {
  clearInterval(elapsedTimer);
  lastUpdatedAt = Date.now();
  const node = document.getElementById("last-updated");
  const tick = () => {
    if (!node) return;
    const seconds = Math.max(0, Math.round((Date.now() - lastUpdatedAt) / 1000));
    node.textContent = `Updated ${seconds}s ago`;
  };
  tick();
  elapsedTimer = setInterval(tick, 5000);
}

function numberDeltaText(_metric, data) {
  if (data?.delta_pct === null || data?.delta_pct === undefined) return "No comparison yet";
  const rounded = Math.abs(Number(data.delta_pct)).toFixed(1).replace(".0", "");
  const direction = Number(data.delta_pct) >= 0 ? "up" : "down";
  return `${direction === "up" ? "↑" : "↓"} ${rounded}% vs last period`;
}

function tooltipForCard(key) {
  const tips = {
    sales: "Total money coming in from completed sales, after refunds. Does not include unpaid invoices.",
    clients_owe: "B2B clients with unpaid or partially-paid invoices. The overdue number counts those more than 30 days old.",
    spent: "All recorded expenses for the period - electricity, rent, supplies, salaries, and more.",
    stock_alerts: "Products that are out of stock or nearly out.",
    sales_today: "Money taken by the current cashier today.",
  };
  return tips[key] || "";
}

function renderHero() {
  const rangeLabel = dashboardData?.range?.label || "This period";
  const isAlt = dashboardData?.viewer?.can_view_b2b === false && dashboardData?.viewer?.alt_sales_today;
  
  let value = 0;
  if (isAlt) {
      value = dashboardData?.viewer?.alt_sales_today?.value || 0;
      document.getElementById("hero-eyebrow").textContent = "YOUR SHIFT TODAY";
  } else {
      value = dashboardData?.numbers?.sales?.value || 0;
      document.getElementById("hero-eyebrow").textContent = `NET SALES · ${rangeLabel.toUpperCase()}`;
  }
  
  document.getElementById("hero-sales-value").textContent = formatMoney(value);
  
  const prevValue = dashboardData?.numbers?.sales?.prev_value || 0;
  const deltaPct = dashboardData?.numbers?.sales?.delta_pct;
  const chip = document.getElementById("hero-sales-chip");
  
  if (isAlt || deltaPct === null || deltaPct === undefined) {
      chip.style.display = "none";
  } else {
      chip.style.display = "inline-flex";
      chip.textContent = `${deltaPct >= 0 ? "↑" : "↓"} ${Math.abs(deltaPct).toFixed(1)}%`;
      chip.className = `hero-chip ${deltaPct >= 0 ? "positive" : "negative"}`;
  }
  
  const narrative = document.getElementById("hero-narrative");
  if (isAlt) {
      narrative.textContent = "Your total sales recorded so far in this shift.";
  } else if (value === 0 || prevValue === 0) {
      narrative.textContent = "You haven't recorded any sales yet for this period.";
  } else {
      const diff = Math.abs(value - prevValue);
      const direction = value >= prevValue ? "more" : "less";
      let paceText = "";
      const paceInsight = (dashboardData?.insights || []).find(i => i.kind === "pace");
      if (paceInsight) {
          paceText = ` ${paceInsight.text}`;
      }
      narrative.innerHTML = `That's ${formatMoney(diff)} ${direction} than the previous period.${paceText}`;
  }
  
  if (value === 0 && !isAlt) {
      document.getElementById("trend-section").style.display = "none";
      document.getElementById("editorial-stats").style.display = "none";
      document.getElementById("briefing-container").style.display = "block";
  } else {
      document.getElementById("trend-section").style.display = "block";
      document.getElementById("editorial-stats").style.display = "flex";
      document.getElementById("briefing-container").style.display = "none";
  }
}

function renderEditorialStats() {
  const owe = dashboardData?.numbers?.clients_owe || {};
  const spent = dashboardData?.numbers?.spent || {};
  const margin = dashboardData?.numbers?.margin || {};
  
  document.getElementById("ed-owe-value").textContent = formatMoney(owe.value || 0);
  document.getElementById("ed-owe-prose").textContent = `${formatNumber(owe.overdue_count || 0)} invoices over 30 days old.`;
  
  document.getElementById("ed-spent-value").textContent = formatMoney(spent.value || 0);
  let spentDelta = "";
  if (spent.delta_pct !== null && spent.delta_pct !== undefined) {
      spentDelta = `${spent.delta_pct >= 0 ? "Up" : "Down"} ${Math.abs(spent.delta_pct).toFixed(1)}% on the previous period.`;
  } else {
      spentDelta = "No comparison available.";
  }
  document.getElementById("ed-spent-prose").textContent = spentDelta;
  
  const marginValue = document.getElementById("ed-margin-value");
  const marginProse = document.getElementById("ed-margin-prose");
  if (margin.value_pct === null || margin.value_pct === undefined) {
      marginValue.textContent = "—";
      marginProse.textContent = "No cost data on items yet.";
  } else {
      marginValue.textContent = `${margin.value_pct.toFixed(1)}%`;
      if (margin.delta_pts !== null && margin.delta_pts !== undefined) {
          marginProse.textContent = `${margin.delta_pts >= 0 ? "Improved" : "Fell"} ${Math.abs(margin.delta_pts).toFixed(1)} points vs last period.`;
      } else {
          marginProse.textContent = "No comparison available.";
      }
  }
}

function renderBriefing() {
  const briefing = dashboardData?.briefing || {};
  document.getElementById("briefing-lead").textContent = briefing.lead || "You haven't recorded any sales yet for this period.";
  document.getElementById("briefing-body").textContent = briefing.body || "";
  const actionsNode = document.getElementById("briefing-actions");
  const actions = briefing.actions || [];
  if (!actions.length) {
    actionsNode.innerHTML = "";
    return;
  }
  actionsNode.innerHTML = actions.map((action) => (
    `<a class="briefing-action" href="${escHtml(action.link)}"><span>${escHtml(action.text)}</span><strong>${escHtml(action.cta)} →</strong></a>`
  )).join("");
}

function chartLabels(buckets) {
  const granularity = dashboardData?.range?.granularity || "day";
  return buckets.map((bucket) => {
    const date = new Date(`${bucket.date}T12:00:00`);
    if (granularity === "month") return date.toLocaleDateString("en-GB", { month: "short", year: "numeric" });
    if (granularity === "week") return `Week of ${date.toLocaleDateString("en-GB", { day: "numeric", month: "short" })}`;
    return date.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  });
}

function renderChart() {
  const buckets = dashboardData?.chart?.buckets || [];
  const labels = chartLabels(buckets);
  const data = buckets.map((bucket) => (Number(bucket.pos || 0) + Number(bucket.b2b || 0) + Number(bucket.refunds || 0)));
  
  if (!salesChart) {
    salesChart = new Chart(document.getElementById("sales-chart"), {
      type: "line",
      data: {
        labels: labels,
        datasets: [{
          data: data,
          fill: true,
          borderColor: getComputedStyle(document.documentElement).getPropertyValue("--accent").trim(),
          backgroundColor: getComputedStyle(document.documentElement).getPropertyValue("--accent-soft").trim(),
          tension: 0.35,
          pointRadius: 0,
          pointHoverRadius: 4
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: "index", intersect: false },
        plugins: { legend: { display: false } },
        scales: {
          x: {
            grid: { display: false, drawBorder: false },
            ticks: {
              callback: function(value, index, values) {
                if (index === 0 || index === values.length - 1 || index === Math.floor(values.length / 2)) {
                  return this.getLabelForValue(value);
                }
                return null;
              },
              maxRotation: 0,
              color: getComputedStyle(document.documentElement).getPropertyValue("--text-muted").trim(),
              font: { family: "DM Sans", size: 11 }
            }
          },
          y: { display: false }
        }
      },
    });
    return;
  }
  salesChart.data.labels = labels;
  salesChart.data.datasets[0].data = data;
  salesChart.data.datasets[0].borderColor = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim();
  salesChart.data.datasets[0].backgroundColor = getComputedStyle(document.documentElement).getPropertyValue("--accent-soft").trim();
  salesChart.update("none");
}

function renderTopProducts() {
  const products = dashboardData?.panels?.top_products_by_revenue || [];
  const container = document.getElementById("top-products-list");
  if (!products.length) {
    container.innerHTML = `<div class="empty-state">No products sold in this range.</div>`;
    return;
  }
  container.innerHTML = products.slice(0, 5).map((product) => {
    return `
      <div class="list-row top-product-row">
        <div class="row-main">
          <span class="row-title">${escHtml(product.name)}</span>
          <span class="row-units">${formatNumber(product.qty || 0)} units</span>
        </div>
        <span class="row-value">${formatMoney(product.revenue || 0)}</span>
      </div>
    `;
  }).join("");
}

function renderInsights() {
  const insights = (dashboardData?.insights || []).filter(i => i.kind !== "pace");
  const container = document.getElementById("insights-list");
  if (!insights.length) {
      container.innerHTML = `<p class="insight-text">Nothing notable to flag for this period.</p>`;
      return;
  }
  container.innerHTML = insights.map(i => {
      return `<p class="insight-text">${i.text}</p>`;
  }).join("");
}

function renderRecentActivity() {
  const rows = (dashboardData?.panels?.recent_activity || []);
  const tbody = document.getElementById("recent-activity");
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="4" class="empty-cell">No activity in this range.</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map((item) => `
    <tr data-link="${escHtml(item.link || "#")}">
      <td class="mono">${escHtml(item.invoice_number || "-")}</td>
      <td>${escHtml(item.customer || "-")}</td>
      <td class="${item.type === "refund" ? "negative" : "positive"}">${escHtml(item.type === "refund" ? `−${formatMoney(Math.abs(item.total || 0))}` : formatMoney(item.total || 0))}</td>
      <td>${escHtml(item.time_relative || "-")}</td>
    </tr>
  `).join("");
  tbody.querySelectorAll("tr[data-link]").forEach((row) => {
    row.addEventListener("click", () => {
      const link = row.dataset.link;
      if (link && link !== "#") window.location.assign(link);
    });
  });
}

function showErrorState(message) {
  document.getElementById("loading").classList.remove("hidden");
  document.getElementById("loading").innerHTML = `<div class="load-error">${escHtml(message)}</div>`;
}

async function loadDashboard() {
  if (dashboardAbortController) dashboardAbortController.abort();
  dashboardAbortController = new AbortController();
  const requestId = ++dashboardRequestId;
  let url = `/dashboard/summary?range=${currentRange}`;
  if (currentRange === "custom" && customStart && customEnd) {
    url += `&start=${customStart}&end=${customEnd}`;
  }
  try {
    const response = await fetch(url, {
      credentials: "same-origin",
      signal: dashboardAbortController.signal,
    });
    if (!response.ok) throw new Error(`Dashboard request failed (${response.status})`);
    const nextData = await response.json();
    if (requestId !== dashboardRequestId) return;
    dashboardData = nextData;
    if (!dashboardHasLoaded) {
      document.getElementById("loading").classList.add("hidden");
      dashboardHasLoaded = true;
    }
    renderBriefing();
    renderHero();
    renderEditorialStats();
    renderChart();
    renderTopProducts();
    renderInsights();
    renderRecentActivity();
    markUpdated();
  } catch (error) {
    if (error.name === "AbortError") return;
    showErrorState(error.message);
  }
}

async function initUser() {
  try {
    const response = await fetch("/auth/me");
    if (response.ok) currentUser = await response.json();
  } catch {}
  const name = currentUser?.name || "Admin";
  const email = currentUser?.email || "-";
  const avatar = (name.trim()[0] || "A").toUpperCase();
  document.getElementById("user-name").textContent = name;
  document.getElementById("user-email").textContent = email;
  document.getElementById("user-avatar").textContent = avatar;
  setGreeting();
}

function bindEvents() {
  if (!window.__appNav) {
    document.getElementById("mode-btn").addEventListener("click", toggleTheme);
  }
  window.addEventListener("app:themechange", refreshThemeUi);
  if (!window.__appNav) {
    document.getElementById("account-trigger").addEventListener("click", (event) => {
      event.stopPropagation();
      const trigger = document.getElementById("account-trigger");
      const dropdown = document.getElementById("account-dropdown");
      const open = dropdown.classList.toggle("open");
      trigger.classList.toggle("open", open);
      trigger.setAttribute("aria-expanded", open ? "true" : "false");
    });
    document.getElementById("signout-btn").addEventListener("click", async () => {
      await fetch("/auth/logout", { method: "POST" });
      window.location.href = "/";
    });
    document.addEventListener("click", (event) => {
      const dropdown = document.getElementById("account-dropdown");
      const trigger = document.getElementById("account-trigger");
      if (dropdown.contains(event.target) || trigger.contains(event.target)) return;
      dropdown.classList.remove("open");
      trigger.classList.remove("open");
      trigger.setAttribute("aria-expanded", "false");
    });
  }
  document.querySelectorAll(".range-btn").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.dataset.range === "custom") {
        openCustomRangePicker();
        return;
      }
      currentRange = button.dataset.range;
      localStorage.setItem("dashboard:range", currentRange);
      updateRangeButtons();
      loadDashboard();
    });
  });
  document.getElementById("range-modal-close").addEventListener("click", closeCustomRangePicker);
  document.getElementById("range-cancel").addEventListener("click", closeCustomRangePicker);
  document.getElementById("range-apply").addEventListener("click", applyCustomRange);
  document.getElementById("custom-range-modal").addEventListener("click", (event) => {
    if (event.target.id === "custom-range-modal") closeCustomRangePicker();
  });
}

function startAutoRefresh() {
  refreshTimer = setInterval(() => {
    if (!document.hidden) loadDashboard();
  }, 60000);
}

async function initDashboard() {
  initTheme();
  refreshThemeUi();
  updateRangeButtons();
  bindEvents();
  await initUser();
  await loadDashboard();
  startAutoRefresh();
}

window.addEventListener("load", initDashboard);
