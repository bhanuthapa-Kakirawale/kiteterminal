// PK-22H: the read-only System Diagnostics view.
//
// Renders GET /api/diagnostics (see backend/diagnostics.py): readiness to
// trade and why not, schema/migrations, database integrity, background
// workers, market session and NSE calendar coverage, broker login status,
// account safety, paper broker, historical-data health, logging and the
// security runtime. The server returns no secrets; this file still sets
// every value through textContent, never innerHTML.
//
// The one action here is the explicit, user-requested rebuild of a symbol's
// flagged price history (PK-22D) — nothing is ever rebuilt automatically.
(function (root) {
  "use strict";

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else node.setAttribute(k, v);
    }
    for (const child of children) {
      if (child == null) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function show(value) {
    if (value === null || value === undefined || value === "") return "—";
    if (typeof value === "boolean") return value ? "yes" : "no";
    if (Array.isArray(value)) return value.length ? value.join(", ") : "—";
    if (typeof value === "object") return JSON.stringify(value);
    return String(value);
  }

  function kvTable(rows) {
    const body = el("tbody");
    for (const [label, value] of rows) {
      body.append(el("tr", {}, el("td", { text: label }), el("td", { text: show(value) })));
    }
    return el("table", { class: "diag-kv" }, body);
  }

  function section(title, ...content) {
    const box = el("details", { class: "table-section", open: "" }, el("summary", {}, el("h2", { text: title })));
    content.forEach((c) => box.append(c));
    return box;
  }

  function issueList(items, kind) {
    if (!items.length) return el("p", { class: "diag-none", text: kind === "blocker" ? "No blockers." : "No warnings." });
    const list = el("ul", { class: `diag-issues diag-${kind}` });
    for (const item of items) {
      list.append(el("li", {}, el("strong", { text: item.code }), el("span", { text: ` — ${item.message || ""}` })));
    }
    return list;
  }

  function workersTable(workers) {
    const head = el("thead", {}, el("tr", {},
      ...["Worker", "Task", "Cycles", "Last OK (UTC)", "Failures in a row", "Last error"].map((h) => el("th", { text: h }))));
    const body = el("tbody");
    for (const name of Object.keys(workers).sort()) {
      const w = workers[name];
      const state = w.task_alive ? "running" : (w.task_state || "not_started");
      body.append(el("tr", { class: w.failing || (w.critical && !w.task_alive) ? "diag-row-bad" : "" },
        el("td", { text: w.critical ? `${name} (critical)` : name }),
        el("td", { text: state }),
        el("td", { text: show(w.cycles) }),
        el("td", { text: show(w.last_ok_at) }),
        el("td", { text: show(w.consecutive_failures) }),
        el("td", { text: show(w.last_error) })));
    }
    return el("table", { class: "diag-table" }, head, body);
  }

  function historyTable(history, onRebuild) {
    const counts = history.counts || {};
    const summary = el("p", { class: "diag-counts",
      text: `OK ${counts.OK || 0} · SUSPECT ${counts.SUSPECT || 0} · NEEDS_REFRESH ${counts.NEEDS_REFRESH || 0}` });
    if (!history.flagged.length) return el("div", {}, summary, el("p", { class: "diag-none", text: "No flagged symbols." }));
    const head = el("thead", {}, el("tr", {},
      ...["Symbol", "Status", "Reason", "Since", ""].map((h) => el("th", { text: h }))));
    const body = el("tbody");
    for (const item of history.flagged) {
      const button = el("button", { class: "btn btn-small btn-outline", type: "button", text: "Rebuild history" });
      button.addEventListener("click", () => onRebuild(item, button));
      body.append(el("tr", {},
        el("td", { text: item.symbol }),
        el("td", { text: item.status }),
        el("td", { text: show(item.status_reason && item.status_reason.code) }),
        el("td", { text: show(item.status_at) }),
        el("td", {}, button)));
    }
    return el("div", {}, summary, el("table", { class: "diag-table" }, head, body));
  }

  async function rebuild(item, button) {
    button.disabled = true;
    try {
      const res = await fetch(`/api/data/history/${encodeURIComponent(item.instrument_token)}/rebuild`, {
        method: "POST", headers: { "X-Kite-Terminal": "1" },
      });
      const body = await res.json().catch(() => null);
      button.textContent = res.ok ? (body && body.data && body.data.queued ? "Rebuild queued" : "Already queued")
        : `Failed (${res.status})`;
    } catch (e) {
      button.textContent = "Failed";
      button.disabled = false;
    }
  }

  function render(container, data) {
    const r = data.readiness;
    const verdict = el("div", { class: `diag-verdict ${r.ready ? "diag-ready" : "diag-not-ready"}` },
      el("strong", { text: r.ready ? "READY TO TRADE" : "NOT READY TO TRADE" }),
      el("span", { text: ` · execution mode ${data.execution_mode.toUpperCase()} · checked ${r.checked_at} UTC` }));
    const s = data.schema;
    const m = data.market;
    const b = data.broker_session;
    const safety = data.account_safety || {};
    const paper = data.paper_broker || {};
    container.replaceChildren(
      verdict,
      section("Readiness", el("h3", { text: "Blockers" }), issueList(r.blockers, "blocker"),
        el("h3", { text: "Warnings" }), issueList(r.warnings, "warning")),
      section("Application & database", kvTable([
        ["Version", data.app.version], ["Commit", data.app.commit && data.app.commit.sha],
        ["Branch", data.app.commit && data.app.commit.ref], ["Python", data.app.python],
        ["Schema version", `${s.current_version} of ${s.latest_version}`],
        ["Pending migrations", s.pending.map((p) => `${p.version} ${p.name}`)],
        ["Schema supported", s.supported], ["Integrity (quick_check)", data.database_integrity.ok ? "ok" : data.database_integrity.result],
        ["Integrity checked", data.database_integrity.checked_at],
        ["This start: migrations applied", data.startup.applied_migrations],
        ["This start: rows changed", data.startup.startup_row_changes],
      ])),
      section("Background workers", workersTable(data.workers)),
      section("Market & NSE calendar", kvTable([
        ["Session", m.session_state], ["Holiday today", m.holiday_name],
        ["Latest completed session", m.latest_completed_session], ["Next trading day", m.next_trading_day],
        ["Holiday list covers", m.covered_years], ["Years NOT covered", m.uncovered_years],
        ["Verified against candles through", m.verified_against_ohlc_through],
        ["Special sessions (Muhurat, budget day) modelled", m.special_sessions_modelled],
      ])),
      section("Broker & account safety", kvTable([
        ["Kite session", b.state], ["Logged in", b.login_time], ["User", b.user_name],
        ["Verified with Kite", b.verified_with_broker],
        ["Trading lock", safety.trading_locked], ["Lock reason", safety.trading_lock_reason],
        ["Circuit breaker open", safety.breaker_open], ["Breaker reason", safety.breaker_reason],
        ["Live entries today", safety.entries_today],
        ["Paper orders", paper.orders_by_status], ["Paper GTTs", paper.gtts_by_status],
      ])),
      section("Historical price data", historyTable(data.historical_data || { counts: {}, flagged: [] }, rebuild)),
      section("Logging & security", kvTable([
        ["Log file", data.logging.enabled ? data.logging.file : "console only"],
        ["Rotation", data.logging.enabled ? `${data.logging.max_bytes} bytes × ${data.logging.backup_count + 1} files` : null],
        ["Bind", `${data.security.bind_host}:${data.security.port}`],
        ["Remote exposure", data.security.remote_exposure], ["Reload", data.security.reload],
        ["Workers", data.security.workers], ["Started by", data.security.started_by],
        ["Allowed hosts", data.security.allowed_hosts],
      ])),
    );
  }

  async function load(container, { refresh = false } = {}) {
    container.replaceChildren(el("p", { class: "diag-none", text: "Loading diagnostics…" }));
    try {
      const res = await fetch(`/api/diagnostics${refresh ? "?refresh=true" : ""}`);
      if (!res.ok) throw new Error(`Request failed: ${res.status}`);
      render(container, (await res.json()).data);
    } catch (e) {
      container.replaceChildren(el("p", { class: "diag-error", text: `Could not load diagnostics: ${e.message}` }));
    }
  }

  root.KTDiagnostics = { load };
})(typeof window !== "undefined" ? window : globalThis);
