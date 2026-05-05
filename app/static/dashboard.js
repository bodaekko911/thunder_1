let currentRange = localStorage.getItem("dashboard:range") || "mtd";
let customStart = null;
let customEnd = null;
let lastUpdatedAt = null;
let elapsedTimer = null;
let refreshTimer = null;
let salesChart = null;
let topProductsTab = "revenue";
let activityFilter = "all";
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
  if (node) node.classList.remove("last-updated-error");
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

function cardSpec(key) {
  const rangeLabel = dashboardData?.range?.label || "this period";
  if (key === "sales") {
    return {
      label: dashboardData?.range?.label === "Today" ? "Sales today" : `Sales ${rangeLabel.toLowerCase()}`,
      value: formatMoney(dashboardData?.numbers?.sales?.value || 0),
      meta: numberDeltaText("sales", dashboardData?.numbers?.sales),
      sparkline: dashboardData?.numbers?.sales?.sparkline || [],
      tooltip: tooltipForCard("sales"),
    };
  }
  if (key === "clients_owe" && !(dashboardData?.viewer?.can_view_b2b)) {
    return {
      label: "Sales today",
      value: formatMoney(dashboardData?.viewer?.alt_sales_today?.value || 0),
      meta: "Your shift total so far",
      sparkline: [],
      tooltip: tooltipForCard("sales_today"),
    };
  }
  if (key === "clients_owe") {
    return {
      label: "Money clients owe you",
      value: formatMoney(dashboardData?.numbers?.clients_owe?.value || 0),
      meta: `${formatNumber(dashboardData?.numbers?.clients_owe?.overdue_count || 0)} overdue`,
      sparkline: [],
      tooltip: tooltipForCard("clients_owe"),
    };
  }
  if (key === "spent") {
    return {
      label: dashboardData?.range?.label === "Today" ? "Money you've spent today" : `Money you've spent ${rangeLabel.toLowerCase()}`,
      value: formatMoney(dashboardData?.numbers?.spent?.value || 0),
      meta: numberDeltaText("spent", dashboardData?.numbers?.spent),
      sparkline: dashboardData?.numbers?.spent?.sparkline || [],
      tooltip: tooltipForCard("spent"),
    };
  }
  if (key === "b2b_cash") {
    const val = dashboardData?.numbers?.b2b_cash?.value || 0;
    const periodLabel = currentRange === "today" ? "today" : rangeLabel.toLowerCase();
    return {
      label: `B2B cash collected ${periodLabel}`,
      value: formatMoney(val),
      meta: "",
      sparkline: [],
      tooltip: "Total cash actually collected from B2B clients (payments received on invoices).",
    };
  }
  // stock_alerts fallback
  return {
    label: "Stock alerts",
    value: `${formatNumber(dashboardData?.numbers?.stock_alerts?.value || 0)} items`,
    meta: `${formatNumber(dashboardData?.numbers?.stock_alerts?.out_count || 0)} out · ${formatNumber(dashboardData?.numbers?.stock_alerts?.low_count || 0)} low`,
    sparkline: [],
    tooltip: tooltipForCard("stock_alerts"),
  };
}

function sparklineBars(values) {
  if (!values?.length) return "";
  const max = Math.max(...values, 1);
  return values.map((value) => `<span style="height:${Math.max(6, Math.round((value / max) * 40))}px"></span>`).join("");
}

function renderNumbers() {
  ["sales", "clients_owe", "b2b_cash", "spent", "stock_alerts"].forEach((key) => {
    const node = document.querySelector(`[data-card="${key}"]`);
    const spec = cardSpec(key);
    // On first render, build the structure once
    let btn = node.querySelector(".number-card-button");
    if (!btn) {
      node.innerHTML = `
        <div class="number-card-button">
          <span class="number-label"></span>
          <strong class="number-value"></strong>
          <span class="number-meta"></span>
          <div class="number-sparkline-or-breakdown"></div>
        </div>
      `;
      btn = node.querySelector(".number-card-button");
    }
    // Update text in-place — no DOM teardown, no flicker
    btn.dataset.tooltip = spec.tooltip;
    btn.querySelector(".number-label").textContent = spec.label;
    btn.querySelector(".number-value").textContent = spec.value;
    btn.querySelector(".number-meta").textContent = spec.meta;
    const extra = btn.querySelector(".number-sparkline-or-breakdown");
    if (spec.sparkline.length) {
      extra.className = "sparkline-bars";
      extra.innerHTML = sparklineBars(spec.sparkline);
    } else if (spec.meta) {
      extra.className = "number-breakdown";
      extra.textContent = spec.meta;
    } else {
      extra.className = "number-breakdown";
      extra.textContent = "";
    }
  });
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

function chartTitle() {
  const label = dashboardData?.range?.label || "This period";
  return `Sales over time — ${label}`;
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
  document.getElementById("chart-title").textContent = chartTitle();
  document.getElementById("chart-table").innerHTML = `
    <tr><th>Date</th><th>POS</th><th>B2B</th><th>Refunds</th><th>Orders</th></tr>
    ${buckets.map((bucket) => `<tr><td>${bucket.date}</td><td>${formatMoneyPrecise(bucket.pos)}</td><td>${formatMoneyPrecise(bucket.b2b)}</td><td>${formatMoneyPrecise(bucket.refunds)}</td><td>${bucket.orders}</td></tr>`).join("")}
  `;
  const accentColor = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim();
  const chartData = {
    labels: chartLabels(buckets),
    datasets: [
      { label: "POS", data: buckets.map((b) => b.pos), backgroundColor: accentColor, stack: "sales" },
      { label: "B2B", data: buckets.map((b) => b.b2b), backgroundColor: "#3b5f8a", stack: "sales" },
      { label: "Refunds", data: buckets.map((b) => b.refunds), backgroundColor: "#b54040", stack: "sales" },
    ],
  };
  const tooltipAfterBody = (items) => {
    const bucket = buckets[items[0]?.dataIndex || 0];
    return [`Transactions: ${bucket?.orders || 0}`];
  };
  if (!salesChart) {
    salesChart = new Chart(document.getElementById("sales-chart"), {
      type: "bar",
      data: chartData,
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: true, position: "top", align: "end" },
          tooltip: { callbacks: { afterBody: tooltipAfterBody } },
        },
        scales: {
          x: { stacked: true, grid: { display: false } },
          y: { stacked: true, grid: { display: false }, ticks: { display: false }, border: { display: false } },
        },
      },
    });
    // don't return — fall through so first render is complete
    return;
  }
  salesChart.data.labels = chartData.labels;
  salesChart.data.datasets.forEach((dataset, i) => {
    dataset.data = chartData.datasets[i].data;
    dataset.backgroundColor = chartData.datasets[i].backgroundColor;
  });
  salesChart.options.plugins.tooltip.callbacks.afterBody = tooltipAfterBody;
  salesChart.update("none");
}

function topProductsTitle() {
  const label = dashboardData?.range?.label || "This period";
  return `Best-sellers ${label.toLowerCase()}`;
}

function renderTopProducts() {
  document.getElementById("top-products-title").textContent = topProductsTitle();
  const key = topProductsTab === "revenue" ? "top_products_by_revenue" : "top_products_by_qty";
  const products = dashboardData?.panels?.[key] || [];
  const maxValue = Math.max(...products.map((product) => topProductsTab === "revenue" ? Number(product.revenue || 0) : Number(product.qty || 0)), 1);
  const container = document.getElementById("top-products-list");
  if (!products.length) {
    container.innerHTML = `<div class="empty-state">No products sold in this range.</div>`;
    return;
  }
  container.innerHTML = products.map((product) => {
    const value = topProductsTab === "revenue" ? Number(product.revenue || 0) : Number(product.qty || 0);
    const label = topProductsTab === "revenue" ? formatMoney(value) : `${formatNumber(value)} units`;
    const width = Math.max(8, Math.round((value / maxValue) * 100));
    return `
      <div class="list-row top-product-row">
        <div class="row-main">
          <span class="row-title">${escHtml(product.name)}</span>
          <span class="row-value">${escHtml(label)}</span>
        </div>
        <span class="row-bar"><span style="width:${width}%"></span></span>
      </div>
    `;
  }).join("");
}

function renderRecentActivity() {
  const rows = (dashboardData?.panels?.recent_activity || []).filter((item) => activityFilter === "all" ? true : item.type === activityFilter);
  const tbody = document.getElementById("recent-activity");
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="4" class="empty-cell">No activity in this range.</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map((item) => `
    <tr data-link="${escHtml(item.link || "#")}">
      <td class="mono">${escHtml(item.invoice_number || "-")}</td>
      <td>${escHtml(item.customer || "-")}</td>
      <td class="${item.type === "refund" ? "negative" : "positive"}">${escHtml(item.type === "refund" ? `-${formatMoney(Math.abs(item.total || 0))}` : formatMoney(item.total || 0))}</td>
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
  if (dashboardHasLoaded) {
    const node = document.getElementById("last-updated");
    if (node) {
      node.textContent = "Refresh failed — retrying";
      node.classList.add("last-updated-error");
    }
    return;
  }
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
    renderNumbers();
    renderChart();
    renderTopProducts();
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
  document.querySelectorAll("[data-top-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      topProductsTab = button.dataset.topTab;
      document.querySelectorAll("[data-top-tab]").forEach((item) => item.classList.toggle("active", item === button));
      renderTopProducts();
    });
  });
  document.querySelectorAll("[data-activity-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      activityFilter = button.dataset.activityFilter;
      document.querySelectorAll("[data-activity-filter]").forEach((item) => item.classList.toggle("active", item === button));
      renderRecentActivity();
    });
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