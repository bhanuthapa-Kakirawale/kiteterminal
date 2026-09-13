class SessionExpiredError extends Error {}
class ApiError extends Error {
  constructor(message, errorCode) {
    super(message);
    this.errorCode = errorCode;
  }
}

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

async function fetchJsonMutate(url, method, body) {
  const res = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) {
    const parsed = await res.json();
    throw new SessionExpiredError(parsed.detail.message);
  }
  const parsed = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = parsed?.detail;
    throw new ApiError(detail?.message || `Request failed: ${res.status}`, detail?.error_code);
  }
  return parsed.data;
}

const API = {
  status: () => fetch("/api/session/status").then((r) => r.json()),
  config: () => fetch("/api/config").then((r) => r.json()),
  profile: () => fetchJson("/api/profile"),
  holdings: () => fetchJson("/api/holdings"),
  positions: () => fetchJson("/api/positions"),
  mfHoldings: () => fetchJson("/api/mf_holdings"),
  margins: () => fetchJson("/api/margins"),
  logout: () => fetchJsonMutate("/api/logout", "POST"),

  searchInstruments: (q) => fetchJson(`/api/instruments/search?q=${encodeURIComponent(q)}`),
  refreshInstruments: () => fetchJsonMutate("/api/instruments/refresh", "POST"),

  listTickets: (filters) => {
    const params = new URLSearchParams();
    if (filters) {
      for (const [k, v] of Object.entries(filters)) {
        if (v !== undefined && v !== null && v !== "") params.set(k, v);
      }
    }
    const qs = params.toString();
    return fetchJson(`/api/tickets${qs ? `?${qs}` : ""}`);
  },
  getTicket: (id) => fetchJson(`/api/tickets/${id}`),
  createTicket: (body) => fetchJsonMutate("/api/tickets", "POST", body),
  editTicket: (id, body) => fetchJsonMutate(`/api/tickets/${id}`, "PUT", body),
  deleteTicket: (id) => fetchJsonMutate(`/api/tickets/${id}`, "DELETE"),
  confirmTicket: (id) => fetchJsonMutate(`/api/tickets/${id}/confirm`, "POST"),
  rejectTicket: (id, reason) => fetchJsonMutate(`/api/tickets/${id}/reject`, "POST", { reason }),
  startTrailing: (id) => fetchJsonMutate(`/api/tickets/${id}/start_trailing`, "POST"),

  getExecutionMode: () => fetchJson("/api/execution/mode"),
  setExecutionMode: (mode, confirmRealMoney) =>
    fetchJsonMutate("/api/execution/mode", "POST", { mode, confirm_real_money: confirmRealMoney }),

  orders: () => fetchJson("/api/orders"),
  trades: () => fetchJson("/api/trades"),
  gtts: () => fetchJson("/api/gtts"),

  getRiskSettings: () => fetchJson("/api/risk/settings"),
  saveRiskSettings: (body) => fetchJsonMutate("/api/risk/settings", "POST", body),
  listSectorMappings: () => fetchJson("/api/risk/sector-mappings"),
  saveSectorMapping: (body) => fetchJsonMutate("/api/risk/sector-mappings", "POST", body),
  deleteSectorMapping: (symbol) => fetchJsonMutate(`/api/risk/sector-mappings/${encodeURIComponent(symbol)}`, "DELETE"),
  listSectors: () => fetchJson("/api/risk/sectors"),
  unmappedSymbols: () => fetchJson("/api/risk/unmapped-symbols"),
  getSettingsHistory: () => fetchJson("/api/risk/settings/history"),
  getPositionSize: (entryPrice, stopLoss) =>
    fetchJson(`/api/risk/position-size?entry_price=${entryPrice}&stop_loss=${stopLoss}`),

  getUiPrefs: () => fetchJson("/api/ui-prefs"),
  saveUiPrefs: (body) => fetchJsonMutate("/api/ui-prefs", "POST", body),

  getDashboardPanels: () => fetchJson("/api/dashboard-panels"),
  saveDashboardPanels: (panels) => fetchJsonMutate("/api/dashboard-panels", "PUT", { panels }),

  getWatchlist: () => fetchJson("/api/watchlist"),
  addWatchlistSymbol: (body) => fetchJsonMutate("/api/watchlist", "POST", body),
  removeWatchlistSymbol: (id) => fetchJsonMutate(`/api/watchlist/${id}`, "DELETE"),

  listStrategies: () => fetchJson("/api/strategies"),
  getStrategyConfig: (strategyName) => fetchJson(`/api/strategies/${encodeURIComponent(strategyName)}`),
  saveStrategyConfig: (strategyName, body) => fetchJsonMutate(`/api/strategies/${encodeURIComponent(strategyName)}`, "PUT", body),
  scanNow: () => fetchJsonMutate("/api/strategies/scan-now", "POST"),
  getStopAdjustments: () => fetchJson("/api/strategies/stop-adjustments?status=pending"),
  confirmStopAdjustment: (id) => fetchJsonMutate(`/api/strategies/stop-adjustments/${id}/confirm`, "POST"),
  rejectStopAdjustment: (id) => fetchJsonMutate(`/api/strategies/stop-adjustments/${id}/reject`, "POST"),
  runBacktest: (body) => fetchJsonMutate("/api/strategies/backtests", "POST", body),
  listBacktests: () => fetchJson("/api/strategies/backtests"),
  getBacktest: (id) => fetchJson(`/api/strategies/backtests/${id}`),

  getJournalModeSummary: () => fetchJson("/api/journal/mode-summary"),
  getJournalStats: (mode) => fetchJson(`/api/journal/stats?mode=${encodeURIComponent(mode)}`),
  getTodaysActions: () => fetchJson("/api/todays-actions"),

  // Phase 8: kill switch
  cancelAllPending: () => fetchJsonMutate("/api/tickets/cancel-all-pending", "POST"),
  cancelOrder: (orderId, variety) => fetchJsonMutate(`/api/orders/${encodeURIComponent(orderId)}/cancel?variety=${encodeURIComponent(variety || "regular")}`, "POST"),
  cancelGtt: (gttId) => fetchJsonMutate(`/api/gtts/${gttId}/cancel`, "POST"),
  emergencyExitPreview: () => fetchJson("/api/kill-switch/emergency-exit-preview"),
  emergencyExitConfirm: (confirmationPhrase) => fetchJsonMutate("/api/kill-switch/emergency-exit-confirm", "POST", { confirmation_phrase: confirmationPhrase }),

  // Phase 8: reconciliation
  listMismatches: (resolved) => fetchJson(`/api/reconciliation/mismatches?resolved=${resolved ? "true" : "false"}`),
  markMismatchReviewed: (id) => fetchJsonMutate(`/api/reconciliation/${id}/mark-reviewed`, "POST"),

  // Phase 8: strategy versioning
  getStrategyVersions: (strategyName) => fetchJson(`/api/strategies/${encodeURIComponent(strategyName)}/versions`),
};

const el = {
  connectGate: document.getElementById("connect-gate"),
  connectBtn: document.getElementById("connect-btn"),
  sessionExpiredBanner: document.getElementById("session-expired-banner"),
  reconnectBtn: document.getElementById("reconnect-btn"),
  genericError: document.getElementById("generic-error"),
  appShell: document.getElementById("app-shell"),
  accountName: document.getElementById("account-name"),
  connectionStatus: document.getElementById("connection-status"),
  feedStatus: document.getElementById("feed-status"),
  refreshBtn: document.getElementById("refresh-btn"),
  alertBanner: document.getElementById("alert-banner"),
  heroMetrics: document.getElementById("hero-metrics"),
  holdingsBody: document.querySelector("#holdings-table tbody"),
  positionsBody: document.querySelector("#positions-table tbody"),
  mfBody: document.querySelector("#mf-table tbody"),

  // Phase 3
  logoutBtn: document.getElementById("logout-btn"),
  marketStatusBadge: document.getElementById("market-status-badge"),
  marketClosedNote: document.getElementById("market-closed-note"),
  trailingIndicator: document.getElementById("trailing-indicator"),
  modeBanner: document.getElementById("mode-banner"),
  modeBannerText: document.getElementById("mode-banner-text"),
  modeToggleCheckbox: document.getElementById("mode-toggle-checkbox"),

  ticketForm: document.getElementById("ticket-form"),
  ticketSymbolInput: document.getElementById("ticket-symbol"),
  symbolOptions: document.getElementById("symbol-options"),
  ticketTrailingEnabled: document.getElementById("ticket-trailing-enabled"),
  ticketTrailingTrigger: document.getElementById("ticket-trailing-trigger"),
  ticketTrailingDistance: document.getElementById("ticket-trailing-distance"),
  ticketFormError: document.getElementById("ticket-form-error"),
  ticketsBody: document.querySelector("#tickets-table tbody"),

  ticketConfirmDialog: document.getElementById("ticket-confirm-dialog"),
  ticketConfirmTitle: document.getElementById("ticket-confirm-title"),
  ticketConfirmBody: document.getElementById("ticket-confirm-body"),
  ticketRiskCheckBody: document.getElementById("ticket-risk-check-body"),
  ticketRejectBtn: document.getElementById("ticket-reject-btn"),
  ticketConfirmBtn: document.getElementById("ticket-confirm-btn"),
  ticketModifyBtn: document.getElementById("ticket-modify-btn"),

  liveModeConfirmDialog: document.getElementById("live-mode-confirm-dialog"),
  liveModeCancelBtn: document.getElementById("live-mode-cancel-btn"),
  liveModeConfirmBtn: document.getElementById("live-mode-confirm-btn"),

  ordersSection: document.getElementById("orders-section"),
  ordersBody: document.querySelector("#orders-table tbody"),
  tradesSection: document.getElementById("trades-section"),
  tradesBody: document.querySelector("#trades-table tbody"),
  gttsSection: document.getElementById("gtts-section"),
  gttsBody: document.querySelector("#gtts-table tbody"),

  // Phase 4
  riskSettingsForm: document.getElementById("risk-settings-form"),
  riskSettingsError: document.getElementById("risk-settings-error"),
  riskSettingsSaved: document.getElementById("risk-settings-saved"),
  sectorMappingForm: document.getElementById("sector-mapping-form"),
  sectorMappingError: document.getElementById("sector-mapping-error"),
  sectorMappingsBody: document.querySelector("#sector-mappings-table tbody"),
  ticketQuantityInput: document.getElementById("ticket-quantity"),
  ticketEntryPriceInput: document.getElementById("ticket-entry-price"),
  ticketStopLossInput: document.getElementById("ticket-stop-loss"),

  // Phase 5
  ticketArchiveBtn: document.getElementById("ticket-archive-btn"),

  tfStatus: document.getElementById("tf-status"),
  tfSource: document.getElementById("tf-source"),
  tfSymbol: document.getElementById("tf-symbol"),
  tfDateFrom: document.getElementById("tf-date-from"),
  tfDateTo: document.getElementById("tf-date-to"),
  tfShowArchived: document.getElementById("tf-show-archived"),
  tfApplyBtn: document.getElementById("tf-apply-btn"),
  tfClearBtn: document.getElementById("tf-clear-btn"),

  ticketDetailDialog: document.getElementById("ticket-detail-dialog"),
  ticketDetailBody: document.getElementById("ticket-detail-body"),
  ticketDetailEvents: document.getElementById("ticket-detail-events"),
  ticketDetailCloseBtn: document.getElementById("ticket-detail-close-btn"),
  ticketDetailEditBtn: document.getElementById("ticket-detail-edit-btn"),
  ticketDetailDeleteBtn: document.getElementById("ticket-detail-delete-btn"),
  ticketDetailArchiveBtn: document.getElementById("ticket-detail-archive-btn"),
  ticketEditForm: document.getElementById("ticket-edit-form"),
  ticketEditError: document.getElementById("ticket-edit-error"),
  teTrailingEnabled: document.getElementById("te-trailing-enabled"),
  teTrailingTrigger: document.getElementById("te-trailing-trigger"),
  teTrailingDistance: document.getElementById("te-trailing-distance"),

  riskSettingsHistoryToggle: document.getElementById("risk-settings-history-toggle"),
  riskSettingsHistoryPanel: document.getElementById("risk-settings-history-panel"),
  riskSettingsHistoryBody: document.querySelector("#risk-settings-history-table tbody"),

  unmappedSymbolsList: document.getElementById("unmapped-symbols-list"),
  sectorOptions: document.getElementById("sector-options"),

  // Phase 6: nav shell + dashboard panels
  sidebar: document.getElementById("sidebar"),
  sidebarToggleBtn: document.getElementById("sidebar-toggle-btn"),
  contentArea: document.getElementById("content-area"),
  dashboardPanelsGrid: document.getElementById("dashboard-panels-grid"),
  dashboardEmptyNote: document.getElementById("dashboard-empty-note"),
  customizeModeToggle: document.getElementById("customize-mode-toggle"),
  addPanelControl: document.getElementById("add-panel-control"),
  addPanelSelect: document.getElementById("add-panel-select"),

  // Phase 4.5: watchlist
  watchlistAddForm: document.getElementById("watchlist-add-form"),
  watchlistSymbolInput: document.getElementById("wl-symbol"),
  watchlistAddError: document.getElementById("watchlist-add-error"),
  watchlistBody: document.querySelector("#watchlist-table tbody"),
  watchlistSummaryBody: document.querySelector("#watchlist-summary-table tbody"),

  strategySelect: document.getElementById("strategy-select"),
  strategyEnabledToggle: document.getElementById("strategy-enabled-toggle"),
  strategyEnabledState: document.getElementById("strategy-enabled-state"),
  strategyParamsForm: document.getElementById("strategy-params-form"),
  strategyParamsFields: document.getElementById("strategy-params-fields"),
  strategyParamsError: document.getElementById("strategy-params-error"),
  scanNowBtn: document.getElementById("scan-now-btn"),
  scanNowResult: document.getElementById("scan-now-result"),
  signalQueueBody: document.querySelector("#signal-queue-table tbody"),
  stopAdjustmentsList: document.getElementById("stop-adjustments-list"),
  backtestForm: document.getElementById("backtest-form"),
  btStrategySelect: document.getElementById("bt-strategy-select"),
  backtestError: document.getElementById("backtest-error"),
  backtestResults: document.getElementById("backtest-results"),
  backtestTradesBody: document.querySelector("#backtest-trades-table tbody"),

  journalModeSelect: document.getElementById("journal-mode-select"),
  journalSampleWarning: document.getElementById("journal-sample-warning"),
  journalOverallStats: document.getElementById("journal-overall-stats"),
  journalEquityCurve: document.getElementById("journal-equity-curve"),
  journalStrategyBody: document.querySelector("#journal-strategy-table tbody"),
  journalSymbolBody: document.querySelector("#journal-symbol-table tbody"),

  taBuyBody: document.querySelector("#ta-buy-table tbody"),
  taExitBody: document.querySelector("#ta-exit-table tbody"),
  taAdjustmentsList: document.getElementById("ta-adjustments-list"),
  taWatchBody: document.querySelector("#ta-watch-table tbody"),
  taNoAction: document.getElementById("ta-no-action"),
  todaysActionsPanelBody: document.getElementById("todays-actions-panel-body"),

  tqReadyBody: document.querySelector("#tq-ready-table tbody"),
  tqWaitingBody: document.querySelector("#tq-waiting-table tbody"),
  tqWatchRefBody: document.querySelector("#tq-watch-ref-table tbody"),
  tqRejectedBody: document.querySelector("#tq-rejected-table tbody"),
  tqRejectedFilter: document.getElementById("tq-rejected-filter"),
  tqExecutedBody: document.querySelector("#tq-executed-table tbody"),

  // Phase 8: kill switch
  cancelAllPendingBtn: document.getElementById("cancel-all-pending-btn"),
  cancelAllPendingDialog: document.getElementById("cancel-all-pending-dialog"),
  cancelAllPendingSummary: document.getElementById("cancel-all-pending-summary"),
  cancelAllPendingCancelBtn: document.getElementById("cancel-all-pending-cancel-btn"),
  cancelAllPendingConfirmBtn: document.getElementById("cancel-all-pending-confirm-btn"),
  emergencyExitAllBtn: document.getElementById("emergency-exit-all-btn"),
  emergencyExitDialog: document.getElementById("emergency-exit-dialog"),
  emergencyExitModeLabel: document.getElementById("emergency-exit-mode-label"),
  emergencyExitPreviewBody: document.querySelector("#emergency-exit-preview-table tbody"),
  emergencyExitPhrase: document.getElementById("emergency-exit-phrase"),
  emergencyExitError: document.getElementById("emergency-exit-error"),
  emergencyExitCancelBtn: document.getElementById("emergency-exit-cancel-btn"),
  emergencyExitConfirmBtn: document.getElementById("emergency-exit-confirm-btn"),

  // Phase 8: reconciliation
  reconciliationList: document.getElementById("reconciliation-list"),

  // Phase 8: strategy versioning / last-disabled
  strategyLastDisabled: document.getElementById("strategy-last-disabled"),
  strategyVersionHistory: document.getElementById("strategy-version-history"),

  // Phase 8: backtest rigor
  btRunWalkForward: document.getElementById("bt-run-walk-forward"),
  btWindowDays: document.getElementById("bt-window-days"),
  btRunMonteCarlo: document.getElementById("bt-run-monte-carlo"),
  btMonteCarloSummary: document.getElementById("bt-monte-carlo-summary"),
  btWalkForwardSection: document.getElementById("bt-walk-forward-section"),
  btWalkForwardBody: document.querySelector("#bt-walk-forward-table tbody"),
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

  // Phase 3
  accountType: null,
  tickets: [],
  executionMode: "dry_run",
  trailingHeartbeat: null, // { last_run_at, active_ticket_count }
  trailingErrorAlerts: [],
  ticketDraftDirty: false,
  lastSymbolMatches: [],
  orders: [],
  trades: [],
  gtts: [],

  // Phase 4
  riskSettings: null,
  sectorMappings: [],
  quantityManuallyEdited: false,

  // Phase 5
  activeDetailTicketId: null,
  unmappedSymbols: [],
  sectors: [],
  settingsHistory: [],

  // Phase 6: nav shell + dashboard panels
  activeSection: "dashboard",
  dashboardPanels: [],
  customizeMode: false,

  // Phase 4.5: watchlist
  watchlist: [],
  lastWatchlistSymbolMatches: [],

  // Phase 5: momentum breakout strategy engine
  strategyConfigs: {}, // { [strategy_name]: { strategy_name, enabled, params, updated_at } }
  signalQueue: [],
  stopAdjustments: [],
  backtestResult: null,

  // Phase 6: journal/analytics, today's actions, trade queue
  journalStats: null,
  todaysActions: null,
  tradeQueueTickets: [],

  // Phase 8: reconciliation
  reconciliationMismatches: [],
};

const TRAILING_CHECK_INTERVAL_SECONDS = 45; // mirrors backend/config.py

function hideAllStates() {
  el.connectGate.hidden = true;
  el.sessionExpiredBanner.hidden = true;
  el.genericError.hidden = true;
  el.appShell.hidden = true;
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
  el.appShell.hidden = false;
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

// NSE hours 9:15-15:30 IST, Mon-Fri. No holiday calendar — a simple time
// check is enough for this indicator's purpose.
function computeMarketStatus() {
  const istMs = Date.now() + 5.5 * 3600 * 1000;
  const d = new Date(istMs);
  const day = d.getUTCDay(); // IST-shifted date, so getUTCDay() reads as IST weekday
  const mins = d.getUTCHours() * 60 + d.getUTCMinutes();
  if (day === 0 || day === 6) return "closed";
  if (mins >= 555 && mins < 930) return "open"; // 9:15-15:30
  if (mins >= 540 && mins < 555) return "pre_open"; // 9:00-9:15 (approximate)
  return "closed";
}

function renderMarketStatus() {
  const status = computeMarketStatus();
  const labels = { open: "Market Open", pre_open: "Pre-Open", closed: "Market Closed" };
  el.marketStatusBadge.hidden = false;
  el.marketStatusBadge.textContent = labels[status];
  el.marketStatusBadge.className = `market-status-badge ${status}`;
  el.marketClosedNote.hidden = status !== "closed";
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

  for (const t of state.tickets) {
    if (t.status === "protection_failed") {
      alerts.push({
        id: `protection-failed-${t.id}`,
        severity: "urgent",
        message: `${t.symbol}: entry placed but protective stop FAILED — UNPROTECTED position, act now`,
        scrollTargetSelector: `tr[data-ticket-id="${t.id}"]`,
      });
    }
  }
  alerts.push(...state.trailingErrorAlerts);

  for (const m of state.reconciliationMismatches || []) {
    alerts.push({
      id: `reconciliation-${m.id}`,
      severity: "urgent",
      message: m.detail,
      scrollTargetSelector: "#reconciliation-list",
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

// ---- Phase 4.5: Watchlist ----

function watchlistDerived(w) {
  const trend = (w.last_price != null && w.sma_200 != null)
    ? (w.last_price >= w.sma_200 ? "▲" : "▼")
    : null;
  const distancePct = (w.last_price != null && w.donchian_high_20)
    ? ((w.last_price - w.donchian_high_20) / w.donchian_high_20) * 100
    : null;
  return { trend, distancePct };
}

function distanceLabel(distancePct) {
  if (distancePct == null) return "—";
  const dir = distancePct >= 0 ? "above" : "below";
  return `${Math.abs(distancePct).toFixed(1)}% ${dir} 20-day high`;
}

function renderWatchlistTable() {
  if (!el.watchlistBody) return;
  el.watchlistBody.innerHTML = "";
  if (state.watchlist.length === 0) {
    el.watchlistBody.innerHTML = `<tr class="empty-row"><td colspan="8">Your watchlist is empty</td></tr>`;
    return;
  }
  for (const w of state.watchlist) {
    const { trend, distancePct } = watchlistDerived(w);
    const tr = document.createElement("tr");
    tr.dataset.instrumentToken = w.instrument_token;
    tr.innerHTML = `
      <td>${w.symbol}</td>
      <td class="num">${w.last_price != null ? fmt(w.last_price) : "—"}</td>
      <td class="num ${w.day_change_percentage != null ? pnlClass(w.day_change_percentage) : ""}">${w.day_change_percentage != null ? fmt(w.day_change_percentage) + "%" : "—"}</td>
      <td>${trend ?? "—"}</td>
      <td class="num">${w.rsi_14 != null ? w.rsi_14.toFixed(1) : "—"}</td>
      <td>${distanceLabel(distancePct)}</td>
      <td>${w.notes ?? ""}</td>
      <td><button class="row-action-btn danger" data-action="remove-watchlist" data-id="${w.id}">Remove</button></td>
    `;
    el.watchlistBody.appendChild(tr);
  }
}

function renderWatchlistSummaryPanel() {
  if (!el.watchlistSummaryBody) return;
  el.watchlistSummaryBody.innerHTML = "";
  if (state.watchlist.length === 0) {
    el.watchlistSummaryBody.innerHTML = `<tr class="empty-row"><td colspan="5">No symbols watched yet</td></tr>`;
    return;
  }
  for (const w of state.watchlist) {
    const { trend, distancePct } = watchlistDerived(w);
    const tr = document.createElement("tr");
    tr.dataset.instrumentToken = w.instrument_token;
    tr.innerHTML = `
      <td>${w.symbol}</td>
      <td class="num">${w.last_price != null ? fmt(w.last_price) : "—"}</td>
      <td>${trend ?? "—"}</td>
      <td class="num">${w.rsi_14 != null ? w.rsi_14.toFixed(1) : "—"}</td>
      <td>${distanceLabel(distancePct)}</td>
    `;
    el.watchlistSummaryBody.appendChild(tr);
  }
}

el.watchlistBody?.addEventListener("click", async (event) => {
  const btn = event.target.closest('button[data-action="remove-watchlist"]');
  if (!btn) return;
  try {
    await API.removeWatchlistSymbol(btn.dataset.id);
    await loadWatchlist();
  } catch (e) {
    alert(e.message);
  }
});

let watchlistSymbolSearchTimer = null;
el.watchlistSymbolInput.addEventListener("input", () => {
  clearTimeout(watchlistSymbolSearchTimer);
  const q = el.watchlistSymbolInput.value.trim();
  if (q.length < 2) return;
  watchlistSymbolSearchTimer = setTimeout(async () => {
    try {
      const matches = await API.searchInstruments(q);
      state.lastWatchlistSymbolMatches = matches;
      el.symbolOptions.innerHTML = matches
        .map((m) => `<option value="${m.tradingsymbol}">${m.name ?? ""}</option>`)
        .join("");
    } catch (e) {
      // leave existing options as-is on a transient search failure
    }
  }, 250);
});

// ---- Phase 5/7: pluggable strategy engine ----

// Client-side mirror of each module's params schema (backend/models.py's
// STRATEGY_PARAMS_MODELS is the source of truth / real validation — this
// is only what's needed to render the right <input> elements and parse
// their values back into the right types). One entry per strategy_name;
// [paramKey, label, parser, step, min, type] per field.
const STRATEGY_PARAM_SCHEMAS = {
  momentum_breakout: [
    ["donchian_lookback", "Donchian lookback (days)", parseInt, "1", "1"],
    ["volume_multiple", "Volume multiple", parseFloat, "0.1", "0.1"],
    ["trend_filter_period", "Trend filter SMA period", parseInt, "1", "1"],
    ["atr_stop_multiple", "ATR stop multiple", parseFloat, "0.1", "0.1"],
    ["target_r_multiple", "Target R multiple", parseFloat, "0.1", "0.1"],
    ["trailing_atr_multiple", "Trailing ATR multiple", parseFloat, "0.1", "0.1"],
    ["thesis_invalidation_ma", "Thesis invalidation EMA period", parseInt, "1", "1"],
    ["time_exit_days", "Time exit (trading days)", parseInt, "1", "1"],
    ["breakeven_trigger_r", "Breakeven trigger (R)", parseFloat, "0.1", "0.1"],
    ["max_signals_per_day", "Max signals per day", parseInt, "1", "1"],
  ],
  trend_pullback: [
    ["trend_filter_period", "Trend filter SMA period", parseInt, "1", "1"],
    ["pullback_ma_period", "Pullback EMA period", parseInt, "1", "1"],
    ["pullback_band_pct", "Pullback band (%)", parseFloat, "0.1", "0.1"],
    ["pullback_lookback_days", "Pullback lookback (days)", parseInt, "1", "1"],
    ["atr_stop_multiple", "ATR stop multiple", parseFloat, "0.1", "0.1"],
    ["target_r_multiple", "Target R multiple", parseFloat, "0.1", "0.1"],
    ["trailing_atr_multiple", "Trailing ATR multiple", parseFloat, "0.1", "0.1"],
    ["thesis_invalidation_ma", "Thesis invalidation EMA period", parseInt, "1", "1"],
    ["time_exit_days", "Time exit (trading days)", parseInt, "1", "1"],
    ["breakeven_trigger_r", "Breakeven trigger (R)", parseFloat, "0.1", "0.1"],
    ["max_signals_per_day", "Max signals per day", parseInt, "1", "1"],
  ],
  rsi_mean_reversion: [
    ["trend_filter_period", "Trend filter SMA period", parseInt, "1", "1"],
    ["rsi_oversold_threshold", "RSI oversold threshold", parseFloat, "1", "1"],
    ["rsi_turn_confirmation", "Require RSI turning up", (v) => v === "true", null, null, "checkbox"],
    ["swing_low_lookback_days", "Swing low lookback (days)", parseInt, "1", "1"],
    ["atr_stop_multiple", "ATR stop multiple", parseFloat, "0.1", "0.1"],
    ["target_r_multiple", "Target R multiple", parseFloat, "0.1", "0.1"],
    ["rsi_exit_level", "RSI exit level", parseFloat, "1", "1"],
    ["thesis_invalidation_ma", "Thesis invalidation EMA period", parseInt, "1", "1"],
    ["time_exit_days", "Time exit (trading days)", parseInt, "1", "1"],
    ["breakeven_trigger_r", "Breakeven trigger (R)", parseFloat, "0.1", "0.1"],
    ["max_signals_per_day", "Max signals per day", parseInt, "1", "1"],
  ],
};

function strategyLabel(name) {
  return name
    .split("_")
    .map((w) => w[0].toUpperCase() + w.slice(1))
    .join(" ");
}

function renderStrategyParamFields(strategyName) {
  const schema = STRATEGY_PARAM_SCHEMAS[strategyName] || [];
  el.strategyParamsFields.innerHTML = schema
    .map(([key, label, , step, min, type]) => {
      const domId = `sp-${key}`;
      if (type === "checkbox") {
        return `
          <div class="field checkbox-field">
            <input id="${domId}" type="checkbox" data-param-key="${key}" />
            <label for="${domId}">${label}</label>
          </div>
        `;
      }
      return `
        <div class="field">
          <label for="${domId}">${label}</label>
          <input id="${domId}" type="number" step="${step}" min="${min}" data-param-key="${key}" required />
        </div>
      `;
    })
    .join("");
}

function readStrategyParamsForm(strategyName) {
  const schema = STRATEGY_PARAM_SCHEMAS[strategyName] || [];
  const params = {};
  for (const [key, , parser, , , type] of schema) {
    const inputEl = document.getElementById(`sp-${key}`);
    params[key] = type === "checkbox" ? parser(String(inputEl.checked)) : parser(inputEl.value);
  }
  return params;
}

function renderStrategyConfig() {
  const strategyName = el.strategySelect.value;
  const cfg = state.strategyConfigs[strategyName];
  if (!cfg) return;

  const schema = STRATEGY_PARAM_SCHEMAS[strategyName] || [];
  for (const [key, , , , , type] of schema) {
    const inputEl = document.getElementById(`sp-${key}`);
    if (!inputEl) continue;
    if (type === "checkbox") inputEl.checked = !!cfg.params[key];
    else inputEl.value = cfg.params[key];
  }

  el.strategyEnabledToggle.checked = cfg.enabled;
  el.strategyEnabledState.textContent = cfg.enabled
    ? `${strategyLabel(strategyName)}: ON — scanning the watchlist daily after market close`
    : `${strategyLabel(strategyName)}: OFF — not scanning`;
  document.getElementById("strategy-status-banner").classList.toggle("strategy-on", cfg.enabled);
  document.getElementById("strategy-status-banner").classList.toggle("strategy-off", !cfg.enabled);
  document.getElementById("strategy-params-title").textContent = `${strategyLabel(strategyName)} — Parameters`;

  if (cfg.last_disabled_at) {
    el.strategyLastDisabled.hidden = false;
    el.strategyLastDisabled.textContent = `Last disabled: ${cfg.last_disabled_at}`;
  } else {
    el.strategyLastDisabled.hidden = true;
  }
}

function strategyVersionRowHtml(v) {
  const diffHtml = v.diff_from_previous
    ? Object.entries(v.diff_from_previous)
        .map(([key, { old, new: newVal }]) => `<span class="changed-key">${key}</span>: ${old} → ${newVal}`)
        .join(", ")
    : null;
  return `
    <div class="strategy-version-row">
      <div class="version-header">
        <span>v${v.version}</span>
        <span class="badge-muted">${v.effective_from} → ${v.effective_to || "current"}</span>
      </div>
      ${diffHtml ? `<div class="strategy-version-diff">Changed: ${diffHtml}</div>` : `<div class="strategy-version-diff">Initial version</div>`}
    </div>
  `;
}

async function loadStrategyVersionHistory(strategyName) {
  try {
    const versions = await API.getStrategyVersions(strategyName);
    el.strategyVersionHistory.innerHTML = versions.length
      ? versions.map(strategyVersionRowHtml).join("")
      : `<div class="empty-row">No version history yet</div>`;
  } catch (e) {
    // leave prior state visible on a transient load failure
  }
}

async function loadStrategySelector() {
  try {
    const list = await API.listStrategies();
    state.strategyConfigs = {};
    for (const s of list) state.strategyConfigs[s.strategy_name] = s;

    const previousValue = el.strategySelect.value;
    el.strategySelect.innerHTML = list
      .map((s) => `<option value="${s.strategy_name}">${strategyLabel(s.strategy_name)}</option>`)
      .join("");
    el.strategySelect.value = list.some((s) => s.strategy_name === previousValue) ? previousValue : list[0].strategy_name;

    el.btStrategySelect.innerHTML = el.strategySelect.innerHTML;
    if (!el.btStrategySelect.value) el.btStrategySelect.value = list[0].strategy_name;

    renderStrategyParamFields(el.strategySelect.value);
    renderStrategyConfig();
    await loadStrategyVersionHistory(el.strategySelect.value);
  } catch (e) {
    // leave prior state visible on a transient load failure
  }
}

el.strategySelect.addEventListener("change", async () => {
  renderStrategyParamFields(el.strategySelect.value);
  renderStrategyConfig();
  await loadStrategyVersionHistory(el.strategySelect.value);
});

el.strategyEnabledToggle.addEventListener("change", async () => {
  const strategyName = el.strategySelect.value;
  const turningOn = el.strategyEnabledToggle.checked;
  if (turningOn && !confirm(
    `Turn ON ${strategyLabel(strategyName)}? It will scan your watchlist once a day after market close and `
    + "propose entry/exit tickets — nothing executes without your explicit confirmation, but this starts "
    + "automated signal generation."
  )) {
    el.strategyEnabledToggle.checked = false;
    return;
  }
  try {
    const cfg = await API.saveStrategyConfig(strategyName, { enabled: turningOn, params: readStrategyParamsForm(strategyName) });
    state.strategyConfigs[strategyName] = { strategy_name: strategyName, ...cfg };
    renderStrategyConfig();
  } catch (e) {
    el.strategyEnabledToggle.checked = !turningOn;
    alert(e.message);
  }
});

el.strategyParamsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  el.strategyParamsError.textContent = "";
  const strategyName = el.strategySelect.value;
  try {
    const cfg = await API.saveStrategyConfig(strategyName, {
      enabled: el.strategyEnabledToggle.checked, params: readStrategyParamsForm(strategyName),
    });
    state.strategyConfigs[strategyName] = { strategy_name: strategyName, ...cfg };
    await loadStrategyVersionHistory(strategyName);
    renderStrategyConfig();
  } catch (e) {
    el.strategyParamsError.textContent = e.message;
  }
});

el.scanNowBtn.addEventListener("click", async () => {
  el.scanNowResult.textContent = "Scanning…";
  try {
    const result = await API.scanNow();
    if (!result.enabled) {
      el.scanNowResult.textContent = "No strategies are enabled — nothing scanned.";
    } else {
      const parts = Object.entries(result.by_strategy).map(
        ([name, r]) => `${strategyLabel(name)}: ${r.entry_tickets.length} entry, ${r.exit_tickets.length} exit`
      );
      el.scanNowResult.textContent =
        `Scan complete — ${parts.join("; ")}; ${result.breakeven_proposals.length} breakeven proposal(s).`;
    }
    await loadSignalQueue();
    await loadStopAdjustments();
  } catch (e) {
    el.scanNowResult.textContent = `Scan failed: ${e.message}`;
  }
});

async function loadSignalQueue() {
  try {
    const all = await API.listTickets({ source: "strategy" });
    state.signalQueue = all.filter((t) => t.status === "pending_confirmation" || t.status === "risk_rejected");
    renderTicketsTable(state.signalQueue, el.signalQueueBody);
  } catch (e) {
    // leave prior state visible on a transient load failure
  }
}

function renderStopAdjustments() {
  el.stopAdjustmentsList.innerHTML = "";
  if (state.stopAdjustments.length === 0) {
    el.stopAdjustmentsList.innerHTML = `<div class="empty-row">No pending stop adjustments</div>`;
    return;
  }
  for (const p of state.stopAdjustments) {
    const div = document.createElement("div");
    div.className = "stop-adjustment-card";
    div.innerHTML = `
      <div>
        <strong>${p.symbol}</strong> — move stop ₹${fmt(p.current_stop)} → ₹${fmt(p.proposed_stop)}
        <div class="rationale">${p.reason}</div>
      </div>
      <div>
        <button class="btn btn-reject" data-action="reject-stop-adj" data-id="${p.id}">Reject</button>
        <button class="btn btn-primary" data-action="confirm-stop-adj" data-id="${p.id}">Confirm</button>
      </div>
    `;
    el.stopAdjustmentsList.appendChild(div);
  }
}

async function loadStopAdjustments() {
  try {
    state.stopAdjustments = await API.getStopAdjustments();
    renderStopAdjustments();
  } catch (e) {
    // leave prior state visible on a transient load failure
  }
}

el.stopAdjustmentsList.addEventListener("click", async (event) => {
  const confirmBtn = event.target.closest('button[data-action="confirm-stop-adj"]');
  const rejectBtn = event.target.closest('button[data-action="reject-stop-adj"]');
  if (!confirmBtn && !rejectBtn) return;
  const btn = confirmBtn || rejectBtn;
  try {
    if (confirmBtn) await API.confirmStopAdjustment(btn.dataset.id);
    else await API.rejectStopAdjustment(btn.dataset.id);
    await loadStopAdjustments();
  } catch (e) {
    alert(e.message);
  }
});

function renderBacktestResults() {
  const r = state.backtestResult;
  if (!r) return;
  el.backtestResults.hidden = false;
  document.getElementById("bt-r-total").textContent = r.total_signals;
  document.getElementById("bt-r-winrate").textContent = r.win_rate != null ? `${(r.win_rate * 100).toFixed(1)}%` : "—";
  document.getElementById("bt-r-avgr").textContent = r.avg_r_multiple != null ? r.avg_r_multiple.toFixed(2) : "—";
  document.getElementById("bt-r-pf").textContent = r.profit_factor != null ? r.profit_factor.toFixed(2) : "—";
  document.getElementById("bt-r-dd").textContent = r.max_drawdown_pct != null ? `${r.max_drawdown_pct.toFixed(2)}%` : "—";
  document.getElementById("bt-r-sharpe").textContent = r.sharpe_ratio != null ? r.sharpe_ratio.toFixed(2) : "—";
  document.getElementById("bt-r-sortino").textContent = r.sortino_ratio != null ? r.sortino_ratio.toFixed(2) : "—";
  document.getElementById("bt-r-maxlosses").textContent = r.max_consecutive_losses ?? "—";
  document.getElementById("bt-r-recovery").textContent = r.recovery_factor != null ? r.recovery_factor.toFixed(2) : "—";
  document.getElementById("bt-r-avgwinloss").textContent =
    `${r.avg_win != null ? "+" + r.avg_win.toFixed(2) : "—"} / ${r.avg_loss != null ? r.avg_loss.toFixed(2) : "—"}`;
  document.getElementById("bt-r-timeinmarket").textContent =
    r.time_in_market_pct != null ? `${r.time_in_market_pct.toFixed(1)}%` : "—";

  if (r.mc_iterations) {
    el.btMonteCarloSummary.hidden = false;
    el.btMonteCarloSummary.textContent =
      `Monte Carlo (${r.mc_iterations} reshuffles): median max drawdown ${r.mc_median_drawdown_pct.toFixed(1)}%, `
      + `95th percentile ${r.mc_p95_drawdown_pct.toFixed(1)}% — final equity ranges ₹${fmt(r.mc_p5_final_equity, 0)} `
      + `to ₹${fmt(r.mc_p95_final_equity, 0)} (order-invariant sum; only drawdown varies across reshuffles).`;
  } else {
    el.btMonteCarloSummary.hidden = true;
  }

  if (r.walk_forward_windows && r.walk_forward_windows.length > 0) {
    el.btWalkForwardSection.hidden = false;
    el.btWalkForwardBody.innerHTML = r.walk_forward_windows
      .map((w) => `
        <tr>
          <td>${w.window_start} → ${w.window_end}</td>
          <td class="num">${w.stats.total_signals}</td>
          <td class="num">${w.stats.win_rate != null ? (w.stats.win_rate * 100).toFixed(1) + "%" : "—"}</td>
          <td class="num">${w.stats.avg_r_multiple != null ? w.stats.avg_r_multiple.toFixed(2) : "—"}</td>
          <td class="num">${w.stats.profit_factor != null ? w.stats.profit_factor.toFixed(2) : "—"}</td>
          <td class="num">${w.stats.max_drawdown_pct != null ? w.stats.max_drawdown_pct.toFixed(2) + "%" : "—"}</td>
        </tr>
      `)
      .join("");
  } else {
    el.btWalkForwardSection.hidden = true;
  }

  el.backtestTradesBody.innerHTML = "";
  if (r.trades.length === 0) {
    el.backtestTradesBody.innerHTML = `<tr class="empty-row"><td colspan="8">No signals in this range</td></tr>`;
    return;
  }
  for (const t of r.trades) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${t.symbol}</td>
      <td>${t.entry_date}</td>
      <td class="num">${fmt(t.entry_price)}</td>
      <td>${t.exit_date ?? "—"}</td>
      <td class="num">${t.exit_price != null ? fmt(t.exit_price) : "—"}</td>
      <td>${t.exit_reason ?? "—"}</td>
      <td class="num">${t.r_multiple != null ? t.r_multiple.toFixed(2) : "—"}</td>
      <td>${t.outcome ?? "—"}</td>
    `;
    el.backtestTradesBody.appendChild(tr);
  }
}

el.backtestForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  el.backtestError.textContent = "";
  const strategyName = el.btStrategySelect.value;
  const startDate = document.getElementById("bt-start-date").value;
  const endDate = document.getElementById("bt-end-date").value;
  const symbolsRaw = document.getElementById("bt-symbols").value.trim();
  const symbols = symbolsRaw ? symbolsRaw.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean) : undefined;
  const runWalkForward = el.btRunWalkForward.checked;
  const windowDays = parseInt(el.btWindowDays.value, 10) || 90;
  const runMonteCarlo = el.btRunMonteCarlo.checked;
  try {
    state.backtestResult = await API.runBacktest({
      strategy_name: strategyName, start_date: startDate, end_date: endDate, symbols,
      run_walk_forward: runWalkForward, window_days: windowDays, run_monte_carlo: runMonteCarlo,
    });
    renderBacktestResults();
  } catch (e) {
    el.backtestError.textContent = e.message;
  }
});

async function loadStrategyData() {
  await loadStrategySelector();
  await loadSignalQueue();
  await loadStopAdjustments();
}

// ---- Phase 6: journal/analytics, today's actions, trade queue ----

function renderEquityCurveSvg(container, curve) {
  if (!curve || curve.length < 2) {
    container.innerHTML = `<div class="empty-row">Not enough closed trades yet for an equity curve</div>`;
    return;
  }
  const width = 600, height = 160, pad = 24;
  const values = curve.map((p) => p.equity);
  const minV = Math.min(...values), maxV = Math.max(...values);
  const range = maxV - minV || 1;
  const stepX = (width - pad * 2) / (curve.length - 1);
  const points = curve
    .map((p, i) => {
      const x = pad + i * stepX;
      const y = pad + (height - pad * 2) * (1 - (p.equity - minV) / range);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const color = values[values.length - 1] >= values[0] ? "var(--pnl-positive)" : "var(--pnl-negative)";
  container.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" class="equity-curve-svg" preserveAspectRatio="none">
      <polyline points="${points}" fill="none" stroke="${color}" stroke-width="2" />
    </svg>
  `;
}

function statsRowHtml(s) {
  return `
    <td class="num">${s.trade_count}</td>
    <td class="num">${s.win_rate != null ? (s.win_rate * 100).toFixed(1) + "%" : "—"}</td>
    <td class="num">${s.expectancy_r != null ? s.expectancy_r.toFixed(2) : "—"}</td>
    <td class="num">${s.profit_factor != null ? s.profit_factor.toFixed(2) : "—"}</td>
    <td class="num">₹${fmt(s.total_realized_pnl)}</td>
  `;
}

function renderJournalStats(stats) {
  state.journalStats = stats;
  el.journalSampleWarning.hidden = !stats.sample_size_warning;

  const o = stats.overall;
  const row = (label, value) => `<div class="row"><span>${label}</span><strong>${value}</strong></div>`;
  el.journalOverallStats.innerHTML = [
    row("Trades", o.trade_count),
    row("Win Rate", o.win_rate != null ? `${(o.win_rate * 100).toFixed(1)}%` : "—"),
    row("Expectancy (R)", o.expectancy_r != null ? o.expectancy_r.toFixed(2) : "—"),
    row("Profit Factor", o.profit_factor != null ? o.profit_factor.toFixed(2) : "—"),
    row("Total Realized P&amp;L", `₹${fmt(o.total_realized_pnl)}`),
    row("Max Drawdown", `₹${fmt(o.max_drawdown_amount)} (${o.max_drawdown_pct.toFixed(1)}%)`),
    row("Avg Holding Period", o.avg_holding_period_calendar_days != null ? `${o.avg_holding_period_calendar_days.toFixed(1)} days` : "—"),
    row("Starting Capital", `₹${fmt(o.starting_capital)}`),
  ].join("");

  renderEquityCurveSvg(el.journalEquityCurve, o.equity_curve);

  el.journalStrategyBody.innerHTML = "";
  if (stats.by_strategy.length === 0) {
    el.journalStrategyBody.innerHTML = `<tr class="empty-row"><td colspan="6">No closed trades yet</td></tr>`;
  } else {
    for (const s of stats.by_strategy) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${s.strategy_name}</td>${statsRowHtml(s)}`;
      el.journalStrategyBody.appendChild(tr);
    }
  }

  el.journalSymbolBody.innerHTML = "";
  if (stats.by_symbol.length === 0) {
    el.journalSymbolBody.innerHTML = `<tr class="empty-row"><td colspan="6">No closed trades yet</td></tr>`;
  } else {
    for (const s of stats.by_symbol) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${s.symbol}</td>${statsRowHtml(s)}`;
      el.journalSymbolBody.appendChild(tr);
    }
  }
}

async function loadJournalStats(mode) {
  try {
    const stats = await API.getJournalStats(mode);
    renderJournalStats(stats);
  } catch (e) {
    // leave prior state visible on a transient load failure
  }
}

async function initJournal() {
  let defaultMode = "live";
  try {
    const summary = await API.getJournalModeSummary();
    defaultMode = summary.live > 0 ? "live" : "dry_run";
  } catch (e) {
    // fall back to "live" as the default
  }
  el.journalModeSelect.value = defaultMode;
  await loadJournalStats(defaultMode);
}

el.journalModeSelect.addEventListener("change", () => {
  loadJournalStats(el.journalModeSelect.value);
});

function renderWatchTable(bodyEl, watchList) {
  bodyEl.innerHTML = "";
  if (!watchList || watchList.length === 0) {
    bodyEl.innerHTML = `<tr class="empty-row"><td colspan="4">Nothing close to breaking out</td></tr>`;
    return;
  }
  for (const w of watchList) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${w.symbol}</td>
      <td class="num">${fmt(w.last_close)}</td>
      <td class="num">${fmt(w.donchian_high_20)}</td>
      <td class="num">${w.distance_pct.toFixed(2)}%</td>
    `;
    bodyEl.appendChild(tr);
  }
}

function renderTodaysActionsView(data) {
  el.taBuyBody.innerHTML = "";
  if (data.buy_candidates.length === 0) {
    el.taBuyBody.innerHTML = `<tr class="empty-row"><td colspan="5">No buy candidates</td></tr>`;
  } else {
    for (const t of data.buy_candidates) {
      const tr = document.createElement("tr");
      tr.classList.add("row-clickable");
      tr.addEventListener("click", () => openConfirmDialog(t));
      tr.innerHTML = `
        <td>${t.symbol}</td><td class="num">${fmt(t.entry_price)}</td>
        <td class="num">${fmt(t.stop_loss)}</td><td class="num">${fmt(t.target_1)}</td><td>${t.rationale}</td>
      `;
      el.taBuyBody.appendChild(tr);
    }
  }

  el.taExitBody.innerHTML = "";
  if (data.exit_candidates.length === 0) {
    el.taExitBody.innerHTML = `<tr class="empty-row"><td colspan="3">No exit candidates</td></tr>`;
  } else {
    for (const t of data.exit_candidates) {
      const tr = document.createElement("tr");
      tr.classList.add("row-clickable");
      tr.addEventListener("click", () => openConfirmDialog(t));
      tr.innerHTML = `<td>${t.symbol}</td><td class="num">${fmt(t.quantity, 0)}</td><td>${t.rationale}</td>`;
      el.taExitBody.appendChild(tr);
    }
  }

  el.taAdjustmentsList.innerHTML = "";
  if (data.pending_adjustments.length === 0) {
    el.taAdjustmentsList.innerHTML = `<div class="empty-row">No pending adjustments</div>`;
  } else {
    for (const p of data.pending_adjustments) {
      const div = document.createElement("div");
      div.className = "stop-adjustment-card";
      div.innerHTML = `<div><strong>${p.symbol}</strong> — ₹${fmt(p.current_stop)} → ₹${fmt(p.proposed_stop)}<div class="rationale">${p.reason}</div></div>`;
      el.taAdjustmentsList.appendChild(div);
    }
  }

  renderWatchTable(el.taWatchBody, data.watch_list);
  el.taNoAction.textContent = `${data.no_action_count} of ${data.total_watchlist} watchlist symbol(s) — no signal`;
}

function renderTodaysActionsPanel(data) {
  if (!el.todaysActionsPanelBody) return;
  el.todaysActionsPanelBody.innerHTML = `
    <div class="row"><span>Buy candidates</span><strong>${data.buy_candidates.length}</strong></div>
    <div class="row"><span>Exit candidates</span><strong>${data.exit_candidates.length}</strong></div>
    <div class="row"><span>Pending adjustments</span><strong>${data.pending_adjustments.length}</strong></div>
    <div class="row"><span>Watch</span><strong>${data.watch_list.length}</strong></div>
    <div class="row"><span>No action</span><strong>${data.no_action_count} of ${data.total_watchlist}</strong></div>
  `;
}

async function loadTodaysActions() {
  try {
    const data = await API.getTodaysActions();
    state.todaysActions = data;
    renderTodaysActionsView(data);
    renderTodaysActionsPanel(data);
  } catch (e) {
    // leave prior state visible on a transient load failure
  }
}

function renderTradeQueue() {
  const tickets = state.tradeQueueTickets || [];
  const ready = tickets.filter((t) => t.status === "pending_confirmation");
  const waiting = tickets.filter((t) => t.status === "draft");
  const rejectedFilter = el.tqRejectedFilter.value;
  const rejected = tickets.filter((t) =>
    rejectedFilter ? t.status === rejectedFilter : t.status === "rejected" || t.status === "risk_rejected"
  );
  const executed = tickets
    .filter((t) => t.status === "executed")
    .sort((a, b) => (a.created_at < b.created_at ? 1 : -1));

  renderTicketsTable(ready, el.tqReadyBody);
  renderTicketsTable(waiting, el.tqWaitingBody);
  renderTicketsTable(rejected, el.tqRejectedBody);
  renderTicketsTable(executed, el.tqExecutedBody);
  renderWatchTable(el.tqWatchRefBody, (state.todaysActions && state.todaysActions.watch_list) || []);
}

async function loadTradeQueue() {
  try {
    state.tradeQueueTickets = await API.listTickets({});
    renderTradeQueue();
  } catch (e) {
    // leave prior state visible on a transient load failure
  }
}

el.tqRejectedFilter.addEventListener("change", renderTradeQueue);

document.querySelectorAll(".tab-btn[data-tq-tab]").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn[data-tq-tab]").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".tq-panel").forEach((p) => {
      p.hidden = p.dataset.tqPanel !== btn.dataset.tqTab;
    });
  });
});

async function loadPhase6Data() {
  await initJournal();
  await loadTodaysActions();
  await loadTradeQueue();
}

el.watchlistAddForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  el.watchlistAddError.textContent = "";
  const formData = new FormData(el.watchlistAddForm);
  try {
    await API.addWatchlistSymbol({
      symbol: formData.get("symbol"),
      notes: formData.get("notes") || null,
    });
    el.watchlistAddForm.reset();
    await loadWatchlist();
  } catch (e) {
    el.watchlistAddError.textContent = e.message;
  }
});

async function loadWatchlist() {
  try {
    state.watchlist = await API.getWatchlist();
    renderWatchlistTable();
    renderWatchlistSummaryPanel();
  } catch (e) {
    // non-critical; leave prior state visible
  }
}

// ---- Phase 3: tickets, orders/trades/GTTs, mode & trailing status ----

function statusBadgeHtml(status) {
  return `<span class="status-badge ${status}">${status.replace(/_/g, " ")}</span>`;
}

function ticketRowHtml(t) {
  return `
    <td>${t.symbol}</td>
    <td>${t.transaction_type}</td>
    <td class="num">${fmt(t.quantity, 0)}</td>
    <td class="num">${fmt(t.entry_price)}</td>
    <td class="num">${fmt(t.current_stop_loss ?? t.stop_loss)}</td>
    <td class="num">${fmt(t.target_1)}</td>
    <td>${t.trailing_enabled ? (t.trailing_active ? "active" : "armed") : "off"}</td>
    <td>${statusBadgeHtml(t.status)}</td>
    <td>${t.mode ?? "—"}</td>
    <td>${t.created_at}</td>
  `;
}

function renderTicketsTable(tickets = state.tickets, bodyEl = el.ticketsBody) {
  // Parameterized (defaulting to the Ticket History view's own state/body)
  // so the Strategies section's Signal Queue can reuse this verbatim
  // against its own ticket array and table body, rather than duplicating it.
  bodyEl.innerHTML = "";
  if (tickets.length === 0) {
    bodyEl.innerHTML = `<tr class="empty-row"><td colspan="10">No tickets yet</td></tr>`;
    return;
  }
  for (const t of tickets) {
    const tr = document.createElement("tr");
    tr.dataset.ticketId = t.id;
    tr.classList.add("row-clickable");
    if (t.status === "pending_confirmation" || t.status === "risk_rejected") {
      tr.title = t.status === "risk_rejected"
        ? "Click to see why this ticket was blocked"
        : "Click to review and confirm/reject this ticket";
      tr.addEventListener("click", () => openConfirmDialog(t));
    } else {
      // draft, confirmed, executed, rejected, cancelled, failed — the
      // general read-only (or, for draft, edit/delete) detail view.
      tr.title = "Click to view details";
      tr.addEventListener("click", () => openDetailDialog(t));
    }
    if (t.status === "protection_failed" || (t.status === "executed" && !t.kite_gtt_id && t.mode === "live")) {
      tr.classList.add("row-flagged");
    }
    if (t.archived) {
      tr.classList.add("stale");
    }
    tr.innerHTML = ticketRowHtml(t);
    bodyEl.appendChild(tr);
  }
}

const _ORDER_TERMINAL_STATUSES = new Set(["COMPLETE", "CANCELLED", "REJECTED"]);

function orderRowHtml(o) {
  const cancellable = !_ORDER_TERMINAL_STATUSES.has(o.status);
  return `
    <td>${o.tradingsymbol ?? ""}</td>
    <td>${o.transaction_type ?? ""}</td>
    <td class="num">${fmt(o.quantity, 0)}</td>
    <td class="num">${fmt(o.price)}</td>
    <td>${o.status ?? ""}</td>
    <td>${o.order_type ?? ""}</td>
    <td>${o.linked_ticket_id ? `#${o.linked_ticket_id}` : "—"}</td>
    <td>${cancellable ? `<button class="row-action-btn danger" data-action="cancel-order" data-order-id="${o.order_id}" data-variety="${o.variety ?? "regular"}">Cancel</button>` : ""}</td>
  `;
}

function renderOrdersTable() {
  el.ordersBody.innerHTML = "";
  if (state.orders.length === 0) {
    el.ordersBody.innerHTML = `<tr class="empty-row"><td colspan="8">No orders</td></tr>`;
    return;
  }
  for (const o of state.orders) {
    const tr = document.createElement("tr");
    if (o.linked_ticket_id) tr.classList.add("row-linked");
    tr.innerHTML = orderRowHtml(o);
    el.ordersBody.appendChild(tr);
  }
}

el.ordersBody.addEventListener("click", async (event) => {
  const btn = event.target.closest('button[data-action="cancel-order"]');
  if (!btn) return;
  if (!confirm(`Cancel order ${btn.dataset.orderId}?`)) return;
  try {
    await API.cancelOrder(btn.dataset.orderId, btn.dataset.variety);
    state.orders = await API.orders();
    renderOrdersTable();
  } catch (e) {
    alert(e.message);
  }
});

function renderTradesTable() {
  el.tradesBody.innerHTML = "";
  if (state.trades.length === 0) {
    el.tradesBody.innerHTML = `<tr class="empty-row"><td colspan="5">No trades</td></tr>`;
    return;
  }
  for (const t of state.trades) {
    const tr = document.createElement("tr");
    if (t.linked_ticket_id) tr.classList.add("row-linked");
    tr.innerHTML = `
      <td>${t.tradingsymbol ?? ""}</td>
      <td>${t.transaction_type ?? ""}</td>
      <td class="num">${fmt(t.quantity, 0)}</td>
      <td class="num">${fmt(t.average_price)}</td>
      <td>${t.linked_ticket_id ? `#${t.linked_ticket_id}` : "—"}</td>
    `;
    el.tradesBody.appendChild(tr);
  }
}

function renderGttsTable() {
  el.gttsBody.innerHTML = "";
  if (state.gtts.length === 0) {
    el.gttsBody.innerHTML = `<tr class="empty-row"><td colspan="6">No GTTs</td></tr>`;
    return;
  }
  for (const g of state.gtts) {
    const tr = document.createElement("tr");
    if (g.linked_ticket_id) tr.classList.add("row-linked");
    const triggers = (g.condition?.trigger_values || []).map((v) => fmt(v)).join(" / ");
    const cancellable = g.status === "active";
    tr.innerHTML = `
      <td>${g.condition?.tradingsymbol ?? ""}</td>
      <td>${g.type ?? ""}</td>
      <td class="num">${triggers}</td>
      <td>${g.status ?? ""}</td>
      <td>${g.linked_ticket_id ? `#${g.linked_ticket_id}` : "—"}</td>
      <td>${cancellable ? `<button class="row-action-btn danger" data-action="cancel-gtt" data-gtt-id="${g.id}">Cancel</button>` : ""}</td>
    `;
    el.gttsBody.appendChild(tr);
  }
}

el.gttsBody.addEventListener("click", async (event) => {
  const btn = event.target.closest('button[data-action="cancel-gtt"]');
  if (!btn) return;
  if (!confirm(`Cancel GTT ${btn.dataset.gttId}? The position will remain open but unprotected.`)) return;
  try {
    await API.cancelGtt(btn.dataset.gttId);
    state.gtts = await API.gtts();
    renderGttsTable();
  } catch (e) {
    alert(e.message);
  }
});

function renderModeBanner() {
  el.modeBanner.hidden = false;
  el.modeToggleCheckbox.checked = state.executionMode === "live";
  if (state.executionMode === "live") {
    el.modeBanner.className = "mode-banner mode-live";
    el.modeBannerText.textContent = "LIVE — real orders will be placed with real money";
  } else {
    el.modeBanner.className = "mode-banner mode-dry-run";
    el.modeBannerText.textContent = "DRY RUN — no real orders will be placed";
  }
}

// ---- Phase 8: kill switch ----

el.cancelAllPendingBtn.addEventListener("click", async () => {
  try {
    const openTickets = await API.listTickets({});
    const pending = openTickets.filter((t) => t.status === "draft" || t.status === "pending_confirmation");
    const symbols = [...new Set(pending.map((t) => t.symbol))];
    el.cancelAllPendingSummary.textContent = pending.length
      ? `This will cancel ${pending.length} ticket(s): ${symbols.join(", ")}.`
      : "There are no draft or pending tickets to cancel.";
    el.cancelAllPendingConfirmBtn.disabled = pending.length === 0;
    el.cancelAllPendingDialog.showModal();
  } catch (e) {
    alert(e.message);
  }
});

el.cancelAllPendingCancelBtn.addEventListener("click", () => el.cancelAllPendingDialog.close());

el.cancelAllPendingConfirmBtn.addEventListener("click", async () => {
  try {
    const result = await API.cancelAllPending();
    el.cancelAllPendingDialog.close();
    await Promise.all([loadSignalQueue(), loadTradeQueue()]);
    alert(`Cancelled ${result.count} ticket(s).`);
  } catch (e) {
    alert(e.message);
  }
});

el.emergencyExitAllBtn.addEventListener("click", async () => {
  try {
    const positions = await API.emergencyExitPreview();
    el.emergencyExitModeLabel.textContent = state.executionMode === "live" ? "LIVE" : "Dry Run";
    el.emergencyExitPreviewBody.innerHTML = positions.length
      ? positions.map((p) => `
          <tr>
            <td>${p.symbol}</td><td class="num">${fmt(p.quantity, 0)}</td>
            <td class="num">${fmt(p.entry_price)}</td><td class="num">${fmt(p.current_stop_loss ?? p.stop_loss)}</td>
            <td>${p.strategy_name ?? "manual"}</td>
          </tr>
        `).join("")
      : `<tr class="empty-row"><td colspan="5">No open positions</td></tr>`;
    el.emergencyExitPhrase.value = "";
    el.emergencyExitError.textContent = "";
    el.emergencyExitConfirmBtn.disabled = true;
    el.emergencyExitDialog.showModal();
  } catch (e) {
    alert(e.message);
  }
});

el.emergencyExitPhrase.addEventListener("input", () => {
  el.emergencyExitConfirmBtn.disabled = el.emergencyExitPhrase.value.trim() !== "EXIT ALL";
});

el.emergencyExitCancelBtn.addEventListener("click", () => el.emergencyExitDialog.close());

el.emergencyExitConfirmBtn.addEventListener("click", async () => {
  el.emergencyExitError.textContent = "";
  try {
    const result = await API.emergencyExitConfirm(el.emergencyExitPhrase.value.trim());
    el.emergencyExitDialog.close();
    await Promise.all([loadSignalQueue(), loadTradeQueue(), loadTodaysActions()]);
    alert(`Emergency exit complete: ${result.count} position(s) processed.`);
  } catch (e) {
    el.emergencyExitError.textContent = e.message;
  }
});

// ---- Phase 8: order/position reconciliation ----

function reconciliationItemHtml(m) {
  return `
    <div class="reconciliation-item">
      <div>${m.detail}</div>
      <button class="btn btn-small" data-action="mark-reviewed" data-id="${m.id}">Mark Reviewed</button>
    </div>
  `;
}

function renderReconciliationList() {
  el.reconciliationList.innerHTML = state.reconciliationMismatches.length
    ? state.reconciliationMismatches.map(reconciliationItemHtml).join("")
    : `<div class="empty-row">No unresolved mismatches</div>`;
}

async function loadReconciliationMismatches() {
  try {
    state.reconciliationMismatches = await API.listMismatches(false);
    renderReconciliationList();
    renderAll();
  } catch (e) {
    // leave prior state visible on a transient load failure
  }
}

el.reconciliationList.addEventListener("click", async (event) => {
  const btn = event.target.closest('button[data-action="mark-reviewed"]');
  if (!btn) return;
  try {
    await API.markMismatchReviewed(btn.dataset.id);
    await loadReconciliationMismatches();
  } catch (e) {
    alert(e.message);
  }
});

function renderTrailingIndicator() {
  const hasTrailingTickets = state.tickets.some((t) => t.trailing_enabled && t.status === "executed" && !t.closed_at);
  if (!state.trailingHeartbeat && !hasTrailingTickets) {
    el.trailingIndicator.hidden = true;
    return;
  }
  el.trailingIndicator.hidden = false;

  if (!state.trailingHeartbeat) {
    el.trailingIndicator.className = "trailing-indicator unknown";
    el.trailingIndicator.title = "Trailing engine: no heartbeat received yet";
    return;
  }

  const ageMs = Date.now() - new Date(state.trailingHeartbeat.last_run_at).getTime();
  const staleThresholdMs = 2 * TRAILING_CHECK_INTERVAL_SECONDS * 1000;
  if (ageMs <= staleThresholdMs) {
    el.trailingIndicator.className = "trailing-indicator active";
    el.trailingIndicator.title = `Trailing engine active — watching ${state.trailingHeartbeat.active_ticket_count} ticket(s)`;
  } else {
    el.trailingIndicator.className = "trailing-indicator stale";
    el.trailingIndicator.title = `Trailing engine STALE — no heartbeat for ${Math.round(ageMs / 1000)}s. ` +
      `The app may be offline; a trailing stop may not be moving.`;
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
  renderWatchlistTable();
  renderWatchlistSummaryPanel();

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

  const watched = state.watchlist.find((w) => w.instrument_token === tick.instrument_token);
  if (watched) {
    watched.last_price = tick.last_price;
    if (tick.close_price) {
      watched.day_change_percentage = ((tick.last_price - tick.close_price) / tick.close_price) * 100;
    }
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
    if (msg.type === "trailing_heartbeat") {
      state.trailingHeartbeat = { last_run_at: msg.last_run_at, active_ticket_count: msg.active_ticket_count };
      renderTrailingIndicator();
    }
    if (msg.type === "trailing_error") {
      state.trailingErrorAlerts.push({
        id: `trailing-error-${msg.ticket_id}-${Date.now()}`,
        severity: "urgent",
        message: msg.message,
        scrollTargetSelector: `tr[data-ticket-id="${msg.ticket_id}"]`,
      });
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

// ---- Phase 3: ticket confirm dialog, form, mode toggle, logout ----

let activeConfirmTicketId = null;

function ticketConfirmHtml(t) {
  const metricsRow = (label, value) => `<div class="row"><span>${label}</span><strong>${value}</strong></div>`;
  const capital = t.entry_price * t.quantity;
  const risk = Math.abs(t.entry_price - t.stop_loss) * t.quantity;
  const rr = t.risk_reward_ratio != null ? t.risk_reward_ratio.toFixed(2) : "—";
  const trailingText = t.trailing_enabled
    ? `Enabled — trigger: ${t.trailing_trigger}, distance ₹${fmt(t.trailing_distance)}`
    : "Off";
  return `
    <div class="row"><span>${t.symbol}</span><strong>${t.transaction_type}</strong></div>
    ${metricsRow("Entry", `₹${fmt(t.entry_price)} (${t.entry_order_type}) × ${fmt(t.quantity, 0)}`)}
    ${metricsRow("Capital", `₹${fmt(capital)}`)}
    ${metricsRow("Stop-loss", `₹${fmt(t.stop_loss)} (Risk ₹${fmt(risk)})`)}
    ${metricsRow("Target 1", `₹${fmt(t.target_1)}`)}
    ${t.target_2 ? metricsRow("Target 2", `₹${fmt(t.target_2)}`) : ""}
    ${metricsRow("Trailing", trailingText)}
    ${metricsRow("Product", t.product_type)}
    ${metricsRow("GTT", t.use_gtt ? "Yes" : "No")}
    ${metricsRow("Risk:Reward", `1:${rr}`)}
    <div class="rationale">${t.rationale}</div>
  `;
}

function riskCheckRowHtml(c) {
  const icon = c.skipped ? "⚠" : c.passed ? "✓" : "✗";
  const cls = c.skipped ? "skipped" : c.passed ? "passed" : "failed";
  let value;
  if (c.skipped || c.actual_value === null || c.actual_value === undefined) {
    // Non-numeric checks (e.g. an exit ticket's "position validated"
    // check) have no actual_value/limit_value to format — show the
    // message directly, same as the skipped-check path.
    value = c.message;
  } else if (c.unit === "ratio") {
    value = `1:${c.actual_value.toFixed(2)} (min 1:${c.limit_value})`;
  } else if (c.unit === "count") {
    value = `${c.actual_value} of ${c.limit_value} max`;
  } else {
    value = `${c.actual_value.toFixed(1)}% (max ${c.limit_value}%)`;
  }
  return `<div class="risk-check-row ${cls}"><span class="icon">${icon}</span><span>${c.label}</span><strong>${value}</strong></div>`;
}

function renderRiskCheckSection(checks) {
  if (!checks || checks.length === 0) {
    el.ticketRiskCheckBody.innerHTML = "";
    return;
  }
  el.ticketRiskCheckBody.innerHTML = `<h3>Risk Check</h3>` + checks.map(riskCheckRowHtml).join("");
}

function openConfirmDialog(ticket) {
  activeConfirmTicketId = ticket.id;
  const rejectedMode = ticket.status === "risk_rejected";
  el.ticketConfirmDialog.classList.toggle("risk-rejected-mode", rejectedMode);
  el.ticketConfirmTitle.textContent = rejectedMode ? "Ticket Rejected — Risk Checks Failed" : "Confirm Trade Ticket";
  el.ticketConfirmBody.innerHTML = ticketConfirmHtml(ticket);
  renderRiskCheckSection(ticket.risk_check_results);
  el.ticketRejectBtn.hidden = rejectedMode;
  el.ticketConfirmBtn.hidden = rejectedMode;
  el.ticketModifyBtn.hidden = !rejectedMode;
  el.ticketArchiveBtn.hidden = !rejectedMode;
  el.ticketConfirmDialog.showModal();
}

el.ticketArchiveBtn.addEventListener("click", async () => {
  if (activeConfirmTicketId == null) return;
  try {
    await API.deleteTicket(activeConfirmTicketId);
  } catch (e) {
    // fall through — history refresh will show real state either way
  }
  el.ticketConfirmDialog.close();
  await refreshTickets();
});

el.ticketRejectBtn.addEventListener("click", async () => {
  if (activeConfirmTicketId == null) return;
  try {
    await API.rejectTicket(activeConfirmTicketId);
  } catch (e) {
    // fall through — history refresh will show real state either way
  }
  el.ticketConfirmDialog.close();
  await refreshTickets();
});

el.ticketConfirmBtn.addEventListener("click", async () => {
  if (activeConfirmTicketId == null) return;
  el.ticketConfirmBtn.disabled = true;
  try {
    await API.confirmTicket(activeConfirmTicketId);
  } catch (e) {
    // execution failures come back as data on the ticket, not thrown errors;
    // a thrown error here means the request itself failed (network/auth).
  } finally {
    el.ticketConfirmBtn.disabled = false;
  }
  el.ticketConfirmDialog.close();
  await refreshTickets();
  await refreshOrderBooks();
});

el.ticketModifyBtn.addEventListener("click", async () => {
  const t = state.tickets.find((x) => x.id === activeConfirmTicketId);
  el.ticketConfirmDialog.close();
  if (!t) return;

  el.ticketSymbolInput.value = t.symbol;
  el.ticketForm.querySelector('[name="transaction_type"]').value = t.transaction_type;
  el.ticketQuantityInput.value = t.quantity;
  el.ticketForm.querySelector('[name="entry_order_type"]').value = t.entry_order_type;
  el.ticketEntryPriceInput.value = t.entry_price;
  el.ticketForm.querySelector('[name="product_type"]').value = t.product_type;
  el.ticketStopLossInput.value = t.stop_loss;
  el.ticketForm.querySelector('[name="target_1"]').value = t.target_1;
  el.ticketForm.querySelector('[name="target_2"]').value = t.target_2 ?? "";
  el.ticketTrailingEnabled.checked = !!t.trailing_enabled;
  el.ticketTrailingTrigger.disabled = !t.trailing_enabled;
  el.ticketTrailingDistance.disabled = !t.trailing_enabled;
  if (t.trailing_enabled) {
    el.ticketTrailingTrigger.value = t.trailing_trigger;
    el.ticketTrailingDistance.value = t.trailing_distance;
  }
  el.ticketForm.querySelector('[name="rationale"]').value = t.rationale;

  // Pre-filled quantity should stick — don't let the position-size
  // auto-populate silently overwrite it right after this.
  state.quantityManuallyEdited = true;
  state.ticketDraftDirty = true;

  try {
    const matches = await API.searchInstruments(t.symbol);
    state.lastSymbolMatches = matches;
    el.symbolOptions.innerHTML = matches
      .map((m) => `<option value="${m.tradingsymbol}">${m.name ?? ""}</option>`)
      .join("");
  } catch (e) {
    // user can retype the symbol to re-trigger search if this fails
  }

  el.ticketForm.scrollIntoView({ behavior: "smooth", block: "start" });
});

function currentTicketFilters() {
  return {
    status: el.tfStatus.value,
    source: el.tfSource.value,
    symbol: el.tfSymbol.value.trim(),
    date_from: el.tfDateFrom.value,
    date_to: el.tfDateTo.value,
    include_archived: el.tfShowArchived.checked,
  };
}

async function refreshTickets() {
  try {
    state.tickets = await API.listTickets(currentTicketFilters());
    renderTicketsTable();
    renderTrailingIndicator();
    renderAll();
  } catch (e) {
    // leave stale data visible rather than blowing away the dashboard
  }
}

el.tfApplyBtn.addEventListener("click", (e) => { e.preventDefault(); refreshTickets(); });
el.tfClearBtn.addEventListener("click", (e) => {
  e.preventDefault();
  el.tfStatus.value = "";
  el.tfSource.value = "";
  el.tfSymbol.value = "";
  el.tfDateFrom.value = "";
  el.tfDateTo.value = "";
  el.tfShowArchived.checked = false;
  refreshTickets();
});

// ---- Phase 5: ticket detail dialog (view/edit/delete/archive) ----

function eventRowHtml(e) {
  return `<div class="event-row">
    <span class="event-time">${e.created_at}</span>
    <span class="event-type">${e.event_type.replace(/_/g, " ")}</span>
    <span class="event-detail">${e.detail ?? ""}</span>
  </div>`;
}

function renderTicketDetailEvents(events) {
  el.ticketDetailEvents.innerHTML = `<h3>History</h3>` + (events && events.length
    ? events.map(eventRowHtml).join("")
    : `<div class="event-row"><span class="event-detail">No events yet</span></div>`);
}

async function openDetailDialog(ticket) {
  state.activeDetailTicketId = ticket.id;
  el.ticketEditForm.hidden = true;
  el.ticketDetailBody.hidden = false;
  el.ticketEditError.textContent = "";

  const statusRow = `<div class="row"><span>Status</span><strong>${ticket.status.replace(/_/g, " ")}</strong></div>`;
  const modeRow = `<div class="row"><span>Mode</span><strong>${ticket.mode ?? "—"}</strong></div>`;
  const orderRow = ticket.kite_order_id
    ? `<div class="row"><span>Kite Order ID</span><strong>${ticket.kite_order_id}</strong></div>` : "";
  const gttRow = ticket.kite_gtt_id
    ? `<div class="row"><span>Kite GTT ID</span><strong>${ticket.kite_gtt_id}</strong></div>` : "";
  el.ticketDetailBody.innerHTML = statusRow + modeRow + orderRow + gttRow + ticketConfirmHtml(ticket);
  renderTicketDetailEvents([]); // populated below once we fetch full detail

  const isDraft = ticket.status === "draft";
  const isSoftDeletable = ["rejected", "cancelled", "failed"].includes(ticket.status);
  el.ticketDetailEditBtn.hidden = !isDraft;
  el.ticketDetailDeleteBtn.hidden = !isDraft;
  el.ticketDetailArchiveBtn.hidden = !isSoftDeletable;

  el.ticketDetailDialog.showModal();

  try {
    const full = await API.getTicket(ticket.id);
    renderTicketDetailEvents(full.events);
  } catch (e) {
    // dialog already shows the core fields; timeline just stays empty on failure
  }
}

el.ticketDetailCloseBtn.addEventListener("click", () => {
  el.ticketDetailDialog.close();
});

el.ticketDetailDeleteBtn.addEventListener("click", async () => {
  if (state.activeDetailTicketId == null) return;
  if (!confirm("Permanently delete this draft ticket? This cannot be undone.")) return;
  try {
    await API.deleteTicket(state.activeDetailTicketId);
  } catch (e) {
    // fall through — refresh shows real state either way
  }
  el.ticketDetailDialog.close();
  await refreshTickets();
});

el.ticketDetailArchiveBtn.addEventListener("click", async () => {
  if (state.activeDetailTicketId == null) return;
  try {
    await API.deleteTicket(state.activeDetailTicketId);
  } catch (e) {
    // fall through
  }
  el.ticketDetailDialog.close();
  await refreshTickets();
});

el.ticketDetailEditBtn.addEventListener("click", () => {
  const t = state.tickets.find((x) => x.id === state.activeDetailTicketId);
  if (!t) return;
  el.ticketDetailBody.hidden = true;
  el.ticketEditForm.hidden = false;

  document.getElementById("te-symbol").value = t.symbol;
  el.ticketEditForm.querySelector('[name="transaction_type"]').value = t.transaction_type;
  el.ticketEditForm.querySelector('[name="quantity"]').value = t.quantity;
  el.ticketEditForm.querySelector('[name="entry_order_type"]').value = t.entry_order_type;
  el.ticketEditForm.querySelector('[name="entry_price"]').value = t.entry_price;
  el.ticketEditForm.querySelector('[name="product_type"]').value = t.product_type;
  el.ticketEditForm.querySelector('[name="stop_loss"]').value = t.stop_loss;
  el.ticketEditForm.querySelector('[name="target_1"]').value = t.target_1;
  el.ticketEditForm.querySelector('[name="target_2"]').value = t.target_2 ?? "";
  el.teTrailingEnabled.checked = !!t.trailing_enabled;
  el.teTrailingTrigger.disabled = !t.trailing_enabled;
  el.teTrailingDistance.disabled = !t.trailing_enabled;
  if (t.trailing_enabled) {
    el.teTrailingTrigger.value = t.trailing_trigger;
    el.teTrailingDistance.value = t.trailing_distance;
  }
  el.ticketEditForm.querySelector('[name="rationale"]').value = t.rationale;
});

el.teTrailingEnabled.addEventListener("change", () => {
  const enabled = el.teTrailingEnabled.checked;
  el.teTrailingTrigger.disabled = !enabled;
  el.teTrailingDistance.disabled = !enabled;
});

el.ticketEditForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  el.ticketEditError.textContent = "";
  if (state.activeDetailTicketId == null) return;

  const formData = new FormData(el.ticketEditForm);
  const trailingEnabled = el.teTrailingEnabled.checked;
  const body = {
    symbol: document.getElementById("te-symbol").value.trim().toUpperCase(),
    transaction_type: formData.get("transaction_type"),
    quantity: Number(formData.get("quantity")),
    entry_price: Number(formData.get("entry_price")),
    entry_order_type: formData.get("entry_order_type"),
    stop_loss: Number(formData.get("stop_loss")),
    target_1: Number(formData.get("target_1")),
    target_2: formData.get("target_2") ? Number(formData.get("target_2")) : null,
    trailing_enabled: trailingEnabled,
    trailing_trigger: trailingEnabled ? formData.get("trailing_trigger") : null,
    trailing_distance: trailingEnabled && formData.get("trailing_distance")
      ? Number(formData.get("trailing_distance")) : null,
    product_type: formData.get("product_type"),
    rationale: formData.get("rationale"),
  };

  try {
    const updated = await API.editTicket(state.activeDetailTicketId, body);
    el.ticketDetailDialog.close();
    await refreshTickets();
    // Re-open on the ticket's new state so the risk-check outcome is visible immediately.
    openConfirmDialog(updated);
  } catch (e) {
    el.ticketEditError.textContent = e.message;
  }
});

async function refreshOrderBooks() {
  try {
    const [orders, trades, gtts] = await Promise.all([API.orders(), API.trades(), API.gtts()]);
    state.orders = orders;
    state.trades = trades;
    state.gtts = gtts;
    renderOrdersTable();
    renderTradesTable();
    renderGttsTable();
  } catch (e) {
    // non-critical view; leave stale data visible
  }
}

// Ticket form: symbol autocomplete
let symbolSearchTimer = null;
el.ticketSymbolInput.addEventListener("input", () => {
  state.ticketDraftDirty = true;
  clearTimeout(symbolSearchTimer);
  const q = el.ticketSymbolInput.value.trim();
  if (q.length < 2) return;
  symbolSearchTimer = setTimeout(async () => {
    try {
      const matches = await API.searchInstruments(q);
      state.lastSymbolMatches = matches;
      el.symbolOptions.innerHTML = matches
        .map((m) => `<option value="${m.tradingsymbol}">${m.name ?? ""}</option>`)
        .join("");
    } catch (e) {
      // leave existing options as-is on a transient search failure
    }
  }, 250);
});

el.ticketTrailingEnabled.addEventListener("change", () => {
  const enabled = el.ticketTrailingEnabled.checked;
  el.ticketTrailingTrigger.disabled = !enabled;
  el.ticketTrailingDistance.disabled = !enabled;
});

// Position-size auto-populate: fills quantity from entry+stop-loss, but
// never overwrites a value the user typed themselves.
el.ticketQuantityInput.addEventListener("input", () => {
  state.quantityManuallyEdited = true;
});

let positionSizeTimer = null;
function maybeAutoPopulateQuantity() {
  if (state.quantityManuallyEdited) return;
  const entryPrice = Number(el.ticketEntryPriceInput.value);
  const stopLoss = Number(el.ticketStopLossInput.value);
  if (!(entryPrice > 0) || !(stopLoss > 0)) return;
  clearTimeout(positionSizeTimer);
  positionSizeTimer = setTimeout(async () => {
    try {
      const r = await API.getPositionSize(entryPrice, stopLoss);
      if (!state.quantityManuallyEdited && r.suggested_qty > 0) {
        el.ticketQuantityInput.value = r.suggested_qty;
      }
    } catch (e) {
      // leave quantity as-is on a transient failure
    }
  }, 300);
}
el.ticketEntryPriceInput.addEventListener("input", maybeAutoPopulateQuantity);
el.ticketStopLossInput.addEventListener("input", maybeAutoPopulateQuantity);

for (const field of el.ticketForm.querySelectorAll("input, select, textarea")) {
  field.addEventListener("input", () => { state.ticketDraftDirty = true; });
}

el.ticketForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  el.ticketFormError.textContent = "";

  const symbol = el.ticketSymbolInput.value.trim().toUpperCase();
  const knownMatch = state.lastSymbolMatches.find((m) => m.tradingsymbol === symbol);
  if (!knownMatch) {
    el.ticketFormError.textContent = "Pick a symbol from the search results.";
    return;
  }

  const formData = new FormData(el.ticketForm);
  const trailingEnabled = el.ticketTrailingEnabled.checked;
  const body = {
    symbol,
    transaction_type: formData.get("transaction_type"),
    quantity: Number(formData.get("quantity")),
    entry_price: Number(formData.get("entry_price")),
    entry_order_type: formData.get("entry_order_type"),
    stop_loss: Number(formData.get("stop_loss")),
    target_1: Number(formData.get("target_1")),
    target_2: formData.get("target_2") ? Number(formData.get("target_2")) : null,
    trailing_enabled: trailingEnabled,
    trailing_trigger: trailingEnabled ? formData.get("trailing_trigger") : null,
    trailing_distance: trailingEnabled && formData.get("trailing_distance")
      ? Number(formData.get("trailing_distance")) : null,
    product_type: formData.get("product_type"),
    rationale: formData.get("rationale"),
  };

  try {
    const ticket = await API.createTicket(body);
    state.tickets.unshift(ticket);
    renderTicketsTable();
    el.ticketForm.reset();
    el.ticketTrailingTrigger.disabled = true;
    el.ticketTrailingDistance.disabled = true;
    state.ticketDraftDirty = false;
    state.quantityManuallyEdited = false;
    openConfirmDialog(ticket);
  } catch (e) {
    el.ticketFormError.textContent = e.message;
  }
});

// Mode toggle
el.modeToggleCheckbox.addEventListener("change", () => {
  if (el.modeToggleCheckbox.checked) {
    el.liveModeConfirmDialog.showModal();
  } else {
    API.setExecutionMode("dry_run", false).then((r) => {
      state.executionMode = r.mode;
      renderModeBanner();
    });
  }
});

el.liveModeCancelBtn.addEventListener("click", () => {
  el.modeToggleCheckbox.checked = false;
  el.liveModeConfirmDialog.close();
});

el.liveModeConfirmBtn.addEventListener("click", async () => {
  try {
    const r = await API.setExecutionMode("live", true);
    state.executionMode = r.mode;
  } catch (e) {
    el.modeToggleCheckbox.checked = false;
  }
  renderModeBanner();
  el.liveModeConfirmDialog.close();
});

// Logout — confirm only if there's unsaved ticket-form/dialog state.
el.logoutBtn.addEventListener("click", async () => {
  const hasOpenConfirm = el.ticketConfirmDialog.open;
  if ((state.ticketDraftDirty || hasOpenConfirm) &&
      !confirm("You have an unsaved ticket draft or an open confirmation. Log out anyway?")) {
    return;
  }
  try {
    await API.logout();
  } catch (e) {
    // proceed to the connect gate regardless — the session is being torn down
  }
  wsIntentionallyClosed = true;
  if (ws) ws.close();
  el.logoutBtn.hidden = true;
  showConnectGate();
});

async function loadTicketingData() {
  try {
    const [tickets, modeResp, orders, trades, gtts] = await Promise.all([
      API.listTickets(),
      API.getExecutionMode(),
      API.orders(),
      API.trades(),
      API.gtts(),
    ]);
    state.tickets = tickets;
    state.executionMode = modeResp.mode;
    state.orders = orders;
    state.trades = trades;
    state.gtts = gtts;
    renderTicketsTable();
    renderModeBanner();
    renderOrdersTable();
    renderTradesTable();
    renderGttsTable();
    renderTrailingIndicator();
  } catch (e) {
    // ticketing is secondary to the core dashboard — don't block on it
  }
}

function renderRiskSettingsForm() {
  if (!state.riskSettings) return;
  const s = state.riskSettings;
  const form = el.riskSettingsForm;
  form.querySelector('[name="max_risk_per_trade_pct"]').value = s.max_risk_per_trade_pct;
  form.querySelector('[name="max_capital_per_trade_pct"]').value = s.max_capital_per_trade_pct;
  form.querySelector('[name="max_open_positions"]').value = s.max_open_positions;
  form.querySelector('[name="max_single_stock_exposure_pct"]').value = s.max_single_stock_exposure_pct;
  form.querySelector('[name="max_sector_exposure_pct"]').value = s.max_sector_exposure_pct;
  form.querySelector('[name="max_total_portfolio_risk_pct"]').value = s.max_total_portfolio_risk_pct;
  form.querySelector('[name="min_risk_reward"]').value = s.min_risk_reward;
}

function renderSectorMappingsTable() {
  el.sectorMappingsBody.innerHTML = "";
  if (state.sectorMappings.length === 0) {
    el.sectorMappingsBody.innerHTML = `<tr class="empty-row"><td colspan="4">No sector mappings yet</td></tr>`;
    return;
  }
  for (const m of state.sectorMappings) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${m.symbol}</td>
      <td>${m.sector}</td>
      <td>${m.added_at}</td>
      <td>
        <button class="row-action-btn" data-action="edit-sector" data-symbol="${m.symbol}">Edit</button>
        <button class="row-action-btn danger" data-action="delete-sector" data-symbol="${m.symbol}">Delete</button>
      </td>
    `;
    el.sectorMappingsBody.appendChild(tr);
  }
}

el.sectorMappingsBody.addEventListener("click", async (event) => {
  const btn = event.target.closest("button[data-action]");
  if (!btn) return;
  const symbol = btn.dataset.symbol;

  if (btn.dataset.action === "delete-sector") {
    if (!confirm(`Remove the sector mapping for ${symbol}?`)) return;
    try {
      await API.deleteSectorMapping(symbol);
      await Promise.all([refreshSectorMappings(), refreshUnmappedSymbols()]);
    } catch (e) {
      alert(e.message);
    }
  } else if (btn.dataset.action === "edit-sector") {
    const current = state.sectorMappings.find((m) => m.symbol === symbol);
    const next = prompt(`New sector for ${symbol}:`, current?.sector ?? "");
    if (next == null || next.trim() === "") return;
    try {
      await API.saveSectorMapping({ symbol, sector: next.trim() });
      await refreshSectorMappings();
    } catch (e) {
      alert(e.message);
    }
  }
});

function renderUnmappedSymbols() {
  if (state.unmappedSymbols.length === 0) {
    el.unmappedSymbolsList.innerHTML = `<span class="empty">None — every held symbol has a sector mapping.</span>`;
    return;
  }
  el.unmappedSymbolsList.innerHTML = state.unmappedSymbols
    .map((s) => `<span class="chip" data-symbol="${s}">${s}</span>`)
    .join("");
}

el.unmappedSymbolsList.addEventListener("click", (event) => {
  const chip = event.target.closest(".chip");
  if (!chip) return;
  document.getElementById("sm-symbol").value = chip.dataset.symbol;
  document.getElementById("sm-sector").focus();
});

async function refreshSectorMappings() {
  state.sectorMappings = await API.listSectorMappings();
  renderSectorMappingsTable();
}

async function refreshUnmappedSymbols() {
  try {
    state.unmappedSymbols = await API.unmappedSymbols();
    renderUnmappedSymbols();
  } catch (e) {
    // non-critical; leave prior list visible
  }
}

function renderRiskSettingsHistoryTable() {
  el.riskSettingsHistoryBody.innerHTML = "";
  if (state.settingsHistory.length === 0) {
    el.riskSettingsHistoryBody.innerHTML = `<tr class="empty-row"><td colspan="4">No changes recorded yet</td></tr>`;
    return;
  }
  for (const h of state.settingsHistory) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${h.field_changed}</td><td>${h.old_value}</td><td>${h.new_value}</td><td>${h.changed_at}</td>`;
    el.riskSettingsHistoryBody.appendChild(tr);
  }
}

el.riskSettingsHistoryToggle.addEventListener("click", async () => {
  const willShow = el.riskSettingsHistoryPanel.hidden;
  if (willShow) {
    try {
      state.settingsHistory = await API.getSettingsHistory();
      renderRiskSettingsHistoryTable();
    } catch (e) {
      // leave panel hidden on failure
      return;
    }
  }
  el.riskSettingsHistoryPanel.hidden = !willShow;
});

async function loadRiskData() {
  try {
    const [settings, mappings, sectors] = await Promise.all([
      API.getRiskSettings(), API.listSectorMappings(), API.listSectors(),
    ]);
    state.riskSettings = settings;
    state.sectorMappings = mappings;
    state.sectors = sectors;
    renderRiskSettingsForm();
    renderSectorMappingsTable();
    el.sectorOptions.innerHTML = sectors.map((s) => `<option value="${s}"></option>`).join("");
    await refreshUnmappedSymbols();
  } catch (e) {
    // non-critical view; leave section hidden
  }
}

el.riskSettingsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  el.riskSettingsError.textContent = "";
  el.riskSettingsSaved.hidden = true;
  const formData = new FormData(el.riskSettingsForm);
  const body = {
    max_risk_per_trade_pct: Number(formData.get("max_risk_per_trade_pct")),
    max_capital_per_trade_pct: Number(formData.get("max_capital_per_trade_pct")),
    max_open_positions: Number(formData.get("max_open_positions")),
    max_single_stock_exposure_pct: Number(formData.get("max_single_stock_exposure_pct")),
    max_sector_exposure_pct: Number(formData.get("max_sector_exposure_pct")),
    max_total_portfolio_risk_pct: Number(formData.get("max_total_portfolio_risk_pct")),
    min_risk_reward: Number(formData.get("min_risk_reward")),
  };
  try {
    state.riskSettings = await API.saveRiskSettings(body);
    renderRiskSettingsForm();
    el.riskSettingsSaved.hidden = false;
    setTimeout(() => { el.riskSettingsSaved.hidden = true; }, 2000);
  } catch (e) {
    el.riskSettingsError.textContent = e.message;
  }
});

el.sectorMappingForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  el.sectorMappingError.textContent = "";
  const formData = new FormData(el.sectorMappingForm);
  try {
    await API.saveSectorMapping({
      symbol: formData.get("symbol"),
      sector: formData.get("sector"),
    });
    await refreshSectorMappings();
    await refreshUnmappedSymbols();
    const sectors = await API.listSectors();
    state.sectors = sectors;
    el.sectorOptions.innerHTML = sectors.map((s) => `<option value="${s}"></option>`).join("");
    el.sectorMappingForm.reset();
  } catch (e) {
    el.sectorMappingError.textContent = e.message;
  }
});

// ---- Phase 6: sidebar navigation / section router ----

const SECTIONS = [
  { id: "dashboard", el: document.getElementById("view-dashboard") },
  { id: "new-ticket", el: document.getElementById("view-new-ticket") },
  { id: "trade-queue", el: document.getElementById("view-trade-queue") },
  { id: "ticket-history", el: document.getElementById("view-ticket-history") },
  { id: "orders-trades", el: document.getElementById("view-orders-trades") },
  { id: "watchlist", el: document.getElementById("view-watchlist") },
  { id: "strategies", el: document.getElementById("view-strategies") },
  { id: "todays-actions", el: document.getElementById("view-todays-actions") },
  { id: "journal", el: document.getElementById("view-journal") },
  { id: "settings", el: document.getElementById("view-settings") },
];

function showSection(id) {
  state.activeSection = id;
  for (const s of SECTIONS) {
    s.el.hidden = s.id !== id;
  }
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.sectionId === id);
  });
}

document.querySelectorAll(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => showSection(btn.dataset.sectionId));
});

function revealTradingNavItems() {
  for (const id of [
    "new-ticket", "ticket-history", "orders-trades", "settings", "watchlist", "strategies",
    "trade-queue", "todays-actions", "journal",
  ]) {
    const btn = document.querySelector(`.nav-item[data-section-id="${id}"]`);
    if (btn) btn.hidden = false;
  }
}

el.sidebarToggleBtn.addEventListener("click", async () => {
  const collapsed = !el.sidebar.classList.contains("collapsed");
  el.sidebar.classList.toggle("collapsed", collapsed);
  try {
    await API.saveUiPrefs({ sidebar_collapsed: collapsed });
  } catch (e) {
    // non-critical — collapse state just won't survive a refresh this time
  }
});

// ---- Phase 6: configurable Dashboard panels ----
//
// Extension point for a future panel type (e.g. watchlist_summary,
// strategy_signals): add its static `.panel-wrapper[data-panel-type="..."]`
// markup block inside #dashboard-panels-grid in index.html, write whatever
// function renders its content, add a PANEL_LABELS entry (used by the "Add
// Panel" dropdown), and insert one dashboard_panels DB row. Nothing in
// renderDashboardGrid/drag/resize/persist below needs to change for that —
// they all operate purely on panel_type strings and generic wrapper markup.
const PANEL_LABELS = {
  portfolio_summary: "Portfolio Summary",
  holdings_table: "Holdings",
  positions_table: "Positions",
  mf_table: "Mutual Funds",
  watchlist_summary: "Watchlist", // first new panel_type added since the extension point was built — see renderWatchlistSummaryPanel()
  todays_actions: "Today's Actions", // Phase 6 — same zero-infra-change extension point, see renderTodaysActionsPanel()
};

function panelWrapperEl(panelType) {
  return document.querySelector(`.panel-wrapper[data-panel-type="${panelType}"]`);
}

function renderDashboardGrid() {
  const visiblePanels = state.dashboardPanels
    .filter((p) => p.visible)
    .sort((a, b) => a.display_order - b.display_order);

  el.dashboardEmptyNote.hidden = visiblePanels.length > 0;
  el.dashboardPanelsGrid.hidden = visiblePanels.length === 0;

  for (const panel of visiblePanels) {
    const wrapper = panelWrapperEl(panel.panel_type);
    if (!wrapper) continue;
    wrapper.hidden = false;
    wrapper.className = `panel-wrapper panel-${panel.size}`;
    wrapper.dataset.panelType = panel.panel_type;
    el.dashboardPanelsGrid.appendChild(wrapper); // moves node into correct order; preserves listeners/content

    const header = wrapper.querySelector(".panel-header");
    header.hidden = !state.customizeMode;
    header.querySelectorAll(".panel-size-btn").forEach((btn) => {
      btn.classList.toggle("active-size", btn.dataset.size === panel.size);
    });
  }

  for (const panelType of Object.keys(PANEL_LABELS)) {
    const cfg = state.dashboardPanels.find((p) => p.panel_type === panelType);
    if (!cfg || !cfg.visible) {
      const wrapper = panelWrapperEl(panelType);
      if (wrapper) wrapper.hidden = true;
    }
  }

  renderAddPanelControl();
}

function renderAddPanelControl() {
  const hiddenPanels = state.dashboardPanels.filter((p) => !p.visible);
  el.addPanelControl.hidden = !state.customizeMode || hiddenPanels.length === 0;
  el.addPanelSelect.innerHTML = `<option value="">Add Panel…</option>` +
    hiddenPanels.map((p) => `<option value="${p.panel_type}">${PANEL_LABELS[p.panel_type] || p.panel_type}</option>`).join("");
}

async function saveDashboardPanels() {
  try {
    state.dashboardPanels = await API.saveDashboardPanels(state.dashboardPanels);
  } catch (e) {
    // keep local state as the source of truth for this session; will
    // resync with the server on next load if the save silently failed
  }
}

let sortableInstance = null;

function initSortable() {
  if (typeof Sortable === "undefined" || sortableInstance) return;
  sortableInstance = Sortable.create(el.dashboardPanelsGrid, {
    handle: ".panel-drag-handle",
    animation: 150,
    onEnd: () => {
      const order = [...el.dashboardPanelsGrid.querySelectorAll(".panel-wrapper")].map((w) => w.dataset.panelType);
      order.forEach((panelType, idx) => {
        const cfg = state.dashboardPanels.find((p) => p.panel_type === panelType);
        if (cfg) cfg.display_order = idx;
      });
      saveDashboardPanels();
    },
  });
}

function destroySortable() {
  sortableInstance?.destroy();
  sortableInstance = null;
}

el.customizeModeToggle.addEventListener("change", () => {
  state.customizeMode = el.customizeModeToggle.checked;
  el.dashboardPanelsGrid.classList.toggle("customize-mode", state.customizeMode);
  renderDashboardGrid();
  if (state.customizeMode) initSortable(); else destroySortable();
});

el.dashboardPanelsGrid.addEventListener("click", (event) => {
  const wrapper = event.target.closest(".panel-wrapper");
  if (!wrapper) return;
  const cfg = state.dashboardPanels.find((p) => p.panel_type === wrapper.dataset.panelType);
  if (!cfg) return;

  const sizeBtn = event.target.closest(".panel-size-btn");
  const hideBtn = event.target.closest(".panel-hide-btn");
  if (sizeBtn) {
    cfg.size = sizeBtn.dataset.size;
    renderDashboardGrid();
    saveDashboardPanels();
  } else if (hideBtn) {
    cfg.visible = false;
    renderDashboardGrid();
    saveDashboardPanels();
  }
});

el.addPanelSelect.addEventListener("change", (event) => {
  const panelType = event.target.value;
  if (!panelType) return;
  const cfg = state.dashboardPanels.find((p) => p.panel_type === panelType);
  if (cfg) {
    const maxOrder = Math.max(-1, ...state.dashboardPanels.map((p) => p.display_order));
    cfg.visible = true;
    cfg.display_order = maxOrder + 1;
  }
  event.target.value = "";
  renderDashboardGrid();
  saveDashboardPanels();
});

async function loadDashboardPanels() {
  try {
    state.dashboardPanels = await API.getDashboardPanels();
    renderDashboardGrid();
  } catch (e) {
    // leave the grid in its default static-HTML order/visibility on failure
  }
}

async function init() {
  const params = new URLSearchParams(location.search);
  if (params.has("login_error")) {
    showGenericError("Login failed or was cancelled.");
  }

  CONFIG = await API.config();
  setupSortHandlers();

  renderMarketStatus();
  setInterval(renderMarketStatus, 30000);
  setInterval(renderTrailingIndicator, 15000);

  try {
    const prefs = await API.getUiPrefs();
    el.sidebar.classList.toggle("collapsed", !!prefs.sidebar_collapsed);
  } catch (e) {
    // default expanded
  }

  const status = await API.status();
  state.sessionStatus = status;
  state.accountType = status.account_type;
  el.logoutBtn.hidden = !status.connected;
  if (!status.connected) {
    showConnectGate();
    return;
  }
  await loadDashboard();
  await loadDashboardPanels();
  showSection("dashboard");

  if (state.accountType === "trading") {
    revealTradingNavItems();
    await loadTicketingData();
    await loadRiskData();
    await loadWatchlist();
    await loadStrategyData();
    await loadPhase6Data();
    await loadReconciliationMismatches();
    setInterval(loadReconciliationMismatches, 60000);
  }
}

el.connectBtn.addEventListener("click", () => { location.href = "/login"; });
el.reconnectBtn.addEventListener("click", () => { location.href = "/login"; });
el.refreshBtn.addEventListener("click", loadDashboard);

init();
