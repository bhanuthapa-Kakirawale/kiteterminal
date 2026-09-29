// PK-19: renderers for the server-derived trade lifecycle.
//
// The server (backend/lifecycle.py) decides WHAT STATE a ticket or position is
// in — lifecycle_state, protection_status, review_required, quantities,
// allowed_actions, ticket_state. This file only decides HOW that is shown. It
// never reads raw ticket columns (status, entry_fill_status, submission_state,
// closed_at, kite_gtt_id ...) to work a state out: if a label is wrong, the
// fix belongs in lifecycle.py, not here.
//
// Pure functions returning HTML strings, no DOM access, so they run unchanged
// in the browser (window.KTLifecycle, loaded before app.js) and in Node
// (tests/test_lifecycle_ui.py renders real API payloads through them).
//
// Every risky state carries a text label and an icon glyph as well as colour.
(function (root) {
  "use strict";

  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  function esc(value) {
    return value == null ? "" : String(value).replace(/[&<>"']/g, (c) => ESC[c]);
  }

  function num(n, decimals = 2) {
    if (n === null || n === undefined || Number.isNaN(n)) return "—";
    return Number(n).toLocaleString("en-IN", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
  }

  const SEVERITY_ICON = { critical: "⛔", warning: "⚠", info: "◷", ok: "●", muted: "○" };

  // One line of help per state, for the badge's tooltip. The state itself
  // (and state_reason, when the server gives one) comes from the server.
  const STATE_HINTS = {
    PENDING_REVIEW: "Draft — its risk checks have not completed.",
    READY_TO_APPROVE: "Passed its risk checks; waiting for your approval.",
    SUBMITTING: "Approved; the order is being sent to the broker.",
    SUBMISSION_UNKNOWN: "The broker call failed without proof of rejection: the order may or may not exist. "
      + "DO NOT RESUBMIT — the app is resolving it from Kite by its order tag.",
    ENTRY_OPEN: "Entry order at the broker, nothing filled yet. Nothing is owned.",
    ENTRY_PARTIAL: "Entry order still working; the filled shares are a real position already.",
    OPEN: "Shares held; no exit order at the broker.",
    EXITING: "Exit order at the broker, not filled. The position stays open and protected until Kite reports "
      + "the fill.",
    EXIT_PARTIAL: "Some shares are sold; the rest are still open.",
    CLOSED: "Every share sold — closed on the broker's fill.",
    REJECTED: "Refused before anything filled.",
    CANCELLED: "The entry order was cancelled with nothing filled.",
    FAILED: "Not executed.",
    NEEDS_REVIEW: "The app cannot tell this position's state from its own data — check it in Kite.",
  };

  const PROTECTION_ICON = {
    PROTECTED: "🛡", MISSING: "⛔", QUANTITY_MISMATCH: "⚠", PRICE_MISMATCH: "⚠", UNKNOWN: "?", NOT_REQUESTED: "○",
  };
  const PROTECTION_SEVERITY = {
    PROTECTED: "ok", MISSING: "critical", QUANTITY_MISMATCH: "critical", PRICE_MISMATCH: "critical",
    UNKNOWN: "warning", NOT_REQUESTED: "muted",
  };
  const PROTECTION_HINTS = {
    PROTECTED: "An active protective GTT covers the shares held (last known reconciliation state).",
    MISSING: "UNPROTECTED — no active protective GTT for the shares held. Place a stop-loss in Kite.",
    QUANTITY_MISMATCH: "The protective GTT does not cover exactly the shares held.",
    PRICE_MISMATCH: "The protective GTT's stop at Kite differs from the app's stop.",
    UNKNOWN: "Protection cannot be judged: the position's quantity or submission is unknown.",
    NOT_REQUESTED: "This ticket asked for no protective GTT.",
  };

  // ---- ticket / order state (a ticket concept, not the position's) ----------

  function ticketStateBadge(ts) {
    if (!ts) return "";
    return `<span class="status-badge ${esc(ts.css)}" data-ticket-state="${esc(ts.code)}" title="${esc(ts.hint)}">`
      + `${esc(ts.label)}</span>`;
  }

  // ---- the position lifecycle -----------------------------------------------

  function lifecycleBadge(lc) {
    if (!lc) return "";
    const hint = [STATE_HINTS[lc.lifecycle_state], lc.state_reason].filter(Boolean).join(" ");
    const scope = lc.unknown_scope === "exit" ? " (exit)" : "";
    return `<span class="lc-badge lc-sev-${esc(lc.severity)}" data-lifecycle="${esc(lc.lifecycle_state)}" `
      + `title="${esc(hint)}"><span class="lc-icon" aria-hidden="true">${SEVERITY_ICON[lc.severity] || ""}</span>`
      + `${esc(lc.lifecycle_label)}${esc(scope)}</span>`;
  }

  function protectionBadge(lc) {
    if (!lc || lc.protection_status === "NOT_APPLICABLE") return `<span class="lc-muted">—</span>`;
    const s = lc.protection_status;
    const gtt = lc.kite_gtt_id ? ` (GTT ${lc.kite_gtt_id})` : "";
    return `<span class="lc-prot lc-sev-${PROTECTION_SEVERITY[s] || "muted"}" data-protection="${esc(s)}" `
      + `title="${esc((PROTECTION_HINTS[s] || "") + gtt)}"><span class="lc-icon" aria-hidden="true">`
      + `${PROTECTION_ICON[s] || ""}</span>${esc(lc.protection_label)}</span>`;
  }

  function modeChip(mode) {
    if (!mode) return "—";
    return mode === "live" ? `<span class="chip live" data-mode="live">LIVE</span>`
      : `<span class="chip dry" data-mode="dry_run">DRY RUN</span>`;
  }

  // Quantities are the server's (PK-13); only their layout is decided here.
  function quantityCell(lc) {
    if (!lc) return "—";
    const sub = (text) => `<div class="lc-qty-sub">${esc(text)}</div>`;
    switch (lc.lifecycle_state) {
      case "ENTRY_OPEN":
      case "ENTRY_PARTIAL":
        return `<span data-qty="entry">${esc(lc.filled_quantity ?? 0)} / ${esc(lc.requested_quantity)} filled</span>`
          + sub("order still working");
      case "EXITING":
      case "EXIT_PARTIAL":
        if (lc.exited_quantity > 0 || (lc.exit_filled_quantity ?? 0) > 0) {
          return `<span data-qty="exit">${esc(lc.open_quantity)} remaining</span>`
            + sub(`${lc.exited_quantity} exited`);
        }
        return `<span data-qty="open">${esc(lc.open_quantity)}</span>`;
      case "CLOSED": {
        const q = lc.closed_trade ? lc.closed_trade.quantity : lc.filled_quantity;
        return `<span data-qty="closed">${esc(q ?? "—")}</span>`;
      }
      case "SUBMISSION_UNKNOWN":
        return `<span data-qty="unknown">${esc(lc.open_quantity ?? lc.requested_quantity)}</span>`
          + sub(lc.unknown_scope === "exit" ? "exit outcome unknown" : "fill unknown");
      default:
        if (lc.open_quantity == null) {
          return lc.is_position ? `<span data-qty="unknown">unknown</span>` + sub(`${lc.requested_quantity} requested`)
            : `<span data-qty="requested">${esc(lc.requested_quantity)}</span>`;
        }
        if (lc.entry_partial) {
          return `<span data-qty="open">${esc(lc.open_quantity)}</span>`
            + sub(`${lc.filled_quantity} / ${lc.requested_quantity} filled`);
        }
        return `<span data-qty="open">${esc(lc.open_quantity)}</span>`;
    }
  }

  function activeExitCell(lc) {
    if (!lc) return "—";
    const x = lc.active_exit;
    if (x) {
      const phase = {
        SUBMITTING: "submitting", WORKING: "working", PARTIAL: "part-filled",
        UNKNOWN: "UNKNOWN — DO NOT RESUBMIT", UNCONFIRMED: "UNCONFIRMED — check Kite",
      }[x.phase] || x.phase;
      const qty = x.filled_quantity != null ? ` · ${x.filled_quantity}/${x.requested_quantity} sold` : "";
      const risky = x.phase === "UNKNOWN" || x.phase === "UNCONFIRMED";
      return `<span class="lc-exit${risky ? " lc-sev-critical" : ""}" data-active-exit="${esc(x.phase)}">`
        + `${risky ? "⚠ " : "⇥ "}Exit #${esc(x.ticket_id)} · ${esc(phase)}${esc(qty)}</span>`;
    }
    if (lc.pending_exit_ticket_id != null) {
      return `<span class="lc-exit" data-active-exit="PENDING">Exit #${esc(lc.pending_exit_ticket_id)} awaiting `
        + `approval</span>`;
    }
    return `<span class="lc-muted">—</span>`;
  }

  function issueBadges(lc) {
    if (!lc || !lc.reconciliation_issues || !lc.reconciliation_issues.length) return "";
    return lc.reconciliation_issues.map((i) =>
      `<span class="lc-issue lc-sev-${i.severity === "critical" ? "critical" : "warning"}" `
      + `data-issue="${esc(i.code)}" title="${esc(i.detail)}"><span class="lc-icon" aria-hidden="true">⚠</span>`
      + `${esc(i.label)}</span>`).join(" ");
  }

  function reviewCell(lc) {
    if (!lc || !lc.review_required) return `<span class="lc-muted">—</span>`;
    return `<span class="lc-review" data-review="required"><span class="lc-icon" aria-hidden="true">⚠</span>`
      + `NEEDS REVIEW</span>${issueBadges(lc) ? `<div class="lc-issues">${issueBadges(lc)}</div>` : ""}`;
  }

  // Buttons follow the server's allowed_actions exactly. They are a
  // convenience: the server re-checks every action when it is invoked.
  const ACTION_BUTTONS = {
    EXIT: (lc) => `<button class="row-action-btn danger" data-action="position-exit" `
      + `data-position-id="${esc(lc.position_id)}">Exit</button>`,
    REVIEW_EXIT: (lc) => `<button class="row-action-btn" data-action="position-review-exit" `
      + `data-ticket-id="${esc(lc.pending_exit_ticket_id)}">Review exit #${esc(lc.pending_exit_ticket_id)}</button>`,
    CANCEL_ORDER: () => `<button class="row-action-btn" data-action="position-orders">Cancel order…</button>`,
    VIEW_RECONCILIATION: () => `<button class="row-action-btn" data-action="position-reconciliation">`
      + `Reconciliation</button>`,
    VIEW_JOURNAL: () => `<button class="row-action-btn" data-action="position-journal">Journal</button>`,
    VIEW_DETAIL: (lc) => `<button class="row-action-btn" data-action="position-detail" `
      + `data-ticket-id="${esc(lc.position_id)}">Details</button>`,
  };

  function actionButtons(lc) {
    if (!lc) return "";
    return (lc.allowed_actions || []).filter((a) => ACTION_BUTTONS[a]).map((a) => ACTION_BUTTONS[a](lc)).join(" ");
  }

  // Unrealised P&L from an LTP the page already has (no extra fetch), for
  // shares actually held at a known broker fill price; "—" otherwise.
  function unrealisedPnl(lc, ltp) {
    if (!lc || ltp == null || lc.entry_fill_price == null || !(lc.open_quantity > 0)
        || !["OPEN", "EXITING", "EXIT_PARTIAL"].includes(lc.lifecycle_state)) return null;
    return (ltp - lc.entry_fill_price) * lc.open_quantity;
  }

  function pnlCell(value) {
    if (value == null) return `<span class="lc-muted">—</span>`;
    return `<span class="${value >= 0 ? "pnl-positive" : "pnl-negative"}">${value >= 0 ? "+" : "−"}₹`
      + `${num(Math.abs(value))}</span>`;
  }

  function activeRowCells(lc, ltp) {
    const entry = lc.entry_fill_price ?? lc.entry_reference_price;
    return `
      <td><strong>${esc(lc.symbol)}</strong>${lc.strategy_name
        ? `<div class="lc-qty-sub">${esc(lc.strategy_name.replace(/_/g, " "))}</div>` : ""}</td>
      <td>${lifecycleBadge(lc)}</td>
      <td>${modeChip(lc.mode)}</td>
      <td class="num">${quantityCell(lc)}</td>
      <td class="num">${num(entry)}${lc.entry_fill_price == null ? `<div class="lc-qty-sub">ref.</div>` : ""}</td>
      <td class="num" data-field="ltp">${num(ltp)}</td>
      <td class="num">${num(lc.current_stop)}</td>
      <td class="num">${num(lc.target)}</td>
      <td>${protectionBadge(lc)}</td>
      <td>${activeExitCell(lc)}</td>
      <td class="num" data-field="upnl">${pnlCell(unrealisedPnl(lc, ltp))}</td>
      <td>${reviewCell(lc)}</td>
      <td class="lc-actions">${actionButtons(lc)}</td>`;
  }

  // fmtTime: the page's own timestamp formatter (it must return escaped HTML).
  function closedRowCells(lc, fmtTime = (ts) => esc(ts || "—")) {
    const t = lc.closed_trade;
    return `
      <td><strong>${esc(lc.symbol)}</strong></td>
      <td>${esc((lc.strategy_name || "manual").replace(/_/g, " "))}</td>
      <td>${lifecycleBadge(lc)}</td>
      <td>${modeChip(lc.mode)}</td>
      <td class="num">${quantityCell(lc)}</td>
      <td class="num">${num(t ? t.entry_price : lc.entry_fill_price)}</td>
      <td class="num">${t ? num(t.average_exit_price) : `<span class="lc-muted">not journaled</span>`}</td>
      <td data-field="opened">${fmtTime(lc.opened_at)}</td>
      <td data-field="closed">${fmtTime(lc.closed_at)}</td>
      <td class="num">${t ? pnlCell(t.realized_pnl) : "—"}</td>
      <td>${reviewCell(lc)}</td>
      <td class="lc-actions">${actionButtons(lc)}</td>`;
  }

  const api = {
    esc, ticketStateBadge, lifecycleBadge, protectionBadge, modeChip, quantityCell, activeExitCell, issueBadges,
    reviewCell, actionButtons, unrealisedPnl, pnlCell, activeRowCells, closedRowCells,
  };
  root.KTLifecycle = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
