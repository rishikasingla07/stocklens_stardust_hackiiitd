// StockLens frontend logic (no build step, no external libraries).
// All server text goes through esc() before it is inserted into the page.

"use strict";

const UNKNOWN_TEXT = "Cause not confidently identified";
const MARKET_PREFIX = "Market explanation leads";
const COUNTER_PREFIX = "counter-evidence: ";
const TIER_NAMES = {
  1: "Tier 1 · Primary (exchange filing, official release)",
  2: "Tier 2 · Reputable media",
  3: "Tier 3 · Brokerage notes / blogs / social",
};
const INVESTIGATORS = [
  { id: "atlas", name: "Atlas", role: "macro" },
  { id: "sherlock", name: "Sherlock", role: "company" },
  { id: "echo", name: "Echo", role: "crowd" },
  { id: "skeptic", name: "Skeptic", role: "against" },
];
const INVESTIGATOR_LABEL = Object.fromEntries(INVESTIGATORS.map((i) => [i.id, `${i.name} (${i.role})`]));
const COMPARE_TICKERS = ["ACMEINFRA", "ZENITHPOLY", "ORIONPHARMA"];

// Timeline axis: the default window around the move, the tick step, and how far
// out an item may sit before it is pinned to the edge as "earlier" / "later".
const AXIS_BEFORE = 30;
const AXIS_AFTER = 60;
const AXIS_TICK = 30;
const AXIS_STRETCH_LIMIT = 180;

// Investigation strip timing (ms): when each investigator chip resolves, and the total.
const CHIP_TIMES = [800, 1350, 1900];
const PROGRESS_MS = 2500;
// Simulate move: how long the trigger result is shown before the investigation strip starts.
const TRIGGER_PAUSE_MS = 1200;

const state = {
  mode: loadMode(),
  stocks: null, // {holdings, watchlist}
  health: null, // {mode} or null if the server could not be reached
  ticker: null,
  card: null,
  run: 0, // bumps on every navigation so stale responses are dropped
  trigger: null, // {ticker, html}: trigger panel kept above a simulated result
  simulating: null, // run number of the Simulate move request in flight
  live: null, // {ticker, steps, calls_used, agent_calls, max_calls, others}: the live result on screen
  liveStatus: null, // last GET /live/status body (poll interval, call budget)
};

// --- Helpers -----------------------------------------------------------------

function esc(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function safeUrl(url) {
  return /^https?:\/\//i.test(String(url || "")) ? url : "#";
}

function $(selector, root = document) {
  return root.querySelector(selector);
}

function $$(selector, root = document) {
  return [...root.querySelectorAll(selector)];
}

function loadMode() {
  try {
    return localStorage.getItem("stocklens-mode") === "expert" ? "expert" : "plain";
  } catch {
    return "plain";
  }
}

function saveMode(mode) {
  try { localStorage.setItem("stocklens-mode", mode); } catch { /* storage unavailable */ }
}

function reducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, Math.max(0, ms)));

function signClass(value) {
  return value > 0 ? "up" : value < 0 ? "down" : "flat";
}

function fmtSigned(value, digits = 2, suffix = "%") {
  const n = Number(value);
  const sign = n > 0 ? "+" : n < 0 ? "−" : "";
  return `${sign}${Math.abs(n).toFixed(digits)}${suffix}`;
}

function fmtPrice(value) {
  return "₹" + Number(value).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

const timeFmt = new Intl.DateTimeFormat("en-IN", {
  timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});
const dateFmt = new Intl.DateTimeFormat("en-IN", {
  timeZone: "Asia/Kolkata", day: "numeric", month: "short", year: "numeric",
});
const shortDateFmt = new Intl.DateTimeFormat("en-IN", {
  timeZone: "Asia/Kolkata", day: "numeric", month: "short",
});

function fmtWhen(iso) {
  if (!iso) return "missing timestamp";
  const d = new Date(iso);
  return `${timeFmt.format(d)} IST, ${dateFmt.format(d)}`;
}

function describeGap(minutes) {
  const abs = Math.abs(minutes);
  let text;
  if (abs < 1) text = "less than a minute";
  else if (abs < 60) text = `${Math.round(abs)} min`;
  else if (abs < 60 * 24) text = `${(abs / 60).toFixed(abs < 600 ? 1 : 0)} h`;
  else text = plural(Math.round(abs / 1440), "day");
  if (minutes < 0) return `${text} before the move`;
  if (minutes > 0) return `${text} after the move began`;
  return "exactly when the move began";
}

/** Readable text from a FastAPI error body: a string detail, or a 422 list of field errors. */
function errorDetail(body) {
  if (!body) return "";
  if (typeof body.detail === "string") return body.detail;
  if (Array.isArray(body.detail)) {
    return body.detail.map((d) => {
      const field = Array.isArray(d.loc) ? d.loc[d.loc.length - 1] : "";
      return `${field ? `${field}: ` : ""}${d.msg || "invalid"}`;
    }).join("; ");
  }
  return "";
}

async function api(method, path, json) {
  let res;
  try {
    const options = { method, headers: { Accept: "application/json" } };
    if (json !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(json);
    }
    res = await fetch(path, options);
  } catch {
    const err = new Error("The StockLens server could not be reached.");
    err.status = 0;
    throw err;
  }
  let body = null;
  try { body = await res.json(); } catch { /* non-JSON body */ }
  if (!res.ok) {
    const err = new Error(errorDetail(body) || `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return body;
}

let toastTimer = null;
function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 4000);
}

// --- Server mode (GET /health) -------------------------------------------------

const healthReady = (async () => {
  const el = $("#mode-status");
  try {
    const body = await api("GET", "/health");
    state.health = { mode: body && body.mode === "live" ? "live" : "replay" };
    el.dataset.mode = state.health.mode;
    $(".status-text", el).textContent = state.health.mode === "live" ? "Live mode" : "Replay mode: recorded evidence";
  } catch {
    state.health = null;
    el.dataset.mode = "offline";
    $(".status-text", el).textContent = "Server unreachable";
  }
  return state.health;
})();

function isReplay() {
  return state.health?.mode === "replay";
}

function isLive() {
  return state.health?.mode === "live";
}

// --- Plain / Expert ----------------------------------------------------------

function applyMode() {
  document.body.classList.toggle("mode-plain", state.mode === "plain");
  document.body.classList.toggle("mode-expert", state.mode === "expert");
  $$(".mode-toggle button").forEach((btn) => {
    btn.setAttribute("aria-pressed", String(btn.dataset.mode === state.mode));
  });
  const full = $("details.rc-full");
  if (full) full.open = state.mode === "expert";
}

// --- My Stocks ---------------------------------------------------------------

function stockCard(stock) {
  const cls = signClass(stock.change);
  const body = `
    <span class="sc-ticker">${esc(stock.ticker)}</span>
    <span class="sc-name">${esc(stock.name)}</span>
    <span class="sc-quote">
      <span class="sc-price">${esc(fmtPrice(stock.price))}</span>
      <span class="sc-change ${cls}">${esc(fmtSigned(stock.change_pct))}</span>
    </span>`;
  if (stock.alert) {
    return `<li><button type="button" class="stock-card is-flagged" data-ticker="${esc(stock.ticker)}"
        aria-label="Investigate ${esc(stock.ticker)}, ${esc(stock.name)}, ${esc(fmtSigned(stock.change_pct))} today, move flagged">
      ${body}
      <span class="sc-foot"><span class="badge-flag">Move flagged</span></span>
    </button></li>`;
  }
  return `<li><div class="stock-card is-quiet" aria-label="${esc(stock.ticker)}, ${esc(stock.name)}, no investigation available">
      ${body}
      <span class="sc-foot"><span class="sc-none">No investigation available</span></span>
    </div></li>`;
}

function renderStockList(el, stocks) {
  el.innerHTML = stocks.length ? stocks.map(stockCard).join("") : `<li class="sc-empty">No stocks.</li>`;
}

async function loadStocks() {
  try {
    state.stocks = await api("GET", "/stocks");
    renderStockList($("#holdings-list"), state.stocks.holdings || []);
    renderStockList($("#watchlist-list"), state.stocks.watchlist || []);
    markSelected();
  } catch (err) {
    const msg = `<li class="sc-error">Could not load your stocks: ${esc(err.message)}
      <button type="button" class="btn btn-small" data-action="reload-stocks">Try again</button></li>`;
    $("#holdings-list").innerHTML = msg;
    $("#watchlist-list").innerHTML = "";
  }
}

function findStock(ticker) {
  if (!state.stocks) return null;
  const all = [...(state.stocks.holdings || []), ...(state.stocks.watchlist || [])];
  return all.find((s) => s.ticker === ticker) || null;
}

function stockName(ticker) {
  const match = findStock(ticker);
  return match ? match.name : "";
}

function markSelected() {
  $$(".stock-card[data-ticker], .compare-card[data-ticker]").forEach((el) => {
    const on = el.dataset.ticker === state.ticker;
    el.classList.toggle("is-selected", on);
    if (on) el.setAttribute("aria-current", "true");
    else el.removeAttribute("aria-current");
  });
  updateSimulateButton();
}

function updateSimulateButton() {
  const btn = $("#simulate-btn");
  if (!btn) return;
  const busy = state.simulating !== null && state.simulating === state.run;
  btn.disabled = !state.ticker || busy;
  btn.classList.toggle("is-busy", busy);
  btn.textContent = busy ? "Simulating…" : "Simulate move";
  btn.title = state.ticker ? `Run the trigger engine on ${state.ticker}'s recorded prices` : "Select a stock first";
}

// --- Evidence model ------------------------------------------------------------

/** Accepted + rejected evidence in one list, numbered in time order (missing timestamps last). */
function numberedEvidence(card) {
  const start = Date.parse(card.move.move_start_at);
  const items = [
    ...card.accepted_evidence.map((e) => ({ e, status: acceptedKind(e, start), reason: "" })),
    ...card.rejected_evidence.map((r) => ({ e: r.evidence, status: "rejected", reason: r.reason })),
  ];
  items.forEach((item) => {
    item.minutes = item.e.first_public_at ? (Date.parse(item.e.first_public_at) - start) / 60000 : null;
  });
  items.sort((a, b) => (a.minutes ?? Infinity) - (b.minutes ?? Infinity));
  items.forEach((item, i) => { item.n = i + 1; });
  return items;
}

/** accepted = could be a cause; corroborating = a Tier 2 report after the move that backs a pre-move filing. */
function acceptedKind(e, start) {
  if (e.investigator !== "skeptic" && e.first_public_at && Date.parse(e.first_public_at) >= start) {
    return "corroborating";
  }
  return "accepted";
}

const STATUS_LABEL = { accepted: "Accepted", corroborating: "Corroborating", rejected: "Set aside" };

function keptReason(item) {
  if (item.e.investigator === "skeptic") return "Skeptic: an earlier hint that the move was partly anticipated.";
  if (item.status === "corroborating") return "Published after the move began, so it only corroborates a filing that was public before it.";
  return "Public before the move started.";
}

function countsByInvestigator(card) {
  const counts = Object.fromEntries(INVESTIGATORS.map((i) => [i.id, 0]));
  [...card.accepted_evidence, ...card.rejected_evidence.map((r) => r.evidence)].forEach((e) => {
    if (e.investigator in counts) counts[e.investigator] += 1;
  });
  return counts;
}

// --- Investigation strip --------------------------------------------------------

function honestyLabel() {
  if (isReplay()) return "Replaying a recorded investigation";
  if (isLive()) return "Live investigation";
  return "";
}

/** "3 of 10 TinyFish calls (1 Agent)" for a live job. */
function liveCallsText(live) {
  const max = live.max_calls ? ` of ${live.max_calls}` : "";
  return `${live.calls_used}${max} TinyFish calls (${live.agent_calls} Agent)`;
}

/** Investigator chip for a live run: Sherlock is live; the others use replay data or are not connected. */
function liveChip(inv, done, n, live) {
  if (inv.id === "sherlock") {
    if (!done) return ["is-searching", "searching live…"];
    return [n ? "is-found" : "is-empty", n ? `found ${plural(n, "item")} live` : "found nothing live"];
  }
  if (live.others === "replay") {
    if (!done) return ["is-dimmed", "recorded data"];
    return [n ? "is-found" : "is-empty", n ? `${plural(n, "item")} (recorded)` : "nothing (recorded)"];
  }
  return ["is-dimmed", live.others ? "not connected live" : "waiting…"];
}

function stripHtml(ticker, { done = false, challenged = false, counts = null, live = null } = {}) {
  const chips = INVESTIGATORS.map((inv) => {
    const n = counts ? counts[inv.id] : null;
    let cls = "is-searching";
    let text = "searching…";
    if (live) {
      [cls, text] = liveChip(inv, done, n, live);
    } else if (inv.id === "skeptic" && !challenged) {
      cls = "is-dimmed";
      text = "runs on Challenge This";
    } else if (done) {
      cls = n ? "is-found" : "is-empty";
      text = n ? `found ${plural(n, "item")}` : "found nothing";
    }
    return `<li class="inv ${cls}" data-inv="${inv.id}">
        <span class="inv-dot" aria-hidden="true"></span>
        <span class="inv-name">${inv.name}</span>
        <span class="inv-role">${inv.role}</span>
        <span class="inv-state">${esc(text)}</span>
      </li>`;
  }).join("");
  const label = live ? `Live investigation · ${liveCallsText(live)}` : honestyLabel();
  const steps = live && live.steps.length
    ? `<ol class="live-steps" aria-label="Live steps">${live.steps.map((s, i) =>
        `<li class="${!done && i === live.steps.length - 1 ? "is-current" : ""}">${esc(s)}</li>`).join("")}</ol>`
    : "";
  return `<section class="strip ${done ? "is-done" : "is-running"} ${live ? "is-live" : ""}" aria-label="Investigators">
    <div class="strip-head">
      <h2 class="strip-title">${live ? `<span class="tag tag-live">LIVE</span> ` : ""}${done ? "Investigation complete" : "Investigation in progress"}
        <span class="strip-ticker">${esc(ticker)}</span></h2>
      ${label ? `<span class="strip-honest">${esc(label)}</span>` : ""}
    </div>
    <ol class="inv-row">${chips}</ol>
    ${steps}
  </section>`;
}

function setChip(inv, n) {
  const el = $(`.inv[data-inv="${inv}"]`);
  if (!el) return;
  el.classList.remove("is-searching", "is-dimmed");
  el.classList.add(n ? "is-found" : "is-empty");
  $(".inv-state", el).textContent = n ? `found ${plural(n, "item")}` : "found nothing";
}

/** Resolve the Atlas / Sherlock / Echo chips one by one, finishing about PROGRESS_MS after `started`. */
async function playStrip(run, counts, started) {
  for (let i = 0; i < CHIP_TIMES.length; i++) {
    await sleep(started + CHIP_TIMES[i] - performance.now());
    if (run !== state.run) return false;
    setChip(INVESTIGATORS[i].id, counts[INVESTIGATORS[i].id]);
  }
  await sleep(started + PROGRESS_MS - performance.now());
  return run === state.run;
}

// --- Price chart and time axis ---------------------------------------------------

/** Linear axis in minutes relative to the move start. Stretches to fit nearby items. */
function axisRange(card, items) {
  let lo = -AXIS_BEFORE;
  let hi = Math.max(AXIS_AFTER, Math.ceil(card.move.window_minutes / AXIS_TICK) * AXIS_TICK);
  items.forEach(({ minutes: m }) => {
    if (m === null) return;
    if (m < lo && m >= -AXIS_STRETCH_LIMIT) lo = Math.floor(m / AXIS_TICK) * AXIS_TICK;
    if (m > hi && m <= AXIS_STRETCH_LIMIT) hi = Math.ceil(m / AXIS_TICK) * AXIS_TICK;
  });
  const pct = (m) => ((Math.min(hi, Math.max(lo, m)) - lo) / (hi - lo)) * 100;
  return { lo, hi, pct };
}

function chartHtml(card, axis) {
  const m = card.move;
  const down = m.pct_change < 0;
  const x0 = axis.pct(0);
  const x1 = axis.pct(m.window_minutes);
  const yStart = down ? 10 : 30;
  const yEnd = m.pct_change === 0 ? yStart : down ? 32 : 8;
  const k = (x1 - x0) * 0.45;
  const line = `M0 ${yStart} L${x0.toFixed(2)} ${yStart}
    C${(x0 + k).toFixed(2)} ${yStart} ${(x1 - k).toFixed(2)} ${yEnd} ${x1.toFixed(2)} ${yEnd} L100 ${yEnd}`;
  const tone = down ? "down" : "up";
  const start = new Date(m.move_start_at);
  const moveText = `${fmtSigned(m.pct_change, 1)} in ${m.window_minutes} min`;
  const volText = `${Number(m.volume_multiple).toFixed(1)}× volume`;
  const markerFlip = x0 > 62 ? "flip" : "";
  const moveFlip = x1 > 55 ? "flip" : "";
  return `<figure class="chart chart-${tone}" aria-label="Illustrative price shape: ${esc(moveText)} on ${esc(volText)}, move began ${esc(timeFmt.format(start))} IST">
    <svg class="chart-svg" viewBox="0 0 100 40" preserveAspectRatio="none" aria-hidden="true" focusable="false">
      <rect class="chart-band" x="${x0.toFixed(2)}" y="0" width="${(x1 - x0).toFixed(2)}" height="40"></rect>
      <path class="chart-line" d="${line}" pathLength="100"></path>
      <line class="chart-marker" x1="${x0.toFixed(2)}" y1="0" x2="${x0.toFixed(2)}" y2="40"></line>
    </svg>
    <span class="chart-label marker-label ${down ? "at-bottom" : "at-top"} ${markerFlip}" style="--x:${x0.toFixed(2)}">move begins ${esc(timeFmt.format(start))}</span>
    <span class="chart-label move-label ${down ? "at-top" : "at-bottom"} ${moveFlip} ${tone}" style="--x:${x1.toFixed(2)}">
      <strong>${esc(moveText)}</strong><span>${esc(volText)}</span></span>
  </figure>`;
}

function axisHtml(card, items, axis) {
  const start = Date.parse(card.move.move_start_at);
  const timed = items.filter((it) => it.minutes !== null);
  const missing = items.filter((it) => it.minutes === null);
  const earlier = timed.filter((it) => it.minutes < axis.lo);
  const later = timed.filter((it) => it.minutes > axis.hi);

  const ticks = [];
  for (let t = axis.lo; t <= axis.hi; t += AXIS_TICK) {
    const label = timeFmt.format(new Date(start + t * 60000));
    ticks.push(`<span class="tick ${t === 0 ? "is-zero" : ""}" style="--x:${axis.pct(t).toFixed(2)}">
      <span class="tick-time">${esc(label)}</span>
      <span class="tick-rel">${t === 0 ? "move" : esc(fmtSigned(t, 0, " min"))}</span></span>`);
  }

  const dots = timed.map((it, i) => {
    const pinned = it.minutes < axis.lo ? "earlier" : it.minutes > axis.hi ? "later" : "";
    const when = pinned ? `${pinned}, ${dateFmt.format(new Date(it.e.first_public_at))}` : describeGap(it.minutes);
    return `<button type="button" class="dot dot-${it.status} ${pinned ? "is-pinned" : ""}" data-n="${it.n}"
        style="--x:${axis.pct(it.minutes).toFixed(2)}; --i:${i}"
        aria-label="Evidence ${it.n}: ${esc(STATUS_LABEL[it.status])}, ${esc(it.e.source_name)}, ${esc(when)}">${it.n}</button>`;
  }).join("");

  const edgeNote = (list, side) => list.length
    ? `<span class="edge-note edge-${side}">${side === "earlier" ? "Earlier" : "Later"}:
        ${list.map((it) => `<span class="edge-item" data-n="${it.n}">#${it.n} ${esc(shortDateFmt.format(new Date(it.e.first_public_at)))}</span>`).join(" ")}</span>`
    : "";

  const missingNote = missing.length
    ? `<p class="missing-note"><span class="missing-label">Missing timestamp:</span>
        ${missing.map((it) => `<span class="edge-item" data-n="${it.n}">#${it.n} ${esc(it.e.source_name)}</span>`).join(", ")}
        <span class="missing-why">not placed, and excluded.</span></p>`
    : "";

  return `<div class="axis" role="group" aria-label="Evidence timeline from ${esc(fmtSigned(axis.lo, 0, " min"))} to ${esc(fmtSigned(axis.hi, 0, " min"))} around the move">
      <div class="axis-track ${earlier.length ? "has-earlier" : ""} ${later.length ? "has-later" : ""}">
        <span class="axis-line" aria-hidden="true"></span>
        <span class="axis-zero" style="--x:${axis.pct(0).toFixed(2)}" aria-hidden="true"></span>
        ${dots}
      </div>
      <div class="axis-ticks" aria-hidden="true">${ticks.join("")}</div>
      ${earlier.length || later.length ? `<div class="edge-notes">${edgeNote(earlier, "earlier")}${edgeNote(later, "later")}</div>` : ""}
    </div>
    ${timed.length ? "" : `<p class="axis-empty">No timestamped evidence to place on the axis.</p>`}
    ${missingNote}
    <ul class="legend" aria-label="Legend">
      <li><span class="key key-accepted"></span>Accepted</li>
      <li><span class="key key-corroborating"></span>Corroborating Tier 2</li>
      <li><span class="key key-rejected"></span>Set aside</li>
    </ul>`;
}

/** Stack dots that would overlap into lanes above the axis line. Re-run on resize. */
function layoutAxis() {
  const track = $(".axis-track");
  if (!track) return;
  const dots = $$(".dot", track);
  const width = track.clientWidth || 300;
  const size = dots[0] ? dots[0].offsetWidth : 36;
  const minGap = ((size + 6) / width) * 100;
  const lanes = [];
  dots
    .map((dot) => ({ dot, x: parseFloat(dot.style.getPropertyValue("--x")) }))
    .sort((a, b) => a.x - b.x)
    .forEach(({ dot, x }) => {
      let lane = lanes.findIndex((lastX) => x - lastX >= minGap);
      if (lane === -1) { lane = lanes.length; lanes.push(x); } else { lanes[lane] = x; }
      dot.style.setProperty("--lane", lane);
    });
  track.style.setProperty("--lanes", Math.max(1, lanes.length));

  // Hide tick labels that would collide, working outwards from the move start.
  const ticks = $$(".tick", track.parentElement);
  ticks.forEach((t) => t.classList.remove("is-hidden"));
  const zero = Math.max(0, ticks.findIndex((t) => t.classList.contains("is-zero")));
  const rect = (t) => t.getBoundingClientRect();
  for (const dir of [-1, 1]) {
    let kept = rect(ticks[zero]);
    for (let i = zero + dir; i >= 0 && i < ticks.length; i += dir) {
      const r = rect(ticks[i]);
      const overlaps = dir < 0 ? r.right + 8 > kept.left : r.left - 8 < kept.right;
      if (overlaps) ticks[i].classList.add("is-hidden");
      else kept = r;
    }
  }
}

function evidenceListHtml(items) {
  if (!items.length) return `<p class="ev-empty">The investigators found no evidence about this move.</p>`;
  const rows = items.map((it) => {
    const e = it.e;
    const gap = it.minutes !== null ? describeGap(it.minutes) : "";
    return `<li class="ev ev-${it.status}" data-n="${it.n}" tabindex="0"
        aria-label="Evidence ${it.n}: ${esc(STATUS_LABEL[it.status])}, ${esc(e.source_name)}">
      <span class="ev-num" aria-hidden="true">${it.n}</span>
      <div class="ev-body">
        <div class="ev-top">
          <span class="ev-status">${esc(STATUS_LABEL[it.status])}</span>
          <span class="ev-source">${esc(e.source_name)}</span>
          <span class="ev-time">${esc(fmtWhen(e.first_public_at))}${gap ? ` · ${esc(gap)}` : ""}</span>
        </div>
        <p class="ev-claim">${esc(e.claim)}</p>
        <p class="ev-why ${it.status === "rejected" ? "is-rejected" : ""}">${
          it.status === "rejected" ? `Why it failed: ${esc(it.reason)}` : esc(keptReason(it))}</p>
        <dl class="ev-meta expert-only">
          <div><dt>Tier</dt><dd>${esc(TIER_NAMES[e.source_tier] || `Tier ${e.source_tier}`)}</dd></div>
          <div><dt>Independence group</dt><dd>${esc(e.independence_group || "none")}</dd></div>
          <div><dt>Investigator</dt><dd>${esc(INVESTIGATOR_LABEL[e.investigator] || e.investigator)}</dd></div>
          <div><dt>Verified</dt><dd>${e.verified ? "Yes" : "No"}</dd></div>
        </dl>
        <blockquote class="ev-snippet expert-only">${esc(e.snippet)}</blockquote>
        <a class="ev-link expert-only" href="${esc(safeUrl(e.url))}" target="_blank" rel="noopener noreferrer">${esc(e.url)}</a>
      </div>
    </li>`;
  }).join("");
  return `<ol class="ev-list" aria-label="Evidence, numbered in time order">${rows}</ol>`;
}

function wireEvidenceLinks(root) {
  let pinned = null;
  const setHot = (n) => {
    $$(".is-hot", root).forEach((el) => el.classList.remove("is-hot"));
    if (n !== null) $$(`[data-n="${n}"]`, root).forEach((el) => el.classList.add("is-hot"));
  };
  $$("[data-n]", root).forEach((el) => {
    const n = el.dataset.n;
    el.addEventListener("mouseenter", () => setHot(n));
    el.addEventListener("mouseleave", () => setHot(pinned));
    el.addEventListener("focus", () => setHot(n));
    el.addEventListener("blur", () => setHot(pinned));
    if (el.classList.contains("dot")) {
      el.addEventListener("click", () => {
        pinned = pinned === n ? null : n;
        setHot(pinned ?? n);
        const row = $(`.ev[data-n="${n}"]`, root);
        if (row && pinned) row.scrollIntoView({ block: "nearest", behavior: reducedMotion() ? "auto" : "smooth" });
      });
    }
  });
}

// --- Confidence ------------------------------------------------------------------

const TONE = { High: "high", Medium: "medium", Low: "low", Unknown: "unknown" };

function toneOf(card) {
  return TONE[card.confidence_label] || "unknown";
}

function scoreHtml(card) {
  const rows = card.score_breakdown.map((s, i) => `
    <li class="score-row" style="--i:${i}">
      <span class="score-rule">${esc(s.rule)}</span>
      <span class="score-pts ${s.points >= 0 ? "pos" : "neg"}">${esc(fmtSigned(s.points, 0, ""))}</span>
    </li>`).join("");
  const sum = card.score_breakdown.reduce((acc, s) => acc + Number(s.points), 0);
  const score = Math.max(0, Math.min(100, Number(card.confidence_score)));
  const tone = toneOf(card);
  const n = card.score_breakdown.length;
  return `<ol class="score-rows">${rows || `<li class="score-row score-none">No scoring rules fired.</li>`}</ol>
    <div class="total" style="--i:${n}">
      <div class="total-bar" role="img" aria-label="Total ${esc(score)} out of 100">
        <span class="total-fill" style="--w:${score}"></span>
        <span class="total-text">Total</span>
        <span class="total-num" data-count="${esc(score)}">${esc(score)}</span>
      </div>
      <div class="gauge tone-${tone}" role="img" aria-label="Confidence ${esc(card.confidence_label)}, ${esc(score)} out of 100">
        <svg viewBox="0 0 120 68" aria-hidden="true" focusable="false">
          <path class="gauge-track" d="M10 62 A50 50 0 0 1 110 62" pathLength="100"></path>
          <path class="gauge-value" d="M10 62 A50 50 0 0 1 110 62" pathLength="100" data-score="${esc(score)}"></path>
        </svg>
        <span class="gauge-num">${esc(score)}</span>
        <span class="gauge-label">${esc(card.confidence_label)}</span>
      </div>
    </div>
    ${sum !== score ? `<p class="score-note">The rules add up to ${esc(sum)}; the score is limited to ${esc(score)} by the 0–100 range and the hard gates.</p>` : ""}`;
}

/** Count the total up, fill the bar and sweep the gauge once the rows have slid in. */
function playScore(root, animate) {
  const num = $(".total-num", root);
  const fill = $(".total-fill", root);
  const gauge = $(".gauge-value", root);
  if (!num) return;
  const target = Number(num.dataset.count);
  const finish = () => {
    num.textContent = String(target);
    fill.classList.add("is-full");
    gauge.style.strokeDashoffset = String(100 - target);
  };
  if (!animate) { finish(); return; }
  num.textContent = "0";
  const rows = $$(".score-row", root).length;
  const delay = 350 + rows * 70;
  const run = state.run;
  setTimeout(() => {
    if (run !== state.run || !num.isConnected) return;
    fill.classList.add("is-full");
    gauge.style.strokeDashoffset = String(100 - target);
    const t0 = performance.now();
    const step = (now) => {
      if (!num.isConnected) return;
      const p = Math.min(1, (now - t0) / 400);
      num.textContent = String(Math.round(target * (1 - Math.pow(1 - p, 3))));
      if (p < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }, delay);
}

// --- Result card -------------------------------------------------------------------

function pillText(card) {
  const label = String(card.confidence_label).toUpperCase();
  return card.confidence_label === "Unknown" ? label : `${label} · ${card.confidence_score} / 100`;
}

function setAsideText(items) {
  const rejected = items.filter((it) => it.status === "rejected");
  if (!rejected.length) return "nothing";
  return rejected.map((it) => `#${it.n} ${it.e.source_name} (${it.reason})`).join("; ");
}

function resultCardHtml(card, items, live = null) {
  const tone = toneOf(card);
  const unknown = card.confidence_label === "Unknown";
  const marketLeads = (card.leading_explanation || "").startsWith(MARKET_PREFIX);
  const alternatives = card.alternatives.length ? card.alternatives.join("; ") : "none";
  const limits = card.limits.length
    ? `<ul class="rc-limits" aria-label="Limits">${card.limits.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>`
    : "";
  const head = `<div class="rc-head">
      <span class="label-pill tone-${tone}">${esc(pillText(card))}</span>
      ${live ? `<span class="tag tag-live" title="Sherlock searched TinyFish live">LIVE</span>
        <span class="tag">${esc(liveCallsText(live))}</span>` : ""}
      ${marketLeads ? `<span class="tag tag-market">Market-wide move</span>` : ""}
    </div>`;

  if (unknown) {
    const raw = (card.explanation_text || "").trim();
    const text = raw.startsWith(UNKNOWN_TEXT) ? raw : `${UNKNOWN_TEXT}.${raw ? ` ${raw}` : ""}`;
    return `<section class="result-card tone-${tone} reveal" style="--d:520ms" aria-labelledby="rc-title">
      ${head}
      <h2 class="rc-title" id="rc-title">And when the honest answer is unknown</h2>
      <p class="rc-explanation">${esc(text)}</p>
      <p class="rc-line"><strong>Alternatives:</strong> ${esc(alternatives)}</p>
      <p class="rc-line"><strong>Set aside:</strong> ${esc(setAsideText(items))}</p>
      <p class="rc-foot">The card shows what was searched, what was found and why each candidate failed.</p>
      ${limits}
    </section>`;
  }
  return `<section class="result-card tone-${tone} reveal" style="--d:520ms" aria-labelledby="rc-title">
    ${head}
    <p class="rc-lead" id="rc-title"><strong>Leading explanation:</strong> ${esc(card.leading_explanation || UNKNOWN_TEXT)}</p>
    <p class="rc-line"><strong>Alternatives:</strong> ${esc(alternatives)}</p>
    <p class="rc-line"><strong>Set aside:</strong> ${esc(setAsideText(items))}</p>
    ${card.explanation_text ? `<details class="rc-full" ${state.mode === "expert" ? "open" : ""}>
      <summary>Full explanation</summary>
      <p>${esc(card.explanation_text)}</p>
    </details>` : ""}
    ${limits}
  </section>`;
}

function summaryText(move) {
  const pct = Number(move.pct_change);
  const verb = pct < 0 ? "fell" : pct > 0 ? "rose" : "was flat";
  const size = pct === 0 ? "" : ` ${Math.abs(pct).toFixed(1)}%`;
  const start = new Date(move.move_start_at);
  return `${move.ticker} ${verb}${size} in ${move.window_minutes} minutes from ${timeFmt.format(start)} IST on ` +
    `${dateFmt.format(start)}, on ${Number(move.volume_multiple).toFixed(1)}× normal volume, while its sector moved ` +
    `${fmtSigned(move.sector_pct_change, 1)} and the index ${fmtSigned(move.index_pct_change, 1)}.`;
}

function actionBarHtml(card, live = null) {
  const modeToggle = `<div class="mode-toggle" role="group" aria-label="Detail level">
      <button type="button" data-mode="plain" aria-pressed="${state.mode === "plain"}">Plain</button>
      <button type="button" data-mode="expert" aria-pressed="${state.mode === "expert"}">Expert</button>
    </div>`;
  if (live) {
    // Receipt and Challenge This rebuild the Card from recorded data, so they would not match a live result.
    return `<div class="action-bar reveal" style="--d:600ms" role="group" aria-label="Investigation actions">
      ${modeToggle}
      <div class="action-buttons">
        <p class="live-note">Evidence Receipt and Challenge This work on recorded investigations only.</p>
        <button type="button" class="btn btn-secondary" data-action="live">Run another live investigation</button>
      </div>
    </div>`;
  }
  return `<div class="action-bar reveal" style="--d:600ms" role="group" aria-label="Investigation actions">
    ${modeToggle}
    <div class="action-buttons">
      <button type="button" class="btn btn-secondary" data-action="receipt" data-ticker="${esc(card.ticker)}"
        aria-label="Evidence Receipt: download JSON and open a printable page">Evidence Receipt</button>
      <button type="button" class="btn btn-primary" data-action="challenge" data-ticker="${esc(card.ticker)}">
        <span class="spinner" aria-hidden="true"></span><span class="btn-text">Challenge This</span></button>
    </div>
  </div>
  <div id="skeptic-banner" class="skeptic-slot" role="status" aria-live="polite"></div>`;
}

function renderResult(card, { animate, challenged = false, live = null } = {}) {
  const items = numberedEvidence(card);
  const axis = axisRange(card, items);
  const name = stockName(card.ticker);
  const view = $("#view");
  const trigger = state.trigger && state.trigger.ticker === card.ticker ? state.trigger.html : "";
  view.innerHTML = `
    ${trigger}
    ${stripHtml(card.ticker, { done: true, challenged, counts: countsByInvestigator(card), live })}
    <div class="result ${animate ? "animate" : ""}">
      <div class="summary reveal" style="--d:0ms">
        <h2 class="summary-ticker">${esc(card.ticker)}${name ? ` <span class="summary-name">${esc(name)}</span>` : ""}</h2>
        <p class="summary-text">${esc(summaryText(card.move))}</p>
      </div>
      <div class="panels">
        <section class="panel evidence-panel reveal" style="--d:90ms" aria-labelledby="ev-title">
          <h3 class="panel-title" id="ev-title">Only evidence public before the move can be a cause</h3>
          <div class="time-frame">
            ${chartHtml(card, axis)}
            ${axisHtml(card, items, axis)}
          </div>
          <p class="chart-note">${live
            ? "Price shape is illustrative. The move size, window and volume were entered in the live form, not read from market data."
            : "Price shape is illustrative. The move size, window and volume are real; no prices are shown."}</p>
          ${evidenceListHtml(items)}
        </section>
        <section class="panel score-panel reveal" style="--d:180ms" aria-labelledby="score-title">
          <h3 class="panel-title" id="score-title">How the confidence is reached</h3>
          ${scoreHtml(card)}
        </section>
      </div>
      ${resultCardHtml(card, items, live)}
      ${actionBarHtml(card, live)}
    </div>`;
  layoutAxis();
  wireEvidenceLinks(view);
  playScore(view, animate);
}

// --- Views -----------------------------------------------------------------------------

function renderHome() {
  $("#view").innerHTML = `<section class="home">
    <h2 class="home-title">One investigation, start to finish</h2>
    <p class="home-text">Choose a stock marked <span class="home-flag">Move flagged</span>. Four investigators gather
      evidence, only what was public before the move can count as a cause, and every point of the confidence
      score is shown.</p>
    <ol class="home-steps">
      <li><span class="home-step-n">1</span>Investigators search macro, company and crowd sources.</li>
      <li><span class="home-step-n">2</span>Evidence is placed on a timeline against the move.</li>
      <li><span class="home-step-n">3</span>Rules add and subtract points to reach a confidence.</li>
    </ol>
    <button type="button" class="btn btn-primary" data-ticker-go="ACMEINFRA">Start with ACMEINFRA</button>
  </section>`;
}

function renderMessage(title, body, { retry = false, tone = "neutral" } = {}) {
  $("#view").innerHTML = `<section class="message tone-${tone}" role="alert">
    <h2 class="message-title">${esc(title)}</h2>
    <p class="message-text">${esc(body)}</p>
    <div class="message-actions">
      ${retry ? `<button type="button" class="btn btn-primary" data-action="retry">Try again</button>` : ""}
      <a class="btn btn-secondary" href="#/">Back to My Stocks</a>
    </div>
  </section>`;
}

function renderFailure(ticker, err) {
  if (err.status === 404) {
    renderMessage("No investigation available",
      `${ticker} has no flagged move on record, so there is nothing to investigate.`);
  } else if (err.status === 0) {
    renderMessage("We could not reach StockLens",
      "The server did not respond. Check that it is running, then try again.", { retry: true, tone: "error" });
  } else {
    renderMessage("Something went wrong with this investigation",
      `The server said: ${err.message}. You can try again.`, { retry: true, tone: "error" });
  }
}

async function investigate(ticker) {
  const run = ++state.run;
  state.ticker = ticker;
  state.card = null;
  state.trigger = null;
  state.live = null;
  markSelected();
  await stocksReady;
  if (run !== state.run) return;
  if (findStock(ticker)?.alert === false) {
    // Known stock without a flagged move: say so without asking the server.
    renderFailure(ticker, { status: 404 });
    return;
  }
  const cardRequest = api("POST", `/investigate/${encodeURIComponent(ticker)}`)
    .then((card) => ({ card }), (error) => ({ error }));
  await healthReady;
  if (run !== state.run) return;

  const animate = !reducedMotion();
  const started = performance.now();
  if (animate) {
    $("#view").innerHTML = stripHtml(ticker);
  } else {
    $("#view").innerHTML = `<p class="loading">Loading the investigation for ${esc(ticker)}…</p>`;
  }

  const { card, error } = await cardRequest;
  if (run !== state.run) return;
  if (error) { renderFailure(ticker, error); return; }
  if (animate && !(await playStrip(run, countsByInvestigator(card), started))) return;

  state.card = card;
  renderResult(card, { animate });
}

// --- Simulate move (trigger engine -> investigation) ---------------------------------

function fmtNumber(value) {
  return String(Number(value));
}

/** The "Trigger engine" panel: checking (sim = null), flagged, or no unusual move. */
function triggerPanelHtml(ticker, sim) {
  const head = (badge) => `<div class="trigger-head">
      <h2 class="trigger-title">Trigger engine <span class="strip-ticker">${esc(ticker)}</span></h2>
      <span class="trigger-badge">${esc(badge)}</span>
    </div>`;
  if (!sim) {
    return `<section class="trigger-panel is-checking" aria-label="Trigger engine">
      ${head("price and volume rules, no AI")}
      <p class="trigger-text">Checking the recorded price and volume against the thresholds…</p>
    </section>`;
  }
  const t = sim.thresholds || {};
  const limits = `${fmtNumber(t.min_pct_change)}% and ${fmtNumber(t.min_volume_multiple)}×`;
  if (!sim.flagged || !sim.move) {
    return `<section class="trigger-panel is-quiet" aria-label="Trigger engine">
      ${head("not flagged")}
      <p class="trigger-text">No unusual move: no investigation started</p>
      <p class="trigger-sub">A move is flagged only when it reaches ${esc(limits)} volume together.</p>
    </section>`;
  }
  const m = sim.move;
  const text = `${fmtSigned(m.pct_change, 1)} in ${m.window_minutes} min, ` +
    `${Number(m.volume_multiple).toFixed(1)}× volume: passes thresholds of ${limits}`;
  return `<section class="trigger-panel is-flagged" aria-label="Trigger engine">
    ${head("move flagged")}
    <p class="trigger-text">${esc(text)}</p>
    <p class="trigger-sub">Move began ${esc(fmtWhen(m.move_start_at))}. Investigation started.</p>
  </section>`;
}

async function simulate(ticker) {
  if (!ticker) { toast("Select a stock first."); return; }
  const run = ++state.run;
  state.ticker = ticker;
  state.card = null;
  state.trigger = null;
  state.live = null;
  state.simulating = run;
  markSelected();
  try {
    const animate = !reducedMotion();
    const started = performance.now();
    $("#view").innerHTML = triggerPanelHtml(ticker, null);
    const request = api("POST", `/simulate/${encodeURIComponent(ticker)}`)
      .then((sim) => ({ sim }), (error) => ({ error }));
    await healthReady;
    const { sim, error } = await request;
    if (run !== state.run) return;
    if (error) {
      if (error.status === 404) renderMessage("The trigger engine cannot run", error.message);
      else renderFailure(ticker, error);
      return;
    }

    const panel = triggerPanelHtml(ticker, sim);
    $("#view").innerHTML = panel;
    if (!sim.flagged || !sim.card) return;
    state.trigger = { ticker, html: panel };

    if (animate) {
      await sleep(started + TRIGGER_PAUSE_MS - performance.now());
      if (run !== state.run) return;
      $("#view").innerHTML = panel + stripHtml(ticker);
      if (!(await playStrip(run, countsByInvestigator(sim.card), performance.now()))) return;
    }
    state.card = sim.card;
    renderResult(sim.card, { animate });
  } finally {
    if (state.simulating === run) state.simulating = null;
    updateSimulateButton();
  }
}

// --- Live investigation (Sherlock live via TinyFish) -------------------------------------

const LIVE_MAX_POLL_FAILURES = 3;

/** Current time in IST as a datetime-local value, e.g. "2026-10-24T10:30". */
function istNowLocal() {
  return new Date(Date.now() + 330 * 60000).toISOString().slice(0, 16);
}

function liveField(name, label, input, hint = "") {
  return `<label class="lf-field" for="lf-${name}">
      <span class="lf-label">${esc(label)}</span>
      ${input}
      ${hint ? `<span class="lf-hint">${esc(hint)}</span>` : ""}
    </label>`;
}

function liveFormHtml(status) {
  const num = (name, step, extra = "") =>
    `<input id="lf-${name}" name="${name}" type="number" step="${step}" required inputmode="decimal" ${extra}>`;
  const calls = status && status.max_calls ? `up to ${status.max_calls} calls per run` : "a few calls per run";
  return `<section class="live-panel" aria-labelledby="live-title">
    <h2 class="live-title" id="live-title"><span class="tag tag-live">LIVE</span> Run a live investigation</h2>
    <p class="live-intro">Sherlock searches NSE and BSE filings live with TinyFish and reads one filing with its
      official timestamp. We have no live market data, so enter the move yourself; the Card will say so.</p>
    <p class="live-warning" role="note"><strong>This uses TinyFish credits</strong> (${esc(calls)}). Repeating the
      same run is served from the cache and is free.</p>
    <form class="live-form" id="live-form" novalidate>
      <div class="lf-grid">
        ${liveField("company", "Company", `<input id="lf-company" name="company" type="text" required maxlength="100" autocomplete="off" placeholder="e.g. Infosys">`)}
        ${liveField("ticker", "NSE ticker", `<input id="lf-ticker" name="ticker" type="text" required maxlength="20" pattern="[A-Za-z0-9&._\\-]+" autocomplete="off" placeholder="e.g. INFY">`)}
        ${liveField("move_start_at", "Move start (IST)", `<input id="lf-move_start_at" name="move_start_at" type="datetime-local" required value="${esc(istNowLocal())}">`)}
        ${liveField("pct_change", "Move %", num("pct_change", "0.1", 'placeholder="-5.8"'), "negative for a fall")}
        ${liveField("volume_multiple", "Volume multiple", num("volume_multiple", "0.1", 'min="0" placeholder="3.1"'), "× normal volume")}
        ${liveField("sector_pct_change", "Sector %", num("sector_pct_change", "0.1", 'placeholder="-0.7"'))}
        ${liveField("index_pct_change", "Index %", num("index_pct_change", "0.1", 'placeholder="-0.2"'))}
      </div>
      <p class="lf-error" id="lf-error" role="alert" hidden></p>
      <div class="lf-actions">
        <button type="submit" class="btn btn-primary" id="lf-submit">Start live investigation</button>
        <a class="btn btn-secondary" href="#/">Cancel</a>
      </div>
    </form>
  </section>`;
}

async function openLive() {
  const run = ++state.run;
  state.ticker = null;
  state.card = null;
  state.trigger = null;
  state.live = null;
  markSelected();
  $("#view").innerHTML = `<p class="loading">Checking whether live investigations are available…</p>`;
  let status;
  try {
    status = await api("GET", "/live/status");
  } catch (err) {
    if (run === state.run) {
      renderMessage("Could not check live mode", `${err.message} You can try again.`,
        { retry: true, tone: "error" });
    }
    return;
  }
  if (run !== state.run) return;
  if (!status.enabled) {
    renderMessage("Live investigation is off", status.message ||
      "Live mode is not switched on for this server. The recorded demos still work.");
    return;
  }
  if (status.active_job_id) {
    toast("A live investigation is already running. Showing its progress.");
    pollLive(status.active_job_id, run, "", status);
    return;
  }
  $("#view").innerHTML = liveFormHtml(status);
  $("#lf-company").focus();
  $("#live-form").dataset.run = String(run);
  state.liveStatus = status;
}

function showFormError(message) {
  const el = $("#lf-error");
  if (!el) return;
  el.textContent = message;
  el.hidden = !message;
}

async function submitLive(form) {
  const run = Number(form.dataset.run);
  if (run !== state.run) return;
  const value = (name) => form.elements[name].value.trim();
  const required = ["company", "ticker", "move_start_at", "pct_change", "volume_multiple",
    "sector_pct_change", "index_pct_change"];
  const missing = required.filter((name) => !value(name));
  if (missing.length) { showFormError("Please fill in every field."); return; }
  const invalid = [...form.elements].find((el) => el.willValidate && !el.checkValidity());
  if (invalid) {
    showFormError(`Please check "${invalid.closest("label")?.querySelector(".lf-label")?.textContent || invalid.name}".`);
    return;
  }
  const payload = {
    company: value("company"),
    ticker: value("ticker").toUpperCase(),
    move_start_at: `${value("move_start_at")}:00+05:30`,
    pct_change: Number(value("pct_change")),
    volume_multiple: Number(value("volume_multiple")),
    sector_pct_change: Number(value("sector_pct_change")),
    index_pct_change: Number(value("index_pct_change")),
  };
  const button = $("#lf-submit");
  button.disabled = true;
  button.textContent = "Starting…";
  showFormError("");
  try {
    const { job_id: jobId } = await api("POST", "/live/start", payload);
    if (run !== state.run) return;
    pollLive(jobId, run, payload.ticker, state.liveStatus);
  } catch (err) {
    if (run !== state.run) return;
    button.disabled = false;
    button.textContent = "Start live investigation";
    if (err.status === 403) {
      renderMessage("Live investigation is off", err.message);
    } else {
      showFormError(err.message);
    }
  }
}

/** Poll GET /live/{job_id} until it is done or failed, showing the real steps in the strip. */
async function pollLive(jobId, run, ticker, status) {
  const every = Math.max(1, Number(status && status.poll_seconds) || 2) * 1000;
  const maxCalls = status && status.max_calls;
  let failures = 0;
  while (run === state.run) {
    let job;
    try {
      job = await api("GET", `/live/${encodeURIComponent(jobId)}`);
      failures = 0;
    } catch (err) {
      if (run !== state.run) return;
      failures += 1;
      if (err.status === 404 || failures >= LIVE_MAX_POLL_FAILURES) {
        renderMessage("Lost track of the live investigation",
          `${err.message} Press "Run live investigation" to check on it again.`, { tone: "error" });
        return;
      }
      await sleep(every);
      continue;
    }
    if (run !== state.run) return;
    const live = { steps: job.steps || [], calls_used: job.calls_used, agent_calls: job.agent_calls,
      max_calls: maxCalls, others: job.others };
    const shownTicker = job.card ? job.card.ticker : job.ticker || ticker;

    if (job.status === "done" && job.card) {
      state.card = job.card;
      state.live = { ...live, ticker: job.card.ticker };
      renderResult(job.card, { animate: !reducedMotion(), live: state.live });
      return;
    }
    if (job.status === "failed") {
      renderMessage("The live investigation failed",
        `${job.error || "Unknown error."} No evidence was invented; you can try again.`, { tone: "error" });
      return;
    }
    $("#view").innerHTML = stripHtml(shownTicker, { live });
    await sleep(every);
  }
}

// --- Actions -----------------------------------------------------------------

function downloadReceipt(ticker) {
  const t = encodeURIComponent(ticker);
  window.open(`/receipt/${t}?format=html`, "_blank", "noopener");
  const a = document.createElement("a");
  a.href = `/receipt/${t}`;
  a.download = `stocklens-receipt-${ticker}.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/** What the Skeptic found in a challenged Card: counter-evidence, else "none found". */
function skepticBannerHtml(card, before) {
  const counter = card.alternatives.filter((a) => a.startsWith(COUNTER_PREFIX)).map((a) => a.slice(COUNTER_PREFIX.length));
  const counterSet = new Set(counter);
  const hints = card.accepted_evidence.filter((e) => e.investigator === "skeptic" && !counterSet.has(e.claim));
  const changed = before &&
    (before.confidence_label !== card.confidence_label || before.confidence_score !== card.confidence_score);
  const body = counter.length
    ? `<p class="sk-main">Counter-evidence found: ${esc(plural(counter.length, "item"))} argue against this cause.</p>
       <ul class="sk-list">${counter.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>`
    : `<p class="sk-main">None found – the answer survived the challenge.</p>`;
  const hintLine = hints.length
    ? `<p class="sk-sub">Earlier hint already counted: ${hints.map((e) => esc(e.claim)).join("; ")}</p>`
    : "";
  const change = changed
    ? `<p class="sk-change">Confidence changed: ${esc(pillText(before))} → ${esc(pillText(card))}</p>`
    : `<p class="sk-sub">Confidence unchanged: ${esc(pillText(card))}.</p>`;
  return `<div class="skeptic-banner ${counter.length ? "has-counter" : "survived"}">
    <h3 class="sk-title">Skeptic result</h3>${body}${hintLine}${change}</div>`;
}

async function challenge(ticker, button) {
  const run = state.run;
  const before = state.card;
  button.disabled = true;
  button.classList.add("is-busy");
  $(".btn-text", button).textContent = "Challenging…";
  const chip = $('.inv[data-inv="skeptic"]');
  if (chip) {
    chip.className = "inv is-searching";
    $(".inv-state", chip).textContent = "searching…";
  }
  try {
    // /challenge returns a full Card (same shape as /investigate) with the Skeptic included.
    const card = await api("POST", `/challenge/${encodeURIComponent(ticker)}`);
    if (run !== state.run) return;
    state.card = card;
    renderResult(card, { animate: !reducedMotion(), challenged: true });
    const slot = $("#skeptic-banner");
    slot.innerHTML = skepticBannerHtml(card, before);
    slot.scrollIntoView({ block: "nearest", behavior: reducedMotion() ? "auto" : "smooth" });
  } catch (err) {
    if (run !== state.run) return;
    button.disabled = false;
    button.classList.remove("is-busy");
    $(".btn-text", button).textContent = "Challenge This";
    if (chip) {
      chip.className = "inv is-dimmed";
      $(".inv-state", chip).textContent = "runs on Challenge This";
    }
    $("#skeptic-banner").innerHTML = `<div class="skeptic-banner is-error">
      <h3 class="sk-title">Challenge failed</h3>
      <p class="sk-main">${esc(err.message)} The result above is unchanged. Try again in a moment.</p></div>`;
  }
}

// --- Routing -----------------------------------------------------------------

function go(ticker) {
  const hash = `#/stock/${encodeURIComponent(ticker)}`;
  if (location.hash === hash) route(); // same stock again: replay the investigation
  else location.hash = hash;
}

function goLive() {
  if (location.hash === "#/live") route(); // already there: start over
  else location.hash = "#/live";
}

function route() {
  const match = location.hash.match(/^#\/stock\/([^/]+)$/);
  if (location.hash === "#/live") {
    document.title = "Live investigation · StockLens";
    openLive();
    $("#investigation").focus({ preventScroll: true });
  } else if (match) {
    let ticker;
    try { ticker = decodeURIComponent(match[1]).toUpperCase(); } catch { ticker = match[1].toUpperCase(); }
    document.title = `${ticker} · StockLens`;
    investigate(ticker);
    const main = $("#investigation");
    main.focus({ preventScroll: true });
    if (main.getBoundingClientRect().top > window.innerHeight * 0.6) {
      main.scrollIntoView({ block: "start", behavior: reducedMotion() ? "auto" : "smooth" });
    }
  } else {
    state.run++;
    state.ticker = null;
    state.card = null;
    state.trigger = null;
    state.live = null;
    document.title = "StockLens";
    markSelected();
    renderHome();
  }
}

document.addEventListener("click", (event) => {
  const modeBtn = event.target.closest(".mode-toggle button");
  if (modeBtn) {
    state.mode = modeBtn.dataset.mode;
    saveMode(state.mode);
    applyMode();
    return;
  }
  const stock = event.target.closest("button[data-ticker], [data-ticker-go]");
  if (stock && !stock.dataset.action) {
    go(stock.dataset.ticker || stock.dataset.tickerGo);
    return;
  }
  const action = event.target.closest("[data-action]");
  if (!action) return;
  if (action.dataset.action === "receipt") {
    downloadReceipt(action.dataset.ticker);
    toast("Receipt downloaded. The printable version opened in a new tab.");
  } else if (action.dataset.action === "challenge") {
    challenge(action.dataset.ticker, action);
  } else if (action.dataset.action === "simulate") {
    simulate(state.ticker);
  } else if (action.dataset.action === "live") {
    goLive();
  } else if (action.dataset.action === "retry") {
    route();
  } else if (action.dataset.action === "reload-stocks") {
    stocksReady = loadStocks();
  }
});

document.addEventListener("submit", (event) => {
  if (event.target.id === "live-form") {
    event.preventDefault();
    submitLive(event.target);
  }
});

document.addEventListener("keydown", (event) => {
  if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) return;
  const target = event.target;
  if (target.closest && target.closest("input, textarea, select, [contenteditable]")) return;
  const index = ["1", "2", "3"].indexOf(event.key);
  if (index !== -1) {
    event.preventDefault();
    go(COMPARE_TICKERS[index]);
  }
});

let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(layoutAxis, 120);
});

window.addEventListener("hashchange", route);
applyMode();
let stocksReady = loadStocks();
route();
