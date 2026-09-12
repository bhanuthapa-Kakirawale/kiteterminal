class SessionExpiredError extends Error {}

async function fetchJson(url) {
  const res = await fetch(url);
  if (res.status === 401) {
    const body = await res.json();
    throw new SessionExpiredError(body.detail.message);
  }
  if (!res.ok) {
    throw new Error(`Request failed: ${res.status}`);
  }
  return (await res.json()).data;
}

const API = {
  status: () => fetch("/api/session/status").then((r) => r.json()),
  config: () => fetch("/api/config").then((r) => r.json()),
  profile: () => fetchJson("/api/profile"),
  holdings: () => fetchJson("/api/holdings"),
  positions: () => fetchJson("/api/positions"),
  mfHoldings: () => fetchJson("/api/mf_holdings"),
  margins: () => fetchJson("/api/margins"),
};

const el = {
  connectGate: document.getElementById("connect-gate"),
  connectBtn: document.getElementById("connect-btn"),
  sessionExpiredBanner: document.getElementById("session-expired-banner"),
  reconnectBtn: document.getElementById("reconnect-btn"),
  genericError: document.getElementById("generic-error"),
  dashboard: document.getElementById("dashboard"),
  accountName: document.getElementById("account-name"),
  connectionStatus: document.getElementById("connection-status"),
  feedStatus: document.getElementById("feed-status"),
  refreshBtn: document.getElementById("refresh-btn"),
  alertBanner: document.getElementById("alert-banner"),
  heroMetrics: document.getElementById("hero-metrics"),
  holdingsBody: document.querySelector("#holdings-table tbody"),
  positionsBody: document.querySelector("#positions-table tbody"),
  mfBody: document.querySelector("#mf-table tbody"),
};

// Populated from GET /api/config at startup — see backend/config.py to tune.
let CONFIG = {
  holding_loss_alert_pct: 8,
  concentration_alert_pct: 20,
  day_pnl_alert_pct: 2,
  session_expiry_warning_minutes: 60,
  margin_alert_threshold_inr: 10000,
};

// Live in-memory state, kept up to date by REST load + WS tick patches.
const state = {
  holdings: [],
  positions: { net: [], day: [] },
  mf: [],
  margins: null,
  sessionStatus: null,
  sort: { key: null, dir: 1 },
};

function hideAllStates() {
  el.connectGate.hidden = true;
  el.sessionExpiredBanner.hidden = true;
  el.genericError.hidden = true;
  el.dashboard.hidden = true;
}

function showConnectGate() {
  hideAllStates();
  el.connectGate.hidden = false;
  el.refreshBtn.hidden = true;
  el.connectionStatus.classList.remove("status-connected", "status-reconnecting");
  el.connectionStatus.classList.add("status-disconnected");
  el.connectionStatus.title = "Disconnected";
}

function showSessionExpired() {
  hideAllStates();
  el.sessionExpiredBanner.hidden = false;
  el.refreshBtn.hidden = true;
  el.connectionStatus.classList.remove("status-connected", "status-reconnecting");
  el.connectionStatus.classList.add("status-disconnected");
  el.connectionStatus.title = "Session expired";
}

function showGenericError(message) {
  hideAllStates();
  el.genericError.hidden = false;
  el.genericError.textContent = `Couldn't load data: ${message}. Try refreshing.`;
  el.refreshBtn.hidden = false;
}

function showDashboard() {
  hideAllStates();
  el.dashboard.hidden = false;
  el.refreshBtn.hidden = false;
  el.connectionStatus.classList.remove("status-disconnected", "status-reconnecting");
  el.connectionStatus.classList.add("status-connected");
  el.connectionStatus.title = "Connected";
}

function pnlClass(value) {
  return value >= 0 ? "pnl-positive" : "pnl-negative";
}

function fmt(n, decimals = 2) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function flashCell(cell) {
  if (!cell) return;
  cell.classList.remove("flash-update");
  // Force reflow so the animation restarts on rapid consecutive ticks.
  void cell.offsetWidth;
  cell.classList.add("flash-update");
}

// ---- Derived view computation (single source of truth for table + alerts) ----

function computeHoldingsView(holdings) {
  const totalValue = holdings.reduce((sum, h) => sum + h.quantity * h.last_price, 0);
  return holdings.map((h) => {
    const value = h.quantity * h.last_price;
    const pnlPct = h.average_price > 0 ? (h.pnl / (h.average_price * h.quantity)) * 100 : 0;
    const lossPct = h.average_price > 0
      ? ((h.last_price - h.average_price) / h.average_price) * 100
      : 0;
    const pctOfPortfolio = totalValue > 0 ? (value / totalValue) * 100 : 0;
    return {
      ...h,
      value,
      pnl_pct: pnlPct,
      loss_pct: lossPct,
      pct_of_portfolio: pctOfPortfolio,
      breach_loss: lossPct <= -CONFIG.holding_loss_alert_pct,
      breach_concentration: pctOfPortfolio > CONFIG.concentration_alert_pct,
    };
  });
}

function minutesUntilMidnightIST() {
  const istMs = Date.now() + 5.5 * 3600 * 1000;
  const istDate = new Date(istMs);
  const msIntoDay =
    istDate.getUTCHours() * 3600000 +
    istDate.getUTCMinutes() * 60000 +
    istDate.getUTCSeconds() * 1000;
  return (24 * 3600000 - msIntoDay) / 60000;
}

function evaluateAlerts(view, totalValue, dayPnl, margins, sessionStatus) {
  const alerts = [];

  for (const h of view) {
    if (h.breach_loss) {
      alerts.push({
        id: `loss-${h.tradingsymbol}`,
        severity: "urgent",
        message: `${h.tradingsymbol} down ${Math.abs(h.loss_pct).toFixed(1)}% from avg cost`,
        scrollTargetSelector: `#holdings-row-${CSS.escape(h.tradingsymbol)}`,
      });
    }
    if (h.breach_concentration) {
      alerts.push({
        id: `conc-${h.tradingsymbol}`,
        severity: "warning",
        message: `${h.tradingsymbol} is ${h.pct_of_portfolio.toFixed(1)}% of portfolio value`,
        scrollTargetSelector: `#holdings-row-${CSS.escape(h.tradingsymbol)}`,
      });
    }
  }

  const dayPnlPct = totalValue > 0 ? (dayPnl / totalValue) * 100 : 0;
  if (dayPnlPct <= -CONFIG.day_pnl_alert_pct) {
    alerts.push({
      id: "day-pnl",
      severity: "urgent",
      message: `Day's P&L down ${Math.abs(dayPnlPct).toFixed(1)}% of portfolio value`,
      scrollTargetSelector: "#hero-metrics",
    });
  }

  if (sessionStatus?.connected) {
    const minsLeft = minutesUntilMidnightIST();
    if (minsLeft <= CONFIG.session_expiry_warning_minutes) {
      alerts.push({
        id: "session-expiry",
        severity: "warning",
        message: `Kite session expires in ~${Math.max(0, Math.round(minsLeft))} min`,
        scrollTargetSelector: "#topbar",
      });
    }
  } else if (sessionStatus && !sessionStatus.connected) {
    alerts.push({
      id: "session-expired",
      severity: "urgent",
      message: "Kite session expired",
      scrollTargetSelector: "#topbar",
    });
  }

  const availableMargin = margins?.equity?.net ?? 0;
  if (availableMargin < CONFIG.margin_alert_threshold_inr) {
    alerts.push({
      id: "margin-low",
      severity: "warning",
      message: `Available margin ₹${fmt(availableMargin, 0)} below threshold`,
      scrollTargetSelector: "#hero-metrics",
    });
  }

  return alerts;
}

function renderAlertBanner(alerts) {
  if (alerts.length === 0) {
    el.alertBanner.hidden = true;
    el.alertBanner.innerHTML = "";
    return;
  }
  el.alertBanner.hidden = false;
  el.alertBanner.innerHTML = alerts
    .map(
      (a) => `
      <div class="alert-chip ${a.severity}" data-scroll-target="${a.scrollTargetSelector}">
        <span class="icon">${a.severity === "urgent" ? "⛔" : "⚠"}</span>
        <span>${a.message}</span>
      </div>`
    )
    .join("");
  el.alertBanner.querySelectorAll(".alert-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const target = document.querySelector(chip.dataset.scrollTarget);
      if (target) {
        target.scrollIntoView({ behavior: "smooth", block: "center" });
        flashCell(target);
      }
    });
  });
}

// ---- Rendering ----

function renderCard(label, value, sub, colorClass, extraClass = "") {
  const card = document.createElement("div");
  card.className = `hero-card ${extraClass}`;
  card.innerHTML = `
    <div class="label">${label}</div>
    <div class="value ${colorClass || ""}">${value}</div>
    ${sub ? `<div class="sub ${colorClass || ""}">${sub}</div>` : ""}
  `;
  return card;
}

function renderHeroMetrics(view, margins) {
  el.heroMetrics.innerHTML = "";

  const portfolioValue = view.reduce((sum, h) => sum + h.value, 0);
  const totalPnl = view.reduce((sum, h) => sum + h.pnl, 0);
  const totalPnlPct = portfolioValue > 0 ? (totalPnl / (portfolioValue - totalPnl)) * 100 : 0;
  const dayPnl = view.reduce((sum, h) => sum + (h.day_change || 0) * h.quantity, 0);
  const dayPnlPct = portfolioValue > 0 ? (dayPnl / portfolioValue) * 100 : 0;
  const availableMargin = margins?.equity?.net ?? 0;

  el.heroMetrics.appendChild(renderCard("Portfolio Value", `₹${fmt(portfolioValue)}`));
  el.heroMetrics.appendChild(
    renderCard(
      "Total P&L",
      `₹${fmt(totalPnl)}`,
      `${totalPnl >= 0 ? "+" : ""}${fmt(totalPnlPct)}%`,
      pnlClass(totalPnl),
      "total-pnl"
    )
  );
  el.heroMetrics.appendChild(
    renderCard(
      "Day's P&L",
      `₹${fmt(dayPnl)}`,
      `${dayPnl >= 0 ? "+" : ""}${fmt(dayPnlPct)}%`,
      pnlClass(dayPnl)
    )
  );
  el.heroMetrics.appendChild(renderCard("Available Margin", `₹${fmt(availableMargin)}`));
}

function holdingRowHtml(h) {
  return `
    <td>${h.tradingsymbol}</td>
    <td class="num" data-field="quantity">${fmt(h.quantity, 0)}</td>
    <td class="num" data-field="average_price">${fmt(h.average_price)}</td>
    <td class="num" data-field="last_price">${fmt(h.last_price)}</td>
    <td class="num ${pnlClass(h.day_change_percentage)}" data-field="day_change_percentage">${fmt(h.day_change_percentage)}%</td>
    <td class="num ${pnlClass(h.pnl)}" data-field="pnl">${fmt(h.pnl)}</td>
    <td class="num ${pnlClass(h.pnl_pct)}" data-field="pnl_pct">${fmt(h.pnl_pct)}%</td>
  `;
}

function renderHoldingsTable(view) {
  el.holdingsBody.innerHTML = "";
  if (view.length === 0) {
    el.holdingsBody.innerHTML = `<tr class="empty-row"><td colspan="7">No holdings</td></tr>`;
    return;
  }
  for (const h of view) {
    const tr = document.createElement("tr");
    tr.id = `holdings-row-${h.tradingsymbol}`;
    tr.dataset.symbol = h.tradingsymbol;
    tr.dataset.instrumentToken = h.instrument_token;
    if (h.breach_loss) tr.classList.add("row-flagged");
    tr.innerHTML = holdingRowHtml(h);
    el.holdingsBody.appendChild(tr);
  }
}

function renderPositionsTable(positions) {
  el.positionsBody.innerHTML = "";
  const net = positions.net || [];
  if (net.length === 0) {
    el.positionsBody.innerHTML = `<tr class="empty-row"><td colspan="6">No open positions</td></tr>`;
    return;
  }
  for (const p of net) {
    const tr = document.createElement("tr");
    tr.dataset.symbol = p.tradingsymbol;
    tr.dataset.instrumentToken = p.instrument_token;
    tr.innerHTML = `
      <td>${p.tradingsymbol}</td>
      <td>${p.product}</td>
      <td class="num" data-field="quantity">${fmt(p.quantity, 0)}</td>
      <td class="num" data-field="average_price">${fmt(p.average_price)}</td>
      <td class="num" data-field="last_price">${fmt(p.last_price)}</td>
      <td class="num ${pnlClass(p.pnl)}" data-field="pnl">${fmt(p.pnl)}</td>
    `;
    el.positionsBody.appendChild(tr);
  }
}

function renderMfTable(mf) {
  el.mfBody.innerHTML = "";
  if (mf.length === 0) {
    el.mfBody.innerHTML = `<tr class="empty-row"><td colspan="6">No mutual fund holdings</td></tr>`;
    return;
  }
  for (const f of mf) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${f.fund}</td>
      <td>${f.folio}</td>
      <td class="num">${fmt(f.quantity)}</td>
      <td class="num">${fmt(f.average_price)}</td>
      <td class="num">${fmt(f.last_price)}</td>
      <td class="num ${pnlClass(f.pnl)}">${fmt(f.pnl)}</td>
    `;
    el.mfBody.appendChild(tr);
  }
}

function renderAccountName(profile) {
  el.accountName.textContent = profile?.user_name ? `· ${profile.user_name}` : "";
}

function renderAll() {
  const view = computeHoldingsView(state.holdings);
  const portfolioValue = view.reduce((sum, h) => sum + h.value, 0);
  const dayPnl = view.reduce((sum, h) => sum + (h.day_change || 0) * h.quantity, 0);

  renderHeroMetrics(view, state.margins);
  renderHoldingsTable(sortRows(view, state.sort.key, state.sort.dir));
  renderPositionsTable(state.positions);
  renderMfTable(state.mf);

  const alerts = evaluateAlerts(view, portfolioValue, dayPnl, state.margins, state.sessionStatus);
  renderAlertBanner(alerts);
}

// ---- Sorting ----

function sortRows(rows, key, dir) {
  if (!key) return rows;
  return [...rows].sort((a, b) => {
    const av = a[key];
    const bv = b[key];
    if (typeof av === "string") return av.localeCompare(bv) * dir;
    return (av - bv) * dir;
  });
}

function setupSortHandlers() {
  document.querySelectorAll("#holdings-table th[data-sort-key]").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.sortKey;
      if (state.sort.key === key) {
        state.sort.dir *= -1;
      } else {
        state.sort.key = key;
        state.sort.dir = 1;
      }
      document
        .querySelectorAll("#holdings-table th")
        .forEach((h) => h.classList.remove("sorted-asc", "sorted-desc"));
      th.classList.add(state.sort.dir === 1 ? "sorted-asc" : "sorted-desc");
      renderAll();
    });
  });
}

// ---- Data loading ----

async function loadDashboard() {
  try {
    const [profile, holdings, positions, mf, margins] = await Promise.all([
      API.profile(),
      API.holdings(),
      API.positions(),
      API.mfHoldings(),
      API.margins(),
    ]);
    state.holdings = holdings;
    state.positions = positions;
    state.mf = mf;
    state.margins = margins;
    renderAccountName(profile);
    renderAll();
    showDashboard();
    connectTickWs();
  } catch (e) {
    if (e instanceof SessionExpiredError) {
      showSessionExpired();
    } else {
      showGenericError(e.message);
    }
  }
}

// ---- Live WebSocket ticks (Part C) ----

const RECONNECT_DELAYS = [1000, 2000, 4000, 8000, 16000, 30000];
let reconnectAttempt = 0;
let ws = null;
let wsOpenedAt = 0;
let wsIntentionallyClosed = false;

function markStale(isStale) {
  document.querySelectorAll('[data-field="last_price"], [data-field="day_change_percentage"], [data-field="pnl"], [data-field="pnl_pct"]')
    .forEach((cell) => cell.classList.toggle("stale", isStale));
}

function setFeedStatus(reconnecting) {
  el.feedStatus.hidden = !reconnecting;
  el.connectionStatus.classList.toggle("status-reconnecting", reconnecting);
  el.connectionStatus.classList.toggle("status-connected", !reconnecting);
}

function applyTick(tick) {
  const row = document.querySelector(`tr[data-instrument-token="${tick.instrument_token}"]`);
  if (!row) return;

  const holding = state.holdings.find((h) => h.instrument_token === tick.instrument_token);
  if (holding) {
    holding.last_price = tick.last_price;
    if (tick.close_price) {
      holding.day_change = tick.last_price - tick.close_price;
      holding.day_change_percentage = (holding.day_change / tick.close_price) * 100;
    }
    holding.pnl = (holding.last_price - holding.average_price) * holding.quantity;
  }

  const position = state.positions.net?.find((p) => p.instrument_token === tick.instrument_token);
  if (position) {
    position.last_price = tick.last_price;
    position.pnl = (position.last_price - position.average_price) * position.quantity;
  }
}

function connectTickWs() {
  wsIntentionallyClosed = false;
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${protocol}//${location.host}/ws/ticks`);

  ws.onopen = () => {
    console.log("[ws] connected");
    wsOpenedAt = Date.now();
    setFeedStatus(false);
    markStale(false);
  };

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "auth_error") {
      wsIntentionallyClosed = true;
      ws.close();
      showSessionExpired();
      return;
    }
    if (msg.type === "ticks") {
      for (const tick of msg.data) {
        applyTick(tick);
        document
          .querySelectorAll(`tr[data-instrument-token="${tick.instrument_token}"] .num`)
          .forEach(flashCell);
      }
      renderAll();
    }
  };

  ws.onclose = (event) => {
    console.warn("[ws] closed", event.code, event.reason);
    if (wsIntentionallyClosed) return;
    setFeedStatus(true);
    markStale(true);
    const openDuration = Date.now() - wsOpenedAt;
    if (openDuration < 5000) {
      reconnectAttempt = Math.min(reconnectAttempt + 1, RECONNECT_DELAYS.length - 1);
    } else {
      reconnectAttempt = 0;
    }
    const delay = RECONNECT_DELAYS[reconnectAttempt];
    setTimeout(() => {
      if (!wsIntentionallyClosed) connectTickWs();
    }, delay);
  };

  ws.onerror = (event) => {
    console.error("[ws] error", event);
    ws.close();
  };
}

async function init() {
  const params = new URLSearchParams(location.search);
  if (params.has("login_error")) {
    showGenericError("Login failed or was cancelled.");
  }

  CONFIG = await API.config();
  setupSortHandlers();

  const status = await API.status();
  state.sessionStatus = status;
  if (!status.connected) {
    showConnectGate();
    return;
  }
  await loadDashboard();
}

el.connectBtn.addEventListener("click", () => { location.href = "/login"; });
el.reconnectBtn.addEventListener("click", () => { location.href = "/login"; });
el.refreshBtn.addEventListener("click", loadDashboard);

init();
