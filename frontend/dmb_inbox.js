// DMB Research Inbox: Daily Market Byte candidate packets the user imported for research.
//
// PRIVATE RESEARCH CONTEXT — NOT A TRADE RECOMMENDATION OR ORDER INSTRUCTION.
//
// Rules this file keeps (backend/dmb_inbox.py, backend/routes/dmb_inbox.py):
// * nothing is imported until the user presses Import on one listed bundle;
// * every action (import, dismiss, note, research watchlist, start ticket) is one
//   explicit click and goes through the server, which records it;
// * the evidence "direction" is shown as evidence only, never as a side;
// * there is no score, rank or probability to show: a packet carries none;
// * Start ticket hands the existing New Ticket form a symbol, an exchange and a
//   research rationale only (app.js openTicketFromDmb). Everything else is typed by
//   the user and goes through the normal risk checks and review.
//
// Renderers are pure functions returning HTML strings (every value through esc), so
// they run unchanged in the browser (window.KTDmbInbox) and in Node.
(function (root) {
  "use strict";

  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  function esc(value) {
    return value == null ? "" : String(value).replace(/[&<>"']/g, (c) => ESC[c]);
  }
  const DASH = "—";
  function isNum(v) { return typeof v === "number" && Number.isFinite(v); }
  function num(v, d = 2) { return isNum(v) ? v.toFixed(d) : DASH; }
  function signed(v, unit) { return isNum(v) ? `${v > 0 ? "+" : ""}${v.toFixed(2)}${unit}` : DASH; }
  function short(id) { return id ? `${String(id).slice(0, 12)}…` : DASH; }

  const DISCLAIMER = "PRIVATE RESEARCH CONTEXT — NOT A TRADE RECOMMENDATION OR ORDER INSTRUCTION.";

  const REASON_TEXT = {
    SUPERSEDED: "a newer version of this candidate was imported",
    DISMISSED: "dismissed",
    INTEGRITY_MISMATCH: "the stored packet no longer matches its id",
    RIGHTS_NOT_APPROVED: "private handoff not permitted",
    CALENDAR_UNCOVERED: "the NSE calendar does not cover the review session",
    INVALID_SESSION_DATE: "the session is not a valid NSE session",
    REVIEW_SESSION_MISMATCH: "review session disagrees with the NSE calendar",
    EXPIRED: "the review session has completed",
    QUALITY_FRESHNESS: "evidence incomplete for the session",
    QUALITY_RADAR_COVERAGE: "Radar coverage incomplete",
    QUALITY_REPLAY_NOT_MATCHED: "detector replay did not match",
    INSTRUMENT_UNRESOLVED: "symbol not in the local instrument list",
  };
  function reasonsText(reasons) {
    return (reasons || []).map((r) => REASON_TEXT[r] || r).join("; ");
  }

  function disclaimerHtml() {
    return `<div class="dmb-disclaimer" role="note"><strong>${esc(DISCLAIMER)}</strong>
      <span>Imported from Daily Market Byte's local handoff files. A packet carries evidence and its
      provenance only: no side, quantity, price level or order. The evidence direction describes what the
      data did; it is never a trade side.</span></div>`;
  }

  function bundlesHtml(listing) {
    if (!listing || listing.configured === false) {
      return `<p class="muted-note">Import is off: ${esc((listing && listing.message) ||
        "set KITE_DMB_HANDOFF_DIR to Daily Market Byte's handoff folder")}.</p>`;
    }
    const rows = listing.data || [];
    if (!rows.length) return `<p class="muted-note">No handoff bundles in the configured folder.</p>`;
    return `<table class="data-table dmb-bundles"><thead><tr><th>Session</th><th>Bundle</th><th>Size</th><th></th></tr></thead><tbody>${
      rows.map((b) => `<tr><td>${esc(b.session_date)}</td><td class="mono" title="${esc(b.bundle_id)}">${esc(short(b.bundle_id))}</td>
        <td>${esc(Math.round((b.size || 0) / 1024))} KB</td>
        <td>${b.imported ? `<span class="muted-note">imported</span>` : ""}
          <button class="btn btn-small" type="button" data-dmb-import="${esc(b.session_date)}|${esc(b.bundle_id)}">Import</button></td></tr>`).join("")
    }</tbody></table>`;
  }

  function badges(item) {
    const out = [];
    if (item.synthetic) out.push(`<span class="chip chip-warn">SYNTHETIC</span>`);
    if (item.superseded_by_item_id) out.push(`<span class="chip">superseded</span>`);
    if (item.dismissed) out.push(`<span class="chip">dismissed</span>`);
    if (item.on_research_watchlist) out.push(`<span class="chip">research watchlist</span>`);
    if ((item.ticket_links || []).length) out.push(`<span class="chip">ticket #${esc(item.ticket_links.map((l) => l.ticket_id).join(", #"))}</span>`);
    return out.join(" ");
  }

  function qualityText(q) {
    if (!q) return DASH;
    const ok = q.freshness_ok && q.radar_coverage_ok && q.replay_status === "MATCHED";
    return ok ? "complete" : `degraded${(q.warnings || []).length ? `: ${q.warnings.join(", ")}` : ""}`;
  }

  function itemsTableHtml(items) {
    if (!items || !items.length) return `<p class="muted-note">Nothing imported yet.</p>`;
    return `<table class="data-table dmb-items"><thead><tr>
      <th>Symbol</th><th>Source session</th><th>Review session</th><th>Evidence as of</th><th>Quality</th>
      <th>Rights</th><th>Status</th><th></th></tr></thead><tbody>${items.map((i) => `<tr data-dmb-row="${esc(i.id)}">
        <td><strong>${esc(i.symbol)}</strong> · ${esc(i.exchange)} ${badges(i)}</td>
        <td>${esc(i.session_date)}</td><td>${esc(i.review_session_date)}</td><td>${esc(i.evidence_as_of)}</td>
        <td>${esc(qualityText(i.quality))}</td>
        <td>${esc((i.rights || {}).handoff_status)} · ${esc((i.rights || {}).data_origin)}</td>
        <td>${i.actionable ? `<span class="chip chip-ok">can start a ticket</span>`
          : `<span class="muted-note" title="${esc(reasonsText(i.reasons))}">research only — ${esc(reasonsText(i.reasons))}</span>`}</td>
        <td><button class="btn btn-ghost btn-small" type="button" data-dmb-open="${esc(i.id)}">Details</button></td>
      </tr>`).join("")}</tbody></table>`;
  }

  function kv(rows) {
    return `<dl class="dmb-kv">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>`;
  }

  function contextTable(obj) {
    const keys = Object.keys(obj || {});
    if (!keys.length) return `<p class="muted-note">Not available for this session.</p>`;
    return kv(keys.map((k) => [k.replace(/_/g, " "), esc(isNum(obj[k]) ? num(obj[k], Number.isInteger(obj[k]) ? 0 : 2) : obj[k] ?? DASH)]));
  }

  function detailHtml(item) {
    const rs = item.radar_state || {};
    const m = item.metrics || {};
    const q = item.quality || {};
    const r = item.rights || {};
    const regime = item.market_regime || {};
    return `<section class="dmb-detail" data-dmb-detail="${esc(item.id)}">
      ${disclaimerHtml()}
      <h2>${esc(item.symbol)} · ${esc(item.exchange)} ${badges(item)}</h2>
      <p class="page-sub">${esc(item.company || "")}${item.sector ? ` · ${esc(item.sector)}` : ""}</p>
      <div class="dmb-grid">
        <div><h3>Packet</h3>${kv([
          ["DMB packet id", `<span class="mono" title="${esc(item.packet_id)}">${esc(item.packet_id)}</span>`],
          ["Source session", esc(item.session_date)],
          ["Evidence as of", esc(item.evidence_as_of)],
          ["Review session", `${esc(item.review_session_date)} (actionable until ${esc(item.expires_at)})`],
          ["Generated", esc(item.generated_at)],
          ["Imported", `${esc(item.imported_at)} UTC · bundle <span class="mono">${esc(short(item.bundle_id))}</span>`],
          ["Supersession", item.superseded_by_item_id ? `superseded by item #${esc(item.superseded_by_item_id)}` : "current version"],
        ])}</div>
        <div><h3>Radar evidence</h3>${kv([
          ["Attention", esc(rs.attention_level)], ["Novelty", esc(rs.novelty_type)], ["Appearance", esc(rs.appearance)],
          ["Families", esc((rs.families || []).join(", "))],
          ["Evidence direction", `${esc(rs.direction)} <span class="muted-note">(what the evidence did — not a trade side)</span>`],
          ["Persistence", esc(rs.persistence)], ["Detectors", esc((item.detector_ids || []).join(", "))],
          ["Day move", esc(signed(m.price_change_pct, "%"))], ["Relative volume", isNum(m.relative_volume) ? `${esc(num(m.relative_volume))}×` : DASH],
          ["vs NIFTY 5D / 20D", `${esc(signed(m.vs_nifty_5d_pp, " pp"))} / ${esc(signed(m.vs_nifty_20d_pp, " pp"))}`],
          ["Session close (evidence)", esc(num(m.close))],
        ])}</div>
      </div>
      <h3>Candidate rationale (DMB detector sentences)</h3>
      <ul>${(item.rationale || []).map((t) => `<li>${esc(t)}</li>`).join("") || `<li class="muted-note">none recorded</li>`}</ul>
      <div class="dmb-grid">
        <div><h3>Market context</h3>${contextTable(item.market_context)}
          <h3>Market regime (context)</h3>${kv([["Label", esc(regime.label)], ["Reason", esc(regime.reason_code)],
            ["Dimensions", esc(Object.entries(regime.dimensions || {}).map(([k, v]) => `${k} ${v}`).join(", ") || DASH)]])}</div>
        <div><h3>Sector context</h3>${contextTable(item.sector_context)}
          <h3>Official events</h3>${(item.official_events || []).length
            ? `<ul>${item.official_events.map((e) => `<li>${esc(e.kind)}: ${esc(e.status)} (${esc(e.source_date || DASH)})</li>`).join("")}</ul>`
            : `<p class="muted-note">none</p>`}</div>
      </div>
      <div class="dmb-grid">
        <div><h3>Quality</h3>${kv([["Evidence complete for the session", esc(q.freshness_ok ? "yes" : "no")],
          ["Detector replay", esc(q.replay_status)], ["Radar coverage complete", esc(q.radar_coverage_ok ? "yes" : "no")],
          ["Warnings", esc((q.warnings || []).join(", ") || "none")]])}</div>
        <div><h3>Provenance and rights</h3>${kv([
          ["Producer", esc(`${(item.producer || {}).system || DASH} / ${(item.producer || {}).component || DASH}`)],
          ["Historical evidence", esc((item.source_artifact_ids || []).join(", "))],
          ["Classification", `${esc(r.research_classification)} <span class="muted-note">(a classification, not a permission)</span>`],
          ["Private handoff", `${esc(r.handoff_status)} — ${esc(r.handoff_basis)}`],
          ["Data origin", esc(r.data_origin)],
          ["Sources", esc((r.sources || []).map((s) => `${s.source_name}: ${s.handoff_status}`).join("; "))],
          ["Public publication", esc(r.public_publication)],
        ])}</div>
      </div>
      <h3>Status</h3>
      <p>${item.actionable ? "This research item can start a manual ticket until its review session completes."
        : `Research only: ${esc(reasonsText(item.reasons))}.`}</p>
      <div class="dmb-actions">
        <button class="btn btn-primary" type="button" data-dmb-start="${esc(item.id)}" ${item.actionable ? "" : "disabled"}>Start ticket</button>
        <button class="btn" type="button" data-dmb-watch="${esc(item.id)}" data-watched="${item.on_research_watchlist ? "1" : "0"}">
          ${item.on_research_watchlist ? "Remove from research watchlist" : "Add to research watchlist"}</button>
        <button class="btn btn-ghost" type="button" data-dmb-dismiss="${esc(item.id)}" ${item.dismissed ? "disabled" : ""}>Dismiss</button>
      </div>
      <p class="muted-note">The research watchlist is the inbox's own list. It is not the strategy watchlist and never
        changes what the strategies scan or trade.</p>
      <form class="dmb-note-form" data-dmb-note="${esc(item.id)}"><label>Add a note
        <textarea name="text" rows="2" maxlength="1000" required></textarea></label>
        <button class="btn btn-small" type="submit">Add note</button></form>
      <h3>Notes</h3>${(item.notes || []).length ? `<ul>${item.notes.map((n) => `<li>${esc(n.text)} <span class="muted-note">${esc(n.at)} UTC</span></li>`).join("")}</ul>` : `<p class="muted-note">none</p>`}
      <h3>History</h3><ul class="dmb-history">${(item.history || []).map((e) => `<li>${esc(e.at)} UTC · ${esc(e.event_type)}</li>`).join("")}</ul>
      ${(item.versions || []).length > 1 ? `<h3>Versions of this candidate</h3><ul>${item.versions.map((v) =>
        `<li class="mono">${esc(short(v.packet_id))} · evidence ${esc(v.evidence_as_of)}${v.superseded_by_item_id ? " · superseded" : " · current"}</li>`).join("")}</ul>` : ""}
    </section>`;
  }

  // ---------------------------------------------------------------- browser wiring
  const state = { items: [], showSuperseded: false, watchOnly: false, open: null, bound: false };
  let deps = { mutate: null, toast: () => {}, startTicket: null };

  function configure(d) { deps = { ...deps, ...d }; }

  async function getJson(url) {
    const res = await fetch(url);
    if (!res.ok) {
      const body = await res.json().catch(() => null);
      throw new Error((body && body.detail && body.detail.message) || `Request failed: ${res.status}`);
    }
    return res.json();
  }

  async function refresh(container) {
    const q = `?include_superseded=${state.showSuperseded}&watchlist=${state.watchOnly}`;
    const [items, bundles] = await Promise.all([getJson(`/api/dmb-inbox/items${q}`), getJson("/api/dmb-inbox/bundles")]);
    state.items = items.data;
    container.querySelector("#dmb-bundles").innerHTML = bundlesHtml(bundles);
    container.querySelector("#dmb-items").innerHTML = itemsTableHtml(state.items);
    if (state.open) await openDetail(container, state.open);
  }

  async function openDetail(container, id) {
    state.open = id;
    const r = await getJson(`/api/dmb-inbox/items/${encodeURIComponent(id)}`);
    container.querySelector("#dmb-detail").innerHTML = detailHtml(r.data);
  }

  async function act(container, fn, success) {
    try {
      const out = await fn();
      if (success) deps.toast(success, { type: "success" });
      await refresh(container);
      return out;
    } catch (e) {
      deps.toast(e.message, { type: "error", timeout: 0 });
      return null;
    }
  }

  function bind(container) {
    if (state.bound) return;
    state.bound = true;
    container.addEventListener("click", async (ev) => {
      const t = ev.target.closest("button");
      if (!t) return;
      if (t.dataset.dmbImport) {
        const [session_date, bundle_id] = t.dataset.dmbImport.split("|");
        const r = await act(container, () => deps.mutate("/api/dmb-inbox/imports", "POST", { session_date, bundle_id }));
        if (r) deps.toast(r.status === "DUPLICATE" ? "Already imported — nothing new." : `Imported ${r.new_item_count} research item(s).`,
          { type: "success" });
      } else if (t.dataset.dmbOpen) {
        await openDetail(container, t.dataset.dmbOpen).catch((e) => deps.toast(e.message, { type: "error" }));
      } else if (t.dataset.dmbDismiss) {
        if (!window.confirm("Dismiss this research item? It stays in the history.")) return;
        await act(container, () => deps.mutate(`/api/dmb-inbox/items/${t.dataset.dmbDismiss}/dismiss`, "POST", {}), "Dismissed.");
      } else if (t.dataset.dmbWatch) {
        const watched = t.dataset.watched !== "1";
        await act(container, () => deps.mutate(`/api/dmb-inbox/items/${t.dataset.dmbWatch}/research-watchlist`, "POST", { watched }),
          watched ? "Added to the research watchlist." : "Removed from the research watchlist.");
      } else if (t.dataset.dmbStart) {
        const item = state.items.find((i) => String(i.id) === t.dataset.dmbStart);
        try {
          const start = await deps.mutate(`/api/dmb-inbox/items/${t.dataset.dmbStart}/start-ticket`, "POST", {});
          deps.startTicket(start, Number(t.dataset.dmbStart), item);
        } catch (e) {
          deps.toast(e.message, { type: "error", timeout: 0, title: "Cannot start a ticket" });
          await refresh(container);
        }
      }
    });
    container.addEventListener("submit", async (ev) => {
      const form = ev.target.closest("form[data-dmb-note]");
      if (!form) return;
      ev.preventDefault();
      const text = form.elements.text.value;
      await act(container, () => deps.mutate(`/api/dmb-inbox/items/${form.dataset.dmbNote}/notes`, "POST", { text }), "Note added.");
    });
    container.addEventListener("change", (ev) => {
      if (ev.target.id === "dmb-show-superseded") state.showSuperseded = ev.target.checked;
      else if (ev.target.id === "dmb-watch-only") state.watchOnly = ev.target.checked;
      else return;
      refresh(container).catch((e) => deps.toast(e.message, { type: "error" }));
    });
  }

  async function load(container) {
    if (!container.querySelector("#dmb-items")) {
      container.innerHTML = `${disclaimerHtml()}
        <details class="table-section" open><summary><h2>Import from Daily Market Byte</h2></summary>
          <p class="muted-note">Importing reads one bundle file and needs an active Zerodha login. Nothing is
            imported automatically and nothing in Daily Market Byte's folder is changed.</p>
          <div id="dmb-bundles"></div></details>
        <div class="dmb-filters"><label><input type="checkbox" id="dmb-show-superseded" /> Show superseded versions</label>
          <label><input type="checkbox" id="dmb-watch-only" /> Research watchlist only</label></div>
        <div id="dmb-items"></div><div id="dmb-detail"></div>`;
    }
    bind(container);
    try {
      await refresh(container);
    } catch (e) {
      container.querySelector("#dmb-items").innerHTML = `<p class="sd-warn warn">Could not load the research inbox: ${esc(e.message)}</p>`;
    }
  }

  root.KTDmbInbox = { esc, DISCLAIMER, reasonsText, disclaimerHtml, bundlesHtml, itemsTableHtml, detailHtml,
    qualityText, configure, load };
})(typeof window !== "undefined" ? window : globalThis);
