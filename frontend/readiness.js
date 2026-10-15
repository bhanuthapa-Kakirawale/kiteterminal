// LIVE-0: the read-only Live Readiness view (the live trading pre-flight).
//
// Renders GET /api/live-readiness (backend/live_preflight.py): READY or NOT
// READY for a live NEW entry, and every reason, grouped by section. READY
// means every known technical precondition passed — nothing more. The page
// never switches the execution mode and never unlocks trading.
//
// Broker access is opt-in: "Run broker check" asks the server for its one
// read-only Kite snapshot. The only writes here are the user's own static-IP
// confirmation and the deliberate outbound-IP check. Every value is set with
// textContent, never innerHTML.
//
// NET-1: the Kite REST route (DIRECT, or PROXY via host:port) is shown in
// the header line, and the outbound-IP check says which route it uses.
//
// viewModel() is pure (no DOM) so tests/js/render_readiness.js can run it in
// Node; render() only draws what it returns.
(function (root) {
  "use strict";

  const STATUS_LABELS = { PASS: "PASS", FAIL: "FAIL", WARN: "WARN", USER_ACTION: "ACTION REQUIRED" };
  const LINK_LABELS = {
    "#settings": "Open Settings", "#diagnostics": "Open Diagnostics", "#positions": "Open Positions",
    "#watchlist": "Open Watchlist",
  };
  // Manual confirmations for the first live order. Never ticked for the user
  // and never stored: they reset every time the page is drawn.
  const FIRST_LIVE_CHECKLIST = [
    "Static IP verified in the Kite developer app",
    "Correct trading account confirmed",
    "Max order notional reviewed",
    "Daily loss limit reviewed",
    "Trading lock intentionally disabled",
    "Order quantity reviewed",
    "BUY + CNC confirmed",
    "Stop and target reviewed",
    "No unresolved broker order",
    "No critical reconciliation warning",
  ];

  function statusLabel(status) {
    return STATUS_LABELS[status] || String(status || "");
  }

  function isBlockingFailure(check) {
    return Boolean(check.blocking) && (check.status === "FAIL" || check.status === "USER_ACTION");
  }

  function verdict(data) {
    const blocking = (data.checks || []).filter(isBlockingFailure);
    if (data.overall_status === "READY") {
      return {
        tone: "ready",
        title: "READY",
        text: "All known technical preconditions for a live new entry passed.",
      };
    }
    if (data.ready_except_trading_lock) {
      return {
        tone: "lock",
        title: "NOT READY — trading lock is ON",
        text: "Every other check passed. Unlock trading deliberately, only when you are about to approve the one reviewed ticket.",
      };
    }
    return {
      tone: "not-ready",
      title: "NOT READY",
      text: `${blocking.length} blocking item${blocking.length === 1 ? "" : "s"} must be resolved before a live new entry.`,
    };
  }

  function restRouteText(route) {
    if (!route || !route.mode) return "";
    if (route.mode === "PROXY") return `REST via PROXY ${route.endpoint}`;
    if (route.mode === "INVALID") return "REST proxy setting INVALID";
    return "REST DIRECT";
  }

  function viewModel(data) {
    const checks = data.checks || [];
    const route = restRouteText(data.rest_route);
    const sections = (data.sections || []).map((s) => {
      const rows = checks.filter((c) => c.section === s.key).map((c) => ({
        code: c.code,
        status: c.status,
        label: statusLabel(c.status),
        kind: c.blocking ? "Blocking" : "Advisory",
        failing: isBlockingFailure(c),
        message: c.message,
        link: c.action_link && LINK_LABELS[c.action_link] ? { href: c.action_link, text: LINK_LABELS[c.action_link] } : null,
      }));
      const worst = rows.some((r) => r.failing) ? "FAIL" : rows.some((r) => r.status === "WARN" || r.status === "USER_ACTION") ? "WARN" : "PASS";
      return { key: s.key, title: s.title, rows, worst, open: worst !== "PASS" };
    }).filter((s) => s.rows.length);
    const staticIp = checks.find((c) => c.code === "STATIC_IP");
    return {
      verdict: verdict(data),
      disclaimer: data.disclaimer,
      meta: `Execution mode ${String(data.execution_mode || "").toUpperCase()} · checked ${data.checked_at} UTC · ` +
        (data.broker_checked ? `broker read ${data.broker_snapshot_at} UTC (read-only)` : "broker not checked yet") +
        (route ? ` · ${route}` : ""),
      blocking: checks.filter(isBlockingFailure).map((c) => ({ code: c.code, label: statusLabel(c.status), message: c.message })),
      warnings: checks.filter((c) => c.status === "WARN").map((c) => ({ code: c.code, message: c.message })),
      sections,
      staticIp: staticIp && staticIp.detail ? staticIp.detail : null,
      restRoute: data.rest_route || null,
      brokerChecked: Boolean(data.broker_checked),
      showChecklist: data.overall_status === "READY",
      checklist: FIRST_LIVE_CHECKLIST.map((text) => ({ text, checked: false })),
    };
  }

  // ------------------------------------------------------------------ DOM

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

  async function post(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "X-Kite-Terminal": "1", "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const payload = await res.json().catch(() => null);
    if (!res.ok) {
      const detail = payload && payload.detail;
      throw new Error((detail && (detail.message || detail.error_code)) || `Request failed: ${res.status}`);
    }
    return payload && payload.data;
  }

  function issueList(items, empty) {
    if (!items.length) return el("p", { class: "rd-none", text: empty });
    const list = el("ul", { class: "rd-issues" });
    for (const item of items) {
      list.append(el("li", {}, el("strong", { text: item.code }), el("span", { text: ` — ${item.message}` })));
    }
    return list;
  }

  function checkTable(rows) {
    const body = el("tbody");
    for (const r of rows) {
      const message = el("td", { class: "rd-msg" }, el("span", { text: r.message }));
      if (r.link) message.append(" ", el("a", { href: r.link.href, class: "rd-link", text: r.link.text }));
      body.append(el("tr", { class: r.failing ? "rd-row-bad" : "" },
        el("td", {}, el("span", { class: `rd-chip rd-chip-${r.status.toLowerCase()}`, text: r.label })),
        el("td", { class: "rd-kind", text: r.kind }),
        message));
    }
    return el("table", { class: "rd-table" },
      el("thead", {}, el("tr", {}, el("th", { text: "Status" }), el("th", { text: "Type" }), el("th", { text: "Detail" }))),
      body);
  }

  function staticIpPanel(detail, reload) {
    const box = el("div", { class: "rd-ip" });
    const input = el("input", { type: "text", id: "rd-ip-input", inputmode: "decimal", autocomplete: "off",
      placeholder: "e.g. 203.0.113.10", "aria-label": "Static IP whitelisted in the Kite developer console" });
    input.value = (detail && detail.static_ip) || "";
    const tick = el("input", { type: "checkbox", id: "rd-ip-confirm" });
    const status = el("p", { class: "rd-ip-status", role: "status" });
    const confirmBtn = el("button", { type: "button", class: "btn btn-small btn-primary", text: "Confirm static IP" });
    const clearBtn = el("button", { type: "button", class: "btn btn-small btn-outline", text: "Clear" });
    const viaProxy = Boolean(detail && detail.egress_route === "PROXY");
    const egressBtn = el("button", { type: "button", class: "btn btn-small btn-outline",
      text: viaProxy ? `Verify outbound IP via the Kite REST proxy (${detail.egress_proxy})`
        : "Check this machine's outbound IP" });
    const run = async (button, fn) => {
      button.disabled = true;
      status.textContent = "";
      try {
        await fn();
        await reload();
      } catch (e) {
        status.textContent = e.message;
        button.disabled = false;
      }
    };
    confirmBtn.addEventListener("click", () => run(confirmBtn, () =>
      post("/api/live-readiness/static-ip", { ip: input.value.trim(), confirm: tick.checked })));
    clearBtn.addEventListener("click", () => run(clearBtn, () => post("/api/live-readiness/static-ip/clear")));
    egressBtn.addEventListener("click", () => run(egressBtn, () => post("/api/live-readiness/egress-check")));
    const facts = [];
    if (detail) {
      facts.push(`State: ${detail.state}`);
      if (detail.confirmed_at) facts.push(`confirmed ${detail.confirmed_at} UTC`);
      if (detail.egress_ip) facts.push(`outbound IP ${detail.egress_ip} at ${detail.egress_checked_at} UTC`);
      if (detail.egress_error) facts.push(`last outbound check failed: ${detail.egress_error}`);
    }
    box.append(
      el("p", { class: "rd-help", text: "Kite accepts API orders only from the static IP registered in your Kite developer " +
        "console app, and its API does not expose that setting. Check the console, then confirm the IP here." }),
      el("p", { class: "rd-facts", text: facts.join(" · ") || "Not confirmed yet." }),
      el("div", { class: "rd-ip-row" }, el("label", { for: "rd-ip-input", text: "Whitelisted static IP" }), input),
      el("label", { class: "rd-tick", for: "rd-ip-confirm" }, tick,
        " I checked: this IP is whitelisted in my Kite developer console app"),
      el("div", { class: "rd-actions" }, confirmBtn, clearBtn, egressBtn),
      el("p", { class: "rd-help", text: viaProxy
        ? `The outbound-IP check makes one request to api.ipify.org through the Kite REST proxy (${detail.egress_proxy}), ` +
          "so it sees the public IP Kite sees for REST calls. It is optional and only runs when you click it."
        : "The outbound-IP check makes one request to api.ipify.org, which sees only this " +
          "machine's public IP. It is optional and only runs when you click it." }),
      status);
    return box;
  }

  function checklistPanel(model) {
    const box = el("div", { class: "rd-checklist" });
    const done = el("p", { class: "rd-none", role: "status" });
    const boxes = model.checklist.map((item, i) => {
      const id = `rd-check-${i}`;
      const input = el("input", { type: "checkbox", id });
      box.append(el("label", { class: "rd-tick", for: id }, input, ` ${item.text}`));
      return input;
    });
    const update = () => {
      const ticked = boxes.filter((b) => b.checked).length;
      done.textContent = ticked === boxes.length
        ? "All items confirmed for this review. Approve only the one reviewed ticket; nothing is sent from this page."
        : `${ticked} of ${boxes.length} confirmed. These are not saved and are never ticked for you.`;
    };
    boxes.forEach((b) => b.addEventListener("change", update));
    update();
    box.append(done);
    return box;
  }

  function section(title, tone, open, ...content) {
    const attrs = { class: `table-section rd-section rd-section-${tone.toLowerCase()}` };
    if (open) attrs.open = "";
    const box = el("details", attrs, el("summary", {}, el("h2", { text: title }),
      el("span", { class: `rd-chip rd-chip-${tone.toLowerCase()}`, text: tone === "FAIL" ? "ATTENTION" : tone })));
    content.forEach((c) => box.append(c));
    return box;
  }

  function render(container, data, reload) {
    const model = viewModel(data);
    const brokerBtn = el("button", { type: "button", class: "btn btn-small btn-primary",
      text: model.brokerChecked ? "Re-run broker check (read-only)" : "Run broker check (read-only)" });
    const localBtn = el("button", { type: "button", class: "btn btn-small btn-ghost", text: "Re-check without broker" });
    brokerBtn.addEventListener("click", () => reload({ broker: true, refresh: true }));
    localBtn.addEventListener("click", () => reload({ broker: false }));
    const nodes = [
      el("div", { class: `rd-verdict rd-${model.verdict.tone}` },
        el("strong", { text: model.verdict.title }), el("span", { text: model.verdict.text })),
      el("p", { class: "rd-disclaimer", text: model.disclaimer }),
      el("p", { class: "rd-meta", text: model.meta }),
      el("div", { class: "rd-actions" }, brokerBtn, localBtn,
        el("a", { href: "#diagnostics", class: "rd-link", text: "System Diagnostics" })),
      section("Blocking items", model.blocking.length ? "FAIL" : "PASS", model.blocking.length > 0,
        issueList(model.blocking, "None.")),
      section("Warnings (never block)", model.warnings.length ? "WARN" : "PASS", false,
        issueList(model.warnings, "None.")),
    ];
    if (model.showChecklist) {
      nodes.push(section("First live order — manual checklist", "PASS", true, checklistPanel(model)));
    }
    for (const s of model.sections) {
      const content = [checkTable(s.rows)];
      if (s.key === "static_ip") content.push(staticIpPanel(model.staticIp, () => reload({ broker: model.brokerChecked })));
      nodes.push(section(s.title, s.worst, s.open || s.key === "static_ip", ...content));
    }
    container.replaceChildren(...nodes);
  }

  async function load(container, { broker = false, refresh = false } = {}) {
    container.replaceChildren(el("p", { class: "rd-none",
      text: broker ? "Running the read-only broker check…" : "Checking readiness…" }));
    const reload = (opts) => load(container, opts || {});
    try {
      const params = new URLSearchParams({ broker: String(broker), refresh: String(refresh) });
      const res = await fetch(`/api/live-readiness?${params}`);
      if (!res.ok) throw new Error(`Request failed: ${res.status}`);
      render(container, (await res.json()).data, reload);
    } catch (e) {
      container.replaceChildren(el("p", { class: "rd-error", text: `Could not load live readiness: ${e.message}` }));
    }
  }

  root.KTReadiness = { load, viewModel, statusLabel, restRouteText, FIRST_LIVE_CHECKLIST };
})(typeof window !== "undefined" ? window : globalThis);
