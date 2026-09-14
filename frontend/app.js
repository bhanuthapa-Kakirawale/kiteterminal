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
  revertRejection: (id) => fetchJsonMutate(`/api/tickets/${id}/revert-rejection`, "POST"),
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

  // Autopilot
  automationStatus: () => fetchJson("/api/automation/status"),
  automationRunNow: () => fetchJsonMutate("/api/automation/run-now", "POST"),
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
  ticketDetailRevertBtn: document.getElementById("ticket-detail-revert-btn"),
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

  // Approvals inbox, autopilot, toasts, theme
  apEntries: document.getElementById("ap-entries"),
  apExits: document.getElementById("ap-exits"),
  apAdjustments: document.getElementById("ap-adjustments"),
  apBlocked: document.getElementById("ap-blocked"),
  apRuns: document.getElementById("ap-runs"),
  apEmpty: document.getElementById("ap-empty"),
  apRefreshBtn: document.getElementById("ap-refresh-btn"),
  viewApprovals: document.getElementById("view-approvals"),
  autopilotCard: document.getElementById("autopilot-card"),
  autopilotPill: document.getElementById("autopilot-pill"),
  autopilotPillText: document.getElementById("autopilot-pill-text"),
  modePill: document.getElementById("mode-pill"),
  navBadgeApprovals: document.getElementById("nav-badge-approvals"),
  navBadgeTradeQueue: document.getElementById("nav-badge-trade-queue"),
  dashApprovalsCallout: document.getElementById("dash-approvals-callout"),
  dashApprovalsText: document.getElementById("dash-approvals-text"),
  dashApprovalsOpen: document.getElementById("dash-approvals-open"),
  toastStack: document.getElementById("toast-stack"),
  appConfirmDialog: document.getElementById("app-confirm-dialog"),
  appConfirmTitle: document.getElementById("app-confirm-title"),
  appConfirmBody: document.getElementById("app-confirm-body"),
  appConfirmOk: document.getElementById("app-confirm-ok"),
  appConfirmCancel: document.getElementById("app-confirm-cancel"),
  themeToggleBtn: document.getElementById("theme-toggle-btn"),
  ticketConfirmExecNote: document.getElementById("ticket-confirm-exec-note"),
  ticketPreview: document.getElementById("ticket-preview"),
  strategyInfo: document.getElementById("strategy-info"),
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

  // Approvals inbox / autopilot
  automationStatus: null,
  approvals: null,
  activeConfirmTicket: null,
  activeDetailTicket: null,
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

const _ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
function esc(value) {
  return value == null ? "" : String(value).replace(/[&<>"']/g, (c) => _ESC[c]);
}

function inr(n, decimals = 2) {
  return n == null || Number.isNaN(n) ? "—" : `₹${fmt(n, decimals)}`;
}

// SQLite's datetime('now') is UTC without a zone marker.
function parseUtc(ts) {
  if (!ts) return null;
  return new Date(/[zZ]$|[+-]\d\d:\d\d$/.test(ts) ? ts : `${ts.replace(" ", "T")}Z`);
}

function fmtDateTime(ts) {
  const d = parseUtc(ts);
  if (!d || Number.isNaN(d.getTime())) return esc(ts ?? "—");
  return d.toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });
}

function fmtDate(isoDate) {
  if (!isoDate) return "—";
  return new Date(`${isoDate}T00:00:00`).toLocaleDateString("en-IN", { weekday: "short", day: "2-digit", month: "short" });
}

function istDateOf(d) {
  return new Date(d.getTime() + 5.5 * 3600 * 1000).toISOString().slice(0, 10);
}

function sideHtml(side) {
  return `<span class="side-pill ${side === "SELL" ? "sell" : "buy"}">${esc(side)}</span>`;
}

function strategyChipHtml(name) {
  return name
    ? `<span class="chip strategy">${esc(strategyLabel(name.replace(/_exit$/, "")))}</span>`
    : `<span class="chip">Manual</span>`;
}

function modeChipHtml(mode) {
  if (!mode) return "—";
  return mode === "live" ? `<span class="chip live">LIVE</span>` : `<span class="chip dry">DRY RUN</span>`;
}

function setBadge(node, n) {
  node.hidden = !n;
  node.textContent = n > 99 ? "99+" : String(n);
}

function setCount(id, n) {
  const node = document.getElementById(id);
  node.textContent = n;
  node.classList.toggle("zero", !n);
}

// ---- Toasts & confirm dialog (replace window.alert/confirm/prompt) ----

const TOAST_ICONS = { success: "✓", error: "!", warning: "!", info: "i" };

function toast(message, { type = "info", title = null, timeout = null } = {}) {
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  node.setAttribute("role", type === "error" ? "alert" : "status");
  node.innerHTML = `
    <span class="toast-icon">${TOAST_ICONS[type] || "i"}</span>
    <div>${title ? `<div class="toast-title">${esc(title)}</div>` : ""}<div class="toast-msg">${esc(message)}</div></div>
    <button class="toast-close" aria-label="Dismiss">×</button>`;
  const dismiss = () => {
    node.classList.add("leaving");
    setTimeout(() => node.remove(), 180);
  };
  node.querySelector(".toast-close").addEventListener("click", dismiss);
  el.toastStack.appendChild(node);
  const ms = timeout ?? (type === "error" || type === "warning" ? 9000 : 5000);
  if (ms > 0) setTimeout(dismiss, ms);
  while (el.toastStack.children.length > 5) el.toastStack.firstElementChild.remove();
}

function confirmAction({ title, message = "", html = "", okLabel = "Confirm", danger = false, okClass = null }) {
  return new Promise((resolve) => {
    el.appConfirmTitle.textContent = title;
    el.appConfirmBody.innerHTML = html || `<p>${esc(message)}</p>`;
    el.appConfirmOk.textContent = okLabel;
    el.appConfirmOk.className = `btn ${okClass || (danger ? "btn-danger" : "btn-primary")}`;
    const finish = (value) => {
      el.appConfirmOk.removeEventListener("click", onOk);
      el.appConfirmCancel.removeEventListener("click", onCancel);
      el.appConfirmDialog.removeEventListener("close", onClose);
      if (el.appConfirmDialog.open) el.appConfirmDialog.close();
      resolve(value);
    };
    const onOk = () => finish(true);
    const onCancel = () => finish(false);
    const onClose = () => finish(false);
    el.appConfirmOk.addEventListener("click", onOk);
    el.appConfirmCancel.addEventListener("click", onCancel);
    el.appConfirmDialog.addEventListener("close", onClose);
    el.appConfirmDialog.showModal();
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
    const unprotected = t.status === "executed" && t.mode === "live" && !t.kite_gtt_id && !t.closed_at
      && t.closes_ticket_id == null && t.use_gtt;
    if (unprotected) {
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
      <div class="alert-chip ${a.severity}" data-scroll-target="${esc(a.scrollTargetSelector)}">
        <span class="icon">${a.severity === "urgent" ? "⛔" : "⚠"}</span>
        <span>${esc(a.message)}</span>
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
      <td>${esc(w.notes ?? "")}</td>
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
    toast("Removed from watchlist.", { type: "success" });
  } catch (e) {
    toast(e.message, { type: "error" });
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

const STRATEGY_INFO = {
  momentum_breakout: "Buys a close above the recent N-day high on above-average volume, only while the stock trades above its long-term SMA. Stop = ATR multiple below entry, target = R multiple; a trailing stop takes over after target 1. Exits if price closes back below the thesis EMA or the trade times out.",
  trend_pullback: "Buys an established uptrend that recently dipped to its pullback EMA and has closed back above it on an up day. Tighter ATR stop than the breakout, R-multiple target with a trailing stop after target 1.",
  rsi_mean_reversion: "Buys a short-term oversold dip (RSI below the threshold, optionally turning up) only while the long-term uptrend is intact. Stop at the recent swing low or ATR (whichever is tighter), fixed R target, and takes profit early once RSI recovers to the exit level.",
};

function strategyLabel(name) {
  if (!name) return "";
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
  el.strategyInfo.textContent = STRATEGY_INFO[strategyName] || "";

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
  if (turningOn && !(await confirmAction({
    title: `Turn on ${strategyLabel(strategyName)}?`,
    message: "The autopilot will scan your watchlist after every close and queue entry/exit signals in Approvals. "
      + "Nothing is sent to the broker until you approve it.",
    okLabel: "Turn on",
  }))) {
    el.strategyEnabledToggle.checked = false;
    return;
  }
  try {
    const cfg = await API.saveStrategyConfig(strategyName, { enabled: turningOn, params: readStrategyParamsForm(strategyName) });
    state.strategyConfigs[strategyName] = { strategy_name: strategyName, ...cfg };
    renderStrategyConfig();
    toast(`${strategyLabel(strategyName)} ${turningOn ? "enabled" : "disabled"}.`, { type: "success" });
    loadAutomationStatus();
  } catch (e) {
    el.strategyEnabledToggle.checked = !turningOn;
    toast(e.message, { type: "error" });
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
    toast(`Parameters saved (version ${cfg.current_version}).`, { type: "success" });
  } catch (e) {
    el.strategyParamsError.textContent = e.message;
  }
});

function scanSummaryHtml(result) {
  const lines = Object.entries(result.by_strategy).map(([name, r]) => {
    const d = r.diagnostics || {};
    const notes = [];
    if (d.insufficient_history?.length) notes.push(`not enough history: ${d.insufficient_history.map(esc).join(", ")}`);
    if (d.stale_data?.length) notes.push(`stale data: ${d.stale_data.map(esc).join(", ")}`);
    if (d.already_signalled?.length) notes.push(`already signalled: ${d.already_signalled.map(esc).join(", ")}`);
    return `<div><strong>${esc(strategyLabel(name))}</strong> — ${r.entry_tickets.length} entry, ${r.exit_tickets.length} exit`
      + (d.evaluated != null ? ` · ${d.evaluated} evaluated` : "")
      + (notes.length ? ` <span class="badge-muted">${notes.join(" · ")}</span>` : "") + "</div>";
  });
  return lines.join("") + `<div>${result.breakeven_proposals.length} breakeven proposal(s)</div>`;
}

el.scanNowBtn.addEventListener("click", async () => {
  el.scanNowBtn.disabled = true;
  el.scanNowResult.textContent = "Scanning…";
  try {
    const result = await API.scanNow();
    if (!result.enabled) {
      el.scanNowResult.textContent = "No strategies are enabled — nothing scanned.";
    } else {
      el.scanNowResult.innerHTML = scanSummaryHtml(result);
      const created = Object.values(result.by_strategy)
        .reduce((n, r) => n + r.entry_tickets.length + r.exit_tickets.length, 0);
      toast(created ? `${created} new signal(s) queued in Approvals.` : "Scan complete — no new signals.",
        { type: created ? "success" : "info" });
    }
    await refreshTicketViews();
  } catch (e) {
    el.scanNowResult.textContent = `Scan failed: ${e.message}`;
  } finally {
    el.scanNowBtn.disabled = false;
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
        <strong>${esc(p.symbol)}</strong> — move stop ₹${fmt(p.current_stop)} → ₹${fmt(p.proposed_stop)}
        <div class="rationale">${esc(p.reason)}</div>
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
    toast(confirmBtn ? "Stop moved to breakeven." : "Stop adjustment rejected.", { type: confirmBtn ? "success" : "info" });
    await refreshTicketViews();
  } catch (e) {
    toast(e.message, { type: "error", title: "Stop adjustment failed" });
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
  const xs = points.split(" ").map((p) => p.split(",")[0]);
  const baseline = height - pad;
  container.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" class="equity-curve-svg" preserveAspectRatio="none">
      <polygon points="${xs[0]},${baseline} ${points} ${xs[xs.length - 1]},${baseline}" fill="${color}" stroke="none" opacity="0.13" />
      <polyline points="${points}" fill="none" stroke="${color}" stroke-width="2" vector-effect="non-scaling-stroke" />
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
      tr.innerHTML = `<td>${esc(strategyLabel(s.strategy_name))}</td>${statsRowHtml(s)}`;
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
        <td>${esc(t.symbol)}</td><td class="num">${fmt(t.entry_price)}</td>
        <td class="num">${fmt(t.stop_loss)}</td><td class="num">${fmt(t.target_1)}</td><td>${esc(t.rationale)}</td>
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
      tr.innerHTML = `<td>${esc(t.symbol)}</td><td class="num">${fmt(t.quantity, 0)}</td><td>${esc(t.rationale)}</td>`;
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
      div.innerHTML = `<div><strong>${esc(p.symbol)}</strong> — ₹${fmt(p.current_stop)} → ₹${fmt(p.proposed_stop)}<div class="rationale">${esc(p.reason)}</div></div>`;
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
  document.getElementById("tq-count-ready").textContent = ready.length || "";
  document.getElementById("tq-count-waiting").textContent = waiting.length || "";
  document.getElementById("tq-count-rejected").textContent = rejected.length || "";
  document.getElementById("tq-count-executed").textContent = executed.length || "";
  setBadge(el.navBadgeTradeQueue, ready.length);
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

const STATUS_LABELS = { pending_confirmation: "awaiting approval", risk_rejected: "risk rejected" };

function statusBadgeHtml(status, ticket = null) {
  if (ticket && status === "executed" && ticket.entry_fill_status === "pending") {
    return `<span class="status-badge awaiting_fill" title="Order placed with the broker — waiting for the fill">placed · awaiting fill</span>`;
  }
  const label = STATUS_LABELS[status] || status.replace(/_/g, " ");
  return `<span class="status-badge ${esc(status)}">${esc(label)}</span>`;
}

function trailingChipHtml(t) {
  if (!t.trailing_enabled) return `<span class="chip">off</span>`;
  return t.trailing_active ? `<span class="chip ok">active</span>` : `<span class="chip warn">armed</span>`;
}

function ticketRowHtml(t) {
  const strategyTag = t.source === "strategy" && t.strategy_name
    ? ` <span class="badge-muted">${esc(strategyLabel(t.strategy_name.replace(/_exit$/, "")))}</span>` : "";
  return `
    <td>${esc(t.symbol)}${strategyTag}</td>
    <td>${sideHtml(t.transaction_type)}</td>
    <td class="num">${fmt(t.quantity, 0)}</td>
    <td class="num">${fmt(t.entry_price)}</td>
    <td class="num">${fmt(t.current_stop_loss ?? t.stop_loss)}</td>
    <td class="num">${fmt(t.target_1)}</td>
    <td>${trailingChipHtml(t)}</td>
    <td>${statusBadgeHtml(t.status, t)}</td>
    <td>${modeChipHtml(t.mode)}</td>
    <td>${fmtDateTime(t.created_at)}</td>
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
    if (t.status === "executed" && t.mode === "live" && !t.kite_gtt_id && !t.closed_at
        && t.closes_ticket_id == null && t.use_gtt) {
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
const _ORDER_STATUS_CLASS = { COMPLETE: "executed", REJECTED: "failed", CANCELLED: "cancelled" };

function orderStatusHtml(status) {
  if (!status) return "";
  return `<span class="status-badge ${_ORDER_STATUS_CLASS[status] || "pending_confirmation"}">${esc(status.toLowerCase())}</span>`;
}

function orderRowHtml(o) {
  const cancellable = !_ORDER_TERMINAL_STATUSES.has(o.status);
  return `
    <td>${esc(o.tradingsymbol ?? "")}</td>
    <td>${o.transaction_type ? sideHtml(o.transaction_type) : ""}</td>
    <td class="num">${fmt(o.quantity, 0)}</td>
    <td class="num">${fmt(o.price)}</td>
    <td title="${esc(o.status_message ?? "")}">${orderStatusHtml(o.status)}</td>
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
  if (!(await confirmAction({
    title: "Cancel order?", message: `Cancel broker order ${btn.dataset.orderId}? This cannot be undone.`,
    okLabel: "Cancel order", danger: true,
  }))) return;
  try {
    await API.cancelOrder(btn.dataset.orderId, btn.dataset.variety);
    toast(`Order ${btn.dataset.orderId} cancelled.`, { type: "success" });
    state.orders = await API.orders();
    renderOrdersTable();
  } catch (e) {
    toast(e.message, { type: "error", title: "Cancel failed" });
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
      <td>${t.transaction_type ? sideHtml(t.transaction_type) : ""}</td>
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
  if (!(await confirmAction({
    title: "Cancel protective GTT?",
    message: `GTT ${btn.dataset.gttId} will be removed. The position stays OPEN but becomes UNPROTECTED.`,
    okLabel: "Cancel GTT", danger: true,
  }))) return;
  try {
    await API.cancelGtt(btn.dataset.gttId);
    toast(`GTT ${btn.dataset.gttId} cancelled — the position is now unprotected.`, { type: "warning" });
    state.gtts = await API.gtts();
    renderGttsTable();
  } catch (e) {
    toast(e.message, { type: "error", title: "Cancel failed" });
  }
});

function renderModeBanner() {
  const live = state.executionMode === "live";
  el.modeBanner.hidden = false;
  el.modeToggleCheckbox.checked = live;
  el.modePill.hidden = false;
  el.modePill.className = `pill mode-pill ${live ? "live" : "dry"}`;
  el.modePill.textContent = live ? "LIVE" : "DRY RUN";
  if (live) {
    el.modeBanner.className = "mode-banner mode-live";
    el.modeBannerText.textContent = "LIVE TRADING — approving a ticket places a real order with real money";
  } else {
    el.modeBanner.className = "mode-banner mode-dry-run";
    el.modeBannerText.textContent = "DRY RUN — approvals are simulated; no real orders are placed";
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
    toast(e.message, { type: "error" });
  }
});

el.cancelAllPendingCancelBtn.addEventListener("click", () => el.cancelAllPendingDialog.close());

el.cancelAllPendingConfirmBtn.addEventListener("click", async () => {
  try {
    const result = await API.cancelAllPending();
    el.cancelAllPendingDialog.close();
    await refreshTicketViews();
    toast(`Cancelled ${result.count} ticket(s).`, { type: "success" });
  } catch (e) {
    toast(e.message, { type: "error" });
  }
});

el.emergencyExitAllBtn.addEventListener("click", async () => {
  try {
    const positions = await API.emergencyExitPreview();
    el.emergencyExitModeLabel.textContent = state.executionMode === "live" ? "LIVE" : "Dry Run";
    el.emergencyExitPreviewBody.innerHTML = positions.length
      ? positions.map((p) => `
          <tr>
            <td>${esc(p.symbol)}${p.entry_fill_status === "pending" ? ' <span class="badge-muted">unfilled — order will be cancelled</span>' : ""}</td>
            <td class="num">${fmt(p.quantity, 0)}</td>
            <td class="num">${fmt(p.entry_price)}</td><td class="num">${fmt(p.current_stop_loss ?? p.stop_loss)}</td>
            <td>${esc(strategyLabel(p.strategy_name) || "Manual")}</td>
          </tr>
        `).join("")
      : `<tr class="empty-row"><td colspan="5">No open positions</td></tr>`;
    el.emergencyExitPhrase.value = "";
    el.emergencyExitError.textContent = "";
    el.emergencyExitConfirmBtn.disabled = true;
    el.emergencyExitDialog.showModal();
  } catch (e) {
    toast(e.message, { type: "error" });
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
    await refreshTicketViews();
    refreshOrderBooks();
    toast(`${result.count} position(s) processed — check Orders & Trades for fills.`,
      { type: "warning", title: "Emergency exit complete" });
  } catch (e) {
    el.emergencyExitError.textContent = e.message;
  }
});

// ---- Phase 8: order/position reconciliation ----

function reconciliationItemHtml(m) {
  return `
    <div class="reconciliation-item">
      <div>${esc(m.detail)}</div>
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
    toast(e.message, { type: "error" });
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
  // Refresh re-runs loadDashboard(); never stack a second socket on top of a live one.
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
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
    if (msg.type === "tickets_changed") {
      scheduleRefresh();
    }
    if (msg.type === "order_update") {
      const type = { success: "success", urgent: "error", warning: "warning" }[msg.severity] || "info";
      toast(msg.message, {
        type, title: msg.severity === "urgent" ? "Action needed" : "Order update",
        timeout: msg.severity === "urgent" ? 0 : null,
      });
      refreshOrderBooks();
    }
    if (msg.type === "automation_run") {
      if (msg.phase === "finished") {
        const type = { success: "success", skipped: "info", failed: "error" }[msg.status] || "info";
        toast(msg.error || runSummaryText(msg) || "Autopilot run finished.", { type, title: `Autopilot run ${msg.status}` });
      }
      loadAutomationStatus();
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
    ? `On — ${t.trailing_trigger === "manual" ? "started manually" : "after target 1"}, ${inr(t.trailing_distance)} behind price`
    : "Off";
  const isExit = t.closes_ticket_id != null;
  const origin = t.source === "strategy" ? strategyChipHtml(t.strategy_name) : `<span class="chip">Manual</span>`;
  return `
    <div class="ticket-head">${sideHtml(t.transaction_type)}<span class="ac-symbol">${esc(t.symbol)}</span>${origin}${isExit ? `<span class="chip warn">closes #${t.closes_ticket_id}</span>` : ""}</div>
    ${metricsRow("Order", `${esc(t.entry_order_type)} × ${fmt(t.quantity, 0)} @ ${inr(t.entry_price)}${t.entry_order_type === "MARKET" ? " (reference)" : ""}`)}
    ${metricsRow("Capital", inr(capital))}
    ${isExit ? "" : metricsRow("Stop-loss", `${inr(t.stop_loss)} · risk ${inr(risk)}`)}
    ${isExit ? "" : metricsRow("Target 1", inr(t.target_1))}
    ${t.target_2 && !isExit ? metricsRow("Target 2", inr(t.target_2)) : ""}
    ${isExit ? "" : metricsRow("Trailing", trailingText)}
    ${metricsRow("Product", esc(t.product_type))}
    ${isExit ? "" : metricsRow("Protective GTT", t.use_gtt ? "Placed automatically with the entry" : "No")}
    ${isExit ? "" : metricsRow("Risk : Reward", `1 : ${rr}`)}
    ${t.filled_price ? metricsRow("Filled at", inr(t.filled_price)) : ""}
    <div class="rationale">${esc(t.rationale)}</div>
  `;
}

// How an approval will be executed — shown before the user commits.
function executionNote(t) {
  const live = state.executionMode === "live";
  if (!live) return { live, text: "DRY RUN — approving simulates the trade; nothing is sent to the broker." };
  const how = computeMarketStatus() === "open"
    ? "a regular order"
    : "an After-Market Order (market closed — it queues for the next session)";
  const gtt = t.closes_ticket_id == null && t.use_gtt ? ", followed by a protective GTT" : "";
  return { live, text: `LIVE — approving sends ${how}${gtt}. This uses real money.` };
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
  return `<div class="risk-check-row ${cls}"><span class="icon">${icon}</span><span>${esc(c.label)}</span><strong>${esc(value)}</strong></div>`;
}

function renderRiskCheckSection(checks) {
  if (!checks || checks.length === 0) {
    el.ticketRiskCheckBody.innerHTML = "";
    return;
  }
  const passed = checks.filter((c) => c.passed).length;
  el.ticketRiskCheckBody.innerHTML = `<h3>Risk checks · ${passed}/${checks.length} passed</h3>`
    + checks.map(riskCheckRowHtml).join("");
}

function openConfirmDialog(ticket) {
  activeConfirmTicketId = ticket.id;
  state.activeConfirmTicket = ticket;
  const rejectedMode = ticket.status === "risk_rejected";
  el.ticketConfirmDialog.classList.toggle("risk-rejected-mode", rejectedMode);
  el.ticketConfirmTitle.textContent = rejectedMode
    ? "Blocked by risk checks"
    : (ticket.closes_ticket_id != null ? "Approve exit" : "Approve trade");
  el.ticketConfirmBody.innerHTML = ticketConfirmHtml(ticket);
  const note = executionNote(ticket);
  el.ticketConfirmExecNote.hidden = rejectedMode;
  el.ticketConfirmExecNote.className = `exec-note${note.live ? " live" : ""}`;
  el.ticketConfirmExecNote.textContent = note.text;
  renderRiskCheckSection(ticket.risk_check_results);
  el.ticketRejectBtn.hidden = rejectedMode;
  el.ticketConfirmBtn.hidden = rejectedMode;
  el.ticketModifyBtn.hidden = !rejectedMode;
  el.ticketArchiveBtn.hidden = !rejectedMode;
  el.ticketConfirmDialog.showModal();
}

// Shared by the confirm dialog and the Approvals inbox. Execution failures
// come back as data on the ticket (not thrown); a thrown error means the
// request itself failed (network/auth/state).
async function approveTicket(id) {
  try {
    reportExecution(await API.confirmTicket(id));
  } catch (e) {
    toast(e.message, { type: "error", title: "Approval failed" });
  }
  await refreshTicketViews();
  refreshOrderBooks();
}

function reportExecution(t) {
  const what = `${t.transaction_type} ${fmt(t.quantity, 0)} ${t.symbol}`;
  if (!t.success) {
    toast(t.execution_message, { type: "error", title: `${what} — not executed` });
  } else if (/UNPROTECTED|FAILED/.test(t.execution_message || "")) {
    toast(t.execution_message, { type: "warning", title: `${what} — placed, check protection`, timeout: 0 });
  } else if (t.mode === "dry_run") {
    toast("Simulated — no real order was sent.", { type: "success", title: `${what} (dry run)` });
  } else {
    toast(t.order_variety === "amo"
      ? "Sent as AMO — it executes when the market opens. The fill is tracked automatically."
      : "Order placed. The fill and protective GTT are tracked automatically.",
    { type: "success", title: `${what} placed` });
  }
}

async function rejectTicketById(id, reason = null) {
  try {
    await API.rejectTicket(id, reason);
    toast("Ticket rejected.", { type: "info" });
  } catch (e) {
    toast(e.message, { type: "error", title: "Reject failed" });
  }
  await refreshTicketViews();
}

el.ticketArchiveBtn.addEventListener("click", async () => {
  if (activeConfirmTicketId == null) return;
  el.ticketConfirmDialog.close();
  try {
    await API.deleteTicket(activeConfirmTicketId);
    toast("Ticket archived.", { type: "info" });
  } catch (e) {
    toast(e.message, { type: "error" });
  }
  await refreshTicketViews();
});

el.ticketRejectBtn.addEventListener("click", async () => {
  if (activeConfirmTicketId == null) return;
  el.ticketConfirmDialog.close();
  await rejectTicketById(activeConfirmTicketId);
});

el.ticketConfirmBtn.addEventListener("click", async () => {
  if (activeConfirmTicketId == null) return;
  el.ticketConfirmBtn.disabled = true;
  try {
    await approveTicket(activeConfirmTicketId);
  } finally {
    el.ticketConfirmBtn.disabled = false;
  }
  el.ticketConfirmDialog.close();
});

el.ticketModifyBtn.addEventListener("click", async () => {
  const t = state.activeConfirmTicket;
  el.ticketConfirmDialog.close();
  if (!t) return;
  showSection("new-ticket");

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

  renderTicketPreview();
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
    <span class="event-time">${fmtDateTime(e.created_at)}</span>
    <span class="event-type">${esc(e.event_type.replace(/_/g, " "))}</span>
    <span class="event-detail">${esc(e.detail ?? "")}</span>
  </div>`;
}

function renderTicketDetailEvents(events) {
  el.ticketDetailEvents.innerHTML = `<h3>History</h3>` + (events && events.length
    ? events.map(eventRowHtml).join("")
    : `<div class="event-row"><span class="event-detail">No events yet</span></div>`);
}

async function openDetailDialog(ticket) {
  state.activeDetailTicketId = ticket.id;
  state.activeDetailTicket = ticket;
  el.ticketEditForm.hidden = true;
  el.ticketDetailBody.hidden = false;
  el.ticketEditError.textContent = "";

  const statusRow = `<div class="row"><span>Status</span><strong>${statusBadgeHtml(ticket.status, ticket)}</strong></div>`;
  const modeRow = `<div class="row"><span>Mode</span><strong>${modeChipHtml(ticket.mode)}${ticket.order_variety === "amo" ? ' <span class="chip">AMO</span>' : ""}</strong></div>`;
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
  el.ticketDetailRevertBtn.hidden = ticket.status !== "rejected";

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
  if (!(await confirmAction({
    title: "Delete draft ticket?", message: "This permanently deletes the draft. It cannot be undone.",
    okLabel: "Delete", danger: true,
  }))) return;
  try {
    await API.deleteTicket(state.activeDetailTicketId);
    toast("Draft deleted.", { type: "info" });
  } catch (e) {
    toast(e.message, { type: "error" });
  }
  el.ticketDetailDialog.close();
  await refreshTicketViews();
});

el.ticketDetailArchiveBtn.addEventListener("click", async () => {
  if (state.activeDetailTicketId == null) return;
  try {
    await API.deleteTicket(state.activeDetailTicketId);
    toast("Ticket archived.", { type: "info" });
  } catch (e) {
    toast(e.message, { type: "error" });
  }
  el.ticketDetailDialog.close();
  await refreshTicketViews();
});

el.ticketDetailRevertBtn.addEventListener("click", async () => {
  if (state.activeDetailTicketId == null) return;
  try {
    const reverted = await API.revertRejection(state.activeDetailTicketId);
    toast(
      reverted.status === "risk_rejected"
        ? "Reverted — but it's currently blocked by risk checks again."
        : "Reverted — back in the Signal Queue for review.",
      { type: reverted.status === "risk_rejected" ? "warning" : "success" },
    );
  } catch (e) {
    toast(e.message, { type: "error" });
  }
  el.ticketDetailDialog.close();
  await refreshTicketViews();
});

el.ticketDetailEditBtn.addEventListener("click", () => {
  const t = state.activeDetailTicket;
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
    await refreshTicketViews();
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
        renderTicketPreview();
      }
    } catch (e) {
      // leave quantity as-is on a transient failure
    }
  }, 300);
}
el.ticketEntryPriceInput.addEventListener("input", maybeAutoPopulateQuantity);
el.ticketStopLossInput.addEventListener("input", maybeAutoPopulateQuantity);

// Live capital / risk / reward preview while filling in a manual ticket.
function renderTicketPreview() {
  const f = new FormData(el.ticketForm);
  const qty = Number(f.get("quantity"));
  const entry = Number(f.get("entry_price"));
  const stop = Number(f.get("stop_loss"));
  const target = Number(f.get("target_1"));
  if (!(qty > 0 && entry > 0 && stop > 0)) {
    el.ticketPreview.hidden = true;
    return;
  }
  const perShare = Math.abs(entry - stop);
  const rr = target > 0 && perShare > 0 ? Math.abs(target - entry) / perShare : null;
  el.ticketPreview.hidden = false;
  el.ticketPreview.innerHTML = [
    ["Capital", inr(entry * qty)],
    ["Risk if stopped", inr(perShare * qty)],
    ["Risk : Reward", rr != null ? `1 : ${rr.toFixed(2)}` : "—"],
    ["On approval", state.executionMode === "live" ? "LIVE — real order" : "Dry run — simulated"],
  ].map(([label, value]) => `<div class="ac-metric"><label>${label}</label><strong>${value}</strong></div>`).join("");
}
el.ticketForm.addEventListener("input", renderTicketPreview);

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
    renderTicketPreview();
    refreshTicketViews();
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
      toast("Switched to DRY RUN — approvals are simulated.", { type: "info" });
    }).catch((e) => {
      el.modeToggleCheckbox.checked = true;
      toast(e.message, { type: "error" });
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
    toast("LIVE trading enabled — approvals now place real orders.", { type: "warning" });
  } catch (e) {
    el.modeToggleCheckbox.checked = false;
    toast(e.message, { type: "error" });
  }
  renderModeBanner();
  el.liveModeConfirmDialog.close();
});

// Logout — confirm only if there's unsaved ticket-form/dialog state.
el.logoutBtn.addEventListener("click", async () => {
  const hasOpenConfirm = el.ticketConfirmDialog.open;
  if ((state.ticketDraftDirty || hasOpenConfirm) && !(await confirmAction({
    title: "Log out?", message: "You have an unsaved ticket draft or an open confirmation.", okLabel: "Log out",
  }))) {
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
    if (!(await confirmAction({
      title: `Remove ${symbol}'s sector?`, message: "It will no longer count toward any sector exposure limit.",
      okLabel: "Remove", danger: true,
    }))) return;
    try {
      await API.deleteSectorMapping(symbol);
      await Promise.all([refreshSectorMappings(), refreshUnmappedSymbols()]);
      toast(`Removed the sector mapping for ${symbol}.`, { type: "success" });
    } catch (e) {
      toast(e.message, { type: "error" });
    }
  } else if (btn.dataset.action === "edit-sector") {
    // Edit in place via the (upserting) mapping form rather than a browser prompt().
    const current = state.sectorMappings.find((m) => m.symbol === symbol);
    document.getElementById("sm-symbol").value = symbol;
    const sectorInput = document.getElementById("sm-sector");
    sectorInput.value = current?.sector ?? "";
    el.sectorMappingForm.scrollIntoView({ behavior: "smooth", block: "center" });
    sectorInput.focus();
    sectorInput.select();
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
    // Not editable here — resend the stored value so saving never wipes it.
    starting_capital: state.riskSettings?.starting_capital ?? null,
  };
  try {
    state.riskSettings = await API.saveRiskSettings(body);
    renderRiskSettingsForm();
    toast("Risk settings saved — every new ticket is checked against them.", { type: "success" });
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
  { id: "approvals", el: document.getElementById("view-approvals") },
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
  if (!SECTIONS.some((s) => s.id === id)) id = "dashboard";
  state.activeSection = id;
  for (const s of SECTIONS) {
    s.el.hidden = s.id !== id;
  }
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.sectionId === id);
  });
  if (location.hash !== `#${id}`) history.replaceState(null, "", `#${id}`);
  if (id === "approvals") loadApprovals();
  if (id === "orders-trades") refreshOrderBooks();
  window.scrollTo({ top: 0 });
}

document.querySelectorAll(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => showSection(btn.dataset.sectionId));
});

// Typing/pasting a #section URL (or a bookmark) switches view without a reload.
window.addEventListener("hashchange", () => {
  const id = location.hash.slice(1);
  const btn = document.querySelector(`.nav-item[data-section-id="${CSS.escape(id)}"]`);
  if (btn && !btn.hidden) showSection(id);
});

function revealTradingNavItems() {
  for (const id of [
    "approvals", "new-ticket", "ticket-history", "orders-trades", "settings", "watchlist", "strategies",
    "trade-queue", "todays-actions", "journal",
  ]) {
    const btn = document.querySelector(`.nav-item[data-section-id="${id}"]`);
    if (btn) btn.hidden = false;
  }
  document.querySelectorAll("[data-trading-only]").forEach((node) => { node.hidden = false; });
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

// ---- Approvals inbox, autopilot status, live refresh, theme ----

let _refreshInFlight = null;
let _refreshQueued = false;
let _refreshTimer = null;

// Reloads every ticket-derived view together so a decision made anywhere
// (a dialog, the inbox, another tab via the WebSocket) shows up everywhere.
function refreshTicketViews() {
  if (state.accountType !== "trading") return Promise.resolve();
  if (_refreshInFlight) {
    _refreshQueued = true;
    return _refreshInFlight;
  }
  _refreshInFlight = Promise.all([
    refreshTickets(), loadSignalQueue(), loadTradeQueue(), loadTodaysActions(), loadStopAdjustments(),
    loadApprovals(), loadAutomationStatus(),
  ]).finally(() => {
    _refreshInFlight = null;
    if (_refreshQueued) {
      _refreshQueued = false;
      refreshTicketViews();
    }
  });
  return _refreshInFlight;
}

function scheduleRefresh(delay = 400) {
  clearTimeout(_refreshTimer);
  _refreshTimer = setTimeout(refreshTicketViews, delay);
}

async function loadApprovals() {
  if (state.accountType !== "trading") return;
  try {
    const [pending, blocked, adjustments] = await Promise.all([
      API.listTickets({ status: "pending_confirmation" }),
      API.listTickets({ status: "risk_rejected" }),
      API.getStopAdjustments(),
    ]);
    const today = istDateOf(new Date());
    state.approvals = {
      entries: pending.filter((t) => t.closes_ticket_id == null),
      exits: pending.filter((t) => t.closes_ticket_id != null),
      adjustments,
      blocked: blocked.filter((t) => {
        const created = parseUtc(t.created_at);
        return created && istDateOf(created) === today;
      }),
    };
    renderApprovals();
  } catch (e) {
    // keep the last rendered inbox on a transient failure
  }
}

function emptyLine(text) {
  return `<div class="empty-row">${esc(text)}</div>`;
}

function renderApprovals() {
  const a = state.approvals;
  if (!a) return;
  const total = a.entries.length + a.exits.length + a.adjustments.length;
  el.apEntries.innerHTML = a.entries.length
    ? a.entries.map((t) => approvalCardHtml(t, "entry")).join("") : emptyLine("No entry signals waiting.");
  el.apExits.innerHTML = a.exits.length
    ? a.exits.map((t) => approvalCardHtml(t, "exit")).join("") : emptyLine("No exit signals waiting.");
  el.apAdjustments.innerHTML = a.adjustments.length
    ? a.adjustments.map(adjustmentCardHtml).join("") : emptyLine("No stop adjustments proposed.");
  el.apBlocked.innerHTML = a.blocked.length
    ? a.blocked.map((t) => approvalCardHtml(t, "blocked")).join("") : emptyLine("Nothing was blocked today.");
  setCount("ap-entries-count", a.entries.length);
  setCount("ap-exits-count", a.exits.length);
  setCount("ap-adjustments-count", a.adjustments.length);
  setCount("ap-blocked-count", a.blocked.length);
  el.apEmpty.hidden = total > 0;
  for (const id of ["ap-entries-group", "ap-exits-group", "ap-adjustments-group"]) {
    document.getElementById(id).hidden = total === 0;
  }
  setBadge(el.navBadgeApprovals, total);
  el.dashApprovalsCallout.hidden = total === 0;
  el.dashApprovalsText.textContent = total === 1
    ? "1 decision is waiting for your approval."
    : `${total} decisions are waiting for your approval.`;
  document.title = total ? `(${total}) Kite Terminal` : "Kite Terminal";
}

function metricHtml(label, value, cls = "") {
  return `<div class="ac-metric"><label>${label}</label><strong class="${cls}">${value}</strong></div>`;
}

function approvalCardHtml(t, kind) {
  const isExit = t.closes_ticket_id != null;
  const checks = t.risk_check_results || [];
  const failed = checks.filter((c) => !c.passed);
  const metrics = isExit
    ? [
        metricHtml("Quantity", fmt(t.quantity, 0)),
        metricHtml("Order", esc(t.entry_order_type)),
        metricHtml("Ref. price", inr(t.entry_price)),
        metricHtml("Value", inr(t.entry_price * t.quantity, 0)),
      ]
    : [
        metricHtml("Quantity", fmt(t.quantity, 0)),
        metricHtml(`Entry · ${esc(t.entry_order_type)}`, inr(t.entry_price)),
        metricHtml("Stop", inr(t.stop_loss), "pnl-negative"),
        metricHtml("Target", inr(t.target_1), "pnl-positive"),
        metricHtml("Capital", inr(t.entry_price * t.quantity, 0)),
        metricHtml("Risk", inr(Math.abs(t.entry_price - t.stop_loss) * t.quantity, 0)),
        metricHtml("R : R", t.risk_reward_ratio != null ? `1 : ${t.risk_reward_ratio.toFixed(2)}` : "—"),
        metricHtml("Trailing", t.trailing_enabled ? `${inr(t.trailing_distance)} after T1` : "Off"),
      ];
  const actions = kind === "blocked"
    ? `<button class="btn btn-ghost btn-small" data-action="details">Why blocked?</button>
       <button class="btn btn-ghost btn-small" data-action="archive">Archive</button>`
    : `<button class="btn btn-ghost btn-small" data-action="details">Details</button>
       <button class="btn btn-outline-danger btn-small" data-action="reject">Reject</button>
       <button class="btn btn-success btn-small" data-action="approve">${isExit ? "Approve exit" : "Approve &amp; place"}</button>`;
  let checkLine = "";
  if (kind === "blocked") {
    checkLine = `<span class="chip live">✗ ${failed.length ? failed.map((c) => esc(c.label)).join(", ") : "risk check failed"}</span>`;
  } else if (checks.length) {
    checkLine = `<span class="chip ok">✓ ${checks.length - failed.length}/${checks.length} risk checks passed</span>`;
  }
  const origin = t.source === "strategy" ? strategyChipHtml(t.strategy_name) : `<span class="chip">Manual</span>`;
  return `
    <article class="approval-card ${kind}" data-ticket-id="${t.id}">
      <div class="ac-top">
        <div class="ac-title">
          ${sideHtml(t.transaction_type)}<span class="ac-symbol">${esc(t.symbol)}</span>${origin}
          ${t.signal_date ? `<span class="badge-muted">signal ${esc(fmtDate(t.signal_date))}</span>` : ""}
        </div>
        <div class="ac-actions">${actions}</div>
      </div>
      <div class="ac-metrics">${metrics.join("")}</div>
      <p class="ac-rationale">${esc(t.rationale)}</p>
      <div class="ac-foot">${checkLine}<span>created ${fmtDateTime(t.created_at)}</span></div>
    </article>`;
}

function adjustmentCardHtml(p) {
  return `
    <article class="approval-card adjust" data-proposal-id="${p.id}">
      <div class="ac-top">
        <div class="ac-title">
          <span class="chip warn">STOP</span><span class="ac-symbol">${esc(p.symbol)}</span>
          <span class="badge-muted">${fmt(p.quantity, 0)} shares</span>
        </div>
        <div class="ac-actions">
          <button class="btn btn-outline-danger btn-small" data-action="reject-adj">Reject</button>
          <button class="btn btn-success btn-small" data-action="approve-adj">Move stop</button>
        </div>
      </div>
      <div class="ac-metrics">
        ${metricHtml("Current stop", inr(p.current_stop))}
        ${metricHtml("Proposed stop", inr(p.proposed_stop), "pnl-positive")}
        ${metricHtml("Entry", inr(p.entry_price))}
      </div>
      <p class="ac-rationale">${esc(p.reason)}</p>
    </article>`;
}

function approvalConfirmHtml(t) {
  const note = executionNote(t);
  const price = t.entry_order_type === "LIMIT" ? ` @ ${inr(t.entry_price)}` : "";
  return `<p>${t.closes_ticket_id != null ? "Close" : "Open"} <strong>${esc(t.transaction_type)} ${fmt(t.quantity, 0)} ${esc(t.symbol)}</strong>
      — ${esc(t.entry_order_type)}${price}.</p>
    <div class="exec-note${note.live ? " live" : ""}">${esc(note.text)}</div>`;
}

el.viewApprovals.addEventListener("click", async (event) => {
  const btn = event.target.closest("button[data-action]");
  const card = btn?.closest(".approval-card");
  if (!btn || !card || card.classList.contains("busy")) return;
  const action = btn.dataset.action;

  if (action === "approve-adj" || action === "reject-adj") {
    const approve = action === "approve-adj";
    const proposal = state.approvals.adjustments.find((p) => p.id === Number(card.dataset.proposalId));
    if (approve && !(await confirmAction({
      title: `Move ${proposal?.symbol ?? ""} stop to breakeven?`,
      html: `<p>Stop ${inr(proposal?.current_stop)} → <strong>${inr(proposal?.proposed_stop)}</strong>.</p>
        <div class="exec-note${state.executionMode === "live" ? " live" : ""}">${state.executionMode === "live"
          ? "LIVE — the protective GTT at the broker is modified." : "DRY RUN — only the app's record is updated."}</div>`,
      okLabel: "Move stop", okClass: "btn-success",
    }))) return;
    card.classList.add("busy");
    try {
      if (approve) await API.confirmStopAdjustment(card.dataset.proposalId);
      else await API.rejectStopAdjustment(card.dataset.proposalId);
      toast(approve ? "Stop moved to breakeven." : "Stop adjustment rejected.", { type: approve ? "success" : "info" });
    } catch (e) {
      card.classList.remove("busy");
      toast(e.message, { type: "error", title: "Stop adjustment failed" });
    }
    await refreshTicketViews();
    return;
  }

  const id = Number(card.dataset.ticketId);
  const a = state.approvals;
  const ticket = [...a.entries, ...a.exits, ...a.blocked].find((t) => t.id === id);
  if (!ticket) return;
  if (action === "details") {
    openConfirmDialog(ticket);
  } else if (action === "approve") {
    const live = state.executionMode === "live";
    const ok = await confirmAction({
      title: live ? `Place a real ${ticket.transaction_type} order?` : "Approve this trade (dry run)?",
      html: approvalConfirmHtml(ticket),
      okLabel: live ? "Place order" : "Approve",
      okClass: live ? "btn-danger" : "btn-success",
    });
    if (!ok) return;
    card.classList.add("busy");
    await approveTicket(id);
  } else if (action === "reject") {
    card.classList.add("busy");
    await rejectTicketById(id, "Rejected from Approvals");
  } else if (action === "archive") {
    card.classList.add("busy");
    try {
      await API.deleteTicket(id);
      toast("Archived.", { type: "info" });
    } catch (e) {
      toast(e.message, { type: "error" });
    }
    await refreshTicketViews();
  }
});

el.apRefreshBtn.addEventListener("click", () => refreshTicketViews());
el.dashApprovalsOpen.addEventListener("click", () => showSection("approvals"));
el.autopilotPill.addEventListener("click", () => showSection("approvals"));

async function loadAutomationStatus() {
  if (state.accountType !== "trading") return;
  try {
    state.automationStatus = await API.automationStatus();
    renderAutomation();
  } catch (e) {
    // keep the last known status on a transient failure
  }
}

function fmtNextRun(iso) {
  const d = new Date(iso);
  const tz = { timeZone: "Asia/Kolkata" };
  const dayKey = (x) => x.toLocaleDateString("en-IN", { ...tz, year: "numeric", month: "numeric", day: "numeric" });
  const time = d.toLocaleTimeString("en-IN", { ...tz, hour: "2-digit", minute: "2-digit", hour12: false });
  if (dayKey(d) === dayKey(new Date())) return `today ${time}`;
  return `${d.toLocaleDateString("en-IN", { ...tz, weekday: "short", day: "numeric", month: "short" })} ${time}`;
}

function runStatusChip(status) {
  const cls = { success: "ok", skipped: "", failed: "live", running: "dry" }[status] ?? "";
  return `<span class="chip ${cls}">${esc(status)}</span>`;
}

function runSummaryText(run) {
  if (run.error) return run.error;
  const s = run.summary || {};
  if (run.status === "skipped") return s.reason || "skipped";
  if (!s.scan) return run.status === "running" ? "in progress" : "";
  if (!s.scan.enabled) return "no strategies enabled";
  let entries = 0;
  let exits = 0;
  for (const r of Object.values(s.scan.by_strategy || {})) {
    entries += r.entry_tickets.length;
    exits += r.exit_tickets.length;
  }
  const expired = (s.expired_signals || []).length;
  return `${entries} entry, ${exits} exit signal(s)${expired ? `, ${expired} expired` : ""}`;
}

function runRowHtml(run) {
  const summary = runSummaryText(run);
  return `<div class="run-row">
    <span>${fmtDateTime(run.started_at)}</span>
    <span>${esc(run.trigger.replace("_", " "))}</span>
    <span>${runStatusChip(run.status)}</span>
    <span class="run-detail" title="${esc(summary)}">session ${esc(fmtDate(run.run_date))} · ${esc(summary)}</span>
  </div>`;
}

function pipelineStepHtml(kind, marker, title, sub) {
  return `<div class="pipeline-step ${kind}"><span class="pipeline-dot">${marker}</span>
    <div class="step-text"><strong>${title}</strong><span>${sub}</span></div></div>`;
}

function autopilotCardHtml(s) {
  const last = s.runs[0] || null;
  const strategies = s.enabled_strategies.length
    ? s.enabled_strategies.map(strategyChipHtml).join(" ")
    : `<span class="chip warn">none enabled</span>`;
  const lastText = last
    ? `${fmtDateTime(last.started_at)} · ${runStatusChip(last.status)} ${esc(runSummaryText(last))}`
    : "No runs yet";
  let nextText = fmtNextRun(s.next_run_at);
  if (s.running) nextText = "running now";
  else if (s.due_now) nextText = s.session_connected ? "due — starting shortly" : "due — log in to Kite to run";
  const state_ = s.running ? '<span class="chip dry">running</span>'
    : s.enabled_strategies.length ? '<span class="chip ok">on</span>' : '<span class="chip warn">idle</span>';
  return `
    <div>
      <div class="ap-headline"><h2>Autopilot</h2>${state_}${modeChipHtml(s.execution_mode)}</div>
      <div class="ap-facts">
        <div class="ap-fact"><label>Strategies</label><span>${strategies}</span></div>
        <div class="ap-fact"><label>Next run</label><span>${esc(nextText)} <span class="badge-muted">daily ${esc(s.schedule_time_ist)} IST</span></span></div>
        <div class="ap-fact"><label>Last run</label><span>${lastText}</span></div>
        <div class="ap-fact"><label>Tracking</label><span>${s.pending.awaiting_fill} order(s) awaiting fill · ${s.pending_data_jobs} data job(s) queued</span></div>
      </div>
      <div class="ap-actions">
        <button class="btn btn-primary btn-small" data-ap-action="run-now" ${s.running ? "disabled" : ""}>Run autopilot now</button>
        <button class="btn btn-ghost btn-small" data-ap-action="strategies">Strategy settings</button>
      </div>
    </div>
    <div class="pipeline">
      <div class="pipeline-title">Every trading day after the close</div>
      ${pipelineStepHtml("auto", "1", "Sync market data", "~3 years of daily candles; mid-session candles corrected")}
      ${pipelineStepHtml("auto", "2", "Scan strategies", "Expire stale signals, size positions, run risk checks")}
      ${pipelineStepHtml("manual", "✓", "You approve or reject", "The only manual step")}
      ${pipelineStepHtml("auto", "3", "Place order + protective GTT", s.settings.amo_when_market_closed ? "Sent as AMO when the market is closed" : "Regular order")}
      ${pipelineStepHtml("auto", "4", "Track fill, trail stop, reconcile", "Runs continuously while the app is open")}
    </div>`;
}

function renderAutomation() {
  const s = state.automationStatus;
  if (!s) return;
  const last = s.runs[0] || null;
  let pillClass = "ok";
  let pillText = `Autopilot · next ${fmtNextRun(s.next_run_at)}`;
  if (s.running) {
    pillClass = "running";
    pillText = "Autopilot running…";
  } else if (!s.enabled_strategies.length) {
    pillClass = "warn";
    pillText = "Autopilot · no strategies on";
  } else if (s.due_now && !s.session_connected) {
    pillClass = "warn";
    pillText = "Autopilot · waiting for login";
  } else if (last && last.status === "failed" && !s.last_session_done) {
    pillClass = "err";
    pillText = "Autopilot · last run failed";
  } else if (s.due_now) {
    pillClass = "warn";
    pillText = "Autopilot · run due";
  }
  el.autopilotPill.hidden = false;
  el.autopilotPill.className = `pill autopilot-pill ${pillClass}`;
  el.autopilotPillText.textContent = pillText;
  el.autopilotCard.innerHTML = autopilotCardHtml(s);
  el.apRuns.innerHTML = s.runs.length ? s.runs.map(runRowHtml).join("") : emptyLine("No autopilot runs yet.");
}

el.autopilotCard.addEventListener("click", async (event) => {
  const btn = event.target.closest("[data-ap-action]");
  if (!btn) return;
  if (btn.dataset.apAction === "strategies") {
    showSection("strategies");
    return;
  }
  btn.disabled = true;
  try {
    await API.automationRunNow();
    toast("Syncing market data and scanning every enabled strategy — this can take a minute.",
      { type: "info", title: "Autopilot started" });
  } catch (e) {
    toast(e.message, { type: "error" });
  }
  setTimeout(loadAutomationStatus, 800);
});

const THEME_ICONS = {
  dark: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
  light: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/></svg>',
};

function applyTheme(theme) {
  const t = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = t;
  el.themeToggleBtn.innerHTML = THEME_ICONS[t];
  el.themeToggleBtn.title = t === "dark" ? "Switch to light theme" : "Switch to dark theme";
  try {
    localStorage.setItem("kt-theme", t);
  } catch (e) {
    // private mode / blocked storage — the server-side pref still applies
  }
}
applyTheme(document.documentElement.dataset.theme);

el.themeToggleBtn.addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  applyTheme(next);
  API.saveUiPrefs({ theme: next }).catch(() => {});
});

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
    if (prefs.theme) applyTheme(prefs.theme);
  } catch (e) {
    // default expanded; theme stays as restored from localStorage
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
  const requestedSection = location.hash.slice(1);
  showSection("dashboard");

  if (state.accountType === "trading") {
    revealTradingNavItems();
    await loadTicketingData();
    await Promise.all([
      loadRiskData(), loadWatchlist(), loadStrategyData(), loadPhase6Data(),
      loadReconciliationMismatches(), loadApprovals(), loadAutomationStatus(),
    ]);
    if (requestedSection) showSection(requestedSection);
    setInterval(loadReconciliationMismatches, 60000);
    // Safety net behind the WebSocket push: queues/badges never go stale.
    setInterval(refreshTicketViews, 60000);
  }
}

el.connectBtn.addEventListener("click", () => { location.href = "/login"; });
el.reconnectBtn.addEventListener("click", () => { location.href = "/login"; });
el.refreshBtn.addEventListener("click", () => {
  loadDashboard();
  refreshTicketViews();
});

init();
