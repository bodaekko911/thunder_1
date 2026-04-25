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
let assistantRequestInFlight = false;
const AI_ASSISTANT_MAX_QUESTION_CHARS = 500;
const AI_ASSISTANT_CONTEXT_LIST_LIMIT = 5;

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
  if (dashboardData) renderChart();
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

function periodDescriptor() {
  const range = dashboardData?.range || {};
  if (range.days === 1) return "day";
  if (range.days) return `${range.days} days`;
  return "period";
}

function currentThemeValue() {
  return window.__appTheme ? window.__appTheme.get() : (document.documentElement.dataset.theme || "dark");
}

function chartLabels(buckets) {
  const granularity = dashboardData?.range?.granularity || "day";
  return buckets.map((bucket) => {
    const date = new Date(`${bucket.date}T12:00:00`);
    if (granularity === "month") return date.toLocaleDateString("en-GB", { month: "short", year: "numeric" });
    if (granularity === "week") return date.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
    return date.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  });
}

function quarterTickIndexes(length) {
  if (length <= 1) return new Set([0]);
  const max = length - 1;
  return new Set([
    0,
    Math.round(max * 0.25),
    Math.round(max * 0.75),
    max,
  ]);
}

function getPaceInsight() {
  return (dashboardData?.insights || []).find((item) => item.kind === "pace") || null;
}

function setEmptyState(isEmpty) {
  const briefing = document.getElementById("briefing-container");
  const trend = document.getElementById("trend-section");
  const stats = document.getElementById("editorial-stats");
  const bestsellers = document.getElementById("bestsellers-column");

  briefing.style.display = isEmpty ? "block" : "none";
  trend.style.display = isEmpty ? "none" : "block";
  stats.style.display = isEmpty ? "none" : "grid";
  bestsellers.style.display = isEmpty ? "none" : "block";
}

function renderHero() {
  const rangeLabel = dashboardData?.range?.label || "This period";
  const sales = dashboardData?.numbers?.sales || {};
  const shiftOverride = dashboardData?.viewer?.can_view_b2b === false ? dashboardData?.viewer?.alt_sales_today : null;
  const useShiftValue = shiftOverride && Number(shiftOverride.value || 0) > 0;
  const heroValue = useShiftValue ? Number(shiftOverride.value || 0) : Number(sales.value || 0);
  const prevValue = Number(sales.prev_value || 0);
  const deltaPct = sales.delta_pct;
  const emptySalesState = !useShiftValue && Number(sales.value || 0) <= 0;

  document.getElementById("hero-eyebrow").textContent = useShiftValue
    ? "YOUR SHIFT TODAY"
    : `NET SALES · ${String(rangeLabel).toUpperCase()}`;
  document.getElementById("hero-sales-value").textContent = formatMoney(heroValue);

  const chip = document.getElementById("hero-sales-chip");
  if (useShiftValue || deltaPct === null || deltaPct === undefined) {
    chip.style.display = "none";
  } else {
    chip.style.display = "inline-flex";
    chip.textContent = `${deltaPct >= 0 ? "↑" : "↓"} ${Math.abs(Number(deltaPct)).toFixed(1)}%`;
    chip.className = `hero-chip ${deltaPct >= 0 ? "positive" : "negative"}`;
  }

  const narrative = document.getElementById("hero-narrative");
  if (useShiftValue) {
    narrative.textContent = "Your total sales recorded so far in this shift.";
  } else if (emptySalesState || prevValue <= 0) {
    narrative.textContent = "You haven't recorded any sales yet for this period.";
  } else {
    const diff = Math.abs(heroValue - prevValue);
    const direction = heroValue >= prevValue ? "more" : "less";
    const paceClause = getPaceInsight() ? " Momentum is stronger in the back half of the period." : "";
    narrative.textContent = `That's ${formatMoney(diff)} ${direction} than the previous ${periodDescriptor()}.${paceClause}`;
  }

  setEmptyState(emptySalesState);
}

function renderEditorialStats() {
  const owe = dashboardData?.numbers?.clients_owe || {};
  const spent = dashboardData?.numbers?.spent || {};
  const margin = dashboardData?.numbers?.margin || {};

  document.getElementById("ed-owe-value").textContent = formatMoney(owe.value || 0);
  document.getElementById("ed-owe-prose").textContent = `Across your B2B ledger. ${formatNumber(owe.overdue_count || 0)} invoices over 30 days old.`;

  document.getElementById("ed-spent-value").textContent = formatMoney(spent.value || 0);
  document.getElementById("ed-spent-prose").textContent = spent.delta_pct === null || spent.delta_pct === undefined
    ? "No comparison available yet."
    : `${spent.delta_pct >= 0 ? "Up" : "Down"} ${Math.abs(Number(spent.delta_pct)).toFixed(1)}% on the previous period.`;

  const marginValue = document.getElementById("ed-margin-value");
  const marginProse = document.getElementById("ed-margin-prose");
  if (margin.value_pct === null || margin.value_pct === undefined) {
    marginValue.textContent = "—";
    marginProse.textContent = "No cost data on items yet.";
    return;
  }

  marginValue.textContent = `${Number(margin.value_pct).toFixed(1)}%`;
  marginProse.textContent = margin.delta_pts === null || margin.delta_pts === undefined
    ? "No comparison available yet."
    : `${Number(margin.delta_pts) >= 0 ? "Improved" : "Fell"} ${Math.abs(Number(margin.delta_pts)).toFixed(1)} points vs last period.`;
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

function renderChart() {
  const chartSection = document.getElementById("trend-section");
  if (chartSection.style.display === "none") {
    if (salesChart) {
      salesChart.destroy();
      salesChart = null;
    }
    return;
  }

  const buckets = dashboardData?.chart?.buckets || [];
  const labels = chartLabels(buckets);
  const data = buckets.map((bucket) => (
    Number(bucket.pos || 0) + Number(bucket.b2b || 0) + Number(bucket.refunds || 0)
  ));
  const tickIndexes = quarterTickIndexes(labels.length);
  const styles = getComputedStyle(document.documentElement);
  const accent = styles.getPropertyValue("--accent").trim();
  const fill = styles.getPropertyValue("--chart-fill").trim();
  const textMuted = styles.getPropertyValue("--text-muted").trim();

  if (!salesChart) {
    salesChart = new Chart(document.getElementById("sales-chart"), {
      type: "line",
      data: {
        labels,
        datasets: [{
          data,
          fill: true,
          borderColor: accent,
          backgroundColor: fill,
          tension: 0.35,
          borderWidth: 2,
          pointRadius: 0,
          pointHoverRadius: 4,
          pointHitRadius: 18,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            displayColors: false,
            callbacks: {
              label(context) {
                return formatMoneyPrecise(context.parsed.y || 0);
              },
            },
          },
        },
        scales: {
          x: {
            grid: { display: false, drawBorder: false },
            border: { display: false },
            ticks: {
              color: textMuted,
              maxRotation: 0,
              autoSkip: false,
              font: { family: "DM Sans", size: 11 },
              callback(value, index) {
                return tickIndexes.has(index) ? labels[index] : "";
              },
            },
          },
          y: {
            display: false,
            grid: { display: false, drawBorder: false },
            border: { display: false },
          },
        },
      },
    });
    return;
  }

  salesChart.data.labels = labels;
  salesChart.data.datasets[0].data = data;
  salesChart.data.datasets[0].borderColor = accent;
  salesChart.data.datasets[0].backgroundColor = fill;
  salesChart.options.scales.x.ticks.color = textMuted;
  salesChart.options.scales.x.ticks.callback = function(value, index) {
    return tickIndexes.has(index) ? labels[index] : "";
  };
  salesChart.update("none");
}

function renderTopProducts() {
  const products = dashboardData?.panels?.top_products_by_revenue || [];
  const container = document.getElementById("top-products-list");
  document.getElementById("top-products-title").textContent = `Best-sellers · ${dashboardData?.range?.label || "This period"}`;
  if (!products.length) {
    container.innerHTML = `<div class="empty-state">No products sold in this range.</div>`;
    return;
  }

  container.innerHTML = products.slice(0, 5).map((product) => `
    <div class="list-row">
      <div class="row-main">
        <span class="row-title">${escHtml(product.name)}</span>
        <span class="row-units">${formatNumber(product.qty || 0)} units</span>
      </div>
      <span class="row-value">${formatMoney(product.revenue || 0)}</span>
    </div>
  `).join("");
}

function highlightInsightText(kind, text) {
  let html = escHtml(text);
  if (kind === "overdue") {
    html = html.replace(/^([^<]+?) hasn&#39;t paid/, "<strong class=\"accent\">$1</strong> hasn&#39;t paid");
    html = html.replace(/invoice (#\S+)/, "invoice <strong>$1</strong>");
  } else if (kind === "stockout") {
    html = html.replace(/^(\d+ products)/, "<strong>$1</strong>");
    html = html.replace(/recently\. ([^.]+?) has been/, "recently. <strong class=\"accent\">$1</strong> has been");
  } else if (kind === "pace") {
    html = html.replace(/last (\d+ days)/, "last <strong>$1</strong>");
    html = html.replace(/(\d+(\.\d+)?%)/, "<strong class=\"accent\">$1</strong>");
  } else if (kind === "margin") {
    html = html.replace(/(\d+(\.\d+)? points)/, "<strong class=\"accent\">$1</strong>");
  } else if (kind === "weekday") {
    html = html.replace(/^([A-Za-z]+s)/, "<strong class=\"accent\">$1</strong>");
    html = html.replace(/overtaking ([A-Za-z]+s)/, "overtaking <strong>$1</strong>");
  }
  return html;
}

function renderInsights() {
  const insights = dashboardData?.insights || [];
  const container = document.getElementById("insights-list");
  if (!insights.length) {
    container.innerHTML = `<p class="insight-text">Nothing notable to flag for this period.</p>`;
    return;
  }

  container.innerHTML = insights.map((item) => (
    `<p class="insight-text">${highlightInsightText(item.kind, item.text)}</p>`
  )).join("");
}

function renderRecentActivity() {
  const rows = dashboardData?.panels?.recent_activity || [];
  const tbody = document.getElementById("recent-activity");
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="4" class="empty-cell">No activity in this range.</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map((item) => `
    <tr data-link="${escHtml(item.link || "#")}">
      <td class="mono">${escHtml(item.invoice_number || "-")}</td>
      <td>${escHtml(item.customer || "-")}</td>
      <td class="${item.type === "refund" ? "negative" : "positive"}">${item.type === "refund" ? `−${escHtml(formatMoney(Math.abs(item.total || 0)))}` : escHtml(formatMoney(item.total || 0))}</td>
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

function bindChatEvents() {
  const trigger = document.getElementById("ai-chat-trigger");
  const widget = document.getElementById("ai-chat-widget");
  const closeBtn = document.getElementById("ai-chat-close");
  const sendBtn = document.getElementById("ai-chat-send");
  const input = document.getElementById("ai-chat-input");
  const body = document.getElementById("ai-chat-body");

  if (!trigger || !widget) return;

  input.maxLength = AI_ASSISTANT_MAX_QUESTION_CHARS;

  trigger.addEventListener("click", () => widget.classList.remove("hidden"));
  closeBtn.addEventListener("click", () => widget.classList.add("hidden"));

  function trimList(items, keys) {
    if (!Array.isArray(items)) return [];
    return items.slice(0, AI_ASSISTANT_CONTEXT_LIST_LIMIT).map((item) => {
      const next = {};
      if (!item || typeof item !== "object") return next;
      keys.forEach((key) => {
        if (Object.prototype.hasOwnProperty.call(item, key)) next[key] = item[key];
      });
      return next;
    });
  }

  function buildAssistantContext() {
    const context = {
      range: currentRange,
      start: currentRange === "custom" ? customStart : undefined,
      end: currentRange === "custom" ? customEnd : undefined,
    };
    if (!dashboardData) return context;

    if (dashboardData.range) {
      context.range = dashboardData.range.key || dashboardData.range.label || currentRange;
      context.start = dashboardData.range.date_from || context.start;
      context.end = dashboardData.range.date_to || context.end;
    }
    if (dashboardData.numbers) {
      context.numbers = {
        sales: dashboardData.numbers.sales,
        clients_owe: dashboardData.numbers.clients_owe,
        spent: dashboardData.numbers.spent,
        stock_alerts: dashboardData.numbers.stock_alerts,
      };
    }
    if (dashboardData.panels) {
      context.panels = {
        top_products_by_revenue: trimList(dashboardData.panels.top_products_by_revenue, ["name", "qty", "revenue"]),
        top_products_by_qty: trimList(dashboardData.panels.top_products_by_qty, ["name", "qty", "revenue"]),
        recent_activity: trimList(dashboardData.panels.recent_activity, ["invoice_number", "customer", "total", "type", "time_relative"]),
      };
    }
    if (dashboardData.briefing) {
      context.briefing = {
        lead: dashboardData.briefing.lead,
        body: dashboardData.briefing.body,
      };
    }
    return context;
  }

  const sendCopilotQuestion = async () => {
    const text = input.value.trim();
    if (!text || assistantRequestInFlight) return;
    if (text.length > AI_ASSISTANT_MAX_QUESTION_CHARS) {
      body.innerHTML += `<div class="chat-bubble ai error">Questions must be ${AI_ASSISTANT_MAX_QUESTION_CHARS} characters or fewer.</div>`;
      body.scrollTop = body.scrollHeight;
      return;
    }
    
    input.value = "";
    assistantRequestInFlight = true;
    sendBtn.disabled = true;
    input.disabled = true;
    body.innerHTML += `<div class="chat-bubble user">${escHtml(text)}</div>`;
    body.scrollTop = body.scrollHeight;
    
    const thinkingId = "thinking-" + Date.now();
    body.innerHTML += `<div id="${thinkingId}" class="chat-bubble ai thinking">Thinking...</div>`;
    body.scrollTop = body.scrollHeight;
    
    try {
      const res = await fetch("/dashboard/assistant/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: text, dashboard_context: buildAssistantContext() })
      });
      if (!res.ok) {
        let errorMessage = "Failed to get response.";
        try {
          const errorPayload = await res.json();
          errorMessage = errorPayload.detail || errorPayload.content || errorMessage;
        } catch {}
        throw new Error(errorMessage);
      }
      const data = await res.json();
      document.getElementById(thinkingId).remove();
      body.innerHTML += `<div class="chat-bubble ai">${escHtml(data.content)}</div>`;
    } catch (err) {
      document.getElementById(thinkingId).remove();
      body.innerHTML += `<div class="chat-bubble ai error">${escHtml(err.message || "Failed to get response.")}</div>`;
    } finally {
      assistantRequestInFlight = false;
      sendBtn.disabled = false;
      input.disabled = false;
      input.focus();
    }
    body.scrollTop = body.scrollHeight;
  };

  sendBtn.addEventListener("click", sendCopilotQuestion);
  input.addEventListener("keypress", (e) => { if (e.key === "Enter") sendCopilotQuestion(); });
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
  bindChatEvents();
  startAutoRefresh();
}

window.addEventListener("load", initDashboard);
