// PK-19: every ticket and position state in this file is decided by the
// SERVER (backend/lifecycle.py) and only rendered here, through lifecycle.js
// (loaded first). No label, flag or button is worked out from raw ticket
// columns (status / fill / submission / closed_at / GTT id) in this file.
const LC = window.KTLifecycle;

class SessionExpiredError extends Error {}
// SEC-1 P1-F: a write refused because this browser holds no credential for
// this launch of the terminal (restart or logout). Reading still works.
class TerminalAuthorizationError extends Error {}
class ApiError extends Error {
  constructor(message, errorCode, failures = null) {
    super(message);
    this.errorCode = errorCode;
    // PK-12: every account-safety reason a refusal found ({code, message}).
    this.failures = failures;
  }
}

// wholeBody: return the whole response body rather than its `data` (a
// payload that also carries e.g. summary counts).
async function fetchJson(url, wholeBody = false) {
  const res = await fetch(url);
  if (res.status === 401) {
    const body = await res.json();
    throw new SessionExpiredError(body.detail.message);
  }
  if (!res.ok) {
    throw new Error(`Request failed: ${res.status}`);
  }
  const body = await res.json();
  return wholeBody ? body : body.data;
}

async function fetchJsonMutate(url, method, body) {
  const res = await fetch(url, {
    method,
    // X-Kite-Terminal is required by the server on every write (CSRF guard, P0-8).
    headers: { "Content-Type": "application/json", "X-Kite-Terminal": "1" },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) {
    const parsed = await res.json().catch(() => null);
    if (parsed?.detail?.error_code === "TERMINAL_SESSION_REQUIRED") {
      showAuthorizeBanner(true);
      throw new TerminalAuthorizationError(parsed.detail.message);
    }
    throw new SessionExpiredError(parsed?.detail?.message || "Session expired");
  }
  const parsed = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = parsed?.detail;
    throw new ApiError(detail?.message || `Request failed: ${res.status}`, detail?.error_code, detail?.failures || null);
  }
  return parsed.data;
}

// PK-16: reconciliation findings about a protective GTT, either direction
// (app -> Kite and Kite -> app), get the structured Protection / GTT table;
// everything else (and every row written before PK-16) keeps the plain list.
const RECONCILIATION_GTT_CODES = new Set([
  "ORPHAN_GTT", "GTT_ON_CLOSED_POSITION", "GTT_QUANTITY_TOO_HIGH", "GTT_QUANTITY_TOO_LOW", "GTT_MISSING",
  "GTT_PRICE_MISMATCH", "MULTIPLE_ACTIVE_GTTS", "UNLINKED_POSSIBLE_GTT", "AMBIGUOUS_GTT_OWNERSHIP",
]);

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
  getTicketChart: (id) => fetchJson(`/api/tickets/${id}/chart`),
  createTicket: (body) => fetchJsonMutate("/api/tickets", "POST", body),
  createAlertActionSell: (body) => fetchJsonMutate("/api/tickets/alert-action/sell", "POST", body),
  createAlertActionBuy: (body) => fetchJsonMutate("/api/tickets/alert-action/buy", "POST", body),
  getPositionReduction: (symbol) => fetchJson(`/api/risk/position-reduction?symbol=${encodeURIComponent(symbol)}`),
  editTicket: (id, body) => fetchJsonMutate(`/api/tickets/${id}`, "PUT", body),
  deleteTicket: (id) => fetchJsonMutate(`/api/tickets/${id}`, "DELETE"),
  // PK-11: a new entry carries the execution mode the user was shown; the
  // server refuses (409) if it is missing or no longer current.
  // PK-12H: allow_amo is sent only when the user ticked "Send as AMO".
  // SEC-1 P1-D: a manual / Quick Action buy also carries the review token
  // the approving view showed; the server refuses a missing or older one.
  confirmTicket: (id, expectedMode, allowAmo = false, reviewToken = undefined) => fetchJsonMutate(
    `/api/tickets/${id}/confirm`, "POST",
    expectedMode ? {
      expected_mode: expectedMode,
      ...(allowAmo ? { allow_amo: true } : {}),
      ...(reviewToken ? { expected_review_token: reviewToken } : {}),
    } : undefined),
  reReviewTicket: (id) => fetchJsonMutate(`/api/tickets/${id}/re-review`, "POST"),
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

  getDashboardPanels: (screen = "dashboard") => fetchJson(`/api/dashboard-panels?screen=${encodeURIComponent(screen)}`),
  saveDashboardPanels: (panels, screen = "dashboard") =>
    fetchJsonMutate(`/api/dashboard-panels?screen=${encodeURIComponent(screen)}`, "PUT", { panels }),

  getWatchlist: () => fetchJson("/api/watchlist"),
  addWatchlistSymbol: (body) => fetchJsonMutate("/api/watchlist", "POST", body),
  removeWatchlistSymbol: (id) => fetchJsonMutate(`/api/watchlist/${id}`, "DELETE"),
  updateWatchlistTag: (id, tag) => fetchJsonMutate(`/api/watchlist/${id}/tag`, "PUT", { tag }),

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

  // PK-12: account-level safety
  accountSafetyStatus: () => fetchJson("/api/account-safety/status"),
  setTradingLock: (locked, reason) => fetchJsonMutate("/api/account-safety/trading-lock", "POST", { locked, reason }),
  resetBreaker: (reason) => fetchJsonMutate("/api/account-safety/breaker/reset", "POST", { reason }),

  // Phase 8: kill switch
  cancelAllPending: () => fetchJsonMutate("/api/tickets/cancel-all-pending", "POST"),
  cancelOrder: (orderId, variety) => fetchJsonMutate(`/api/orders/${encodeURIComponent(orderId)}/cancel?variety=${encodeURIComponent(variety || "regular")}`, "POST"),
  cancelGtt: (gttId) => fetchJsonMutate(`/api/gtts/${gttId}/cancel`, "POST"),
  emergencyExitPreview: () => fetchJson("/api/kill-switch/emergency-exit-preview"),

  // PK-19: app-managed positions with their server-derived lifecycle. The
  // whole body is returned: the active view also carries header counts.
  appPositions: (view) => fetchJson(`/api/app-positions?view=${encodeURIComponent(view)}`, true),
  // Creates an exit ticket for approval — never sends an order by itself.
  exitPosition: (positionId) => fetchJsonMutate(`/api/app-positions/${positionId}/exit`, "POST"),
  emergencyExitConfirm: (confirmationPhrase) => fetchJsonMutate("/api/kill-switch/emergency-exit-confirm", "POST", { confirmation_phrase: confirmationPhrase }),

  // Phase 8: reconciliation
  listMismatches: (resolved) => fetchJson(`/api/reconciliation/mismatches?resolved=${resolved ? "true" : "false"}`),
  markMismatchReviewed: (id, reason) => fetchJsonMutate(`/api/reconciliation/${id}/mark-reviewed`, "POST", { reason }),
  reconciliationStatus: () => fetchJson("/api/reconciliation/status"),

  // Phase 8: strategy versioning
  getStrategyVersions: (strategyName) => fetchJson(`/api/strategies/${encodeURIComponent(strategyName)}/versions`),

  // Autopilot
  automationStatus: () => fetchJson("/api/automation/status"),
  automationRunNow: () => fetchJsonMutate("/api/automation/run-now", "POST"),
};

const el = {
  connectGate: document.getElementById("connect-gate"),
  authorizeBanner: document.getElementById("authorize-banner"),
  authorizeBtn: document.getElementById("authorize-btn"),
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
  // PK-12 account safety
  accountSafetyBar: document.getElementById("account-safety-bar"),
  tradingLockPill: document.getElementById("trading-lock-pill"),
  breakerPill: document.getElementById("breaker-pill"),
  accountSafetySummary: document.getElementById("account-safety-summary"),
  tradingLockBtn: document.getElementById("trading-lock-btn"),
  breakerResetBtn: document.getElementById("breaker-reset-btn"),
  ticketConfirmAmo: document.getElementById("ticket-confirm-amo"),
  ticketConfirmAmoCheckbox: document.getElementById("ticket-confirm-amo-checkbox"),

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
  ticketConfirmReview: document.getElementById("ticket-confirm-review"),
  ticketReReviewBtn: document.getElementById("ticket-rereview-btn"),
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

  // Signal Detail view
  signalDetail: document.getElementById("signal-detail"),
  sdSide: document.getElementById("sd-side"),
  sdSymbol: document.getElementById("sd-symbol"),
  sdLifecycle: document.getElementById("sd-lifecycle"),
  sdStrategy: document.getElementById("sd-strategy"),
  sdSignalDate: document.getElementById("sd-signal-date"),
  sdModePill: document.getElementById("sd-mode-pill"),
  sdLtp: document.getElementById("sd-ltp"),
  sdLtpChange: document.getElementById("sd-ltp-change"),
  sdAsof: document.getElementById("sd-asof"),
  sdPrev: document.getElementById("sd-prev"),
  sdNext: document.getElementById("sd-next"),
  sdPosition: document.getElementById("sd-position"),
  sdClose: document.getElementById("sd-close"),
  sdTheme: document.getElementById("sd-theme"),
  sdToggles: document.getElementById("sd-toggles"),
  sdChart: document.getElementById("sd-chart"),
  sdChartNote: document.getElementById("sd-chart-note"),
  sdEvidenceInner: document.getElementById("sd-evidence-inner"),
  sdKiteOpen: document.getElementById("sd-kite-open"),
  sdKiteMore: document.getElementById("sd-kite-more"),
  sdKiteMenu: document.getElementById("sd-kite-menu"),
  sdKiteNote: document.getElementById("sd-kite-note"),
  sdExecNote: document.getElementById("sd-exec-note"),
  sdApprove: document.getElementById("sd-approve"),
  sdReject: document.getElementById("sd-reject"),

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
  watchlistTagInput: document.getElementById("wl-tag"),
  watchlistAddError: document.getElementById("watchlist-add-error"),
  watchlistBody: document.querySelector("#watchlist-table tbody"),
  watchlistSummaryBody: document.querySelector("#watchlist-summary-table tbody"),
  watchlistNearBreakoutBody: document.querySelector("#watchlist-near-breakout-table tbody"),
  watchlistCount: document.getElementById("watchlist-count"),
  watchlistGroupToggle: document.getElementById("watchlist-group-toggle"),
  tagOptions: document.getElementById("tag-options"),

  // Phase 9: configurable Watchlist panels (same mechanism as Dashboard panels)
  watchlistPanelsGrid: document.getElementById("watchlist-panels-grid"),
  watchlistEmptyNote: document.getElementById("watchlist-empty-note"),
  watchlistCustomizeModeToggle: document.getElementById("watchlist-customize-mode-toggle"),
  watchlistAddPanelControl: document.getElementById("watchlist-add-panel-control"),
  watchlistAddPanelSelect: document.getElementById("watchlist-add-panel-select"),

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
  emergencyExitPreviewBody: document.querySelector("#emergency-exit-preview-table tbody"),
  emergencyExitPhrase: document.getElementById("emergency-exit-phrase"),
  emergencyExitError: document.getElementById("emergency-exit-error"),
  emergencyExitCancelBtn: document.getElementById("emergency-exit-cancel-btn"),
  emergencyExitConfirmBtn: document.getElementById("emergency-exit-confirm-btn"),

  // Phase 8: reconciliation
  reconciliationList: document.getElementById("reconciliation-list"),
  // PK-16: the Kite -> app protection view
  reconciliationStatus: document.getElementById("reconciliation-status"),
  reconciliationGtt: document.getElementById("reconciliation-gtt"),
  reconciliationGttBody: document.querySelector("#reconciliation-gtt-table tbody"),

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
  navBadgePositions: document.getElementById("nav-badge-positions"),
  positionsActiveBody: document.querySelector("#positions-active-table tbody"),
  positionsClosedBody: document.querySelector("#positions-closed-table tbody"),
  positionsSummary: document.getElementById("positions-summary"),
  positionsRefreshBtn: document.getElementById("positions-refresh-btn"),
  dashApprovalsCallout: document.getElementById("dash-approvals-callout"),
  dashApprovalsText: document.getElementById("dash-approvals-text"),
  dashApprovalsOpen: document.getElementById("dash-approvals-open"),
  toastStack: document.getElementById("toast-stack"),
  appConfirmDialog: document.getElementById("app-confirm-dialog"),
  appConfirmTitle: document.getElementById("app-confirm-title"),
  appConfirmBody: document.getElementById("app-confirm-body"),
  appConfirmOk: document.getElementById("app-confirm-ok"),

  // Quick Action popup (Dashboard per-stock alert -> Sell/Buy)
  quickActionDialog: document.getElementById("quick-action-dialog"),
  qaTitle: document.getElementById("qa-title"),
  qaHeader: document.getElementById("qa-header"),
  qaSellSection: document.getElementById("qa-sell-section"),
  qaSellTitle: document.getElementById("qa-sell-title"),
  qaSellQty: document.getElementById("qa-sell-qty"),
  qaSellOrderType: document.getElementById("qa-sell-order-type"),
  qaSellRationale: document.getElementById("qa-sell-rationale"),
  qaSellPreview: document.getElementById("qa-sell-preview"),
  qaSellSubmit: document.getElementById("qa-sell-submit"),
  qaSellError: document.getElementById("qa-sell-error"),
  qaBuySection: document.getElementById("qa-buy-section"),
  qaBuyQty: document.getElementById("qa-buy-qty"),
  qaBuyOrderType: document.getElementById("qa-buy-order-type"),
  qaBuyEntryPrice: document.getElementById("qa-buy-entry-price"),
  qaBuyProductType: document.getElementById("qa-buy-product-type"),
  qaBuyStopLoss: document.getElementById("qa-buy-stop-loss"),
  qaBuyTarget1: document.getElementById("qa-buy-target-1"),
  qaBuyRationale: document.getElementById("qa-buy-rationale"),
  qaBuyPreview: document.getElementById("qa-buy-preview"),
  qaBuySubmit: document.getElementById("qa-buy-submit"),
  qaBuyError: document.getElementById("qa-buy-error"),
  qaCloseBtn: document.getElementById("qa-close-btn"),
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
  accountSafety: null,  // PK-12: /api/account-safety/status — display only
  trailingHeartbeat: null, // { last_run_at, active_ticket_count }
  trailingErrorAlerts: [],
  currentAlerts: [],
  quickActionAlert: null,
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

  // Phase 4.5: watchlist
  watchlist: [],
  lastWatchlistSymbolMatches: [],
  watchlistGroupByTag: false,
  watchlistCollapsedGroups: new Set(),

  // Phase 9: configurable Watchlist panels
  watchlistPanels: [],

  // Phase 5: momentum breakout strategy engine
  strategyConfigs: {}, // { [strategy_name]: { strategy_name, enabled, params, updated_at } }
  signalQueue: [],
  stopAdjustments: [],
  backtestResult: null,

  // Phase 6: journal/analytics, today's actions, trade queue
  journalStats: null,
  todaysActions: null,
  tradeQueueTickets: [],
  // PK-19: app-managed positions as the server describes them.
  appPositions: { active: [], closed: [], summary: null, tab: "active" },

  // Phase 8: reconciliation
  reconciliationMismatches: [],
  reconciliationStatus: null,

  // Approvals inbox / autopilot
  automationStatus: null,
  approvals: null,
  activeConfirmTicket: null,
  activeDetailTicket: null,
};

const TRAILING_CHECK_INTERVAL_SECONDS = 45; // mirrors backend/config.py

// SEC-1 P1-F: the terminal credential is an HttpOnly cookie set by the Kite
// login callback; this page never sees it. It only learns whether it has one
// (session status terminal_authorized) or that a write was refused for lack
// of it, and then offers the login that issues a new one.
function showAuthorizeBanner(show) {
  state.terminalAuthorized = !show;
  el.authorizeBanner.hidden = !show;
}

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

// Shared by ticketConfirmHtml/approvalCardHtml so a ticket's origin (how it
// was created) always reads the same way wherever it's shown — including
// source='alert_action' (the Dashboard Quick Action popup), which used to
// fall through to the generic "Manual" label alongside true manual tickets.
function originChipHtml(t) {
  if (t.source === "strategy") return strategyChipHtml(t.strategy_name);
  if (t.source === "alert_action") return `<span class="chip strategy">Quick Action</span>`;
  return `<span class="chip">Manual</span>`;
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

// NSE hours 9:15-15:30 IST, Mon-Fri. Exchange holidays come from the server
// (PK-22C: /api/account-safety/status carries today's session_state), which
// owns the NSE calendar; the browser only checks weekday and time.
function computeMarketStatus() {
  const istMs = Date.now() + 5.5 * 3600 * 1000;
  const d = new Date(istMs);
  const day = d.getUTCDay(); // IST-shifted date, so getUTCDay() reads as IST weekday
  const mins = d.getUTCHours() * 60 + d.getUTCMinutes();
  if (day === 0 || day === 6) return "closed";
  const safety = state.accountSafety;
  if (safety && safety.session_state === "HOLIDAY" && safety.trading_date_ist === d.toISOString().slice(0, 10)) {
    return "closed";
  }
  if (mins >= 555 && mins < 930) return "open"; // 9:15-15:30
  if (mins >= 540 && mins < 555) return "pre_open"; // 9:00-9:15 (approximate)
  return "closed";
}

function renderMarketStatus() {
  const status = computeMarketStatus();
  const labels = { open: "Market Open", pre_open: "Pre-Open", closed: "Market Closed" };
  el.marketStatusBadge.hidden = false;
  el.marketStatusBadge.textContent = labels[status];
  el.marketStatusBadge.title = state.accountSafety?.holiday_name ? `NSE holiday: ${state.accountSafety.holiday_name}` : "";
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
        message: `${h.tradingsymbol} down ${Math.abs(h.loss_pct).toFixed(1)}% from avg cost of ${inr(h.average_price)}`,
        scrollTargetSelector: `#holdings-row-${CSS.escape(h.tradingsymbol)}`,
        type: "breach_loss",
        symbol: h.tradingsymbol,
        holding: h,
      });
    }
    if (h.breach_concentration) {
      alerts.push({
        id: `conc-${h.tradingsymbol}`,
        severity: "warning",
        message: `${h.tradingsymbol} is ${h.pct_of_portfolio.toFixed(1)}% of portfolio value`,
        scrollTargetSelector: `#holdings-row-${CSS.escape(h.tradingsymbol)}`,
        type: "breach_concentration",
        symbol: h.tradingsymbol,
        holding: h,
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
    // PK-19: the server's protection status of the ticket's own position.
    const unprotected = t.lifecycle?.position_id === t.id && t.lifecycle.protection_status === "MISSING";
    if (unprotected) {
      alerts.push({
        id: `protection-failed-${t.id}`,
        severity: "urgent",
        message: `${t.symbol}: no active protective stop — UNPROTECTED position (${t.lifecycle.open_quantity ?? "?"} share(s)), act now`,
        scrollTargetSelector: `tr[data-ticket-id="${t.id}"]`,
      });
    }
  }
  alerts.push(...state.trailingErrorAlerts);

  for (const m of state.reconciliationMismatches || []) {
    alerts.push({
      id: `reconciliation-${m.id}`,
      severity: m.severity === "warning" ? "warning" : "urgent",
      message: m.detail,
      scrollTargetSelector: RECONCILIATION_GTT_CODES.has(m.issue_code) ? "#reconciliation-gtt" : "#reconciliation-list",
    });
  }

  return alerts;
}

function renderAlertBanner(alerts) {
  state.currentAlerts = alerts;
  if (alerts.length === 0) {
    el.alertBanner.hidden = true;
    el.alertBanner.innerHTML = "";
    return;
  }
  el.alertBanner.hidden = false;
  el.alertBanner.innerHTML = alerts
    .map(
      (a) => `
      <div class="alert-chip ${a.severity}" data-alert-id="${esc(a.id)}" data-scroll-target="${esc(a.scrollTargetSelector)}">
        <span class="icon">${a.severity === "urgent" ? "⛔" : "⚠"}</span>
        <span>${esc(a.message)}</span>
      </div>`
    )
    .join("");
  el.alertBanner.querySelectorAll(".alert-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const alert = state.currentAlerts.find((a) => a.id === chip.dataset.alertId);
      // Quick Action modal only for per-stock alerts on a trading account —
      // portfolio-level alerts (day P&L, margin, session) and any other
      // account type keep today's scroll-only behavior.
      if (alert?.type && state.accountType === "trading") {
        openQuickActionModal(alert);
        return;
      }
      const target = document.querySelector(chip.dataset.scrollTarget);
      if (target) {
        target.scrollIntoView({ behavior: "smooth", block: "center" });
        flashCell(target);
      }
    });
  });
}

// ---- Quick Action popup (Dashboard per-stock alert -> Sell/Buy) ----

async function openQuickActionModal(alert) {
  state.quickActionAlert = alert;
  el.qaSellError.textContent = "";
  el.qaBuyError.textContent = "";
  const h = alert.holding;
  const isLoss = alert.type === "breach_loss";

  const qaRow = (label, value, cls = "") => `<div class="row"><span>${label}</span><strong class="${cls}">${value}</strong></div>`;
  el.qaTitle.textContent = `${h.tradingsymbol} — Quick Action`;
  el.qaHeader.innerHTML = [
    qaRow("Quantity", fmt(h.quantity, 0)),
    qaRow("Avg Cost", inr(h.average_price)),
    qaRow("LTP", inr(h.last_price)),
    qaRow("Unrealized P&L", `${inr(h.pnl)} (${h.pnl_pct >= 0 ? "+" : ""}${fmt(h.pnl_pct)}%)`, pnlClass(h.pnl)),
  ].join("") + `<div class="rationale">${esc(alert.message)}</div>`;

  // Concentration alert -> Sell only (buying more would worsen the exact
  // problem being flagged); down-X% alert -> both Sell and Buy.
  el.qaBuySection.hidden = !isLoss;
  el.qaSellTitle.textContent = isLoss ? "Sell to cut loss" : "Sell to reduce exposure";
  el.qaSellSubmit.textContent = isLoss ? "Sell to cut loss" : "Sell to reduce exposure";

  el.qaSellOrderType.value = "MARKET";
  el.qaSellRationale.value = alert.message;
  el.qaSellQty.value = h.quantity; // down-X% default: full holding, editable
  renderQaSellPreview();

  if (!isLoss) {
    // Concentration default: the quantity that brings this position back to
    // exactly max_single_stock_exposure_pct (risk_engine's own basis, not
    // the dashboard alert's own MTM-basis %) — fetched once on open, not
    // per keystroke.
    try {
      const reduction = await API.getPositionReduction(h.tradingsymbol);
      if (state.quickActionAlert === alert) { // modal wasn't closed/reopened while awaiting
        el.qaSellQty.value = reduction.suggested_qty || h.quantity;
        el.qaHeader.innerHTML += `<div class="row"><span>Exposure (market value)</span>` +
          `<strong>${fmt(reduction.current_exposure_pct)}% → target ${fmt(reduction.target_exposure_pct)}%</strong></div>`;
        renderQaSellPreview();
      }
    } catch (e) {
      // fall back to the full holding quantity, already set above
    }
  }

  if (isLoss) {
    el.qaBuyOrderType.value = "MARKET";
    el.qaBuyEntryPrice.value = h.last_price;
    el.qaBuyProductType.value = "CNC";
    el.qaBuyStopLoss.value = "";
    el.qaBuyTarget1.value = "";
    el.qaBuyQty.value = "";
    el.qaBuyRationale.value = `Averaging down — ${alert.message}`;
    renderQaBuyPreview();
  }

  el.quickActionDialog.showModal();
}

function renderQaSellPreview() {
  const alert = state.quickActionAlert;
  if (!alert) return;
  const h = alert.holding;
  const qty = Number(el.qaSellQty.value) || 0;
  if (qty <= 0) {
    el.qaSellPreview.innerHTML = "";
    return;
  }
  const exitPrice = h.last_price;
  // Same formula backend/journal.py:record_closed_trade uses for a real
  // closed_trades row (realized_pnl = (exit_price - entry_price) *
  // quantity) — evaluated here against an ESTIMATED exit (current LTP) for
  // a live preview instead of a real fill, not a separate calculation.
  const realizedPnl = (exitPrice - h.average_price) * qty;
  const realizedPnlPct = h.average_price ? (realizedPnl / (h.average_price * qty)) * 100 : 0;
  el.qaSellPreview.innerHTML = [
    ["Est. exit price", `${inr(exitPrice)} (estimate — market order)`],
    ["Realized P&L", `${inr(realizedPnl)} (${realizedPnlPct >= 0 ? "+" : ""}${fmt(realizedPnlPct)}%)`],
  ].map(([label, value]) => `<div class="ac-metric"><label>${label}</label><strong>${value}</strong></div>`).join("");
}

function renderQaBuyPreview() {
  const alert = state.quickActionAlert;
  if (!alert) return;
  const h = alert.holding;
  const newQty = Number(el.qaBuyQty.value) || 0;
  const price = Number(el.qaBuyEntryPrice.value) || h.last_price;
  if (newQty <= 0) {
    el.qaBuyPreview.innerHTML = "";
    return;
  }
  const totalQty = h.quantity + newQty;
  const newAvg = (h.quantity * h.average_price + newQty * price) / totalQty;
  const newValue = totalQty * h.last_price;
  const view = computeHoldingsView(state.holdings);
  const totalPortfolioValue = view.reduce((sum, r) => sum + r.value, 0)
    - h.quantity * h.last_price + newValue; // swap this symbol's old MTM value for the post-buy one
  const newPct = totalPortfolioValue > 0 ? (newValue / totalPortfolioValue) * 100 : 0;
  el.qaBuyPreview.innerHTML = [
    ["Est. entry price", inr(price)],
    ["New blended avg cost", inr(newAvg)],
    ["New position value", inr(newValue)],
    ["New % of portfolio", `${fmt(newPct)}%`],
  ].map(([label, value]) => `<div class="ac-metric"><label>${label}</label><strong>${value}</strong></div>`).join("");
}

el.qaSellQty.addEventListener("input", renderQaSellPreview);
el.qaBuyQty.addEventListener("input", renderQaBuyPreview);
el.qaBuyEntryPrice.addEventListener("input", renderQaBuyPreview);

el.qaSellSubmit.addEventListener("click", async () => {
  const alert = state.quickActionAlert;
  if (!alert) return;
  el.qaSellError.textContent = "";
  const qty = Number(el.qaSellQty.value);
  if (!(qty > 0)) {
    el.qaSellError.textContent = "Enter a quantity greater than zero.";
    return;
  }
  try {
    const ticket = await API.createAlertActionSell({
      symbol: alert.symbol,
      quantity: qty,
      entry_order_type: el.qaSellOrderType.value,
      entry_price: alert.holding.last_price,
      rationale: el.qaSellRationale.value || alert.message,
      alert_type: alert.type,
    });
    el.quickActionDialog.close();
    openConfirmDialog(ticket);
  } catch (e) {
    el.qaSellError.textContent = e.message;
  }
});

el.qaBuySubmit.addEventListener("click", async () => {
  const alert = state.quickActionAlert;
  if (!alert) return;
  el.qaBuyError.textContent = "";
  const body = {
    symbol: alert.symbol,
    exchange: "NSE",
    transaction_type: "BUY",
    quantity: Number(el.qaBuyQty.value),
    entry_price: Number(el.qaBuyEntryPrice.value),
    entry_order_type: el.qaBuyOrderType.value,
    stop_loss: Number(el.qaBuyStopLoss.value),
    target_1: Number(el.qaBuyTarget1.value),
    target_2: null,
    trailing_enabled: false,
    trailing_trigger: null,
    trailing_distance: null,
    product_type: el.qaBuyProductType.value,
    use_gtt: true,
    rationale: el.qaBuyRationale.value,
  };
  if (!(body.quantity > 0) || !(body.entry_price > 0) || !(body.stop_loss > 0) || !(body.target_1 > 0)) {
    el.qaBuyError.textContent = "Quantity, entry price, stop-loss and target 1 are all required.";
    return;
  }
  try {
    const ticket = await API.createAlertActionBuy(body);
    el.quickActionDialog.close();
    openConfirmDialog(ticket);
  } catch (e) {
    el.qaBuyError.textContent = e.message;
  }
});

el.qaCloseBtn.addEventListener("click", () => {
  // Closing without choosing an action does nothing — no ticket created, no
  // state change. The underlying alert reappears unchanged on next refresh.
  el.quickActionDialog.close();
});

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

const WATCHLIST_COLSPAN = 9;
const UNTAGGED_LABEL = "Untagged";

function watchlistRowHtml(w) {
  const { trend, distancePct } = watchlistDerived(w);
  return `
    <td>${esc(w.symbol)}</td>
    <td class="num">${w.last_price != null ? fmt(w.last_price) : "—"}</td>
    <td class="num ${w.day_change_percentage != null ? pnlClass(w.day_change_percentage) : ""}">${w.day_change_percentage != null ? fmt(w.day_change_percentage) + "%" : "—"}</td>
    <td>${trend ?? "—"}</td>
    <td class="num">${w.rsi_14 != null ? w.rsi_14.toFixed(1) : "—"}</td>
    <td>${distanceLabel(distancePct)}</td>
    <td><input class="wl-tag-input" list="tag-options" maxlength="50" data-id="${w.id}" value="${esc(w.tag ?? "")}" placeholder="—" /></td>
    <td>${esc(w.notes ?? "")}</td>
    <td><button class="row-action-btn danger" data-action="remove-watchlist" data-id="${w.id}">Remove</button></td>
  `;
}

function watchlistRowEl(w) {
  const tr = document.createElement("tr");
  tr.dataset.instrumentToken = w.instrument_token;
  tr.innerHTML = watchlistRowHtml(w);
  return tr;
}

function renderWatchlistTable() {
  if (el.watchlistCount) el.watchlistCount.textContent = `(${state.watchlist.length})`;
  if (!el.watchlistBody) return;
  el.watchlistBody.innerHTML = "";
  if (state.watchlist.length === 0) {
    el.watchlistBody.innerHTML = `<tr class="empty-row"><td colspan="${WATCHLIST_COLSPAN}">Your watchlist is empty</td></tr>`;
    return;
  }

  if (!state.watchlistGroupByTag) {
    for (const w of state.watchlist) el.watchlistBody.appendChild(watchlistRowEl(w));
    return;
  }

  const groups = new Map(); // tag label -> rows, insertion order = display order
  for (const w of state.watchlist) {
    const label = w.tag || UNTAGGED_LABEL;
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(w);
  }
  const labels = [...groups.keys()].sort((a, b) => {
    if (a === UNTAGGED_LABEL) return 1;
    if (b === UNTAGGED_LABEL) return -1;
    return a.localeCompare(b);
  });

  for (const label of labels) {
    const rows = groups.get(label);
    const collapsed = state.watchlistCollapsedGroups.has(label);
    const header = document.createElement("tr");
    header.className = "wl-group-row";
    header.dataset.group = label;
    header.innerHTML = `<td colspan="${WATCHLIST_COLSPAN}"><button type="button" class="wl-group-toggle" data-group="${esc(label)}">
      ${collapsed ? "▸" : "▾"} ${esc(label)} <span class="badge-muted">(${rows.length})</span></button></td>`;
    el.watchlistBody.appendChild(header);
    for (const w of rows) {
      const tr = watchlistRowEl(w);
      tr.hidden = collapsed;
      el.watchlistBody.appendChild(tr);
    }
  }
}

function renderTagOptions() {
  if (!el.tagOptions) return;
  const tags = [...new Set(state.watchlist.map((w) => w.tag).filter(Boolean))].sort((a, b) => a.localeCompare(b));
  el.tagOptions.innerHTML = tags.map((t) => `<option value="${esc(t)}"></option>`).join("");
}

function renderWatchlistNearBreakoutPanel(watchList = []) {
  if (!el.watchlistNearBreakoutBody) return;
  el.watchlistNearBreakoutBody.innerHTML = "";
  if (watchList.length === 0) {
    el.watchlistNearBreakoutBody.innerHTML = `<tr class="empty-row"><td colspan="4">No symbols currently near a breakout</td></tr>`;
    return;
  }
  for (const w of watchList) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${esc(w.symbol)}</td>
      <td class="num">${fmt(w.last_close)}</td>
      <td class="num">${fmt(w.donchian_high_20)}</td>
      <td class="num ${pnlClass(w.distance_pct)}">${fmt(w.distance_pct)}%</td>
    `;
    el.watchlistNearBreakoutBody.appendChild(tr);
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
  const removeBtn = event.target.closest('button[data-action="remove-watchlist"]');
  if (removeBtn) {
    try {
      await API.removeWatchlistSymbol(removeBtn.dataset.id);
      await loadWatchlist();
      toast("Removed from watchlist.", { type: "success" });
    } catch (e) {
      toast(e.message, { type: "error" });
    }
    return;
  }

  const groupToggle = event.target.closest(".wl-group-toggle");
  if (groupToggle) {
    const label = groupToggle.dataset.group;
    if (state.watchlistCollapsedGroups.has(label)) state.watchlistCollapsedGroups.delete(label);
    else state.watchlistCollapsedGroups.add(label);
    renderWatchlistTable();
  }
});

el.watchlistBody?.addEventListener("change", async (event) => {
  const input = event.target.closest(".wl-tag-input");
  if (!input) return;
  const id = input.dataset.id;
  const tag = input.value.trim() || null;
  const row = state.watchlist.find((w) => String(w.id) === String(id));
  if (row && row.tag === tag) return; // no-op — avoid a pointless request/re-render on blur
  try {
    await API.updateWatchlistTag(id, tag);
    await loadWatchlist();
  } catch (e) {
    toast(e.message, { type: "error" });
    if (row) input.value = row.tag ?? ""; // revert the input to the last known-good value
  }
});

el.watchlistGroupToggle?.addEventListener("change", () => {
  state.watchlistGroupByTag = el.watchlistGroupToggle.checked;
  renderWatchlistTable();
  API.saveUiPrefs({ watchlist_group_by_tag: state.watchlistGroupByTag }).catch(() => {});
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

function _btPct(v, digits = 2) {
  return v != null ? `${v >= 0 ? "+" : ""}${v.toFixed(digits)}%` : "—";
}

// PK-21: the execution assumptions and the universe caveat are shown with
// every result, so a number is never read without the model that produced it.
function renderBacktestContext(r) {
  const c = r.execution_config || {};
  const universe = r.universe || {};
  const warn = document.getElementById("bt-universe-warning");
  warn.hidden = !universe.warning;
  warn.textContent = universe.warning ? `⚠ ${universe.warning}` : "";

  const costs = c.costs_enabled ? "costs ON (STT, exchange, SEBI, GST, stamp, DP)" : "costs OFF";
  document.getElementById("bt-execution-summary").textContent =
    `Execution: ${c.entry_model_label || "—"} · slippage ${c.entry_slippage_bps ?? "—"} / ${c.exit_slippage_bps ?? "—"} bps`
    + ` (entry / exit) · ${costs} · capital ${inr(c.initial_capital, 0)} · max ${c.max_open_positions ?? "—"} positions`
    + ` · risk ${c.risk_per_trade_pct ?? "—"}% per trade · same-bar stop+target: stop first`
    + ` · close-based exits and trailing stops act next session`
    + (c.simulate_breakeven ? " · breakeven assumed approved" : "");

  const others = (r.warnings || []).filter((w) => w !== universe.warning);
  const box = document.getElementById("bt-data-warnings");
  box.hidden = others.length === 0;
  box.innerHTML = others.map((w) => `<div>${esc(w)}</div>`).join("");
}

function renderBacktestResults() {
  const r = state.backtestResult;
  if (!r) return;
  el.backtestResults.hidden = false;
  renderBacktestContext(r);
  const m = r.metrics || {};
  const set = (id, text) => { document.getElementById(id).textContent = text; };
  set("bt-r-initial", inr(m.initial_capital, 0));
  set("bt-r-ending", inr(m.ending_equity, 0));
  set("bt-r-gross", `${inr(m.gross_pnl, 0)} / ${_btPct(m.gross_return_pct)}`);
  set("bt-r-costs", inr(m.costs, 0));
  set("bt-r-net", `${inr(m.net_pnl, 0)} / ${_btPct(m.net_return_pct)}`);
  set("bt-r-cagr", m.net_cagr_pct != null ? _btPct(m.net_cagr_pct) : "— (under 1 year)");
  set("bt-r-skipped", `${m.skipped_signals ?? "—"} / ${m.unexecuted_signals ?? "—"}`);
  set("bt-r-avgtrade", `${inr(m.avg_trade, 0)} / ${inr(m.median_trade, 0)}`);
  set("bt-r-largest", `${inr(m.largest_win, 0)} / ${inr(m.largest_loss, 0)}`);
  set("bt-r-exposure", m.exposure_pct != null ? `${m.exposure_pct.toFixed(1)}%` : "—");
  set("bt-r-holding", m.avg_holding_days != null ? m.avg_holding_days.toFixed(1) : "—");
  document.getElementById("bt-r-total").textContent = r.total_signals;
  document.getElementById("bt-r-winrate").textContent = r.win_rate != null ? `${(r.win_rate * 100).toFixed(1)}%` : "—";
  document.getElementById("bt-r-avgr").textContent = r.avg_r_multiple != null ? r.avg_r_multiple.toFixed(2) : "—";
  document.getElementById("bt-r-pf").textContent = r.profit_factor != null ? r.profit_factor.toFixed(2) : "—";
  document.getElementById("bt-r-dd").textContent = r.max_drawdown_pct != null
    ? `${r.max_drawdown_pct.toFixed(2)}% (${inr(r.max_drawdown_amount, 0)})` : "—";
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
      `Monte Carlo (${r.mc_iterations} reshuffles of closed-trade net P&L): median max drawdown `
      + `${r.mc_median_drawdown_pct.toFixed(1)}%, 95th percentile ${r.mc_p95_drawdown_pct.toFixed(1)}% — final equity `
      + `ranges ₹${fmt(r.mc_p5_final_equity, 0)} to ₹${fmt(r.mc_p95_final_equity, 0)} (order-invariant sum; only `
      + `drawdown varies across reshuffles; closed-trade only, so it understates the MTM drawdown above).`;
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
    el.backtestTradesBody.innerHTML = `<tr class="empty-row"><td colspan="13">No trades in this range</td></tr>`;
    return;
  }
  for (const t of r.trades) {
    const tr = document.createElement("tr");
    const flags = [t.gap_fill ? "gap fill" : null, t.data_warning ? "data warning" : null].filter(Boolean);
    tr.innerHTML = `
      <td>${esc(t.symbol)}</td>
      <td>${esc(t.signal_date ?? "—")}</td>
      <td>${esc(t.entry_date)}</td>
      <td class="num">${fmt(t.entry_price)}</td>
      <td class="num">${t.quantity ?? "—"}</td>
      <td>${esc(t.exit_date ?? "—")}</td>
      <td class="num">${t.exit_price != null ? fmt(t.exit_price) : "—"}</td>
      <td>${esc(t.exit_reason ?? "—")}${flags.length ? ` <span class="badge-muted">${esc(flags.join(", "))}</span>` : ""}</td>
      <td class="num">${t.gross_pnl != null ? fmt(t.gross_pnl) : "—"}</td>
      <td class="num">${t.costs != null ? fmt(t.costs) : "—"}</td>
      <td class="num">${t.net_pnl != null ? fmt(t.net_pnl) : "—"}</td>
      <td class="num">${t.r_multiple != null ? t.r_multiple.toFixed(2) : "—"}</td>
      <td>${esc(t.outcome ?? "—")}</td>
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
  const num = (id) => {
    const v = parseFloat(document.getElementById(id).value);
    return Number.isFinite(v) ? v : undefined;
  };
  try {
    state.backtestResult = await API.runBacktest({
      strategy_name: strategyName, start_date: startDate, end_date: endDate, symbols,
      run_walk_forward: runWalkForward, window_days: windowDays, run_monte_carlo: runMonteCarlo,
      initial_capital: num("bt-initial-capital"), max_open_positions: num("bt-max-positions"),
      entry_slippage_bps: num("bt-entry-slippage"), exit_slippage_bps: num("bt-exit-slippage"),
      entry_model: document.getElementById("bt-entry-model").value,
      costs_enabled: document.getElementById("bt-costs-enabled").checked,
      simulate_breakeven: document.getElementById("bt-simulate-breakeven").checked,
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
    renderWatchlistNearBreakoutPanel(data.watch_list);
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

// ---- PK-19: Positions view ----
//
// Every state, quantity, flag and button here comes from the server's
// lifecycle (GET /api/app-positions); lifecycle.js renders it. The LTP is the
// one the page already keeps live from /ws/ticks (holdings / positions) —
// no extra broker call per position.

function positionLtp(token) {
  const holding = (state.holdings || []).find((h) => h.instrument_token === token);
  const position = (state.positions?.net || []).find((p) => p.instrument_token === token);
  return holding?.last_price ?? position?.last_price ?? null;
}

function renderPositionsSummary(summary) {
  if (!summary) {
    el.positionsSummary.innerHTML = "";
    return;
  }
  const chip = (label, n, sev) => `<span class="lc-summary-chip${n && sev ? ` lc-sev-${sev}` : ""}">`
    + `<strong>${n}</strong> ${label}</span>`;
  el.positionsSummary.innerHTML = [
    chip("open", summary.open), chip("waiting to fill", summary.waiting_to_fill, "info"),
    chip("exiting", summary.exiting, "warning"), chip("need attention", summary.needs_attention, "critical"),
    chip("unprotected", summary.unprotected, "critical"),
  ].join("");
}

function renderAppPositions() {
  const { active, closed, summary } = state.appPositions;
  renderPositionsSummary(summary);
  document.getElementById("pos-count-active").textContent = active.length || "";
  document.getElementById("pos-count-closed").textContent = closed.length || "";
  setBadge(el.navBadgePositions, summary?.needs_attention || 0);

  el.positionsActiveBody.innerHTML = active.length ? "" :
    `<tr class="empty-row"><td colspan="13">No active positions opened by this terminal</td></tr>`;
  for (const lc of active) {
    const tr = document.createElement("tr");
    tr.dataset.positionId = lc.position_id;
    tr.dataset.lifecycle = lc.lifecycle_state;
    tr.dataset.positionToken = lc.instrument_token;
    tr.classList.add(`lc-row-${lc.severity}`);
    tr.innerHTML = LC.activeRowCells(lc, positionLtp(lc.instrument_token));
    el.positionsActiveBody.appendChild(tr);
  }
  el.positionsClosedBody.innerHTML = closed.length ? "" :
    `<tr class="empty-row"><td colspan="12">No closed positions yet</td></tr>`;
  for (const lc of closed) {
    const tr = document.createElement("tr");
    tr.dataset.positionId = lc.position_id;
    tr.dataset.lifecycle = lc.lifecycle_state;
    tr.innerHTML = LC.closedRowCells(lc, fmtDateTime);
    el.positionsClosedBody.appendChild(tr);
  }
}

// Ticks move only the LTP and unrealised P&L cells — the rows (and their
// buttons) are not rebuilt under the cursor.
function updatePositionPrices() {
  for (const lc of state.appPositions.active) {
    const tr = el.positionsActiveBody.querySelector(`tr[data-position-id="${lc.position_id}"]`);
    if (!tr) continue;
    const ltp = positionLtp(lc.instrument_token);
    tr.querySelector('[data-field="ltp"]').textContent = fmt(ltp);
    tr.querySelector('[data-field="upnl"]').innerHTML = LC.pnlCell(LC.unrealisedPnl(lc, ltp));
  }
}

async function loadAppPositions() {
  if (state.accountType !== "trading") return;
  try {
    const [active, closed] = await Promise.all([API.appPositions("active"), API.appPositions("closed")]);
    state.appPositions.active = active.data;
    state.appPositions.summary = active.summary;
    state.appPositions.closed = closed.data;
    renderAppPositions();
  } catch (e) {
    // leave prior state visible on a transient load failure
  }
}

document.querySelectorAll(".tab-btn[data-pos-tab]").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.appPositions.tab = btn.dataset.posTab;
    document.querySelectorAll(".tab-btn[data-pos-tab]").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".pos-panel").forEach((p) => {
      p.hidden = p.dataset.posPanel !== btn.dataset.posTab;
    });
  });
});

el.positionsRefreshBtn.addEventListener("click", loadAppPositions);

async function onPositionAction(event) {
  const btn = event.target.closest("button[data-action]");
  if (!btn) return;
  const action = btn.dataset.action;
  try {
    if (action === "position-exit") {
      btn.disabled = true;
      // Creates an exit ticket for the shares actually held; nothing is sent
      // until it is approved in the dialog that opens next.
      const ticket = await API.exitPosition(btn.dataset.positionId);
      await loadAppPositions();
      openConfirmDialog(ticket);
    } else if (action === "position-review-exit") {
      openConfirmDialog(await API.getTicket(btn.dataset.ticketId));
    } else if (action === "position-detail") {
      openDetailDialog(await API.getTicket(btn.dataset.ticketId));
    } else if (action === "position-orders") {
      showSection("orders-trades");
    } else if (action === "paper-cancel") {
      // PK-20: cancels at the paper broker. As for Kite (PK-9), the request
      // is not the outcome — the order monitor resolves the position.
      if (!(await confirmAction({
        title: "Cancel paper order?", message: `Cancel paper order ${btn.dataset.orderId} at the simulated broker? `
          + "It may already have filled; the position updates when the order monitor sees the outcome.",
        okLabel: "Cancel paper order", danger: true,
      }))) return;
      btn.disabled = true;
      await API.cancelOrder(btn.dataset.orderId, "regular");
      toast(`Cancellation requested for paper order ${btn.dataset.orderId}.`, { type: "info" });
      await loadAppPositions();
    } else if (action === "position-journal") {
      showSection("journal");
    } else if (action === "position-reconciliation") {
      showSection("trade-queue");
      document.getElementById("reconciliation-list")?.closest("details")?.scrollIntoView({ behavior: "smooth" });
    }
  } catch (e) {
    toast(e.message, { type: e.errorCode ? "warning" : "error",
      title: action === "position-exit" ? "Exit not created" : "Action failed", timeout: 0 });
    btn.disabled = false;
    loadAppPositions();
  }
}

el.positionsActiveBody.addEventListener("click", onPositionAction);
el.positionsClosedBody.addEventListener("click", onPositionAction);

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
      tag: formData.get("tag") || null,
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
    renderTagOptions();
  } catch (e) {
    // non-critical; leave prior state visible
  }
}

// ---- Phase 3: tickets, orders/trades/GTTs, mode & trailing status ----

const STATUS_LABELS = { pending_confirmation: "awaiting approval", risk_rejected: "risk rejected" };

// PK-2: a broker submission whose outcome is not known. Deliberately unlike
// rejected/cancelled/failed — the order may be live at the broker.
function isSubmissionUnresolved(t) {
  return ["UNKNOWN", "NEEDS_REVIEW"].includes(t.ticket_state?.code);
}

// The ticket / order state (server-derived ticket_state). A ticket payload
// without one (a bare status string, e.g. a plain filter label) falls back
// to its status text.
function statusBadgeHtml(status, ticket = null) {
  if (ticket?.ticket_state) return LC.ticketStateBadge(ticket.ticket_state);
  const label = STATUS_LABELS[status] || String(status).replace(/_/g, " ");
  return `<span class="status-badge ${esc(status)}">${esc(label)}</span>`;
}

// The POSITION a ticket belongs to (an entry's own, an exit's position), as
// a small chip next to the ticket state: "executed" is not "closed" (PK-8).
function positionChipHtml(t) {
  const lc = t.lifecycle;
  if (!lc || !lc.is_position && !lc.is_closed) return "";
  const own = lc.position_id === t.id ? "" : ` #${lc.position_id}`;
  return ` <span class="lc-chip" title="Position${esc(own)}: ${esc(lc.lifecycle_label)}">${LC.lifecycleBadge(lc)}</span>`;
}

function trailingChipHtml(t) {
  if (!t.trailing_enabled) return `<span class="chip">off</span>`;
  return t.trailing_active ? `<span class="chip ok">active</span>` : `<span class="chip warn">armed</span>`;
}

// PK-13: the requested quantity alone is not the position. For a ticket
// whose position is live the server's quantities are shown when they differ
// from the request: "Req 100 · Filled 40 · Open 40" for a position, "Exit 40
// · Filled 15 · Remaining 25" for an exit order still working.
function quantityCellHtml(t) {
  const qty = fmt(t.quantity, 0);
  const lc = t.lifecycle;
  if (!lc || !lc.is_position) return qty;
  if (lc.position_id === t.id) {
    if (lc.filled_quantity === t.quantity && lc.open_quantity === t.quantity) return qty;
    return `${qty}<div class="badge-muted" title="Requested · broker-filled · still held">Req ${esc(lc.requested_quantity)} · Filled ${esc(lc.filled_quantity ?? "?")} · Open ${esc(lc.open_quantity ?? "?")}</div>`;
  }
  const x = lc.active_exit;
  if (x && x.ticket_id === t.id && x.filled_quantity != null && x.filled_quantity !== x.requested_quantity) {
    return `${qty}<div class="badge-muted" title="Exit requested · filled · not sold">Exit ${esc(x.requested_quantity)} · Filled ${esc(x.filled_quantity)} · Remaining ${esc(x.remaining_quantity)}</div>`;
  }
  return qty;
}

function ticketRowHtml(t) {
  const strategyTag = t.source === "strategy" && t.strategy_name
    ? ` <span class="badge-muted">${esc(strategyLabel(t.strategy_name.replace(/_exit$/, "")))}</span>` : "";
  return `
    <td>${esc(t.symbol)}${strategyTag}</td>
    <td>${sideHtml(t.transaction_type)}</td>
    <td class="num">${quantityCellHtml(t)}</td>
    <td class="num">${fmt(t.entry_price)}</td>
    <td class="num">${fmt(t.current_stop_loss ?? t.stop_loss)}</td>
    <td class="num">${fmt(t.target_1)}</td>
    <td>${trailingChipHtml(t)}</td>
    <td>${statusBadgeHtml(t.status, t)}${positionChipHtml(t)}</td>
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
    // PK-19: flagged when the server says the ticket or its position needs
    // attention (UNKNOWN, unprotected, mismatch, partial, review).
    if (t.ticket_state?.tone === "critical" || (t.lifecycle?.is_position
        && (t.lifecycle.review_required || t.lifecycle.entry_partial
            || ["ENTRY_PARTIAL", "EXIT_PARTIAL"].includes(t.lifecycle.lifecycle_state)))) {
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
    // PK-9: an accepted cancel request is not a cancellation — the order may
    // still fill. The linked ticket updates when the order monitor sees it.
    await API.cancelOrder(btn.dataset.orderId, btn.dataset.variety);
    toast(`Cancellation requested for order ${btn.dataset.orderId}. Kite decides the outcome; the ticket `
      + "updates when the order monitor sees it.", { type: "info" });
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

// ---- PK-12: account-level safety (display + explicit requests only) ----

function inr(v) {
  return `₹${Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

async function loadAccountSafety() {
  try {
    state.accountSafety = await API.accountSafetyStatus();
  } catch (e) {
    return;  // keep the last known state; the server still enforces every control
  }
  renderAccountSafety();
}

function renderAccountSafety() {
  const s = state.accountSafety;
  if (!s) return;
  el.accountSafetyBar.hidden = false;
  el.tradingLockPill.hidden = !s.trading_locked;
  el.tradingLockPill.textContent = s.trading_locked
    ? `TRADING LOCKED — no new entries${s.trading_lock_reason ? ` (${s.trading_lock_reason})` : ""}` : "";
  el.tradingLockBtn.textContent = s.trading_locked ? "Unlock trading" : "Lock trading";
  el.breakerPill.hidden = !s.breaker_open;
  el.breakerPill.textContent = s.breaker_open ? "BROKER CIRCUIT BREAKER OPEN — new entries blocked" : "";
  el.breakerPill.title = s.breaker_reason || "";
  el.breakerResetBtn.hidden = !s.breaker_open;

  const lossLimits = [];
  if (s.max_daily_loss_amount != null) lossLimits.push(inr(s.max_daily_loss_amount));
  if (s.max_daily_loss_pct != null) lossLimits.push(`${s.max_daily_loss_pct}% of portfolio`);
  const parts = [
    `Entries today ${s.entries_today}/${s.max_new_entries_per_day ?? "∞"}`,
    `Realized live P&L today ${inr(s.realized_live_pnl_today)}`
      + (lossLimits.length ? ` (loss limit ${lossLimits.join(" or ")}${s.daily_loss_limit_hit ? " — HIT" : ""})`
        : " (daily loss limit NOT SET)"),
    s.max_order_notional != null ? `Max order ${inr(s.max_order_notional)}` : "Max order value NOT SET",
    s.market_open ? "Market open" : "Market closed — entries (live or paper) need explicit AMO",
  ];
  if (!s.breaker_open && s.breaker_recent_failures) parts.push(`Broker failures ${s.breaker_recent_failures}/${s.breaker_threshold ?? "off"}`);
  el.accountSafetySummary.textContent = parts.join(" · ");
  el.accountSafetyBar.classList.toggle("blocked", s.trading_locked || s.breaker_open || s.daily_loss_limit_hit);
}

el.tradingLockBtn.addEventListener("click", async () => {
  const locking = !state.accountSafety?.trading_locked;
  const ok = await confirmAction({
    title: locking ? "Lock trading?" : "Unlock trading?",
    html: locking
      ? `<p>No new entry can be approved — manual, strategy or pending — until trading is unlocked. Exits,
         Emergency Exit, GTT protection and order monitoring keep working.</p>
         <label class="field">Reason<input id="trading-lock-reason" type="text" maxlength="500" placeholder="e.g. broker incident" /></label>`
      : "<p>New entries can be approved again. Every other account-safety limit still applies.</p>",
    okLabel: locking ? "Lock trading" : "Unlock",
    danger: locking,
  });
  if (!ok) return;
  const reason = locking ? (document.getElementById("trading-lock-reason")?.value || "").trim() : "";
  try {
    state.accountSafety = await API.setTradingLock(locking, reason || null);
    renderAccountSafety();
    toast(locking ? "Trading locked — no new entries." : "Trading unlocked.", { type: locking ? "warning" : "success" });
  } catch (e) {
    toast(e.message, { type: "error", title: "Trading lock not changed" });
  }
});

el.breakerResetBtn.addEventListener("click", async () => {
  const ok = await confirmAction({
    title: "Reset the broker circuit breaker?",
    html: `<p>${esc(state.accountSafety?.breaker_reason || "")}</p>
           <p>Only reset once the broker is responding normally. The reset is logged.</p>
           <label class="field">Reason<input id="breaker-reset-reason" type="text" maxlength="500" /></label>`,
    okLabel: "Reset breaker",
  });
  if (!ok) return;
  const reason = (document.getElementById("breaker-reset-reason")?.value || "").trim();
  try {
    state.accountSafety = await API.resetBreaker(reason || null);
    renderAccountSafety();
    toast("Broker circuit breaker reset.", { type: "success" });
  } catch (e) {
    toast(e.message, { type: "error", title: "Breaker not reset" });
  }
});

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
    el.emergencyExitPreviewBody.innerHTML = positions.length
      ? positions.map((p) => `
          <tr>
            <td>${esc(p.symbol)}${["ENTRY_OPEN", "ENTRY_PARTIAL"].includes(p.lifecycle_state) ? ' <span class="badge-muted">order still working — the unfilled rest will be cancelled</span>' : ""}</td>
            <td class="num" title="Sells what is actually held (broker-filled, less any shares already sold)">${fmt(p.open_quantity ?? 0, 0)}${p.open_quantity !== p.quantity ? `<div class="badge-muted">of ${esc(p.quantity)} ordered</div>` : ""}</td>
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
    // PK-8: an exit order placed is not a position closed — the positions
    // stay open (EXITING) until Kite reports each fill.
    const results = result.results || [];
    const submitted = results.filter((r) => r.status === "exit_submitted").length;
    // PK-10: one result per position. Everything this click did NOT submit an
    // exit order for (already exiting, UNKNOWN, refused, failed, entry cancel
    // requested, risk check unavailable) is listed by name — never hidden
    // inside the count, and never called "closed".
    const others = results.filter((r) => r.status !== "exit_submitted");
    toast(`${submitted} exit order(s) placed for ${result.count} position(s). They stay open (EXITING) until `
      + "Kite reports the fills — check Orders & Trades.",
    { type: "warning", title: `Emergency exit: ${submitted} submitted, ${others.length} not exited` });
    if (others.length) {
      toast(others.map((r) => `${r.symbol} [${r.status}]: ${r.message || r.status}`).join(" · "),
        { type: "error", title: `${others.length} position(s) NOT exited by this click — check Kite`, timeout: 0 });
    }
    // SEC-1: an unresolved critical reconciliation finding never blocks an
    // exit, but the affected positions are named.
    const warned = results.filter((r) => r.warning);
    if (warned.length) {
      toast(warned.map((r) => r.warning).join(" · "),
        { type: "warning", title: `${warned.length} position(s) with an unresolved critical finding`, timeout: 0 });
    }
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

function reconciliationQty(value) {
  return value == null ? "—" : esc(String(value));
}

function reconciliationGttRowHtml(m) {
  const issue = (m.issue_code || "").replaceAll("_", " ");
  return `
    <tr title="${esc(m.detail)}">
      <td>${esc(m.symbol || "—")}</td>
      <td><strong>${esc(issue)}</strong><div class="reconciliation-detail">${esc(m.detail)}</div></td>
      <td class="num">${reconciliationQty(m.expected_quantity)}</td>
      <td class="num">${reconciliationQty(m.broker_quantity)}</td>
      <td>${m.gtt_id == null ? "—" : esc(String(m.gtt_id))}</td>
      <td>${esc(m.position_state || "—")}</td>
      <td><span class="chip ${m.severity === "warning" ? "warn" : "live"}">${esc(m.severity || "urgent")}</span></td>
      <td>${esc(m.suggested_action || "CHECK KITE")}</td>
      <td><button class="btn btn-small" data-action="mark-reviewed" data-id="${m.id}">Mark Reviewed</button></td>
    </tr>
  `;
}

function renderReconciliationStatus() {
  const s = state.reconciliationStatus;
  if (!s || !s.checked_at) {
    el.reconciliationStatus.textContent = "";
    return;
  }
  const at = new Date(`${s.checked_at.replace(" ", "T")}Z`).toLocaleTimeString();
  const parts = [];
  if (s.gtt_book === "unavailable") {
    parts.push(`GTT book UNAVAILABLE at ${at} — GTT checks did not run (${s.gtt_error || "unknown error"})`);
  } else if (s.gtt_book === "ok") {
    parts.push(`GTT book checked at ${at}: ${s.active_gtts} active GTT(s)`);
  }
  if (s.holdings === "unavailable") {
    parts.push(`holdings UNAVAILABLE — shortfall check did not run (${s.holdings_error || "unknown error"})`);
  }
  el.reconciliationStatus.textContent = parts.join(" · ");
  el.reconciliationStatus.classList.toggle("unavailable", s.gtt_book === "unavailable" || s.holdings === "unavailable");
}

function renderReconciliationList() {
  const all = state.reconciliationMismatches;
  const gtt = all.filter((m) => RECONCILIATION_GTT_CODES.has(m.issue_code));
  const rest = all.filter((m) => !RECONCILIATION_GTT_CODES.has(m.issue_code));
  el.reconciliationGtt.hidden = gtt.length === 0;
  el.reconciliationGttBody.innerHTML = gtt.map(reconciliationGttRowHtml).join("");
  el.reconciliationList.innerHTML = rest.length
    ? rest.map(reconciliationItemHtml).join("")
    : gtt.length ? "" : `<div class="empty-row">No unresolved mismatches</div>`;
  renderReconciliationStatus();
}

async function loadReconciliationMismatches() {
  try {
    const [rows, status] = await Promise.all([
      API.listMismatches(false),
      API.reconciliationStatus().catch(() => state.reconciliationStatus),
    ]);
    state.reconciliationMismatches = rows;
    state.reconciliationStatus = status;
    renderReconciliationList();
    renderAll();
  } catch (e) {
    // leave prior state visible on a transient load failure
  }
}

// SEC-1: a critical finding blocks live new entries until it is marked
// reviewed, so the review is a logged act with a mandatory reason.
async function markMismatchReviewedWithReason(id) {
  const ok = await confirmAction({
    title: "Mark this finding reviewed?",
    html: `<p>Only after you have checked the position and its GTT in Kite. Reviewing a critical finding
           re-allows live entries. The review and its reason are logged.</p>
           <label class="field">What did you check?<input id="recon-review-reason" type="text" maxlength="500" /></label>`,
    okLabel: "Mark reviewed",
  });
  if (!ok) return;
  const reason = (document.getElementById("recon-review-reason")?.value || "").trim();
  if (!reason) {
    toast("A reason is required — nothing was marked reviewed.", { type: "error" });
    return;
  }
  try {
    await API.markMismatchReviewed(id, reason);
    await loadReconciliationMismatches();
  } catch (e) {
    toast(e.message, { type: "error" });
  }
}

el.reconciliationGtt.addEventListener("click", async (event) => {
  const btn = event.target.closest('button[data-action="mark-reviewed"]');
  if (!btn) return;
  await markMismatchReviewedWithReason(btn.dataset.id);
});

el.reconciliationList.addEventListener("click", async (event) => {
  const btn = event.target.closest('button[data-action="mark-reviewed"]');
  if (!btn) return;
  await markMismatchReviewedWithReason(btn.dataset.id);
});

function renderTrailingIndicator() {
  // PK-19: "a position is still held" is the server lifecycle's call.
  const hasTrailingTickets = state.tickets.some((t) => t.trailing_enabled && t.lifecycle?.is_holding
    && t.lifecycle.position_id === t.id);
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
  updatePositionPrices();

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
  // SEC-1 P1-F: the handshake needs this launch's credential; without it the
  // server refuses the socket, so do not keep retrying — the banner says why.
  if (state.terminalAuthorized === false) return;
  wsIntentionallyClosed = false;
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${protocol}//${location.host}/ws/ticks`);

  ws.onopen = () => {
    console.log("[ws] connected");
    const isReconnect = wsOpenedAt > 0;
    wsOpenedAt = Date.now();
    setFeedStatus(false);
    markStale(false);
    // PK-22F: events sent while the socket was down (or dropped by the
    // server's overflow close, code 1013) are not replayed: re-read state.
    if (isReconnect) {
      scheduleRefresh();
      loadAccountSafety();
    }
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
        sdApplyTick(tick);
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
      loadAccountSafety();  // entries-today / P&L may have moved
    }
    if (msg.type === "account_safety_changed") {
      loadAccountSafety();
    }
    if (msg.type === "mode_changed" && msg.mode !== state.executionMode) {
      // PK-11: another session switched the execution mode. Show the new
      // mode at once and close an open approval of a NEW entry, whose
      // execution note is now wrong. The server's expected_mode check stays
      // the real protection; this only keeps the display honest.
      state.executionMode = msg.mode;
      renderModeBanner();
      const open = state.activeConfirmTicket;
      if (el.ticketConfirmDialog.open && open && open.closes_ticket_id == null) el.ticketConfirmDialog.close();
      scheduleRefresh();
      const own = state.ownModeSwitch === msg.mode;
      state.ownModeSwitch = null;
      if (!own) {
        toast(`Execution mode changed to ${msg.mode === "live" ? "LIVE" : "DRY RUN"} in another session. Review tickets again before approving.`,
          { type: "warning", title: "Execution mode changed", timeout: 0 });
      }
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
  const origin = originChipHtml(t);
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

// The mode approving this ticket executes in. A ticket carries its own mode
// once stamped — an exit inherits the mode of the position it closes, so the
// global toggle must not be shown as its mode. A new entry awaiting approval
// always follows the global toggle: the server claims it in the global mode,
// even if an earlier, refused claim left a stale mode stamped on it (PK-11).
// Mirrors execution.is_closing_sell on the server: a SELL that exits or
// reduces a position. Everything else is a NEW entry (PK-12 applies).
function isClosingSell(t) {
  return t.transaction_type === "SELL" && (t.closes_ticket_id != null || t.source === "alert_action");
}

// PK-12H: a NEW entry while the market is closed needs explicit AMO consent —
// live, and since PK-20 dry run too (a paper order follows the same rule).
function needsAmoConsent(t) {
  return !isClosingSell(t) && computeMarketStatus() !== "open";
}

function ticketExecMode(t) {
  if (t && isNewEntryAwaitingApproval(t)) return state.executionMode;
  return t?.mode || state.executionMode;
}

function isNewEntryAwaitingApproval(t) {
  return t.closes_ticket_id == null && ["draft", "pending_confirmation", "risk_rejected"].includes(t.status);
}

// PK-11: what an approval sends as expected_mode — the mode THIS view showed
// the user, captured before they decided, never re-read at send time. Exits
// send none: they run in their position's own mode.
function shownModeFor(t) {
  return t && t.closes_ticket_id == null ? ticketExecMode(t) : undefined;
}

// SEC-1 P1-D: the review token THIS view showed — captured when the view is
// rendered, never re-read at send time, so a view left open across a
// Re-review (or an edit) in another tab cannot approve what it did not show.
// Strategy entries and exits carry none.
// SEC-1 P1-E: "accepted by the broker" and "filled" are different facts. The
// server decides both (lifecycle.order_progress, from broker-reported fills
// only); this only words them. Nothing is shown before the order is sent.
const ACCEPTANCE_LABELS = {
  SUBMITTING: "sending…", UNKNOWN: "outcome UNKNOWN — do not resubmit", NEEDS_REVIEW: "needs review in Kite",
  ACCEPTED: "accepted", REJECTED: "rejected", NOT_FOUND: "never reached the broker", NOT_RECORDED: "not recorded",
};
const FILL_LABELS = {
  NONE: "nothing filled", UNKNOWN: "unknown", AWAITING_FILL: "awaiting fill", PARTIAL: "partly filled",
  FILLED: "filled", CANCELLED: "cancelled, nothing filled", REJECTED: "rejected by the exchange",
  UNCONFIRMED: "fill not confirmed — check Kite", NOT_RECORDED: "not recorded",
};

function orderProgressRowHtml(p) {
  if (!p || p.acceptance === "NOT_SUBMITTED") return "";
  const qty = p.filled_quantity != null ? ` · ${fmt(p.filled_quantity, 0)}/${fmt(p.requested_quantity, 0)}` : "";
  const working = p.working ? " · order working" : "";
  const legacy = p.fill_evidence === "LEGACY_RECORD" ? " (legacy record)" : "";
  const broker = p.simulated ? "Paper broker" : "Broker";
  return `<div class="row"><span>${broker} / fill</span><strong>${esc(ACCEPTANCE_LABELS[p.acceptance] || p.acceptance)}`
    + ` · ${esc(FILL_LABELS[p.fill] || p.fill)}${qty}${working}${legacy}</strong></div>`;
}

function shownReviewFor(t) {
  return t && t.review && t.review.subject ? (t.review.token || undefined) : undefined;
}

function reviewNoteText(t) {
  const r = t.review;
  const created = `Created ${fmtDateTime(t.created_at)}`;
  if (r.current) return `${created} · reviewed ${fmtDateTime(r.reviewed_at)} — current.`;
  if (r.code === "TICKET_EVIDENCE_MISMATCH") return `${created} — ${r.reason}. It cannot be approved; create a new ticket.`;
  return `${created} — needs a re-review before approval: ${r.reason}. Re-review re-runs the risk checks against `
    + "your current portfolio; check the levels against the market first.";
}

const MODE_BINDING_ERRORS = ["EXECUTION_MODE_CHANGED", "EXECUTION_MODE_REQUIRED"];

async function resyncExecutionMode() {
  try {
    state.executionMode = (await API.getExecutionMode()).mode;
  } catch (e) {
    // keep the last known mode; the server check still protects approvals
  }
  renderModeBanner();
}

// How an approval will be executed — shown before the user commits.
function executionNote(t) {
  const live = ticketExecMode(t) === "live";
  if (!live) {
    // PK-20: a paper order at the simulated broker, never a real one.
    const amo = computeMarketStatus() !== "open" && !isClosingSell(t)
      ? " The market is closed — tick \"Send as AMO\" to queue a paper AMO for the next session." : "";
    return { live, text: "DRY RUN — approving sends a PAPER order to the simulated broker; nothing is sent to Kite. "
      + `It fills only when the observed market price allows.${amo}` };
  }
  const open = computeMarketStatus() === "open";
  if (!open && !isClosingSell(t)) {
    // PK-12H: never an AMO by default — only with the checkbox below.
    return { live, text: "LIVE — the market is closed. Tick \"Send as AMO\" to queue a real order for the next session "
      + "(followed by a protective GTT); without it the server refuses the approval. This uses real money." };
  }
  const how = open
    ? "a regular order"
    : "an After-Market Order (market closed — it queues for the next session)";
  const gtt = t.closes_ticket_id == null && t.use_gtt ? ", followed by a protective GTT" : "";
  return { live, text: `LIVE — approving sends ${how}${gtt}. This uses real money.` };
}

function riskCheckRowHtml(c, { withLinks = false } = {}) {
  const icon = c.skipped ? "⚠" : c.passed ? "✓" : "✗";
  const cls = c.skipped ? "skipped" : c.passed ? "passed" : "failed";
  // An unmapped sector blocks a new entry (PK-14, SECTOR_MAPPING_REQUIRED;
  // older stored results show it as skipped). The detail panel points at
  // the screen where it is fixed.
  const unmapped = c.rule === "max_sector_exposure_pct" && (c.skipped || c.code === "SECTOR_MAPPING_REQUIRED");
  const fix = withLinks && unmapped
    ? ` <a class="sd-fix-link" href="#settings" data-sd-close="1">Settings → Sector mappings</a>` : "";
  let value;
  if (c.code === "SECTOR_MAPPING_REQUIRED") {
    value = "Sector: UNMAPPED — BLOCKED, mapping required";
  } else if (c.skipped || c.actual_value === null || c.actual_value === undefined) {
    // Non-numeric checks (e.g. an exit ticket's "position validated"
    // check) have no actual_value/limit_value to format — show the
    // message directly, same as the skipped-check path.
    value = c.message;
  } else if (c.unit === "ratio") {
    value = `1:${c.actual_value.toFixed(2)} (min 1:${c.limit_value})`;
  } else if (c.unit === "count") {
    value = `${c.actual_value} of ${c.limit_value} max`;
  } else if (c.current_value !== null && c.current_value !== undefined) {
    value = `current ${c.current_value.toFixed(1)}% → ${c.actual_value.toFixed(1)}% (max ${c.limit_value}%)`;
  } else {
    value = `${c.actual_value.toFixed(1)}% (max ${c.limit_value}%)`;
  }
  const shown = c.skipped && withLinks ? `skipped — ${value}` : value;
  return `<div class="risk-check-row ${cls}"><span class="icon">${icon}</span><span>${esc(c.label)}</span><strong>${esc(shown)}${fix}</strong></div>`;
}

// Extracted so the Signal Detail panel's RISK CHECKS section renders through
// the exact same rows as the confirm dialog — expanding the "6/6" badge into
// named checks must never become a second, subtly different renderer.
function riskCheckSectionHtml(checks, heading = "Risk checks", options = {}) {
  if (!checks || checks.length === 0) return "";
  const passed = checks.filter((c) => c.passed).length;
  return `<h3>${esc(heading)} · ${passed}/${checks.length} passed</h3>`
    + checks.map((c) => riskCheckRowHtml(c, options)).join("");
}

function renderRiskCheckSection(checks) {
  el.ticketRiskCheckBody.innerHTML = riskCheckSectionHtml(checks);
}

function openConfirmDialog(ticket) {
  activeConfirmTicketId = ticket.id;
  state.activeConfirmTicket = ticket;
  state.activeConfirmShownMode = shownModeFor(ticket);  // what the exec note below shows
  state.activeConfirmShownReview = shownReviewFor(ticket);  // P1-D: the review this dialog shows
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
  // PK-12H: always starts unticked — consent is given per approval.
  el.ticketConfirmAmo.hidden = rejectedMode || !needsAmoConsent(ticket);
  el.ticketConfirmAmoCheckbox.checked = false;
  renderRiskCheckSection(ticket.risk_check_results);
  // SEC-1 P1-D: a manual / Quick Action buy whose review is no longer current
  // offers Re-review instead of Approve (the server would refuse it anyway).
  const review = !rejectedMode && ticket.review && ticket.review.subject ? ticket.review : null;
  const needsReReview = !!review && !review.current && review.code !== "TICKET_EVIDENCE_MISMATCH";
  el.ticketConfirmReview.hidden = !review;
  el.ticketConfirmReview.className = `review-note${review && !review.current ? " stale" : ""}`;
  el.ticketConfirmReview.textContent = review ? reviewNoteText(ticket) : "";
  el.ticketRejectBtn.hidden = rejectedMode;
  el.ticketConfirmBtn.hidden = rejectedMode || (!!review && !review.current);
  el.ticketReReviewBtn.hidden = !needsReReview;
  el.ticketReReviewBtn.disabled = false;
  el.ticketModifyBtn.hidden = !rejectedMode;
  el.ticketArchiveBtn.hidden = !rejectedMode;
  el.ticketConfirmDialog.showModal();
}

// Shared by the confirm dialog and the Approvals inbox. Execution failures
// come back as data on the ticket (not thrown); a thrown error means the
// request itself failed (network/auth/state).
const ACCOUNT_SAFETY_TITLES = {
  TRADING_LOCKED: "Trading is locked",
  DAILY_LOSS_LIMIT: "Daily loss limit reached",
  DAILY_ENTRY_LIMIT: "Daily entry limit reached",
  MAX_NOTIONAL_EXCEEDED: "Order value above the absolute cap",
  STALE_SIGNAL: "Signal is stale",
  PRICE_DEVIATION_EXCEEDED: "Price moved too far from the plan",
  STOP_ALREADY_INVALID: "Stop already crossed",
  BROKER_CIRCUIT_OPEN: "Broker circuit breaker is open",
  AMO_CONFIRMATION_REQUIRED: "Market closed — AMO needs your explicit consent",
  AMO_DISABLED: "Market closed — AMO disabled",
  ACCOUNT_SAFETY_UNAVAILABLE: "Account safety could not be checked",
  // SEC-1 P1-D
  TICKET_REVIEW_REQUIRED: "This ticket needs a re-review",
  TICKET_REVIEW_CHANGED: "This ticket changed since you looked at it",
  TICKET_EVIDENCE_MISMATCH: "Ticket details were changed outside the app",
};

// The codes of every reason the server gave, not only the first: a review
// refusal may be listed after, say, TRADING_LOCKED.
function failureCodes(e) {
  return [e.errorCode, ...(e.failures || []).map((f) => f.code)];
}

// allowAmo is true only when the user ticked "Send as AMO" in the dialog.
// reviewToken is the P1-D review token the approving view showed.
async function approveTicket(id, expectedMode, allowAmo = false, reviewToken = undefined) {
  try {
    reportExecution(await API.confirmTicket(id, expectedMode, allowAmo, reviewToken));
  } catch (e) {
    if (e instanceof ApiError && MODE_BINDING_ERRORS.includes(e.errorCode)) {
      // PK-11: the mode shown is not the server's mode. Nothing was
      // executed; show the current mode and make the user review again —
      // never retry, never approve in the new mode automatically.
      await resyncExecutionMode();
      toast(e.message, { type: "warning", title: "Execution mode changed. Review the ticket again before approving.", timeout: 0 });
    } else if (e instanceof ApiError && ACCOUNT_SAFETY_TITLES[e.errorCode]) {
      // PK-12: refused by the account safety layer — nothing was sent. Show
      // every reason; never retry. An AMO refusal reopens the dialog, where
      // the user can tick "Send as AMO" themselves.
      const reasons = (e.failures || [{ code: e.errorCode, message: e.message }]).map((f) => f.message).join("\n");
      toast(reasons, { type: "warning", title: `${ACCOUNT_SAFETY_TITLES[e.errorCode]} — nothing was sent`, timeout: 0 });
      // P1-D: a stale review reopens the dialog in its Re-review state; a
      // changed one reopens it on the current ticket. Never retried.
      const codes = failureCodes(e);
      const reopen = (codes.includes("AMO_CONFIRMATION_REQUIRED") && !allowAmo)
        || codes.includes("TICKET_REVIEW_REQUIRED") || codes.includes("TICKET_REVIEW_CHANGED");
      if (reopen) {
        try {
          openConfirmDialog(await API.getTicket(id));
        } catch (ignored) { /* the toast already explains */ }
      }
    } else {
      toast(e.message, { type: "error", title: "Approval failed" });
    }
  }
  loadAccountSafety();
  await refreshTicketViews();
  refreshOrderBooks();
}

function reportExecution(t) {
  const what = `${t.transaction_type} ${fmt(t.quantity, 0)} ${t.symbol}`;
  if (isSubmissionUnresolved(t)) {
    toast(t.execution_message, { type: "warning", title: `${what} — outcome UNKNOWN, do not resubmit`, timeout: 0 });
  } else if (!t.success) {
    toast(t.execution_message, { type: "error", title: `${what} — not executed` });
  } else if (/UNPROTECTED|FAILED/.test(t.execution_message || "")) {
    toast(t.execution_message, { type: "warning", title: `${what} — placed, check protection`, timeout: 0 });
  } else if (t.mode === "dry_run") {
    // PK-20: a PAPER order at the simulated broker — it fills (or not) only
    // when the observed market price allows; the position follows.
    toast(`PAPER order ${t.kite_order_id || ""} placed at the simulated broker — no real order was sent. It fills `
      + "when the observed market price allows.", { type: "success", title: `${what} (dry run)` });
  } else if (t.ticket_state?.code === "EXITING") {
    toast(t.order_variety === "amo"
      ? "Exit sent as AMO — it executes when the market opens. The position stays open, with its protective GTT, until Kite reports the fill."
      : "Exit placed — the position stays open, with its protective GTT, until Kite reports the fill.",
    { type: "success", title: `${what} — EXITING` });
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
  // Read before the dialog closes; only a visible, ticked box is consent.
  const allowAmo = !el.ticketConfirmAmo.hidden && el.ticketConfirmAmoCheckbox.checked;
  const id = activeConfirmTicketId;
  el.ticketConfirmDialog.close();
  try {
    await approveTicket(id, state.activeConfirmShownMode, allowAmo, state.activeConfirmShownReview);
  } finally {
    el.ticketConfirmBtn.disabled = false;
  }
});

// SEC-1 P1-D: the explicit re-review of a manual / Quick Action buy whose
// review is no longer current. Nothing is sent to the broker; the dialog
// reopens on the re-reviewed ticket, and approving it is a separate click.
el.ticketReReviewBtn.addEventListener("click", async () => {
  if (activeConfirmTicketId == null || el.ticketReReviewBtn.disabled) return;
  const id = activeConfirmTicketId;
  el.ticketReReviewBtn.disabled = true;
  try {
    const t = await API.reReviewTicket(id);
    if (t.status === "pending_confirmation") {
      toast("Re-reviewed: risk checks re-run against your current portfolio. Check the levels, then approve.",
        { type: "success" });
    } else {
      toast("Re-reviewed — but it is now blocked by risk checks.", { type: "warning" });
    }
    if (el.ticketConfirmDialog.open) el.ticketConfirmDialog.close();
    openConfirmDialog(t);
  } catch (e) {
    // Tampered details reject the ticket; a state conflict means it moved on
    // (approved or rejected elsewhere). Either way this dialog is stale.
    const gone = e instanceof ApiError && ["TICKET_EVIDENCE_MISMATCH", "CONFLICT_INVALID_STATE"].includes(e.errorCode);
    if (gone) el.ticketConfirmDialog.close();
    toast(e.message, { type: "error", timeout: 0,
      title: gone ? "Re-review refused" : "Re-review failed — the ticket is unchanged" });
  } finally {
    el.ticketReReviewBtn.disabled = false;
  }
  await refreshTicketViews();
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

// PK-19: the position a ticket belongs to, as the server describes it.
function positionDetailHtml(t) {
  const lc = t.lifecycle;
  if (!lc) return "";
  const which = lc.position_id === t.id ? "" : ` <span class="badge-muted">position #${esc(lc.position_id)}</span>`;
  const rows = [`<div class="row"><span>Position</span><strong>${LC.lifecycleBadge(lc)}${which}</strong></div>`];
  if (lc.is_position || lc.is_closed) {
    rows.push(`<div class="row"><span>Held</span><strong>${LC.quantityCell(lc)}</strong></div>`);
    rows.push(`<div class="row"><span>Protection</span><strong>${LC.protectionBadge(lc)}</strong></div>`);
  }
  if (lc.active_exit || lc.pending_exit_ticket_id != null) {
    rows.push(`<div class="row"><span>Exit</span><strong>${LC.activeExitCell(lc)}</strong></div>`);
  }
  if (lc.review_required) {
    rows.push(`<div class="row"><span>Review</span><strong>${LC.reviewCell(lc)}</strong></div>`);
  }
  return rows.join("");
}

async function openDetailDialog(ticket) {
  state.activeDetailTicketId = ticket.id;
  state.activeDetailTicket = ticket;
  el.ticketEditForm.hidden = true;
  el.ticketDetailBody.hidden = false;
  el.ticketEditError.textContent = "";

  // PK-19: the ticket's own state and its POSITION's lifecycle are separate
  // concepts — an executed exit ticket does not mean the position is closed.
  const statusRow = `<div class="row"><span>Ticket</span><strong>${statusBadgeHtml(ticket.status, ticket)}</strong></div>`
    + positionDetailHtml(ticket);
  const modeRow = `<div class="row"><span>Mode</span><strong>${modeChipHtml(ticket.mode)}${ticket.order_variety === "amo" ? ' <span class="chip">AMO</span>' : ""}</strong></div>`;
  // PK-20: a dry-run ticket's ids are the paper broker's, never Kite's.
  const isPaper = (id) => typeof id === "string" && id.startsWith("PAPER-");
  const orderRow = ticket.kite_order_id
    ? `<div class="row"><span>${isPaper(ticket.kite_order_id) ? "Paper Order ID" : "Kite Order ID"}</span>`
      + `<strong>${esc(ticket.kite_order_id)}</strong></div>` : "";
  const gttRow = ticket.kite_gtt_id
    ? `<div class="row"><span>${isPaper(ticket.kite_gtt_id) ? "Paper GTT ID" : "Kite GTT ID"}</span>`
      + `<strong>${esc(ticket.kite_gtt_id)}</strong></div>` : "";
  const tagRow = ticket.order_tag
    ? `<div class="row"><span>Kite order tag</span><strong>${esc(ticket.order_tag)}</strong></div>` : "";
  el.ticketDetailBody.innerHTML = statusRow + modeRow + orderProgressRowHtml(ticket.order_progress) + orderRow
    + gttRow + tagRow + ticketConfirmHtml(ticket);
  renderTicketDetailEvents([]); // populated below once we fetch full detail

  const isDraft = ticket.status === "draft";
  const isSoftDeletable = ["rejected", "cancelled", "failed"].includes(ticket.status);
  // Exit / reduction tickets are never editable (the server refuses too).
  const isClosing = ticket.closes_ticket_id != null
    || (ticket.transaction_type === "SELL" && ticket.source === "alert_action");
  el.ticketDetailEditBtn.hidden = !isDraft || isClosing;
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
    // P1-D: reverting is not a review — a manual ticket from an earlier
    // session window or Kite login still needs a Re-review before approval.
    const needsReReview = reverted.review?.subject && !reverted.review.current;
    toast(
      reverted.status === "risk_rejected"
        ? "Reverted — but it's currently blocked by risk checks again."
        : needsReReview
          ? `Reverted — it still needs a re-review before approval (${reverted.review.reason}).`
          : "Reverted — back in the queue, awaiting approval.",
      { type: reverted.status === "risk_rejected" || needsReReview ? "warning" : "success" },
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
    await linkDmbTicket(ticket);
    clearDmbHandoff();
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
    state.ownModeSwitch = "dry_run";  // this tab's own switch: its broadcast is not "another session"
    API.setExecutionMode("dry_run", false).then((r) => {
      state.executionMode = r.mode;
      renderModeBanner();
      toast("Switched to DRY RUN — approvals are simulated.", { type: "info" });
    }).catch((e) => {
      el.modeToggleCheckbox.checked = true;
      toast(e.message, { type: "error" });
    }).finally(() => {
      state.ownModeSwitch = null;
    });
  }
});

el.liveModeCancelBtn.addEventListener("click", () => {
  el.modeToggleCheckbox.checked = false;
  el.liveModeConfirmDialog.close();
});

el.liveModeConfirmBtn.addEventListener("click", async () => {
  try {
    state.ownModeSwitch = "live";  // this tab's own switch: its broadcast is not "another session"
    const r = await API.setExecutionMode("live", true);
    state.executionMode = r.mode;
    toast("LIVE trading enabled — approvals now place real orders.", { type: "warning" });
  } catch (e) {
    el.modeToggleCheckbox.checked = false;
    // SEC-1: LIVE is armed only after Live Readiness passes with its broker
    // check; a new Kite login disarms it again.
    toast(e.message, e.errorCode === "LIVE_READINESS_NOT_MET"
      ? { type: "error", title: "LIVE not enabled — see Live Readiness", timeout: 0 }
      : { type: "error" });
  }
  state.ownModeSwitch = null;
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
  loadAccountSafety();
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
  // PK-12: empty = disabled (null) — except that since LIVE-0 a live new
  // entry is refused while Max Order Value or Max Daily Loss (₹) is empty.
  for (const name of ACCOUNT_SAFETY_FIELDS) {
    form.querySelector(`[name="${name}"]`).value = s[name] ?? "";
  }
}

const ACCOUNT_SAFETY_FIELDS = [
  "max_order_notional", "max_new_entries_per_day", "max_entry_price_deviation_pct",
  "max_daily_loss_amount", "max_daily_loss_pct", "broker_breaker_threshold", "broker_breaker_window_minutes",
];

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
    el.unmappedSymbolsList.innerHTML = `<span class="empty">None — every held or watched symbol has a sector.</span>`;
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
  // PK-12: sent explicitly every time; an empty field disables that limit
  // (for Max Order Value / Max Daily Loss ₹ that also blocks live entries).
  for (const name of ACCOUNT_SAFETY_FIELDS) {
    const raw = String(formData.get(name) ?? "").trim();
    body[name] = raw === "" ? null : Number(raw);
  }
  try {
    state.riskSettings = await API.saveRiskSettings(body);
    renderRiskSettingsForm();
    loadAccountSafety();
    toast("Risk settings saved — every new ticket is checked against them.", { type: "success" });
    el.riskSettingsSaved.hidden = false;
    setTimeout(() => { el.riskSettingsSaved.hidden = true; }, 2000);
  } catch (e) {
    el.riskSettingsError.textContent = e.message;
  }
});

// LIVE-0: the FIRST LIVE / CAUTIOUS profile only FILLS the form after an
// explicit confirmation — nothing is saved until the user presses Save. It
// never invents a rupee amount: Max Order Value and Max Daily Loss (₹) keep
// whatever the user has (or must type) and become required; the other
// exposure limits stay as they are.
document.getElementById("first-live-profile-btn").addEventListener("click", async () => {
  const ok = await confirmAction({
    title: "Fill the FIRST LIVE / CAUTIOUS profile?",
    message: "This sets Max New Entries / Day to 1 in the form and makes Max Order Value and Max Daily Loss (₹) " +
      "required. It does not choose any rupee amount for you and saves nothing — review every field, then Save. " +
      "Keep the trading lock ON until Live Readiness shows READY.",
    okLabel: "Fill the form",
  });
  if (!ok) return;
  const form = el.riskSettingsForm;
  form.querySelector('[name="max_new_entries_per_day"]').value = 1;
  for (const name of ["max_order_notional", "max_daily_loss_amount"]) {
    form.querySelector(`[name="${name}"]`).required = true;
  }
  document.getElementById("first-live-profile-note").hidden = false;
  const notional = form.querySelector('[name="max_order_notional"]');
  (notional.value === "" ? notional : form.querySelector('[name="max_daily_loss_amount"]')).focus();
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
  { id: "positions", el: document.getElementById("view-positions") },
  { id: "ticket-history", el: document.getElementById("view-ticket-history") },
  { id: "orders-trades", el: document.getElementById("view-orders-trades") },
  { id: "watchlist", el: document.getElementById("view-watchlist") },
  { id: "volume-research", el: document.getElementById("view-volume-research") },
  { id: "dmb-inbox", el: document.getElementById("view-dmb-inbox") },
  { id: "strategies", el: document.getElementById("view-strategies") },
  { id: "todays-actions", el: document.getElementById("view-todays-actions") },
  { id: "journal", el: document.getElementById("view-journal") },
  { id: "settings", el: document.getElementById("view-settings") },
  { id: "diagnostics", el: document.getElementById("view-diagnostics") },
  { id: "live-readiness", el: document.getElementById("view-live-readiness") },
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
  if (id === "positions") loadAppPositions();
  if (id === "diagnostics") window.KTDiagnostics.load(document.getElementById("diagnostics-root"));
  if (id === "live-readiness") window.KTReadiness.load(document.getElementById("live-readiness-root"));
  if (id === "volume-research") window.KTResearch.load(document.getElementById("volume-research-root"));
  if (id === "dmb-inbox") window.KTDmbInbox.load(document.getElementById("dmb-inbox-root"));
  window.scrollTo({ top: 0 });
}

// PK-26: Price-Volume Research — reload re-reads the batch; the detail dialog closes.
document.getElementById("vr-reload-btn").addEventListener("click", () => {
  window.KTResearch.load(document.getElementById("volume-research-root"));
});
document.getElementById("vr-detail-close-btn").addEventListener("click", () => {
  document.getElementById("vr-detail-dialog").close();
});

// DMB Research Inbox (frontend/dmb_inbox.js). Start ticket opens THIS form with
// only the symbol, the exchange and a research rationale; direction, order type
// and product start unselected (a placeholder the browser's `required` refuses),
// quantity auto-fill is off, and every price is typed by the user. The ticket is
// created by the unchanged POST /api/tickets path (risk checks, P1-D review); only
// after that is it linked back to the research item.
const DMB_PLACEHOLDER_FIELDS = ["transaction_type", "entry_order_type", "product_type"];

function clearDmbHandoff() {
  state.dmbHandoff = null;
  document.getElementById("dmb-handoff-banner").hidden = true;
  for (const name of DMB_PLACEHOLDER_FIELDS) {
    const select = el.ticketForm.querySelector(`[name="${name}"]`);
    select.querySelectorAll("option[data-dmb-placeholder]").forEach((o) => o.remove());
    select.selectedIndex = 0;
  }
  state.quantityManuallyEdited = false;
}

async function openTicketFromDmb(start, itemId) {
  showSection("new-ticket");
  el.ticketForm.reset();
  clearDmbHandoff();
  el.ticketTrailingTrigger.disabled = true;
  el.ticketTrailingDistance.disabled = true;
  for (const name of DMB_PLACEHOLDER_FIELDS) {
    const select = el.ticketForm.querySelector(`[name="${name}"]`);
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "— choose —";
    placeholder.dataset.dmbPlaceholder = "1";
    select.insertBefore(placeholder, select.firstChild);
    select.value = "";
  }
  el.ticketSymbolInput.value = start.prefill.symbol;
  el.ticketForm.querySelector('[name="rationale"]').value = start.prefill.rationale;
  document.getElementById("dmb-handoff-exchange").textContent = start.prefill.exchange;
  document.getElementById("dmb-handoff-banner").hidden = false;
  state.dmbHandoff = { itemId, startEventId: start.start_event_id, nonce: start.handoff_nonce,
                       symbol: start.prefill.symbol };
  state.quantityManuallyEdited = true;   // no position-size auto-fill: the user enters the quantity
  state.ticketDraftDirty = true;
  try {
    const matches = await API.searchInstruments(start.prefill.symbol);
    state.lastSymbolMatches = matches;
    el.symbolOptions.innerHTML = matches
      .map((m) => `<option value="${m.tradingsymbol}">${m.name ?? ""}</option>`)
      .join("");
  } catch (e) {
    // the user can retype the symbol to re-trigger the search
  }
  renderTicketPreview();
  el.ticketForm.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function linkDmbTicket(ticket) {
  const h = state.dmbHandoff;
  if (!h || ticket.symbol !== h.symbol) return;
  try {
    await fetchJsonMutate(`/api/dmb-inbox/items/${h.itemId}/ticket-link`, "POST",
      { ticket_id: ticket.id, start_event_id: h.startEventId, handoff_nonce: h.nonce });
  } catch (e) {
    toast(`The ticket was created, but linking it to the research item failed: ${e.message}`, { type: "warning" });
  }
}

window.KTDmbInbox.configure({ mutate: fetchJsonMutate, toast, startTicket: openTicketFromDmb });
document.getElementById("dmb-reload-btn").addEventListener("click", () => {
  window.KTDmbInbox.load(document.getElementById("dmb-inbox-root"));
});
document.getElementById("dmb-handoff-cancel-btn").addEventListener("click", clearDmbHandoff);

// PK-22H: re-check forces a fresh database integrity check.
document.getElementById("diagnostics-refresh-btn").addEventListener("click", () => {
  window.KTDiagnostics.load(document.getElementById("diagnostics-root"), { refresh: true });
});

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
    "trade-queue", "todays-actions", "journal", "positions", "volume-research", "dmb-inbox",
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

// ---- Phase 6/9: configurable panel grids (Dashboard, and Watchlist reusing
// the exact same mechanism — see createPanelController) ----
//
// Extension point for a future panel type: add its static
// `.panel-wrapper[data-panel-type="..."]` markup block inside the relevant
// grid element in index.html, write whatever function renders its content,
// add a PANEL_LABELS entry (used by that screen's "Add Panel" dropdown), and
// insert one dashboard_panels DB row (scoped to the right `screen`). Nothing
// in createPanelController below needs to change for that — it operates
// purely on panel_type strings and generic wrapper markup, scoped to
// whichever grid element it was constructed with.
const PANEL_LABELS = {
  portfolio_summary: "Portfolio Summary",
  holdings_table: "Holdings",
  positions_table: "Positions",
  mf_table: "Mutual Funds",
  watchlist_summary: "Watchlist", // first new panel_type added since the extension point was built — see renderWatchlistSummaryPanel()
  todays_actions: "Today's Actions", // Phase 6 — same zero-infra-change extension point, see renderTodaysActionsPanel()
  watchlist_all: "Watchlist", // Phase 9 — Watchlist screen's own panel grid, see renderWatchlistTable()
  watchlist_near_breakout: "Near Breakout", // Phase 9 — see renderWatchlistNearBreakoutPanel()
};

// One controller per screen (Dashboard, Watchlist) sharing identical
// drag/resize/hide/add/persist logic — only the DOM elements, the `screen`
// scoping param sent to the API, and the bit of `state` it reads/writes
// differ. This is the reuse the Watchlist panel grid was built on top of,
// rather than a second copy of the Dashboard's panel code.
function createPanelController({ screen, gridEl, emptyNoteEl, customizeToggleEl, addPanelControlEl, addPanelSelectEl, getPanels, setPanels }) {
  let sortableInstance = null;

  function panelWrapperEl(panelType) {
    return gridEl.querySelector(`.panel-wrapper[data-panel-type="${panelType}"]`);
  }

  function render() {
    const panels = getPanels();
    const customizeMode = customizeToggleEl.checked;
    const visiblePanels = panels.filter((p) => p.visible).sort((a, b) => a.display_order - b.display_order);

    emptyNoteEl.hidden = visiblePanels.length > 0;
    gridEl.hidden = visiblePanels.length === 0;

    for (const panel of visiblePanels) {
      const wrapper = panelWrapperEl(panel.panel_type);
      if (!wrapper) continue;
      wrapper.hidden = false;
      wrapper.className = `panel-wrapper panel-${panel.size}`;
      wrapper.dataset.panelType = panel.panel_type;
      gridEl.appendChild(wrapper); // moves node into correct order; preserves listeners/content

      const header = wrapper.querySelector(".panel-header");
      header.hidden = !customizeMode;
      header.querySelectorAll(".panel-size-btn").forEach((btn) => {
        btn.classList.toggle("active-size", btn.dataset.size === panel.size);
      });
    }

    for (const panelType of Object.keys(PANEL_LABELS)) {
      const cfg = panels.find((p) => p.panel_type === panelType);
      if (!cfg || !cfg.visible) {
        const wrapper = panelWrapperEl(panelType);
        if (wrapper) wrapper.hidden = true;
      }
    }

    renderAddControl();
  }

  function renderAddControl() {
    const panels = getPanels();
    const hiddenPanels = panels.filter((p) => !p.visible);
    addPanelControlEl.hidden = !customizeToggleEl.checked || hiddenPanels.length === 0;
    addPanelSelectEl.innerHTML = `<option value="">Add Panel…</option>` +
      hiddenPanels.map((p) => `<option value="${p.panel_type}">${PANEL_LABELS[p.panel_type] || p.panel_type}</option>`).join("");
  }

  async function save() {
    try {
      setPanels(await API.saveDashboardPanels(getPanels(), screen));
    } catch (e) {
      // keep local state as the source of truth for this session; will
      // resync with the server on next load if the save silently failed
    }
  }

  function initSortable() {
    if (typeof Sortable === "undefined" || sortableInstance) return;
    sortableInstance = Sortable.create(gridEl, {
      handle: ".panel-drag-handle",
      animation: 150,
      onEnd: () => {
        const order = [...gridEl.querySelectorAll(".panel-wrapper")].map((w) => w.dataset.panelType);
        const panels = getPanels();
        order.forEach((panelType, idx) => {
          const cfg = panels.find((p) => p.panel_type === panelType);
          if (cfg) cfg.display_order = idx;
        });
        save();
      },
    });
  }

  function destroySortable() {
    sortableInstance?.destroy();
    sortableInstance = null;
  }

  customizeToggleEl.addEventListener("change", () => {
    gridEl.classList.toggle("customize-mode", customizeToggleEl.checked);
    render();
    if (customizeToggleEl.checked) initSortable(); else destroySortable();
  });

  gridEl.addEventListener("click", (event) => {
    const wrapper = event.target.closest(".panel-wrapper");
    if (!wrapper) return;
    const cfg = getPanels().find((p) => p.panel_type === wrapper.dataset.panelType);
    if (!cfg) return;

    const sizeBtn = event.target.closest(".panel-size-btn");
    const hideBtn = event.target.closest(".panel-hide-btn");
    if (sizeBtn) {
      cfg.size = sizeBtn.dataset.size;
      render();
      save();
    } else if (hideBtn) {
      cfg.visible = false;
      render();
      save();
    }
  });

  addPanelSelectEl.addEventListener("change", (event) => {
    const panelType = event.target.value;
    if (!panelType) return;
    const panels = getPanels();
    const cfg = panels.find((p) => p.panel_type === panelType);
    if (cfg) {
      const maxOrder = Math.max(-1, ...panels.map((p) => p.display_order));
      cfg.visible = true;
      cfg.display_order = maxOrder + 1;
    }
    event.target.value = "";
    render();
    save();
  });

  async function load() {
    try {
      setPanels(await API.getDashboardPanels(screen));
      render();
    } catch (e) {
      // leave the grid in its default static-HTML order/visibility on failure
    }
  }

  return { load, render };
}

const dashboardPanelController = createPanelController({
  screen: "dashboard",
  gridEl: el.dashboardPanelsGrid,
  emptyNoteEl: el.dashboardEmptyNote,
  customizeToggleEl: el.customizeModeToggle,
  addPanelControlEl: el.addPanelControl,
  addPanelSelectEl: el.addPanelSelect,
  getPanels: () => state.dashboardPanels,
  setPanels: (panels) => { state.dashboardPanels = panels; },
});

const watchlistPanelController = createPanelController({
  screen: "watchlist",
  gridEl: el.watchlistPanelsGrid,
  emptyNoteEl: el.watchlistEmptyNote,
  customizeToggleEl: el.watchlistCustomizeModeToggle,
  addPanelControlEl: el.watchlistAddPanelControl,
  addPanelSelectEl: el.watchlistAddPanelSelect,
  getPanels: () => state.watchlistPanels,
  setPanels: (panels) => { state.watchlistPanels = panels; },
});

async function loadDashboardPanels() {
  await dashboardPanelController.load();
}

async function loadWatchlistPanels() {
  await watchlistPanelController.load();
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
    loadApprovals(), loadAutomationStatus(), loadAppPositions(),
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
  // The Signal Detail view reads its ticket and its ←/→ queue out of
  // state.approvals, so this is the single point at which it learns that a
  // tickets_changed broadcast has changed either (D8).
  sdOnTicketsChanged();
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
  // Chart links on entry signals only: the Signal Detail view answers "do I
  // trust this entry enough to place it", which is not the question an exit
  // signal or a stop adjustment poses.
  const chartLinks = isExit ? "" : chartLinksHtml(t);
  const actions = kind === "blocked"
    ? `${chartLinks}
       <button class="btn btn-ghost btn-small" data-action="details">Why blocked?</button>
       <button class="btn btn-ghost btn-small" data-action="archive">Archive</button>`
    : `${chartLinks}
       <button class="btn btn-ghost btn-small" data-action="details">Details</button>
       <button class="btn btn-outline-danger btn-small" data-action="reject">Reject</button>
       <button class="btn btn-success btn-small" data-action="approve">${isExit ? "Approve exit" : "Approve &amp; place"}</button>`;
  let checkLine = "";
  if (kind === "blocked") {
    checkLine = `<span class="chip live">✗ ${failed.length ? failed.map((c) => esc(c.label)).join(", ") : "risk check failed"}</span>`;
  } else if (checks.length) {
    checkLine = `<span class="chip ok">✓ ${checks.length - failed.length}/${checks.length} risk checks passed</span>`;
  }
  const origin = originChipHtml(t);
  return `
    <article class="approval-card ${kind}" data-ticket-id="${t.id}">
      <div class="ac-top">
        <div class="ac-title">
          ${sideHtml(t.transaction_type)}<span class="ac-symbol">${esc(t.symbol)}</span>${origin}
          ${t.signal_date ? `<span class="badge-muted">signal ${esc(fmtDate(t.signal_date))}</span>` : ""}
          ${t.status === "pending_confirmation" && t.review?.subject && !t.review.current ? `<span class="chip warn" title="${esc(t.review.reason || "")}">needs re-review</span>` : ""}
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
        <div class="exec-note${proposal?.mode !== "dry_run" ? " live" : ""}">${proposal?.mode !== "dry_run"
          ? "LIVE position — the protective GTT at the broker is modified." : "Dry-run position — only the app's record is updated."}</div>`,
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
  if (action === "chart") {
    openSignalDetail(ticket);
  } else if (action === "kite") {
    const primary = (ticket.kite_chart?.options || []).find((o) => o.key === "kite_tvc");
    if (primary?.enabled && primary.url) window.open(primary.url, "_blank", "noopener");
    else toast(primary?.reason || "No Kite chart link could be resolved for this symbol.", { type: "warning" });
  } else if (action === "details") {
    openConfirmDialog(ticket);
  } else if (action === "approve") {
    if (needsAmoConsent(ticket) || (ticket.review?.subject && !ticket.review.current)) {
      // PK-12H: the only place to consent to an AMO is the dialog's checkbox.
      // P1-D: the only place to re-review a stale manual ticket is the dialog.
      openConfirmDialog(ticket);
      return;
    }
    const live = ticketExecMode(ticket) === "live";
    const shownMode = shownModeFor(ticket);  // the mode the dialog below shows
    const shownReview = shownReviewFor(ticket);  // P1-D: the review this card shows
    const ok = await confirmAction({
      title: live ? `Place a real ${ticket.transaction_type} order?` : "Approve this trade (dry run)?",
      html: approvalConfirmHtml(ticket),
      okLabel: live ? "Place order" : "Approve",
      okClass: live ? "btn-danger" : "btn-success",
    });
    if (!ok) return;
    card.classList.add("busy");
    await approveTicket(id, shownMode, false, shownReview);
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

// =====================================================================
// Signal Detail — the full-screen "why this signal" workspace.
//
// Deliberately NOT built on #ticket-confirm-dialog: that dialog is a modal
// prompt shared with the New Ticket form, and this is a two-pane workspace
// with its own chart lifecycle and keyboard navigation between signals.
// What it DOES reuse is everything that matters for correctness — the same
// riskCheckRowHtml rows, the same confirmAction() dialog, and the same
// approveTicket()/rejectTicketById() handlers that post to
// /api/tickets/{id}/confirm. There is no second execution path here.
// =====================================================================

// How the one pane is divided between price and its subpanels, as
// scaleMargins ({top, bottom} fractions of the pane). Keyed by subpanel count:
// every strategy gets volume, and rsi_mean_reversion additionally gets RSI.
// The price scale stays linear across the whole pane, so its axis labels keep
// going underneath the subpanels; leaving too much room below price makes it
// extrapolate down to meaningless (even negative) prices. These margins keep
// the subpanels readable without pushing that extrapolation absurd.
const SD_SCALE_BANDS = {
  1: { price: { top: 0.06, bottom: 0.24 }, panels: [{ top: 0.80, bottom: 0.02 }] },
  2: {
    price: { top: 0.05, bottom: 0.38 },
    panels: [{ top: 0.63, bottom: 0.20 }, { top: 0.84, bottom: 0.02 }],
  },
};

// Short on-chart captions; the full label and detail live in the tooltip.
const SD_MARKER_TEXT = {
  pullback: "Pullback", recovery: "Recovery", breakout: "Breakout",
  signal: "Signal", swing_low: "Swing low",
};

const SD_MARKER_COLOURS = {
  pullback: "--accent-amber", recovery: "--pnl-positive",
  breakout: "--pnl-positive", signal: "--accent", swing_low: "--pnl-negative",
};

const sd = {
  ticket: null,
  shownReview: undefined,   // P1-D: the review token captured when the view opened
  data: null,
  chart: null,
  priceSeries: null,
  series: new Map(),   // key -> { api, spec }
  hidden: new Set(),   // series keys the user has toggled off
  queue: [],           // pending entry-signal ticket ids, for ←/→
  ltp: null,
  prevClose: null,
  resizeObserver: null,
  disposed: true,
  priceLines: [],      // entry/stop/target, kept for assertion in tests/e2e_ui.py
  buildCount: 0,       // increments per chart build, so a re-theme is observable
};

// Deliberate, read-only handle for the Playwright driver in tests/e2e_ui.py.
// Price lines and chart colours are painted into a canvas, so there is no DOM
// for a browser test to assert against; without this the E2E check could only
// confirm that *a* canvas exists, not that the three levels were drawn or that
// a theme toggle actually re-rendered them.
window.__signalDetail = sd;

function chartLinksHtml(t) {
  const chart = t.kite_chart || {};
  const primary = (chart.options || []).find((o) => o.key === "kite_tvc");
  const kiteEnabled = primary && primary.enabled && primary.url;
  const kiteTitle = kiteEnabled
    ? "Open this symbol's chart on kite.zerodha.com (needs an active Kite web session)"
    : (primary && primary.reason) || "No Kite chart link could be resolved for this symbol.";
  return `<button class="btn btn-ghost btn-small" data-action="chart" title="Annotated chart and the full evidence trail">Chart</button>
    <button class="btn btn-ghost btn-small" data-action="kite" title="${esc(kiteTitle)}"${kiteEnabled ? "" : " disabled"}>Kite</button>`;
}

// ---- theme ----------------------------------------------------------
// Every colour the chart draws is read from the live CSS custom properties,
// so the chart is themed by the same tokens as the rest of the app and can
// be re-themed by simply re-reading them after the toggle flips.
function sdToken(name, fallback = "#888") {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name);
  return (value || "").trim() || fallback;
}

function sdChartTheme() {
  return {
    text: sdToken("--text-secondary"),
    grid: sdToken("--border"),
    border: sdToken("--border-strong"),
    up: sdToken("--pnl-positive"),
    down: sdToken("--pnl-negative"),
    crosshair: sdToken("--text-muted"),
  };
}

// ---- open / close ---------------------------------------------------

function sdPendingQueue() {
  const entries = state.approvals?.entries ?? [];
  return entries.filter((t) => t.closes_ticket_id == null).map((t) => t.id);
}

async function openSignalDetail(ticket) {
  sd.ticket = ticket;
  // P1-D: captured when the view opens; sdOnTicketsChanged swaps sd.ticket
  // for a fresher copy, which must not silently upgrade what was shown.
  sd.shownReview = shownReviewFor(ticket);
  sd.queue = sdPendingQueue();
  sd.hidden = new Set();
  sd.disposed = false;
  el.signalDetail.hidden = false;
  document.body.classList.add("sd-open");
  el.sdEvidenceInner.innerHTML = `<div class="empty-row">Loading the evidence trail…</div>`;
  el.sdChartNote.hidden = true;
  sdRenderHeader(ticket, null);

  try {
    sd.data = await API.getTicketChart(ticket.id);
  } catch (e) {
    sd.data = null;
    el.sdEvidenceInner.innerHTML = `<div class="sd-section"><h3>Could not load this signal</h3>
      <p class="muted-note">${esc(e.message)}</p></div>`;
    return;
  }
  if (sd.disposed) return;
  sdRenderHeader(ticket, sd.data);
  sdRenderEvidence();
  sdRenderActions();
  sdBuildChart();
}

function closeSignalDetail() {
  sd.disposed = true;
  sdDestroyChart();
  el.signalDetail.hidden = true;
  el.sdKiteMenu.hidden = true;
  el.sdKiteMore.setAttribute("aria-expanded", "false");
  document.body.classList.remove("sd-open");
  sd.ticket = null;
  sd.data = null;
}

function sdDestroyChart() {
  if (sd.resizeObserver) {
    sd.resizeObserver.disconnect();
    sd.resizeObserver = null;
  }
  if (sd.chart) {
    sd.chart.remove();
    sd.chart = null;
  }
  sd.priceSeries = null;
  sd.series.clear();
  el.sdChart.innerHTML = "";
}

// ---- header ---------------------------------------------------------

function sdSeedPrice(symbol, token) {
  // Seeded from data the app already holds so the header is populated
  // immediately, then kept live by the existing /ws/ticks subscription —
  // this view never opens a second socket, and never calls the broker.
  const holding = (state.holdings || []).find((h) => h.instrument_token === token);
  const watched = (state.watchlist || []).find((w) => w.instrument_token === token);
  const source = holding || watched;
  sd.ltp = source?.last_price ?? null;
  sd.prevClose = holding?.close_price ?? watched?.last_close ?? null;
}

function sdRenderHeader(ticket, data) {
  el.sdSide.innerHTML = sideHtml(ticket.transaction_type);
  el.sdSymbol.textContent = ticket.symbol;
  el.sdStrategy.innerHTML = originChipHtml(ticket);
  el.sdSignalDate.textContent = ticket.signal_date ? `signal ${fmtDate(ticket.signal_date)}` : "";
  el.sdSignalDate.hidden = !ticket.signal_date;

  const mode = data?.execution_mode ?? state.executionMode;
  el.sdModePill.innerHTML = mode === "live"
    ? `<span class="chip live">LIVE — real money</span>`
    : `<span class="chip dry">DRY RUN — simulated</span>`;

  // PK-19: the ticket's state and its position's lifecycle, as two concepts.
  const ts = data?.ticket_state ?? ticket.ticket_state;
  const lc = data?.lifecycle ?? ticket.lifecycle;
  el.sdLifecycle.innerHTML = (ts ? `<span class="sd-lc-part">Ticket ${LC.ticketStateBadge(ts)}</span>` : "")
    + (lc && (lc.is_position || lc.is_closed)
      ? `<span class="sd-lc-part">Position ${LC.lifecycleBadge(lc)} ${LC.protectionBadge(lc)}</span>` : "");

  sdSeedPrice(ticket.symbol, ticket.instrument_token);
  sdRenderLtp();

  el.sdAsof.textContent = data?.freshness?.last_candle_date
    ? fmtDate(data.freshness.last_candle_date) : "—";

  const index = sd.queue.indexOf(ticket.id);
  el.sdPosition.textContent = sd.queue.length > 1 && index >= 0
    ? `${index + 1} of ${sd.queue.length}` : "";
  const single = sd.queue.length < 2;
  el.sdPrev.disabled = single;
  el.sdNext.disabled = single;
}

function sdRenderLtp() {
  if (sd.ltp == null) {
    el.sdLtp.textContent = "—";
    el.sdLtpChange.textContent = computeMarketStatus() === "open" ? "waiting for a tick" : "market closed";
    el.sdLtpChange.className = "sd-ltp-change muted-note";
    return;
  }
  el.sdLtp.textContent = inr(sd.ltp);
  if (sd.prevClose) {
    const pct = ((sd.ltp - sd.prevClose) / sd.prevClose) * 100;
    el.sdLtpChange.textContent = `${pct >= 0 ? "+" : ""}${fmt(pct, 2)}%`;
    el.sdLtpChange.className = `sd-ltp-change ${pnlClass(pct)}`;
  } else {
    el.sdLtpChange.textContent = "";
    el.sdLtpChange.className = "sd-ltp-change";
  }
}

// Called from the existing /ws/ticks handler — no second socket (C4).
function sdApplyTick(tick) {
  if (el.signalDetail.hidden || !sd.ticket) return;
  if (tick.instrument_token !== sd.ticket.instrument_token) return;
  sd.ltp = tick.last_price;
  if (tick.close_price) sd.prevClose = tick.close_price;
  sdRenderLtp();
  sdRenderContextSection();
}

// ---- chart ----------------------------------------------------------

function sdBuildChart() {
  sdDestroyChart();
  if (!window.LightweightCharts) {
    el.sdChartNote.hidden = false;
    el.sdChartNote.textContent =
      "The charting library (/static/vendor/) could not be loaded, so the chart cannot be drawn. "
      + "The evidence panel on the right is served by this app and is unaffected.";
    return;
  }
  const data = sd.data;
  if (!data || !data.candles.length) {
    el.sdChartNote.hidden = false;
    el.sdChartNote.textContent = data?.diagnostic?.message || "No candles are available for this symbol.";
    el.sdToggles.innerHTML = "";
    return;
  }
  if (data.diagnostic) {
    el.sdChartNote.hidden = false;
    el.sdChartNote.textContent = data.diagnostic.message;
  }

  const LWC = window.LightweightCharts;
  const theme = sdChartTheme();
  sd.buildCount += 1;
  sd.theme = theme;
  sd.chart = LWC.createChart(el.sdChart, {
    width: el.sdChart.clientWidth,
    height: el.sdChart.clientHeight,
    layout: {
      background: { color: "transparent" }, textColor: theme.text, attributionLogo: false,
      panes: { separatorColor: theme.border, separatorHoverColor: theme.crosshair, enableResize: false },
    },
    grid: { vertLines: { color: theme.grid }, horzLines: { color: theme.grid } },
    rightPriceScale: { borderColor: theme.border },
    timeScale: { borderColor: theme.border, rightOffset: 4 },
    crosshair: { mode: LWC.CrosshairMode ? LWC.CrosshairMode.Normal : 0 },
    localization: { priceFormatter: (p) => p.toLocaleString("en-IN", { maximumFractionDigits: 2 }) },
  });

  const { entry: lvEntry, stop: lvStop, target: lvTarget } = data.levels;
  sd.priceSeries = sd.chart.addSeries(LWC.CandlestickSeries, {
    upColor: theme.up, downColor: theme.down,
    borderUpColor: theme.up, borderDownColor: theme.down,
    wickUpColor: theme.up, wickDownColor: theme.down,
    // Price lines do not participate in autoscale on their own, so a target
    // above the recent candle range would simply be cropped off the top.
    autoscaleInfoProvider: (original) => {
      const base = original();
      const levels = [lvEntry, lvStop, lvTarget].filter((v) => v != null);
      if (!base || !base.priceRange || !levels.length) return base;
      return {
        ...base,
        priceRange: {
          minValue: Math.min(base.priceRange.minValue, ...levels),
          maxValue: Math.max(base.priceRange.maxValue, ...levels),
        },
      };
    },
  });
  sd.priceSeries.setData(data.candles);

  for (const spec of data.overlays) {
    const api = sd.chart.addSeries(LWC.LineSeries, {
      color: sdToken(spec.color_token), lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
      visible: !sd.hidden.has(spec.key),
    });
    api.setData(spec.data);
    sd.series.set(spec.key, { api, spec });
  }

  // Subpanels are stacked into the SAME pane using their own overlay price
  // scales and scaleMargins, rather than lightweight-charts' multi-pane API.
  // That is not a stylistic preference: with more than one pane, v5 lays the
  // panes out ignoring the separator and time-axis rows, overflows its own
  // container, and stops painting the date labels entirely — a chart you are
  // using to judge a trade needs its dates. Overlay scales are the library's
  // own long-standing idiom for a volume subpanel and keep the axis intact.
  const bands = SD_SCALE_BANDS[data.subpanels.length] || SD_SCALE_BANDS[1];
  sd.priceSeries.priceScale().applyOptions({ scaleMargins: bands.price });

  data.subpanels.forEach((spec, index) => {
    const band = bands.panels[index];
    const type = spec.kind === "histogram" ? LWC.HistogramSeries : LWC.LineSeries;
    const api = sd.chart.addSeries(type, {
      color: sdToken(spec.color_token), lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
      visible: !sd.hidden.has(spec.key),
      priceScaleId: spec.key,
      ...(spec.kind === "histogram" ? { priceFormat: { type: "volume" } } : {}),
    });
    api.priceScale().applyOptions({ scaleMargins: band });
    api.setData(spec.data);
    sd.series.set(spec.key, { api, spec });

    if (spec.companion) {
      // Shares the subpanel's scale, so the average line sits on the volume
      // bars rather than being rescaled against the price axis.
      const companion = sd.chart.addSeries(LWC.LineSeries, {
        color: sdToken(spec.companion.color_token), lineWidth: 2,
        priceLineVisible: false, lastValueVisible: false,
        visible: !sd.hidden.has(spec.companion.key),
        priceScaleId: spec.key,
      });
      companion.setData(spec.companion.data);
      sd.series.set(spec.companion.key, { api: companion, spec: spec.companion });
    }
    // RSI's own thresholds ride as price lines on the RSI series itself, so
    // they stay pinned to its scale rather than the price scale.
    for (const guide of spec.guides || []) {
      api.createPriceLine({
        price: guide.value, color: sdToken(guide.color_token), lineWidth: 1,
        lineStyle: 2, axisLabelVisible: false, title: guide.label,
      });
    }
  });

  sdDrawLevels();
  sdDrawMarkers();
  sdRenderToggles();
  sdAttachTooltip();

  // Deferred to the next frame: with autoSize the container has no measured
  // width yet at this point, and a time scale with zero width silently
  // discards the range, leaving the full 450-bar history on screen with the
  // recent bars compressed into an illegible smear.
  requestAnimationFrame(() => {
    if (!sd.chart) return;
    // The container has its measured size by now; the initial create used
    // whatever clientHeight was available synchronously, which can be 0.
    sdFitChart();
    const total = data.candles.length;
    // The older bars stay loaded (and scrollable) because the 200-period
    // trend line needs them; they just don't need to be on screen.
    const span = Math.min(total, 120);
    sd.chart.timeScale().setVisibleLogicalRange({ from: total - span, to: total + 3 });
    sdPositionZones();
  });

  sd.resizeObserver = new ResizeObserver(() => {
    sdFitChart();
    sdPositionZones();
  });
  sd.resizeObserver.observe(el.sdChart);
}

// lightweight-charts lays multi-pane charts out as a table whose rows are the
// panes PLUS a separator row between each pair PLUS the time-axis row — but it
// sizes the panes themselves from the full height it was given, ignoring those
// extra rows. The table therefore always ends up taller than the box, and what
// overflows the bottom is the date axis. (enableResize:false stops the
// separators being draggable but does not shrink them.)
//
// So: give the chart its container height, measure how far the table actually
// overshot, and hand back a height reduced by exactly that much. One
// corrective pass is enough — the excess is a function of pane count, not of
// the height. This does not loop with the ResizeObserver above, which watches
// the container, not the chart's own internal table.
function sdFitChart() {
  if (!sd.chart) return;
  const box = el.sdChart;
  if (!box.clientWidth || !box.clientHeight) return;
  sd.chart.resize(box.clientWidth, box.clientHeight);
  const table = box.querySelector("table");
  if (!table) return;
  const excess = Math.round(table.getBoundingClientRect().height) - box.clientHeight;
  if (excess > 1) sd.chart.resize(box.clientWidth, box.clientHeight - excess);
}

function sdDrawLevels() {
  sd.priceLines = [];
  const { entry, stop, target } = sd.data.levels;
  const lines = [
    { price: entry, color: sdToken("--accent"), style: 2, title: `Entry ${inr(entry)}` },
    { price: stop, color: sdToken("--pnl-negative"), style: 0, title: `Stop ${inr(stop)}` },
    { price: target, color: sdToken("--pnl-positive"), style: 0, title: `Target ${inr(target)}` },
  ];
  for (const line of lines) {
    if (line.price == null) continue;
    sd.priceSeries.createPriceLine({
      price: line.price, color: line.color, lineWidth: 2, lineStyle: line.style,
      axisLabelVisible: true, title: line.title,
    });
    sd.priceLines.push({ title: line.title, price: line.price, color: line.color });
  }
  // Risk and reward as shaded bands. lightweight-charts has no band
  // primitive, so these are absolutely-positioned elements kept in sync with
  // the price scale — which makes the R:R proportion visible at a glance
  // instead of something the user has to compute from three numbers.
  el.sdChart.querySelectorAll(".sd-zone").forEach((n) => n.remove());
  for (const kind of ["risk", "reward"]) {
    const zone = document.createElement("div");
    zone.className = `sd-zone sd-zone-${kind}`;
    el.sdChart.appendChild(zone);
  }
  sdPositionZones();
  sd.chart.timeScale().subscribeVisibleLogicalRangeChange(sdPositionZones);
}

function sdPositionZones() {
  if (!sd.priceSeries || !sd.data) return;
  const { entry, stop, target } = sd.data.levels;
  const y = (price) => (price == null ? null : sd.priceSeries.priceToCoordinate(price));
  const place = (selector, a, b) => {
    const node = el.sdChart.querySelector(selector);
    if (!node) return;
    const top = y(a), bottom = y(b);
    if (top == null || bottom == null) { node.style.display = "none"; return; }
    const paneHeight = sd.chart?.panes?.()[0]?.getHeight?.() ?? el.sdChart.clientHeight;
    const hi = Math.max(Math.min(top, bottom), 0);
    const lo = Math.min(Math.max(top, bottom), paneHeight);
    if (lo <= hi) { node.style.display = "none"; return; }
    node.style.display = "block";
    node.style.top = `${hi}px`;
    node.style.height = `${lo - hi}px`;
  };
  place(".sd-zone-risk", entry, stop);
  place(".sd-zone-reward", entry, target);
}

function sdDrawMarkers() {
  const markers = (sd.data.markers || []).filter((m) => m.date);
  if (!markers.length || !window.LightweightCharts?.createSeriesMarkers) return;
  window.LightweightCharts.createSeriesMarkers(sd.priceSeries, markers.map((m) => ({
    time: m.date,
    position: m.kind === "swing_low" || m.kind === "pullback" ? "belowBar" : "aboveBar",
    color: sdToken(SD_MARKER_COLOURS[m.kind] || "--accent"),
    shape: m.kind === "swing_low" || m.kind === "pullback" ? "arrowUp" : "arrowDown",
    text: SD_MARKER_TEXT[m.kind] || m.label,
  })));
}

function sdAttachTooltip() {
  const tip = document.createElement("div");
  tip.className = "sd-tooltip";
  tip.hidden = true;
  el.sdChart.appendChild(tip);

  const markerByDate = new Map((sd.data.markers || []).filter((m) => m.date).map((m) => [m.date, m]));
  const candleByDate = new Map(sd.data.candles.map((c) => [c.time, c]));

  sd.chart.subscribeCrosshairMove((param) => {
    const time = param.time;
    if (!time || !param.point || !candleByDate.has(time)) { tip.hidden = true; return; }
    const candle = candleByDate.get(time);
    const rows = [
      `<div class="sd-tip-head">${esc(fmtDate(time))}</div>`,
      `<div class="sd-tip-ohlc">O ${inr(candle.open)} · H ${inr(candle.high)} · L ${inr(candle.low)} · C ${inr(candle.close)}</div>`,
    ];
    // Every visible indicator's value AT THAT BAR — the point of this view is
    // checking the strategy's claimed numbers against the plotted ones.
    for (const { api, spec } of sd.series.values()) {
      if (sd.hidden.has(spec.key)) continue;
      const point = param.seriesData.get(api);
      if (!point || point.value == null) continue;
      const value = spec.key === "volume" || spec.key === "avg_volume"
        ? fmt(point.value, 0) : inr(point.value);
      rows.push(`<div class="sd-tip-row"><span>${esc(spec.label)}</span><strong>${value}</strong></div>`);
    }
    const marker = markerByDate.get(time);
    if (marker) {
      rows.push(`<div class="sd-tip-marker"><strong>${esc(marker.label)}</strong>${
        marker.detail ? `<div>${esc(marker.detail)}</div>` : ""}</div>`);
    }
    tip.innerHTML = rows.join("");
    tip.hidden = false;
    const box = el.sdChart.getBoundingClientRect();
    const left = Math.min(Math.max(param.point.x + 16, 8), box.width - tip.offsetWidth - 8);
    tip.style.left = `${left}px`;
    tip.style.top = `${Math.min(param.point.y + 16, box.height - tip.offsetHeight - 8)}px`;
  });
}

function sdRenderToggles() {
  const specs = [...sd.series.values()].map(({ spec }) => spec);
  el.sdToggles.innerHTML = specs.map((spec) => `
    <label class="sd-toggle" title="${esc(spec.description || spec.label)}">
      <input type="checkbox" data-sd-series="${esc(spec.key)}" ${sd.hidden.has(spec.key) ? "" : "checked"}>
      <span class="sd-swatch" style="background:${esc(sdToken(spec.color_token))}"></span>
      ${esc(spec.label)}
    </label>`).join("");
}

el.sdToggles.addEventListener("change", (event) => {
  const key = event.target.dataset?.sdSeries;
  if (!key) return;
  const entry = sd.series.get(key);
  if (!entry) return;
  if (event.target.checked) sd.hidden.delete(key);
  else sd.hidden.add(key);
  entry.api.applyOptions({ visible: event.target.checked });
});

// ---- evidence panel (D1–D6) -----------------------------------------

function sdSection(id, title, body, { collapsed = false } = {}) {
  return `<details class="sd-section" id="sd-sec-${id}"${collapsed ? "" : " open"}>
    <summary><h3>${esc(title)}</h3></summary>
    <div class="sd-section-body">${body}</div>
  </details>`;
}

function sdConditionRowHtml(c) {
  const icon = c.passed ? "✓" : "✗";
  return `<div class="sd-cond ${c.passed ? "passed" : "failed"}">
    <div class="sd-cond-head"><span class="icon">${icon}</span><span class="sd-cond-name">${esc(c.name)}</span>
      <span class="sd-cond-verdict">${c.passed ? "PASS" : "FAIL"}</span></div>
    <div class="sd-cond-values"><strong>${esc(c.value_label)}</strong>
      <span class="sd-cond-op">${esc(c.operator)}</span>
      <strong>${esc(c.threshold_label)}</strong></div>
    ${c.detail ? `<div class="sd-cond-detail">${esc(c.detail)}</div>` : ""}
    <p class="sd-cond-rule">${esc(c.description)}</p>
  </div>`;
}

function sdSizingRowHtml(step) {
  let value = "—";
  if (step.value != null) {
    if (step.unit === "inr") value = inr(step.value, step.value % 1 === 0 ? 0 : 2);
    else if (step.unit === "ratio") value = `1 : ${fmt(step.value, 2)}`;
    else value = fmt(step.value, 0);
  }
  return `<div class="sd-calc-row">
    <span class="sd-calc-label">${esc(step.label)}</span>
    <span class="sd-calc-formula">${esc(step.formula)}</span>
    <strong class="sd-calc-value">${esc(value)}</strong>
    ${step.note ? `<div class="sd-calc-note">${esc(step.note)}</div>` : ""}
  </div>`;
}

function sdRankLine(rank) {
  if (!rank || rank.position == null) return "";
  const qualified = rank.qualified_count;
  const cap = rank.max_signals_per_day;
  // "Ranked 1 of 2 kept, from 1 candidate" is a confusing way to say the
  // strategy found exactly one setup — the cap never came into play.
  if (qualified != null && qualified <= 1) {
    return `<p class="sd-rank">The only candidate this strategy found that day
      (it may keep up to ${cap}).</p>`;
  }
  const kept = Math.min(cap, qualified ?? cap);
  const barely = qualified != null && qualified > cap && rank.position === cap;
  return `<p class="sd-rank${barely ? " warn" : ""}">Ranked <strong>${rank.position}</strong> of
    ${kept} kept${qualified != null ? `, out of ${qualified} candidates that qualified` : ""} that day.${
    barely ? " This one only just made the cut." : ""}</p>`;
}

// PK-24 — accumulation-evidence score, from the same signal-bar feature
// snapshot as the Volume context section. A description of accumulation-like
// price/volume evidence: it plays no part in the signal, approval, risk or
// sizing. Neutral styling on purpose (no P&L colours, no trade instruction).
// Its display name and disclaimer come from the server (NAME, DISCLAIMER);
// kept outside the PK-23 volume functions, whose wording test forbids the
// name. Every value is set through esc()/fmt().
function sdSmartMoneyList(items) {
  return `<ul class="sd-sm-list">${items.map((t) => `<li>${esc(t.reason || t.label)}</li>`).join("")}</ul>`;
}

function sdSmartMoneyHtml(s) {
  if (!s) {
    return `<p class="muted-note">No stored price history is linked to this ticket, so there is no score.</p>`;
  }
  const disclaimer = `<p class="sd-sm-disclaimer">${esc(s.disclaimer || "")}</p>`;
  const confidence = { HIGH: "High", MEDIUM: "Medium", LOW: "Low", INSUFFICIENT_DATA: "Insufficient data" }[
    s.confidence] || s.confidence || "—";
  if (s.score == null) {
    return `<div class="sd-warn info"><strong>Score not available.</strong>
      ${(s.confidence_reasons || []).map(esc).join(" ")}</div>
      <div class="sd-calc-row"><span class="sd-calc-label">Confidence</span>
      <span class="sd-calc-formula"></span><strong class="sd-calc-value">${esc(confidence)}</strong></div>
      ${disclaimer}`;
  }
  const width = Math.max(0, Math.min(100, Number(s.score)));
  const reasons = (s.confidence_reasons || []).length
    ? `<div class="sd-calc-note">${(s.confidence_reasons || []).map(esc).join(" ")}</div>` : "";
  const positives = (s.top_positive || []).length
    ? `<p class="sd-sm-sub">Top positives</p>${sdSmartMoneyList(s.top_positive)}`
    : `<p class="muted-note">No component added meaningful accumulation-like evidence.</p>`;
  const cautions = (s.top_negative || []).length
    ? `<p class="sd-sm-sub">Cautions</p>${sdSmartMoneyList(s.top_negative)}` : "";
  const warnings = (s.warnings || []).length
    ? `<div class="sd-warn info">${(s.warnings || []).map(esc).join(" ")}</div>` : "";
  return `<div class="sd-sm-head">
      <span class="sd-sm-score">${esc(s.name || "Score")}: ${esc(fmt(s.score, 0))} / 100</span>
      <span class="sd-sm-band">${esc((s.band || {}).label || "")}</span>
      <span class="sd-sm-conf">Confidence: <strong>${esc(confidence)}</strong></span>
    </div>
    <div class="sd-sm-meter" role="img" aria-label="${esc(`Score ${fmt(s.score, 0)} of 100; 50 is neutral`)}">
      <span style="width:${width}%"></span><i></i></div>
    ${reasons}${warnings}${positives}${cautions}${disclaimer}
    <p class="sd-vi-foot">Score as of the ${esc(s.as_of_date || "—")} candle · score v${esc(s.score_version)}
      (features v${esc(s.feature_version)}) · 50 is neutral · context only, not part of the signal.</p>`;
}

// PK-23 — volume context. Descriptive only: none of these numbers took part
// in the signal, its sizing or its risk checks. Every value is set through
// esc()/fmt(); the wording comes from the server's factual interpretation.
function sdVolumeRow(label, value, context, note) {
  return `<div class="sd-calc-row">
    <span class="sd-calc-label">${esc(label)}</span>
    <span class="sd-calc-formula">${esc(context || "")}</span>
    <strong class="sd-calc-value">${esc(value)}</strong>
    ${note ? `<div class="sd-calc-note">${esc(note)}</div>` : ""}
  </div>`;
}

function sdTimes(x) {
  return x == null ? "—" : `${fmt(x, 2)}×`;
}

function sdCrore(x) {
  return x == null ? "—" : `₹${fmt(x / 1e7, 2)} cr`;
}

function sdOrdinal(n) {
  const s = n % 100 >= 11 && n % 100 <= 13 ? "th" : ({ 1: "st", 2: "nd", 3: "rd" }[n % 10] || "th");
  return `${n}${s}`;
}

function sdVolumeHtml(v) {
  if (!v) {
    return `<p class="muted-note">No stored price history is linked to this ticket, so there is no volume
      context to show.</p>`;
  }
  const quality = v.data_quality || {};
  const banner = quality.status && quality.status !== "OK"
    ? `<div class="sd-warn ${quality.status === "UNAVAILABLE" ? "warn" : "info"}">
        <strong>${quality.status === "UNAVAILABLE" ? "Volume features unavailable." : "Partial volume features."}</strong>
        ${(v.warnings || []).map(esc).join(" ")}</div>`
    : "";
  if (quality.status === "UNAVAILABLE" || !v.relative_volume) return banner;

  const rv = v.relative_volume, liq = v.liquidity, st = v.structure, obv = v.obv;
  const ev = v.evidence, con = v.contraction, pb = v.pullback, bo = v.breakout;
  const rows = [];
  rows.push(sdVolumeRow("RVOL20", sdTimes(rv.rvol_20), "vs prior 20 sessions (signal bar excluded)"));
  rows.push(sdVolumeRow("RVOL20 (median)", sdTimes(rv.rvol_median_20), "vs the prior-20 median"));
  rows.push(sdVolumeRow("Volume percentile", rv.volume_percentile_60 == null ? "—"
    : sdOrdinal(Math.round(rv.volume_percentile_60)), "of the prior 60 sessions"));
  rows.push(sdVolumeRow("Traded value", sdCrore(liq.traded_value),
    `20-session avg ${sdCrore(liq.avg_traded_value_20)}`,
    liq.liquidity_warning ? `Below the ${sdCrore(liq.low_liquidity_threshold)} low-liquidity analytics threshold.` : null));
  rows.push(sdVolumeRow("Close location", st.close_location == null ? "—" : fmt(st.close_location, 2),
    "1.00 = closed at the high, 0.00 = at the low"));
  const divergence = {
    obv_leads_price: "OBV at a 20-session high, price not",
    price_unconfirmed: "price at a 20-session high, OBV not",
    confirmed: "OBV and price both at 20-session highs",
    none: "",
  }[obv.divergence] || "";
  rows.push(sdVolumeRow("OBV trend (20)", obv.trend == null ? "—" : `${obv.trend} (${fmt(obv.slope_20, 2)})`,
    divergence));
  rows.push(sdVolumeRow("Accumulation- / distribution-like",
    ev.accumulation_like_10 == null ? "—" : `${ev.accumulation_like_10} / ${ev.distribution_like_10}`,
    ev.accumulation_like_20 == null ? "last 10 sessions"
      : `last 10 sessions (${ev.accumulation_like_20} / ${ev.distribution_like_20} over 20)`));
  rows.push(sdVolumeRow("Volume contraction", sdTimes(con.contraction_ratio_5_20),
    con.contraction_ratio_5_20 == null ? "prior-5 vs prior-20 average"
      : `prior-5 vs prior-20 average — ${con.pre_breakout_volume_contraction ? "contracting" : "not contracting"}`));
  if (bo && bo.is_breakout) {
    rows.push(sdVolumeRow("Breakout participation", sdTimes(bo.volume_ratio),
      `close ${fmt(bo.close_above_level_pct, 2)}% above the prior ${bo.lookback}-session closing high; `
      + `close location ${bo.close_quality == null ? "—" : fmt(bo.close_quality, 2)}`));
  }
  if (pb && pb.in_pullback && pb.pullback_volume_ratio != null) {
    rows.push(sdVolumeRow("Pullback volume", sdTimes(pb.pullback_volume_ratio),
      `${pb.pullback_bars} session(s), ${fmt(Math.abs(pb.pullback_depth_pct), 1)}% off the 20-session high; `
      + `this bar ${sdTimes(pb.recovery_volume_ratio)} the pullback's volume`));
  }
  const cmp = v.strategy_comparison;
  const rule = cmp && cmp.has_volume_rule
    ? `<p class="sd-vi-rule">Strategy rule: ${esc(sdTimes(cmp.legacy_multiple))} its 20-day average
        <em>including</em> the signal bar (needs more than ${esc(sdTimes(cmp.required_multiple))},
        ${cmp.legacy_pass ? "met" : "not met"}); prior-only RVOL20 is ${esc(sdTimes(cmp.rvol_20_prior_only))}.</p>`
    : (cmp ? `<p class="sd-vi-rule">${esc(cmp.rule)}</p>` : "");
  const summary = (v.interpretation || []).length
    ? `<details class="sd-vi-summary"><summary>In words</summary><ul>${
      v.interpretation.map((line) => `<li>${esc(line)}</li>`).join("")}</ul></details>`
    : "";
  return `${banner}${rows.join("")}${rule}${summary}
    <p class="sd-vi-foot">As of the ${esc(v.as_of_date || "—")} candle · feature v${esc(v.feature_version)} ·
      descriptive measurements, not part of the signal.</p>`;
}

function sdRenderEvidence() {
  const d = sd.data;
  const t = sd.ticket;
  const sections = [];

  // D1 — STRATEGY. Description is served by the backend strategy module, so
  // a fourth strategy appears here without this file changing.
  sections.push(sdSection("strategy", "Strategy", `
    <div class="sd-strategy-name">${esc(d.strategy_label || "Manual ticket")}</div>
    ${d.strategy_description ? `<p class="sd-strategy-desc">${esc(d.strategy_description)}</p>` : ""}
    ${sdRankLine(d.rank)}
    ${d.rationale ? `<p class="sd-rationale">${esc(d.rationale)}</p>` : ""}`));

  // D2 — ENTRY CONDITIONS: the "why this stock" answer.
  sections.push(sdSection("conditions", "Entry conditions", d.conditions.length
    ? d.conditions.map(sdConditionRowHtml).join("")
    : `<p class="muted-note">This ticket was not produced by a strategy scan, so there are no
       entry conditions to show.</p>`));

  // PK-23 — VOLUME CONTEXT at the signal bar (context, never a condition).
  sections.push(sdSection("volume", "Volume context", sdVolumeHtml(d.volume_intelligence)));

  // PK-24 — score at the signal bar (context, never a condition).
  sections.push(sdSection("smartmoney", "Accumulation evidence", sdSmartMoneyHtml(d.smart_money_score)));

  // D3 — RISK CHECKS, expanded from the card's "6/6" badge, rendered through
  // the same rows the confirm dialog uses.
  sections.push(sdSection("risk", "Risk checks",
    riskCheckSectionHtml(t.risk_check_results, "Checks", { withLinks: true })
    || `<p class="muted-note">No risk-check record on this ticket.</p>`));

  // D4 — POSITION SIZING: the derivation, not just the answer.
  const sizing = d.sizing || {};
  const adjustments = (sizing.adjustments || []).map((a) =>
    `<div class="sd-adjust">${esc(a.label)} ${inr(a.from)} → <strong>${inr(a.to)}</strong> — ${esc(a.reason)}</div>`).join("");
  sections.push(sdSection("sizing", "Position sizing",
    (sizing.steps || []).map(sdSizingRowHtml).join("")
    + (adjustments || (sizing.tick_size
      ? `<div class="sd-adjust muted-note">All three levels already sit exactly on the
         ₹${esc(sizing.tick_size)} tick grid — no snapping was needed.</div>` : ""))));

  // D5 — EXIT PLAN.
  const plan = d.exit_plan || {};
  sections.push(sdSection("exit", "Exit plan", `
    <p class="sd-exit-trailing">${esc(plan.trailing || "")}</p>
    ${(plan.shared_rules || []).map((r) => `<div class="sd-exit-rule">
        <strong>${esc(r.name)}</strong><p>${esc(r.detail)}</p></div>`).join("")}`));

  // D6 — CONTEXT / WARNINGS.
  sections.push(sdSection("context", "Context & warnings", `<div id="sd-context-body"></div>`));

  el.sdEvidenceInner.innerHTML = sections.join("");
  sdRenderContextSection();
}

function sdRenderContextSection() {
  const body = document.getElementById("sd-context-body");
  if (!body || !sd.data) return;
  const d = sd.data;
  const entry = d.levels.entry;
  const items = [];

  // Execution mode, prominently — a Live approval must never be mistaken for
  // a Dry Run one.
  items.push(d.execution_mode === "live"
    ? `<div class="sd-warn live"><strong>LIVE mode.</strong> Approving this places a real order with
       real money at your broker.</div>`
    : `<div class="sd-warn info"><strong>Dry Run mode.</strong> Approving this simulates the trade;
       nothing is sent to the broker.</div>`);

  // Distance from the limit price to where the stock is actually trading.
  if (sd.ltp != null && entry) {
    const driftPct = ((sd.ltp - entry) / entry) * 100;
    const away = driftPct > 0.75;
    items.push(`<div class="sd-warn ${away ? "warn" : "info"}">
      Last price ${inr(sd.ltp)} is <strong>${driftPct >= 0 ? "+" : ""}${fmt(driftPct, 2)}%</strong>
      from the ${inr(entry)} limit.${away
        ? " Price has run past the limit — a LIMIT order at this level may not fill."
        : ""}</div>`);
  } else {
    items.push(`<div class="sd-warn info">No live price yet, so the distance to the
      ${inr(entry)} limit can't be shown.</div>`);
  }

  // An existing position in the same symbol.
  const open = (state.tickets || []).filter((x) =>
    x.symbol === d.symbol && x.lifecycle?.is_holding && x.lifecycle.position_id === x.id);
  if (open.length) {
    items.push(`<div class="sd-warn warn">You already hold an open position in
      <strong>${esc(d.symbol)}</strong> (${open.map((x) => `${fmt(x.lifecycle.open_quantity, 0)} @ ${inr(x.lifecycle.entry_fill_price ?? x.entry_price)}`).join(", ")}).
      Approving this adds to it.</div>`);
  }

  if (d.freshness?.warning) {
    items.push(`<div class="sd-warn warn">${esc(d.freshness.warning)}</div>`);
  } else if (d.freshness?.signal_age_sessions >= 1) {
    items.push(`<div class="sd-warn info">Signal candle is ${d.freshness.signal_age_sessions} session(s)
      behind the last completed session (limit ${d.freshness.max_staleness_days} days).</div>`);
  }

  body.innerHTML = items.join("");
}

// ---- actions (D7) ---------------------------------------------------

function sdRenderActions() {
  const t = sd.ticket;
  const decidable = t.status === "pending_confirmation" || t.status === "draft";
  // A draft has not passed risk checks, so the server refuses to confirm it.
  el.sdApprove.hidden = t.status !== "pending_confirmation";
  el.sdReject.hidden = !decidable;
  const note = executionNote(t);
  el.sdExecNote.className = `exec-note${note.live ? " live" : ""}`;
  el.sdExecNote.textContent = decidable ? note.text : `This ticket is ${t.status.replace(/_/g, " ")}.`;
  el.sdApprove.textContent = note.live ? "Approve & place (LIVE)" : "Approve & place";
  el.sdApprove.className = `btn ${note.live ? "btn-danger" : "btn-success"}`;

  const options = sd.data?.kite_chart?.options || [];
  const primary = options.find((o) => o.key === "kite_tvc");
  el.sdKiteOpen.textContent = "Open in Kite";
  el.sdKiteOpen.disabled = !(primary && primary.enabled);
  el.sdKiteOpen.title = primary && primary.enabled
    ? "Needs an existing Kite web session. The Kite chart will NOT carry the entry/stop/target lines — "
      + "those exist only on the in-app chart above."
    : (primary?.reason || "No Kite chart link could be resolved.");
  el.sdKiteNote.textContent = primary && primary.enabled
    ? "Kite opens in a new tab and shows no entry/stop/target lines."
    : (primary?.reason || "");

  el.sdKiteMenu.innerHTML = options.map((o) => `
    <button type="button" class="sd-kite-item" role="menuitem" data-sd-kite="${esc(o.key)}"
            ${o.enabled ? "" : "disabled"} title="${esc(o.reason || o.note || "")}">
      <span>${esc(o.label)}</span><small>${esc(o.note || o.reason || "")}</small>
    </button>`).join("");
}

function sdOpenKite(key) {
  const option = (sd.data?.kite_chart?.options || []).find((o) => o.key === key);
  if (!option || !option.enabled) return;
  if (option.key === "copy_symbol") {
    navigator.clipboard?.writeText(sd.data.symbol)
      .then(() => toast(`Copied “${sd.data.symbol}” to the clipboard.`, { type: "success" }))
      .catch(() => toast("Could not access the clipboard.", { type: "error" }));
    return;
  }
  window.open(option.url, "_blank", "noopener");
}

// ---- wiring ---------------------------------------------------------

el.sdClose.addEventListener("click", closeSignalDetail);

el.sdKiteOpen.addEventListener("click", () => sdOpenKite("kite_tvc"));

el.sdKiteMore.addEventListener("click", () => {
  const open = el.sdKiteMenu.hidden;
  el.sdKiteMenu.hidden = !open;
  el.sdKiteMore.setAttribute("aria-expanded", String(open));
});

el.sdKiteMenu.addEventListener("click", (event) => {
  const key = event.target.closest("[data-sd-kite]")?.dataset.sdKite;
  if (!key) return;
  el.sdKiteMenu.hidden = true;
  el.sdKiteMore.setAttribute("aria-expanded", "false");
  sdOpenKite(key);
});

document.addEventListener("click", (event) => {
  if (el.signalDetail.hidden || el.sdKiteMenu.hidden) return;
  if (!event.target.closest(".sd-kite")) {
    el.sdKiteMenu.hidden = true;
    el.sdKiteMore.setAttribute("aria-expanded", "false");
  }
});

// The Settings → Sector mappings link inside a skipped risk check.
el.sdEvidenceInner.addEventListener("click", (event) => {
  if (event.target.closest("[data-sd-close]")) closeSignalDetail();
});

// Approve / Reject reuse the EXACT handlers the Approvals card uses, so the
// confirmation dialog still appears and there is one execution path.
el.sdApprove.addEventListener("click", async () => {
  const t = sd.ticket;
  if (!t) return;
  if (needsAmoConsent(t) || (t.review?.subject && !t.review.current)) {
    // PK-12H: AMO consent is only ever given in the ticket dialog.
    // P1-D: so is the re-review of a stale manual ticket.
    closeSignalDetail();
    openConfirmDialog(t);
    return;
  }
  const live = ticketExecMode(t) === "live";
  const shownMode = shownModeFor(t);  // the mode the dialog below shows
  const ok = await confirmAction({
    title: live ? `Place a real ${t.transaction_type} order?` : "Approve this trade (dry run)?",
    html: approvalConfirmHtml(t),
    okLabel: live ? "Place order" : "Approve",
    okClass: live ? "btn-danger" : "btn-success",
  });
  if (!ok) return;
  el.sdApprove.disabled = true;
  try {
    await approveTicket(t.id, shownMode, false, sd.shownReview);
  } finally {
    el.sdApprove.disabled = false;
  }
  closeSignalDetail();
});

el.sdReject.addEventListener("click", async () => {
  const t = sd.ticket;
  if (!t) return;
  await rejectTicketById(t.id, "Rejected from the Signal Detail view");
  closeSignalDetail();
});

// D9 — Escape closes; ←/→ move between pending signals without closing.
function sdStep(delta) {
  if (sd.queue.length < 2 || !sd.ticket) return;
  const index = sd.queue.indexOf(sd.ticket.id);
  if (index < 0) return;
  const nextId = sd.queue[(index + delta + sd.queue.length) % sd.queue.length];
  const next = (state.approvals?.entries ?? []).find((t) => t.id === nextId);
  if (next) openSignalDetail(next);
}

el.sdPrev.addEventListener("click", () => sdStep(-1));
el.sdNext.addEventListener("click", () => sdStep(1));

document.addEventListener("keydown", (event) => {
  if (el.signalDetail.hidden) return;
  // Never steal keys from the confirmation dialog layered on top of this view.
  if (document.querySelector("dialog[open]")) return;
  if (event.key === "Escape") {
    event.preventDefault();
    closeSignalDetail();
  } else if (event.key === "ArrowLeft") {
    event.preventDefault();
    sdStep(-1);
  } else if (event.key === "ArrowRight") {
    event.preventDefault();
    sdStep(1);
  }
});

// D8 — react to tickets_changed: if this signal was approved, rejected or
// auto-expired elsewhere, never leave an armed Approve button on screen.
function sdOnTicketsChanged() {
  if (el.signalDetail.hidden || !sd.ticket) return;
  const entries = state.approvals?.entries ?? [];
  const fresh = entries.find((t) => t.id === sd.ticket.id);
  if (!fresh) {
    toast("This signal is no longer pending — it was approved, rejected or expired elsewhere.",
      { type: "info", title: "Signal closed" });
    closeSignalDetail();
    return;
  }
  sd.ticket = fresh;
  sd.queue = sdPendingQueue();
  sdRenderHeader(fresh, sd.data);
  sdRenderActions();
}

// Chart colours are read from CSS custom properties, so a theme flip means
// rebuilding the chart against the newly-current tokens (C3).
function sdOnThemeChange() {
  if (el.signalDetail.hidden || !sd.data) return;
  sdBuildChart();
}

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
  const label = t === "dark" ? "Switch to light theme" : "Switch to dark theme";
  for (const button of [el.themeToggleBtn, el.sdTheme]) {
    button.innerHTML = THEME_ICONS[t];
    button.title = label;
  }
  try {
    localStorage.setItem("kt-theme", t);
  } catch (e) {
    // private mode / blocked storage — the server-side pref still applies
  }
}
applyTheme(document.documentElement.dataset.theme);

function toggleTheme() {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  applyTheme(next);
  sdOnThemeChange();
  API.saveUiPrefs({ theme: next }).catch(() => {});
}

el.themeToggleBtn.addEventListener("click", toggleTheme);
el.sdTheme.addEventListener("click", toggleTheme);

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
    state.watchlistGroupByTag = !!prefs.watchlist_group_by_tag;
    if (el.watchlistGroupToggle) el.watchlistGroupToggle.checked = state.watchlistGroupByTag;
  } catch (e) {
    // default expanded/flat; theme stays as restored from localStorage
  }

  const status = await API.status();
  state.sessionStatus = status;
  state.accountType = status.account_type;
  el.logoutBtn.hidden = !status.connected;
  if (!status.connected) {
    showConnectGate();
    return;
  }
  showAuthorizeBanner(!status.terminal_authorized);
  await loadDashboard();
  await loadDashboardPanels();
  const requestedSection = location.hash.slice(1);
  showSection("dashboard");

  if (state.accountType === "trading") {
    revealTradingNavItems();
    await loadTicketingData();
    await Promise.all([
      loadRiskData(), loadWatchlist(), loadWatchlistPanels(), loadStrategyData(), loadPhase6Data(),
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
el.authorizeBtn.addEventListener("click", () => { location.href = "/login"; });
el.refreshBtn.addEventListener("click", () => {
  loadDashboard();
  refreshTicketViews();
});

init();
