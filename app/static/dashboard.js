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
let assistantHistory = [];
let currentUser = null;
let loadingTimer = null;

const ASSISTANT_CHIPS = [
  "What did I sell today?",
  "What did I sell this month?",
  "Who owes me money?",
  "Which products are running low?",
  "Biggest customer this month?",
  "Best-selling products?",
  "How much did I spend this month?",
  "Show me sales this week",
  "Compare this month to last month",
  "How much stock do I have in total?",
];

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
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("dashboard:theme", theme);
  document.getElementById("mode-btn").textContent = theme === "dark" ? "☾" : "☀";
  if (salesChart) salesChart.update();
}

function toggleTheme() {
  setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
}

function initTheme() {
  setTheme(localStorage.getItem("dashboard:theme") || "light");
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

function numberDeltaText(metric, data) {
  if (data?.delta_pct === null || data?.delta_pct === undefined) return "No comparison yet";
  const rounded = Math.abs(Number(data.delta_pct)).toFixed(1).replace(".0", "");
  const direction = Number(data.delta_pct) >= 0 ? "up" : "down";
  const suffix = metric === "spent" ? "vs last period" : "vs last period";
  return `${direction === "up" ? "↑" : "↓"} ${rounded}% ${suffix}`;
}

function tooltipForCard(key) {
  const tips = {
    sales: "Total money coming in from completed sales, after refunds. Does not include unpaid invoices.",
    clients_owe: "B2B clients with unpaid or partially-paid invoices. The overdue number counts those more than 30 days old.",
    spent: "All recorded expenses for the period - electricity, rent, supplies, salaries, and more.",
    stock_alerts: "Products that are out of stock or nearly out. Click to see the list.",
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
      detail: "",
      sparkline: dashboardData?.numbers?.sales?.sparkline || [],
      click: () => submitAssistantQuestion(rangeLabel === "Today" ? "show me sales today" : `show me sales ${rangeLabel.toLowerCase()}`),
      tooltip: tooltipForCard("sales"),
    };
  }
  if (key === "clients_owe" && !(dashboardData?.viewer?.can_view_b2b)) {
    return {
      label: "Sales today",
      value: formatMoney(dashboardData?.viewer?.alt_sales_today?.value || 0),
      meta: "Your shift total so far",
      detail: "",
      sparkline: [],
      click: () => window.location.assign("/pos"),
      tooltip: tooltipForCard("sales_today"),
    };
  }
  if (key === "clients_owe") {
    return {
      label: "Money clients owe you",
      value: formatMoney(dashboardData?.numbers?.clients_owe?.value || 0),
      meta: `${formatNumber(dashboardData?.numbers?.clients_owe?.overdue_count || 0)} overdue`,
      detail: "",
      sparkline: [],
      click: () => window.location.assign("/b2b/?filter=outstanding"),
      tooltip: tooltipForCard("clients_owe"),
    };
  }
  if (key === "spent") {
    return {
      label: dashboardData?.range?.label === "Today" ? "Money you've spent today" : `Money you've spent ${rangeLabel.toLowerCase()}`,
      value: formatMoney(dashboardData?.numbers?.spent?.value || 0),
      meta: numberDeltaText("spent", dashboardData?.numbers?.spent),
      detail: "",
      sparkline: dashboardData?.numbers?.spent?.sparkline || [],
      click: () => window.location.assign(`/expenses/?range=${currentRange}`),
      tooltip: tooltipForCard("spent"),
    };
  }
  return {
    label: "Stock alerts",
    value: `${formatNumber(dashboardData?.numbers?.stock_alerts?.value || 0)} items`,
    meta: `${formatNumber(dashboardData?.numbers?.stock_alerts?.out_count || 0)} out · ${formatNumber(dashboardData?.numbers?.stock_alerts?.low_count || 0)} low`,
    detail: "",
    sparkline: [],
    click: () => window.location.assign("/inventory/?filter=low-stock"),
    tooltip: tooltipForCard("stock_alerts"),
  };
}

function sparklineBars(values) {
  if (!values?.length) return "";
  const max = Math.max(...values, 1);
  return values.map((value) => `<span style="height:${Math.max(6, Math.round((value / max) * 40))}px"></span>`).join("");
}

function renderNumbers() {
  ["sales", "clients_owe", "spent", "stock_alerts"].forEach((key) => {
    const node = document.querySelector(`[data-card="${key}"]`);
    const spec = cardSpec(key);
    node.innerHTML = `
      <button type="button" class="number-card-button" data-tooltip="${escHtml(spec.tooltip)}">
        <span class="number-label">${escHtml(spec.label)}</span>
        <strong class="number-value">${escHtml(spec.value)}</strong>
        <span class="number-meta">${escHtml(spec.meta)}</span>
        ${spec.sparkline.length ? `<div class="sparkline-bars">${sparklineBars(spec.sparkline)}</div>` : `<div class="number-breakdown">${escHtml(spec.meta)}</div>`}
      </button>
    `;
    node.querySelector("button").addEventListener("click", spec.click);
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
  if (salesChart) salesChart.destroy();
  salesChart = new Chart(document.getElementById("sales-chart"), {
    type: "bar",
    data: {
      labels: chartLabels(buckets),
      datasets: [
        { label: "POS", data: buckets.map((bucket) => bucket.pos), backgroundColor: getComputedStyle(document.documentElement).getPropertyValue("--accent").trim(), stack: "sales" },
        { label: "B2B", data: buckets.map((bucket) => bucket.b2b), backgroundColor: "#3b5f8a", stack: "sales" },
        { label: "Refunds", data: buckets.map((bucket) => bucket.refunds), backgroundColor: "#b54040", stack: "sales" },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: true, position: "top", align: "end" },
        tooltip: {
          callbacks: {
            afterBody(items) {
              const bucket = buckets[items[0]?.dataIndex || 0];
              return [`Transactions: ${bucket?.orders || 0}`];
            },
          },
        },
      },
      scales: {
        x: { stacked: true, grid: { display: false } },
        y: { stacked: true, grid: { display: false }, ticks: { display: false }, border: { display: false } },
      },
      onClick(_event, elements) {
        if (!elements.length) return;
        const bucket = buckets[elements[0].index];
        if (!bucket) return;
        submitAssistantQuestion(`show me sales on ${bucket.date}`);
      },
    },
  });
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
      <button type="button" class="list-row top-product-row" data-name="${escHtml(product.name)}">
        <div class="row-main">
          <span class="row-title">${escHtml(product.name)}</span>
          <span class="row-value">${escHtml(label)}</span>
        </div>
        <span class="row-bar"><span style="width:${width}%"></span></span>
      </button>
    `;
  }).join("");
  container.querySelectorAll(".top-product-row").forEach((button) => {
    button.addEventListener("click", () => submitAssistantQuestion(`show me details for ${button.dataset.name}`));
  });
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

function assistantResultParts(response) {
  const result = response?.result || {};
  return {
    message: result.message || response?.message || "No answer returned.",
    highlights: result.highlights || response?.highlights || [],
    table: result.table || response?.table || null,
    suggestions: result.suggestions || response?.suggestions || [],
  };
}

function renderHighlights(items) {
  if (!items?.length) return "";
  return `<div class="assistant-highlights">${items.map((item) => `
    <div class="assistant-stat">
      <span>${escHtml(item.label || "")}</span>
      <strong>${escHtml(item.value || "")}</strong>
    </div>
  `).join("")}</div>`;
}

function renderTable(table) {
  if (!table?.columns?.length || !table?.rows?.length) return "";
  return `
    <div class="assistant-table-wrap">
      <table class="assistant-table">
        <thead><tr>${table.columns.map((column) => `<th class="${column.align === "right" ? "right" : ""}">${escHtml(column.label)}</th>`).join("")}</tr></thead>
        <tbody>${table.rows.map((row) => `<tr>${table.columns.map((column) => `<td class="${column.align === "right" ? "right" : ""}">${escHtml(row[column.key])}</td>`).join("")}</tr>`).join("")}</tbody>
      </table>
    </div>
  `;
}

function renderSuggestionChips(items) {
  if (!items?.length) return "";
  return `<div class="assistant-followups">${items.map((item) => `<button type="button" class="chip-btn assistant-followup" data-question="${escHtml(item)}">${escHtml(item)}</button>`).join("")}</div>`;
}

function renderAssistantHistory() {
  const container = document.getElementById("assistantHistory");
  if (!assistantHistory.length) {
    container.innerHTML = `<div class="assistant-placeholder">Your recent questions will appear here.</div>`;
    return;
  }
  container.innerHTML = assistantHistory.map((entry, index) => {
    if (entry.loading) {
      return `
        <article class="assistant-entry">
          <div class="assistant-entry-head"><strong>${escHtml(entry.question)}</strong></div>
          <div class="assistant-loading"><span></span><span></span><span></span></div>
        </article>
      `;
    }
    const parts = assistantResultParts(entry.response);
    const unsupported = entry.response?.supported === false;
    const fallback = unsupported ? `<p class="assistant-fallback">I'm not sure how to answer that yet. Try one of these:</p>` : "";
    return `
      <article class="assistant-entry" id="assistant-entry-${index}">
        <div class="assistant-entry-head">
          <button type="button" class="assistant-jump" data-question="${escHtml(entry.question)}">← jump to question</button>
          <button type="button" class="assistant-copy" data-copy="${escHtml(parts.message)}">Copy</button>
        </div>
        <p class="assistant-question">${escHtml(entry.question)}</p>
        <div class="assistant-answer">
          <p class="assistant-message">${escHtml(parts.message)}</p>
          ${fallback}
          ${renderHighlights(parts.highlights)}
          ${renderTable(parts.table)}
          ${renderSuggestionChips(parts.suggestions)}
        </div>
      </article>
    `;
  }).join("");
  container.querySelectorAll(".assistant-jump").forEach((button) => {
    button.addEventListener("click", () => {
      document.getElementById("assistantInput").value = button.dataset.question || "";
      document.getElementById("assistantInput").focus();
    });
  });
  container.querySelectorAll(".assistant-copy").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(button.dataset.copy || "");
        button.textContent = "Copied";
        setTimeout(() => { button.textContent = "Copy"; }, 1000);
      } catch {}
    });
  });
  container.querySelectorAll(".assistant-followup").forEach((button) => {
    button.addEventListener("click", () => submitAssistantQuestion(button.dataset.question || ""));
  });
}

function pushAssistantEntry(question, response, loading = false) {
  if (loading) {
    assistantHistory.unshift({ question, loading: true, response: null });
  } else if (assistantHistory.length && assistantHistory[0].loading && assistantHistory[0].question === question) {
    assistantHistory[0] = { question, response, loading: false };
  } else {
    assistantHistory.unshift({ question, response, loading: false });
  }
  assistantHistory = assistantHistory.slice(0, 5);
  renderAssistantHistory();
}

async function submitAssistantQuestion(question) {
  const clean = String(question || "").trim();
  if (!clean) return;
  document.getElementById("assistantInput").value = clean;
  pushAssistantEntry(clean, null, true);
  clearTimeout(loadingTimer);
  loadingTimer = setTimeout(renderAssistantHistory, 300);
  try {
    const response = await fetch("/dashboard/assistant", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: clean }),
    });
    const payload = await response.json();
    pushAssistantEntry(clean, payload, false);
  } catch {
    pushAssistantEntry(clean, {
      supported: false,
      message: "I couldn't reach the assistant. Try again.",
      suggestions: ASSISTANT_CHIPS.slice(0, 3),
    }, false);
  }
}

function renderAssistantChips() {
  const container = document.getElementById("assistant-chips");
  container.innerHTML = ASSISTANT_CHIPS.map((question) => `<button type="button" class="chip-btn" data-question="${escHtml(question)}">${escHtml(question)}</button>`).join("");
  container.querySelectorAll(".chip-btn").forEach((button) => {
    button.addEventListener("click", () => submitAssistantQuestion(button.dataset.question || ""));
  });
}

function showErrorState(message) {
  document.getElementById("loading").innerHTML = `<div class="load-error">${escHtml(message)}</div>`;
}

async function loadDashboard() {
  let url = `/dashboard/summary?range=${currentRange}`;
  if (currentRange === "custom" && customStart && customEnd) {
    url += `&start=${customStart}&end=${customEnd}`;
  }
  try {
    const response = await fetch(url, { credentials: "same-origin" });
    if (!response.ok) throw new Error(`Dashboard request failed (${response.status})`);
    dashboardData = await response.json();
    document.getElementById("loading").style.display = "none";
    renderBriefing();
    renderNumbers();
    renderChart();
    renderTopProducts();
    renderRecentActivity();
    markUpdated();
  } catch (error) {
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
  document.getElementById("mode-btn").addEventListener("click", toggleTheme);
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
  document.getElementById("assistantSend").addEventListener("click", () => submitAssistantQuestion(document.getElementById("assistantInput").value));
  document.getElementById("assistantInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      submitAssistantQuestion(document.getElementById("assistantInput").value);
    }
  });
  document.getElementById("assistant-clear").addEventListener("click", () => {
    assistantHistory = [];
    renderAssistantHistory();
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

setTheme = function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("dashboard:theme", theme);
  document.getElementById("mode-btn").innerHTML = theme === "light" ? "&#9728;&#65039;" : "&#127769;";
  if (salesChart) salesChart.update();
};

async function initDashboard() {
  initTheme();
  updateRangeButtons();
  renderAssistantChips();
  renderAssistantHistory();
  bindEvents();
  await initUser();
  await loadDashboard();
  startAutoRefresh();
}

window.addEventListener("load", initDashboard);
