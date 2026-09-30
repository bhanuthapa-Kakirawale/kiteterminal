// PK-26: the Price-Volume Research view — a descriptive evidence explorer.
//
// Research / exploration only. One batch request (GET /api/research/volume-scan)
// brings every watchlist row plus the scanner's own catalogue: fields, the
// explicit filters, the transparent presets and the default sort. Filtering and
// sorting then happen here with the SAME condition lists the server defines and
// applies (backend/research_scanner.py: matches / sort_rows — parity-tested in
// tests/test_research_scanner_ui.py), so the table, the CSV export and the API
// always agree.
//
// Rules this file keeps (PK-25 classified Smart Money Score v1.0 LIMITED):
// * the default order comes from the server and is the symbol — never the score;
// * nothing is filtered until the user picks a filter or preset;
// * the score is a plain number in neutral text — no band label, badge or
//   P&L colour — and its limitation, validation status and disclaimer are
//   always on screen, taken verbatim from the server;
// * a strategy signal is shown beside the evidence, never combined with it.
//
// Renderers are pure functions returning HTML strings (every value through
// esc), so they run unchanged in the browser (window.KTResearch) and in Node.
(function (root) {
  "use strict";

  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  function esc(value) {
    return value == null ? "" : String(value).replace(/[&<>"']/g, (c) => ESC[c]);
  }

  const DASH = "—";
  function isNum(v) { return typeof v === "number" && Number.isFinite(v); }
  function fixed(v, d) { return isNum(v) ? v.toFixed(d) : DASH; }
  function times(v) { return isNum(v) ? `${v.toFixed(2)}×` : DASH; }
  function signedPct(v) { return isNum(v) ? `${v > 0 ? "+" : ""}${v.toFixed(2)}%` : DASH; }
  function crore(v) {
    if (!isNum(v)) return DASH;
    const cr = v / 1e7;
    return `₹${cr.toLocaleString("en-IN", { maximumFractionDigits: cr >= 100 ? 0 : 2 })} cr`;
  }
  function yesNo(v) { return v === true ? "yes" : v === false ? "no" : DASH; }
  function ordinal(n) {
    const r = Math.round(n);
    const suffix = r % 100 >= 11 && r % 100 <= 13 ? "th" : ({ 1: "st", 2: "nd", 3: "rd" }[r % 10] || "th");
    return `${r}${suffix}`;
  }

  // ------------------------------------------------------------ pure logic (mirrors the server)

  // AND of every condition; a missing value never matches.
  function matches(row, conditions) {
    for (const c of conditions) {
      const v = row[c.field];
      if (v === null || v === undefined) return false;
      if (c.op === ">=") { if (!(v >= c.value)) return false; }
      else if (c.op === "<=") { if (!(v <= c.value)) return false; }
      else if (c.op === "==") { if (v !== c.value) return false; }
      else throw new Error(`unknown operator ${c.op}`);
    }
    return true;
  }

  function cmp(a, b) { return a < b ? -1 : a > b ? 1 : 0; }

  // Missing values last; ties by symbol, then instrument token.
  function sortRows(rows, key, order) {
    const tie = rows.slice().sort((a, b) => cmp(a.symbol, b.symbol) || cmp(a.instrument_token, b.instrument_token));
    const present = tie.filter((r) => r[key] !== null && r[key] !== undefined);
    const missing = tie.filter((r) => r[key] === null || r[key] === undefined);
    const dir = order === "desc" ? -1 : 1;
    present.sort((a, b) => dir * cmp(a[key], b[key]));   // stable: ties keep symbol order
    return present.concat(missing);
  }

  // The same mapping as research_scanner.conditions_from_params, driven by the
  // catalogue the server sent (meta.filters / meta.presets).
  function conditionsFromValues(values, meta) {
    const out = [];
    if (values.preset) {
      const preset = meta.presets.find((p) => p.key === values.preset);
      if (!preset) throw new Error(`unknown preset ${values.preset}`);
      preset.conditions.forEach((c) => out.push({ ...c }));
    }
    for (const [name, spec] of Object.entries(meta.filters.numeric)) {
      const raw = values[name];
      if (raw === undefined || raw === null || raw === "") continue;
      const value = Number(raw);
      if (!Number.isFinite(value)) continue;
      out.push({ field: spec.field, op: spec.op, value });
    }
    for (const [name, choices] of Object.entries(meta.filters.choice)) {
      const raw = values[name];
      if (!raw) continue;
      (choices[raw] || []).forEach((c) => out.push({ ...c }));
    }
    for (const [name, spec] of Object.entries(meta.filters.text)) {
      if (values[name]) out.push({ field: spec.field, op: spec.op, value: String(values[name]) });
    }
    return out;
  }

  function queryString(values, sort) {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(values)) if (v !== undefined && v !== null && v !== "") params.set(k, v);
    if (sort) { params.set("sort", sort.key); params.set("order", sort.order); }
    return params.toString();
  }

  const OP_TEXT = { ">=": "≥", "<=": "≤", "==": "=" };
  function conditionText(c, meta) {
    const field = (meta.fields.find((f) => f.name === c.field) || {}).label || c.field;
    const value = typeof c.value === "boolean" ? (c.value ? "yes" : "no")
      : c.field === "avg_traded_value_20" ? crore(c.value) : String(c.value).replace(/_/g, " ");
    return `${field} ${OP_TEXT[c.op] || c.op} ${value}`;
  }

  // ------------------------------------------------------------ renderers

  function noticeHtml(meta) {
    const sm = meta.smart_money;
    return `<div class="vr-notice">
      <p class="vr-purpose">${esc(meta.scanner.purpose)}</p>
      <p class="vr-limitation"><strong>${esc(sm.label)}:</strong> ${esc(sm.limitation)}
        <span class="vr-validation">${esc(sm.validation.summary)}</span></p>
      <p class="vr-disclaimer">${esc(sm.disclaimer)}</p>
    </div>`;
  }

  function presetsHtml(meta, activeKey) {
    const buttons = [`<button type="button" class="vr-preset${activeKey ? "" : " active"}" data-preset="">None</button>`]
      .concat(meta.presets.map((p) => `<button type="button" class="vr-preset${p.key === activeKey ? " active" : ""}"
        data-preset="${esc(p.key)}" title="${esc(p.description)}">${esc(p.label)}</button>`));
    const active = meta.presets.find((p) => p.key === activeKey);
    const detail = active
      ? `<p class="vr-preset-def"><strong>${esc(active.label)}:</strong> ${esc(active.description)}
         <span class="vr-conds">${active.conditions.map((c) => esc(conditionText(c, meta))).join(" AND ")}</span></p>`
      : `<p class="vr-preset-def">Presets are plain AND-combinations of the volume features shown in the table —
         no score and no weighting. Pick one to see its exact conditions.</p>`;
    return `<div class="vr-presets" role="group" aria-label="Research presets">${buttons.join("")}</div>${detail}`;
  }

  function option(value, label, current) {
    return `<option value="${esc(value)}"${value === (current || "") ? " selected" : ""}>${esc(label)}</option>`;
  }

  function numberField(name, label, values, attrs) {
    return `<div class="field"><label for="vr-f-${name}">${esc(label)}</label>
      <input id="vr-f-${name}" data-filter="${name}" type="number" inputmode="decimal" ${attrs}
        value="${esc(values[name] == null ? "" : values[name])}" /></div>`;
  }

  function selectField(name, label, choices, values) {
    return `<div class="field"><label for="vr-f-${name}">${esc(label)}</label>
      <select id="vr-f-${name}" data-filter="${name}">${choices.map(([v, l]) => option(v, l, values[name])).join("")}</select></div>`;
  }

  function filtersHtml(meta, values, tags) {
    const smsSet = values.sms_min !== undefined && values.sms_min !== "" || values.sms_max !== undefined && values.sms_max !== "";
    return `<div class="filter-row vr-filters">
      ${numberField("min_rvol", "RVOL20 ≥", values, 'min="0" step="0.1" placeholder="e.g. 1.5"')}
      ${numberField("min_volume_percentile", "Volume percentile ≥", values, 'min="0" max="100" step="5" placeholder="e.g. 90"')}
      ${numberField("min_avg_traded_value_cr", "Avg traded value ≥ ₹ cr", values, 'min="0" step="1" placeholder="e.g. 5"')}
      ${numberField("min_close_location", "Close location ≥", values, 'min="0" max="1" step="0.05" placeholder="0–1"')}
      ${numberField("min_accumulation_10", "Accumulation-like (10) ≥", values, 'min="0" step="1"')}
      ${numberField("max_distribution_10", "Distribution-like (10) ≤", values, 'min="0" step="1"')}
      ${selectField("obv", "OBV", [["", "Any"], ["rising", "Rising"], ["falling", "Falling"], ["new_high", "20-session high"],
        ["obv_leads_price", "Leads price"], ["price_unconfirmed", "Price unconfirmed"]], values)}
      ${selectField("volume", "Volume", [["", "Any"], ["contracting", "Contracting"], ["expanding", "Expanding"],
        ["contraction_then_expansion", "Contraction → expansion"]], values)}
      ${selectField("breakout", "Breakout", [["", "Any"], ["present", "Close above prior-30 high"], ["absent", "None"]], values)}
      ${selectField("pullback", "Pullback", [["", "Any"], ["any", "In pullback"], ["low_volume", "Low-volume pullback"]], values)}
      ${selectField("signal", "Strategy signal", [["", "Any"], ["active", "Has an active signal"], ["none", "No signal"]], values)}
      ${selectField("tag", "Tag", [["", "Any"]].concat(tags.map((t) => [t, t])), values)}
    </div>
    <fieldset class="vr-sms-filter">
      <legend>${esc(meta.smart_money.label)} range — research reference only, off by default</legend>
      <div class="filter-row">
        ${numberField("sms_min", "Min", values, 'min="0" max="100" step="5"')}
        ${numberField("sms_max", "Max", values, 'min="0" max="100" step="5"')}
      </div>
      <p class="vr-inline-warn"${smsSet ? "" : " hidden"}>${esc(meta.smart_money.filter_warning)}</p>
    </fieldset>`;
  }

  const VOLUME_STATE = { contracting: "Contracting", expanding: "Expanding",
    contraction_then_expansion: "Contraction → expansion", normal: "Normal" };
  const QUALITY = { OK: "OK", DEGRADED: "Degraded", UNAVAILABLE: "Unavailable" };

  // [key used for sorting (null = not sortable), header, class]
  const COLUMNS = [
    ["symbol", "Symbol", ""], ["rvol_20", "RVOL20", "num"], ["volume_percentile_60", "Vol %", "num"],
    ["avg_traded_value_20", "₹ Traded value", "num"], ["close_location", "Close loc.", "num"],
    ["obv_slope_20", "OBV (20)", ""], ["accumulation_like_10", "Acc 10", "num"],
    ["distribution_like_10", "Dist 10", "num"], [null, "Volume", ""], [null, "Breakout / pullback", ""],
    ["sms_score", "SMS v1.0", "num"], [null, "Conf.", ""], [null, "Data", ""],
  ];

  function headHtml(sort) {
    return `<tr>${COLUMNS.map(([key, label, cls]) => {
      if (!key) return `<th class="${cls} vr-nosort">${esc(label)}</th>`;
      const state = sort.key === key ? (sort.order === "asc" ? " sorted-asc" : " sorted-desc") : "";
      const aria = sort.key === key ? (sort.order === "asc" ? "ascending" : "descending") : "none";
      return `<th class="${cls}${state}" data-sort="${esc(key)}" aria-sort="${aria}" tabindex="0">${esc(label)}</th>`;
    }).join("")}</tr>`;
  }

  function contextText(r) {
    if (r.is_breakout) return "Close above prior-30 high";
    if (r.in_pullback && isNum(r.pullback_depth_pct)) {
      const vol = isNum(r.pullback_volume_ratio) ? ` on ${times(r.pullback_volume_ratio)} vol` : "";
      return `Pullback ${Math.abs(r.pullback_depth_pct).toFixed(1)}%${vol}`;
    }
    return DASH;
  }

  function rowHtml(r) {
    const unavailable = r.data_quality === "UNAVAILABLE";
    const signals = (r.active_signals || []).map((s) => `<span class="vr-signal" title="Active strategy signal">${esc(s.strategy)}</span>`).join("");
    const nWarn = (r.warnings || []).length;
    const warnTitle = (r.warnings || []).concat(r.notes || []).join("\n");
    const data = `${esc(QUALITY[r.data_quality] || DASH)}${nWarn ? ` <span class="vr-warncount" title="${esc(warnTitle)}">⚠ ${nWarn}</span>` : ""}`;
    const obv = r.obv_trend ? `${esc(r.obv_trend)} <span class="vr-sub">${esc(fixed(r.obv_slope_20, 2))}</span>` : DASH;
    return `<tr class="vr-row${unavailable ? " vr-unavailable" : ""}" data-token="${esc(r.instrument_token)}" tabindex="0">
      <td><span class="vr-sym">${esc(r.symbol)}</span> <span class="vr-sub">${esc(r.exchange)}</span>${signals ? `<div>${signals}</div>` : ""}</td>
      <td class="num">${esc(times(r.rvol_20))}</td>
      <td class="num">${esc(fixed(r.volume_percentile_60, 0))}</td>
      <td class="num">${esc(crore(r.avg_traded_value_20))}${isNum(r.relative_traded_value_20) ? `<div class="vr-sub">today ${esc(times(r.relative_traded_value_20))}</div>` : ""}</td>
      <td class="num">${esc(fixed(r.close_location, 2))}</td>
      <td>${obv}</td>
      <td class="num">${esc(r.accumulation_like_10 == null ? DASH : r.accumulation_like_10)}</td>
      <td class="num">${esc(r.distribution_like_10 == null ? DASH : r.distribution_like_10)}</td>
      <td>${esc(VOLUME_STATE[r.volume_state] || DASH)}</td>
      <td>${esc(contextText(r))}</td>
      <td class="num vr-sms">${esc(fixed(r.sms_score, 1))}</td>
      <td>${esc(r.sms_score == null ? DASH : r.sms_confidence)}</td>
      <td>${data}</td>
    </tr>`;
  }

  function tableHtml(rows, sort) {
    const body = rows.length ? rows.map(rowHtml).join("")
      : `<tr><td colspan="${COLUMNS.length}" class="vr-empty">No watchlist instrument matches every selected condition.</td></tr>`;
    return `<div class="table-scroll vr-table-wrap"><table class="vr-table"><thead>${headHtml(sort)}</thead><tbody>${body}</tbody></table></div>`;
  }

  function statusHtml(data, shown, sort, conditions, meta) {
    const u = data.universe;
    const sortField = (meta.fields.find((f) => f.name === sort.key) || {}).label || sort.key;
    const conds = conditions.length ? conditions.map((c) => esc(conditionText(c, meta))).join(" AND ") : "none";
    return `<p class="vr-status">Showing <strong>${shown}</strong> of ${u.instruments} watchlist instruments
      · ${u.features_unavailable} without usable history
      · as of ${esc(data.requested_as_of || data.latest_completed_session)}
      · sorted by ${esc(sortField)} ${sort.order === "asc" ? "↑" : "↓"}${sort.key === meta.sort.default.key && sort.order === meta.sort.default.order ? " (default)" : ""}
      · conditions: ${conds}</p>`;
  }

  function kv(label, value) {
    return `<div class="vr-kv"><span class="vr-k">${esc(label)}</span><span class="vr-v">${value}</span></div>`;
  }

  function list(items, cls) {
    return items.length ? `<ul class="${cls}">${items.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>` : "";
  }

  function evidenceHtml(f) {
    const rv = f.relative_volume, liq = f.liquidity, st = f.structure, obv = f.obv;
    const con = f.contraction, pb = f.pullback, ev = f.evidence, bo = f.breakout, an = f.anomalies;
    const breakout = bo.is_breakout
      ? `close ${esc(fixed(bo.close_above_level_pct, 2))}% above the prior ${esc(bo.lookback)}-session closing high (${esc(fixed(bo.level, 2))}); close location ${esc(fixed(bo.close_quality, 2))}; base volume ${esc(times(bo.base_contraction_ratio))}`
      : `no close above the prior ${esc(bo.lookback)}-session closing high${isNum(bo.level) ? ` (${esc(fixed(bo.level, 2))})` : ""}`;
    const pullback = pb.in_pullback
      ? `${esc(fixed(Math.abs(pb.pullback_depth_pct || 0), 2))}% below the 20-session closing high over ${esc(pb.pullback_bars)} session(s); pullback volume ${esc(times(pb.pullback_volume_ratio))} the volume into that high${pb.low_volume_pullback ? " (low-volume)" : ""}; today ${esc(times(pb.recovery_volume_ratio))} the pullback's volume`
      : "not in a pullback";
    const flags = [["abnormal volume", an.abnormal_volume], ["extreme volume", an.extreme_volume],
      ["large negative return, weak close", an.negative_shock], ["large positive return, close near high", an.positive_shock]]
      .filter(([, v]) => v).map(([l]) => l);
    return [
      kv("RVOL20", `${esc(times(rv.rvol_20))} <span class="vr-sub">median-based ${esc(times(rv.rvol_median_20))}</span>`),
      kv("Volume percentile (prior 60)", isNum(rv.volume_percentile_60) ? esc(ordinal(rv.volume_percentile_60)) : DASH),
      kv("Traded value", `${esc(crore(liq.traded_value))} vs 20-session avg ${esc(crore(liq.avg_traded_value_20))} (${esc(times(liq.relative_traded_value_20))})${liq.liquidity_warning ? ` <span class="vr-sub">below the ${esc(crore(liq.low_liquidity_threshold))}/day low-liquidity threshold</span>` : ""}`),
      kv("Close location", `${esc(fixed(st.close_location, 2))} <span class="vr-sub">1.00 = at the high, 0.00 = at the low</span>`),
      kv("Price change", esc(signedPct(st.price_change_pct))),
      kv("OBV (20 sessions)", `${esc(obv.trend || DASH)}, net ${esc(fixed(obv.slope_20, 2))} · 20-session high: ${esc(yesNo(obv.obv_new_high_20))} · vs price: ${esc((obv.divergence || DASH).replace(/_/g, " "))}`),
      kv("Accumulation-like / distribution-like", `${esc(ev.accumulation_like_10 ?? DASH)} / ${esc(ev.distribution_like_10 ?? DASH)} in 10 sessions (${esc(ev.accumulation_like_20 ?? DASH)} / ${esc(ev.distribution_like_20 ?? DASH)} in 20)`),
      kv("Volume contraction / expansion", `prior-5 / prior-20 ${esc(times(con.contraction_ratio_5_20))} · contracting: ${esc(yesNo(con.pre_breakout_volume_contraction))} · expanding today: ${esc(yesNo(con.current_volume_expansion))}`),
      kv("Breakout context", breakout),
      kv("Pullback context", pullback),
      kv("Flags", esc(flags.length ? flags.join(", ") : "none")),
    ].join("");
  }

  function contributors(items) {
    return items.length
      ? `<ul class="vr-contrib">${items.map((c) => `<li><span class="vr-pts">${esc(c.points > 0 ? "+" : "")}${esc(fixed(c.points, 1))}</span> ${esc(c.label)}${c.reason ? ` — ${esc(c.reason)}` : ""}</li>`).join("")}</ul>`
      : `<p class="vr-sub">None above half a point.</p>`;
  }

  // The score block. Neutral text only: the number, its confidence, what it
  // is made of, and the PK-25 validation status — never a band or a cue.
  function smsHtml(s, display, row) {
    const v = display.validation;
    const head = s.score == null
      ? `<p class="vr-sms-line">${esc(display.label)}: not available</p>${list(s.confidence_reasons || [], "vr-reasons")}`
      : `<p class="vr-sms-line">${esc(display.label)}: <span class="vr-sms">${esc(fixed(s.score, 1))}</span>
           <span class="vr-sub">(50 is neutral)</span> · Confidence: ${esc(s.confidence)}
           <span class="vr-sub">· coverage ${esc(fixed((s.coverage || 0) * 100, 0))}%</span></p>
         <h4>Largest positive contributors</h4>${contributors(s.top_positive || [])}
         <h4>Cautions</h4>${contributors(s.top_negative || [])}
         ${(s.confidence_reasons || []).length ? `<h4>Confidence notes</h4>${list(s.confidence_reasons, "vr-reasons")}` : ""}
         ${list(s.warnings || [], "vr-reasons")}`;
    const extreme = row && row.extreme_up_bar ? `<p class="sd-warn info">${esc(display.extreme_up_bar.note)}</p>` : "";
    return `<section class="vr-detail-sms">
      <h3>${esc(display.label)} — reference only</h3>
      ${head}${extreme}
      <div class="vr-validation-box">
        <p><strong>${esc(v.summary)}</strong></p>
        <p>${esc(display.limitation)}</p>
        ${list(v.findings, "vr-reasons")}
        <p class="vr-sub">${esc(v.use)}</p>
      </div>
      <p class="vr-disclaimer">${esc(s.disclaimer || display.disclaimer)}</p>
    </section>`;
  }

  function signalHtml(row) {
    const sigs = row.active_signals || [];
    if (!sigs.length) return "";
    return `<section class="vr-detail-signal"><h3>Current strategy signal</h3>
      ${sigs.map((s) => `<p>Signal: <strong>${esc(s.strategy)}</strong> <span class="vr-sub">${esc(s.signal_date)} candle · ${esc(s.status)}</span></p>`).join("")}
      <p class="vr-sub">Shown beside the evidence below. The scanner does not combine a signal and the evidence
        into one judgement; approval is decided in Approvals, unaffected by this page.</p></section>`;
  }

  function detailHtml(d) {
    const row = d.row, f = d.volume_features;
    const unavailable = row.data_quality === "UNAVAILABLE";
    const quality = `<section><h3>Data quality</h3>
      ${kv("Status", `${esc(QUALITY[row.data_quality] || DASH)}${row.data_quality_codes.length ? ` <span class="vr-sub">${esc(row.data_quality_codes.join(", "))}</span>` : ""}`)}
      ${kv("History status", esc(row.history_status || "not yet recorded"))}
      ${kv("Bars stored", esc(row.bars_available ?? DASH))}
      ${list(row.warnings.concat(row.notes), "vr-reasons")}</section>`;
    return `<div class="vr-detail">
      <h2>${esc(row.symbol)} <span class="vr-sub">${esc(row.exchange)} · token ${esc(row.instrument_token)}</span></h2>
      <p class="vr-sub">As of the ${esc(row.as_of_date || DASH)} candle · features v${esc(f.feature_version)} · descriptive measurements, not a signal</p>
      ${signalHtml(row)}
      ${unavailable ? `<p class="sd-warn warn">Volume evidence is unavailable for this instrument: ${esc(row.warnings[0] || "no usable history")}</p>`
        : `<section><h3>Price-volume evidence</h3>${evidenceHtml(f)}</section>
           <details class="vr-words"><summary>In words</summary>${list(f.interpretation || [], "vr-reasons")}</details>`}
      ${smsHtml(d.smart_money, d.smart_money_display, row)}
      ${quality}
    </div>`;
  }

  // ------------------------------------------------------------ DOM controller (browser only)

  const state = { data: null, values: {}, sort: null, asOf: "" };
  const TEXT_OR_CHOICE = new Set(["obv", "volume", "breakout", "pullback", "signal", "tag"]);

  function apiValues() {
    const out = {};
    for (const [k, v] of Object.entries(state.values)) {
      if (v === "" || v == null) continue;
      if (k === "min_avg_traded_value_cr") out.min_avg_traded_value = Number(v) * 1e7;
      else out[k] = v;
    }
    return out;
  }

  function currentRows() {
    const conditions = conditionsFromValues(apiValues(), state.data);
    return { conditions, rows: sortRows(state.data.rows.filter((r) => matches(r, conditions)), state.sort.key, state.sort.order) };
  }

  function exportHref() {
    const values = apiValues();
    if (state.asOf) values.as_of = state.asOf;
    return `/api/research/volume-scan.csv?${queryString(values, state.sort)}`;
  }

  function renderResults(container) {
    const { conditions, rows } = currentRows();
    container.querySelector(".vr-status-slot").innerHTML = statusHtml(state.data, rows.length, state.sort, conditions, state.data);
    container.querySelector(".vr-table-slot").innerHTML = tableHtml(rows, state.sort);
    const link = document.getElementById("vr-export-link");
    if (link) link.setAttribute("href", exportHref());
    const warn = container.querySelector(".vr-inline-warn");
    if (warn) warn.hidden = !(state.values.sms_min || state.values.sms_max);
  }

  function renderAll(container) {
    const meta = state.data;
    const tags = Array.from(new Set(meta.rows.map((r) => r.watchlist_tag).filter(Boolean))).sort();
    container.innerHTML = `${noticeHtml(meta)}
      <div class="vr-controls">
        <div class="vr-asof field"><label for="vr-asof">As of (optional)</label>
          <input id="vr-asof" type="date" value="${esc(state.asOf)}" /></div>
        <div class="vr-preset-slot">${presetsHtml(meta, state.values.preset)}</div>
        <details class="table-section vr-filter-box"><summary><h2>Evidence filters</h2></summary>
          ${filtersHtml(meta, state.values, tags)}
          <button type="button" class="btn btn-ghost btn-small vr-clear">Clear all filters</button>
        </details>
      </div>
      <div class="vr-status-slot"></div>
      <div class="vr-table-slot"></div>`;
    renderResults(container);
  }

  async function openDetail(token) {
    const dialog = document.getElementById("vr-detail-dialog");
    const body = document.getElementById("vr-detail-body");
    body.innerHTML = `<p class="vr-sub">Loading…</p>`;
    if (!dialog.open) dialog.showModal();
    try {
      const q = state.asOf ? `?as_of=${encodeURIComponent(state.asOf)}` : "";
      const res = await fetch(`/api/research/volume-scan/${encodeURIComponent(token)}${q}`);
      if (!res.ok) throw new Error(`Request failed: ${res.status}`);
      body.innerHTML = detailHtml((await res.json()).data);
    } catch (e) {
      body.innerHTML = `<p class="sd-warn warn">Could not load the evidence: ${esc(e.message)}</p>`;
    }
  }

  function bind(container) {
    if (container.dataset.vrBound) return;
    container.dataset.vrBound = "1";
    container.addEventListener("click", (ev) => {
      const preset = ev.target.closest(".vr-preset");
      if (preset) {
        state.values.preset = preset.dataset.preset || "";
        container.querySelector(".vr-preset-slot").innerHTML = presetsHtml(state.data, state.values.preset);
        renderResults(container);
        return;
      }
      if (ev.target.closest(".vr-clear")) {
        state.values = {};
        renderAll(container);
        return;
      }
      const th = ev.target.closest("th[data-sort]");
      if (th) { toggleSort(container, th.dataset.sort); return; }
      const tr = ev.target.closest("tr.vr-row");
      if (tr) openDetail(tr.dataset.token);
    });
    container.addEventListener("keydown", (ev) => {
      if (ev.key !== "Enter" && ev.key !== " ") return;
      const th = ev.target.closest("th[data-sort]");
      if (th) { ev.preventDefault(); toggleSort(container, th.dataset.sort); return; }
      const tr = ev.target.closest("tr.vr-row");
      if (tr) { ev.preventDefault(); openDetail(tr.dataset.token); }
    });
    const onFilter = (ev) => {
      const name = ev.target.dataset && ev.target.dataset.filter;
      if (name) {
        state.values[name] = ev.target.value;
        renderResults(container);
      } else if (ev.target.id === "vr-asof" && ev.type === "change") {
        state.asOf = ev.target.value;
        load(container);
      }
    };
    container.addEventListener("input", (ev) => { if (!TEXT_OR_CHOICE.has(ev.target.dataset && ev.target.dataset.filter)) onFilter(ev); });
    container.addEventListener("change", onFilter);
  }

  function toggleSort(container, key) {
    if (state.sort.key === key) state.sort = { key, order: state.sort.order === "asc" ? "desc" : "asc" };
    else state.sort = { key, order: key === "symbol" ? "asc" : "desc" };
    renderResults(container);
  }

  async function load(container) {
    bind(container);
    container.innerHTML = `<p class="vr-sub">Loading the watchlist's price-volume evidence…</p>`;
    try {
      const q = state.asOf ? `?as_of=${encodeURIComponent(state.asOf)}` : "";
      const res = await fetch(`/api/research/volume-scan${q}`);
      if (!res.ok) throw new Error(`Request failed: ${res.status}`);
      state.data = (await res.json()).data;
      if (!state.sort) state.sort = { ...state.data.sort.default };
      renderAll(container);
    } catch (e) {
      container.innerHTML = `<p class="sd-warn warn">Could not load the research scanner: ${esc(e.message)}</p>`;
    }
  }

  root.KTResearch = {
    esc, matches, sortRows, conditionsFromValues, queryString, conditionText,
    noticeHtml, presetsHtml, filtersHtml, tableHtml, statusHtml, detailHtml, smsHtml, load,
  };
})(typeof window !== "undefined" ? window : globalThis);
