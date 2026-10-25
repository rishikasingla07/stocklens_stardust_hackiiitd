// StockLens frontend logic (no build step, no external libraries).
// All server text goes through esc() before it is inserted into the page.

"use strict";

const UNKNOWN_TEXT = "Cause not confidently identified";
const MARKET_PREFIX = "Market explanation leads";
const TIER_NAMES = {
  1: "Tier 1 · Primary (exchange filing, official release)",
  2: "Tier 2 · Reputable media",
  3: "Tier 3 · Brokerage notes / blogs / social",
};
const INVESTIGATORS = [
  { id: "atlas", name: "Atlas", role: "macro" },
  { id: "sherlock", name: "Sherlock", role: "company" },
  { id: "echo", name: "Echo", role: "crowd" },
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
  user: null, // {account_id} once logged in
  stocks: null, // GET /watchlist body: {tickers, stocks: [rows]}
  catalog: null, // GET /catalog rows (loaded when the picker first opens)
  health: null, // {mode} or null if the server could not be reached
  ticker: null,
  card: null,
  run: 0, // bumps on every navigation so stale responses are dropped
  trigger: null, // {ticker, html}: trigger panel kept above a simulated result
  simulating: null, // run number of the Simulate move request in flight
  live: null, // {ticker, steps, calls_used, agent_calls, max_calls, others}: the live result on screen
  liveStatus: null, // last GET /live/status body (poll interval, call budget)
  history: [], // GET /history rows, newest first (no card_json)
  saved: null, // the history row whose saved Card is on screen, if any
  historyReturnFocus: null, // element to refocus when the History drawer closes
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
    // A 401 outside /auth means the session ended (logged out elsewhere or expired).
    if (res.status === 401 && !path.startsWith("/auth/") && path !== "/me") sessionLost();
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
    $("#auth-demo").hidden = state.health.mode !== "replay"; // the demo account exists in replay mode only
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

const NO_DEMO_COVERAGE = "No investigation available in this demo";

/** One watchlist card. Only demo-coverage stocks have a (recorded) price; the rest show their sector. */
function stockCard(stock) {
  const priced = stock.price !== null && stock.price !== undefined;
  const quote = priced
    ? `<span class="sc-quote">
        <span class="sc-price">${esc(fmtPrice(stock.price))}</span>
        <span class="sc-change ${signClass(stock.change)}">${esc(fmtSigned(stock.change_pct))}</span>
      </span>`
    : `<span class="sc-sector">${esc(stock.sector || "")}</span>`;
  const body = `
    <span class="sc-ticker">${esc(stock.ticker)}</span>
    <span class="sc-name">${esc(stock.name)}</span>
    ${quote}`;
  if (stock.alert) {
    return `<li><button type="button" class="stock-card is-flagged" data-ticker="${esc(stock.ticker)}"
        aria-label="Investigate ${esc(stock.ticker)}, ${esc(stock.name)}, ${esc(fmtSigned(stock.change_pct))} today, move flagged">
      ${body}
      <span class="sc-foot"><span class="badge-flag">Move flagged</span></span>
      ${latestSlot(stock.ticker)}
    </button></li>`;
  }
  const note = stock.demo_coverage ? "No flagged move" : NO_DEMO_COVERAGE;
  return `<li><button type="button" class="stock-card is-quiet" data-ticker="${esc(stock.ticker)}"
      aria-label="${esc(stock.ticker)}, ${esc(stock.name)}: ${esc(note)}">
      ${body}
      <span class="sc-foot"><span class="sc-none">${esc(note)}</span></span>
      ${latestSlot(stock.ticker)}
    </button></li>`;
}

// --- Compare cases (labels from the real results) ------------------------------

/** "High 100", "Unknown", "Medium 50 · Market-wide": the label a recorded case really gets. */
function caseLabel(c) {
  const base = c.label === "Unknown" ? "Unknown" : `${c.label} ${c.score}`;
  return c.market_leads ? `${base} · Market-wide` : base;
}

function caseDesc(c) {
  if (!c.leading_explanation) return UNKNOWN_TEXT;
  // A market-led explanation reads "Market explanation leads: ... Cause: ..."; show the cause.
  const cause = c.leading_explanation.split(" Cause: ")[1];
  return c.market_leads && cause ? `Market-wide: ${cause}` : c.leading_explanation;
}

async function loadCases() {
  let cases = [];
  try {
    cases = await api("GET", "/cases");
  } catch {
    // fall through: every card says its result is unavailable
  }
  const byTicker = Object.fromEntries(cases.map((c) => [c.ticker, c]));
  $$(".compare-card[data-ticker]").forEach((el, i) => {
    const c = byTicker[el.dataset.ticker];
    const label = $(".compare-label", el);
    const desc = $(".compare-desc", el);
    el.classList.remove("tone-pending", "tone-high", "tone-medium", "tone-low", "tone-unknown");
    if (!c) {
      el.classList.add("tone-unknown");
      label.textContent = "Result unavailable";
      desc.textContent = "";
      el.setAttribute("aria-label", `Compare case ${i + 1}: ${el.dataset.ticker}`);
      return;
    }
    el.classList.add(`tone-${TONE[c.label] || "unknown"}`);
    label.textContent = caseLabel(c);
    desc.textContent = caseDesc(c);
    el.setAttribute("aria-label", `Compare case ${i + 1}: ${caseLabel(c)}, ${c.ticker}`);
  });
}

// --- History (saved investigations) ------------------------------------------

const SOURCE_LABEL = {
  investigate: "Investigate", simulate: "Simulate move", live: "Live run",
};

function relTime(iso) {
  const seconds = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  return `${days} ${days === 1 ? "day" : "days"} ago`;
}

function savedLabel(row) {
  return row.label === "Unknown" ? "Unknown" : `${row.label} ${row.score}`;
}

function latestSlot(ticker) {
  return `<span class="sc-latest" data-latest-for="${esc(ticker)}" hidden></span>`;
}

/** Fill each stock card's "latest result" chip from state.history. */
function renderLatestChips() {
  const latest = {};
  for (const row of state.history) if (!(row.ticker in latest)) latest[row.ticker] = row;
  $$("[data-latest-for]").forEach((el) => {
    const row = latest[el.dataset.latestFor];
    el.hidden = !row;
    if (!row) return;
    el.className = `sc-latest tone-${TONE[row.label] || "unknown"}`;
    el.textContent = `${savedLabel(row)} – ${relTime(row.created_at)}`;
    el.title = `Latest saved result: ${savedLabel(row)}, ${fmtWhen(row.created_at)} (${SOURCE_LABEL[row.source] || row.source})`;
  });
}

function renderHistoryList() {
  const list = $("#history-list");
  if (!list) return;
  if (!state.history.length) {
    list.innerHTML = `<li class="history-empty">No saved investigations yet. Investigate a flagged stock and it appears here.</li>`;
    return;
  }
  list.innerHTML = state.history.map((row) => `<li>
      <button type="button" class="history-item tone-${TONE[row.label] || "unknown"}" data-action="history-open"
        data-history-id="${esc(row.id)}"
        aria-label="Open saved result: ${esc(row.ticker)}, ${esc(savedLabel(row))}, ${esc(relTime(row.created_at))}, from ${esc(SOURCE_LABEL[row.source] || row.source)}">
        <span class="hi-ticker">${esc(row.ticker)}</span>
        <span class="label-pill hi-label">${esc(row.label === "Unknown" ? "UNKNOWN" : `${row.label.toUpperCase()} · ${row.score}`)}</span>
        <span class="hi-time" title="${esc(fmtWhen(row.created_at))}">${esc(relTime(row.created_at))}</span>
        <span class="tag hi-source">${esc(SOURCE_LABEL[row.source] || row.source)}${row.mode === "live" ? " · live" : ""}</span>
      </button>
    </li>`).join("");
}

async function loadHistory() {
  try {
    state.history = await api("GET", "/history");
  } catch {
    return; // history is a convenience: the rest of the page works without it
  }
  renderLatestChips();
  if (!$("#history-drawer").hidden) renderHistoryList();
}

function openHistory() {
  const drawer = $("#history-drawer");
  state.historyReturnFocus = document.activeElement;
  renderHistoryList();
  drawer.hidden = false;
  $("#history-backdrop").hidden = false;
  $('[data-action="history"]')?.setAttribute("aria-expanded", "true");
  $('[data-action="history-close"]', drawer).focus();
  loadHistory();
}

function closeHistory() {
  const drawer = $("#history-drawer");
  if (drawer.hidden) return;
  drawer.hidden = true;
  $("#history-backdrop").hidden = true;
  $('[data-action="history"]')?.setAttribute("aria-expanded", "false");
  const back = state.historyReturnFocus;
  state.historyReturnFocus = null;
  if (back && back.isConnected) back.focus();
}

/** Show a saved Card exactly as stored. Nothing is re-run. */
async function openSaved(id) {
  const run = ++state.run;
  state.trigger = null;
  state.live = null;
  state.card = null;
  $("#view").innerHTML = `<p class="loading">Opening saved result…</p>`;
  let card;
  try {
    card = await api("GET", `/history/${encodeURIComponent(id)}`);
  } catch (err) {
    if (run !== state.run) return;
    if (err.status === 404) {
      renderMessage("Saved result not found", "This saved investigation no longer exists.");
    } else {
      renderFailure("this saved result", err);
    }
    return;
  }
  if (run !== state.run) return;
  if (!state.history.some((row) => String(row.id) === String(id))) await loadHistory();
  if (run !== state.run) return;
  const row = state.history.find((r) => String(r.id) === String(id)) || { id, ticker: card.ticker };
  state.saved = row;
  state.card = card;
  state.ticker = card.ticker;
  document.title = `${card.ticker} (saved) · StockLens`;
  markSelected();
  renderResult(card, { animate: false, saved: row });
}

function renderWatchlist() {
  const stocks = (state.stocks && state.stocks.stocks) || [];
  $("#watchlist-list").innerHTML = stocks.length
    ? stocks.map(stockCard).join("")
    : `<li class="sc-empty">Your watchlist is empty. Add the stocks you hold or follow.
        <button type="button" class="btn btn-primary btn-small" data-action="picker">Add stocks</button></li>`;
  $(".stocks-edit").textContent = stocks.length ? "Edit list" : "Add stocks";
  renderLatestChips();
  markSelected();
}

async function loadStocks() {
  try {
    state.stocks = await api("GET", "/watchlist");
    renderWatchlist();
  } catch (err) {
    if (err.status === 401) return; // back on the login screen
    $("#watchlist-list").innerHTML = `<li class="sc-error">Could not load your stocks: ${esc(err.message)}
      <button type="button" class="btn btn-small" data-action="reload-stocks">Try again</button></li>`;
  }
}

function findStock(ticker) {
  if (!state.stocks) return null;
  return (state.stocks.stocks || []).find((s) => s.ticker === ticker) || null;
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
  const uncovered = findStock(state.ticker)?.demo_coverage === false;
  btn.disabled = !state.ticker || busy || uncovered;
  btn.classList.toggle("is-busy", busy);
  btn.textContent = busy ? "Simulating…" : "Simulate move";
  btn.title = !state.ticker ? "Select a stock first"
    : uncovered ? `${NO_DEMO_COVERAGE} for ${state.ticker}`
      : `Run the trigger engine on ${state.ticker}'s recorded prices`;
  const hint = $("#simulate-hint");
  if (hint) {
    const text = busy ? "Checking price and volume…"
      : !state.ticker ? "Select a stock, then simulate its move"
        : uncovered ? `${NO_DEMO_COVERAGE} for ${state.ticker}`
          : `Run the trigger engine on ${state.ticker}'s recorded prices`;
    if (hint.textContent !== text) hint.textContent = text; // only changes are announced
  }
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
  if (e.first_public_at && Date.parse(e.first_public_at) >= start) {
    return "corroborating";
  }
  return "accepted";
}

const STATUS_LABEL = { accepted: "Accepted", corroborating: "Corroborating", rejected: "Set aside" };

function keptReason(item) {
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

function stripHtml(ticker, { done = false, counts = null, live = null } = {}) {
  const chips = INVESTIGATORS.map((inv) => {
    const n = counts ? counts[inv.id] : null;
    let cls = "is-searching";
    let text = "searching…";
    if (live) {
      [cls, text] = liveChip(inv, done, n, live);
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

function resultCardHtml(card, items, live = null, saved = null) {
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
      ${saved ? `<span class="tag tag-saved">Saved result</span>` : ""}
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

function actionBarHtml(card, live = null, saved = null) {
  const modeToggle = `<div class="mode-toggle" role="group" aria-label="Detail level">
      <button type="button" data-mode="plain" aria-pressed="${state.mode === "plain"}">Plain</button>
      <button type="button" data-mode="expert" aria-pressed="${state.mode === "expert"}">Expert</button>
    </div>`;
  if (saved) {
    // Evidence Receipt builds a fresh Card, so it would not match this saved one.
    const again = saved.source === "live"
      ? `<button type="button" class="btn btn-secondary" data-action="live">Run another live investigation</button>`
      : `<button type="button" class="btn btn-secondary" data-ticker-go="${esc(card.ticker)}">Run a fresh investigation</button>`;
    return `<div class="action-bar" role="group" aria-label="Investigation actions">
      ${modeToggle}
      <div class="action-buttons">
        <p class="live-note">This is a saved result. Evidence Receipt works on a fresh investigation.</p>
        ${again}
      </div>
    </div>`;
  }
  if (live) {
    // Evidence Receipt rebuilds the Card from recorded data, so it would not match a live result.
    return `<div class="action-bar reveal" style="--d:600ms" role="group" aria-label="Investigation actions">
      ${modeToggle}
      <div class="action-buttons">
        <p class="live-note">Evidence Receipt works on recorded investigations only.</p>
        <button type="button" class="btn btn-secondary" data-action="live">Run another live investigation</button>
      </div>
    </div>`;
  }
  return `<div class="action-bar reveal" style="--d:600ms" role="group" aria-label="Investigation actions">
    ${modeToggle}
    <div class="action-buttons">
      <button type="button" class="btn btn-primary" data-action="receipt" data-ticker="${esc(card.ticker)}"
        aria-label="Evidence Receipt: download JSON and open a printable page">Evidence Receipt</button>
    </div>
  </div>`;
}

function renderResult(card, { animate, live = null, saved = null } = {}) {
  if (!saved) state.saved = null;
  const items = numberedEvidence(card);
  const axis = axisRange(card, items);
  const name = stockName(card.ticker);
  const view = $("#view");
  const trigger = state.trigger && state.trigger.ticker === card.ticker ? state.trigger.html : "";
  const savedBanner = saved ? `<p class="saved-banner" role="note"><span class="tag tag-saved">Saved result</span>
      ${saved.created_at ? `Saved ${esc(fmtWhen(saved.created_at))} (${esc(relTime(saved.created_at))})` : "Saved earlier"}${
      saved.source ? ` from ${esc(SOURCE_LABEL[saved.source] || saved.source)}` : ""}. Shown exactly as stored; nothing was re-run.</p>` : "";
  view.innerHTML = `
    ${savedBanner}
    ${trigger}
    ${stripHtml(card.ticker, { done: true, counts: countsByInvestigator(card), live })}
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
      ${resultCardHtml(card, items, live, saved)}
      ${actionBarHtml(card, live, saved)}
    </div>`;
  layoutAxis();
  wireEvidenceLinks(view);
  playScore(view, animate);
}

// --- Views -----------------------------------------------------------------------------

function renderHome() {
  $("#view").innerHTML = `<section class="home">
    <h2 class="home-title">One investigation, start to finish</h2>
    <p class="home-text">Choose a stock marked <span class="home-flag">Move flagged</span>. Three investigators gather
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

function renderNoCoverage(ticker) {
  const name = stockName(ticker);
  renderMessage(NO_DEMO_COVERAGE,
    `${ticker}${name ? ` (${name})` : ""} is a real NSE stock in your watchlist, but this demo has no recorded ` +
    "evidence or prices for it, so StockLens cannot investigate it here. Try ACMEINFRA, ZENITHPOLY or ORIONPHARMA.");
}

function renderFailure(ticker, err) {
  if (err.status === 401) return; // the login screen is already showing
  if (err.status === 404 && String(err.message || "").startsWith(NO_DEMO_COVERAGE)) {
    renderNoCoverage(ticker);
  } else if (err.status === 404) {
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
  if (findStock(ticker)?.demo_coverage === false) {
    renderNoCoverage(ticker);
    return;
  }
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
  loadHistory();
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
    loadHistory();

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
      loadHistory();
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

// --- Accounts (login, sign-up, logout) -------------------------------------------
// Demo-grade auth. The session is an HttpOnly cookie, so this script never sees the token.

const ACCOUNT_ID_RE = /^[A-Za-z0-9_.-]{3,32}$/;
let appStarted = false;

function setAuthMode(mode) {
  const form = $("#auth-form");
  form.dataset.mode = mode;
  const signup = mode === "signup";
  $("#auth-tab-login").setAttribute("aria-selected", String(!signup));
  $("#auth-tab-signup").setAttribute("aria-selected", String(signup));
  $("#auth-title").textContent = signup ? "Create a StockLens account" : "Log in to StockLens";
  $("#auth-submit").textContent = signup ? "Sign up" : "Log in";
  $("#auth-password").autocomplete = signup ? "new-password" : "current-password";
  showAuthError("");
}

function showAuthError(message) {
  const el = $("#auth-error");
  el.textContent = message;
  el.hidden = !message;
}

/** Show the login screen (optionally with a message) and stop whatever was running. */
function showAuth(message = "") {
  state.run++;
  state.user = null;
  state.stocks = null;
  state.history = [];
  state.ticker = null;
  state.card = null;
  state.trigger = null;
  state.live = null;
  state.saved = null;
  closeHistory();
  const picker = $("#picker");
  if (picker.open) picker.close();
  document.body.classList.add("is-anon");
  document.body.classList.remove("is-booting");
  document.title = "Log in · StockLens";
  // Start the next login at home, so it never re-runs the previous user's page.
  if (location.hash && location.hash !== "#/") window.history.replaceState(null, "", "#/");
  $("#auth-password").value = "";
  showAuthError(message);
  $("#auth-account").focus();
}

function sessionLost() {
  if (state.user) showAuth("Your session has ended. Please log in again.");
}

/** Logged in: fill the header and load everything that needs a user. */
function enterApp(user) {
  state.user = user;
  $("#account-id").textContent = user.account_id;
  document.body.classList.remove("is-anon", "is-booting");
  stocksReady = loadStocks();
  loadHistory();
  loadCases();
  if (!appStarted) {
    appStarted = true;
    setInterval(() => {
      if (!state.user) return;
      renderLatestChips();
      if (!$("#history-drawer").hidden) renderHistoryList();
    }, 60000); // keep "N min ago" current
  }
  route();
}

async function submitAuth(form) {
  const mode = form.dataset.mode;
  const accountId = form.elements.account_id.value.trim();
  const password = form.elements.password.value;
  if (!accountId || !password) { showAuthError("Enter your account ID and password."); return; }
  if (mode === "signup") {
    if (!ACCOUNT_ID_RE.test(accountId)) {
      showAuthError("Account ID: 3–32 characters, using letters, numbers, dot, dash or underscore.");
      return;
    }
    if (password.length < 6) { showAuthError("Password: at least 6 characters."); return; }
  }
  const button = $("#auth-submit");
  button.disabled = true;
  showAuthError("");
  try {
    const user = await api("POST", mode === "signup" ? "/auth/signup" : "/auth/login",
      { account_id: accountId, password });
    form.elements.password.value = "";
    enterApp(user);
  } catch (err) {
    showAuthError(err.status === 0 ? "The StockLens server could not be reached." : err.message);
  } finally {
    button.disabled = false;
  }
}

async function logout() {
  try { await api("POST", "/auth/logout"); } catch { /* the cookie is cleared either way */ }
  showAuth("");
  toast("Logged out.");
}

// --- Stock picker ------------------------------------------------------------------

function pickerItemHtml(s, checked, group) {
  return `<li data-group="${esc(group)}" data-search="${esc(`${s.ticker} ${s.name} ${s.sector}`.toLowerCase())}">
    <label class="picker-item">
      <input type="checkbox" name="ticker" value="${esc(s.ticker)}" ${checked ? "checked" : ""}>
      <span class="pi-text">
        <span class="pi-ticker">${esc(s.ticker)}</span>
        <span class="pi-name">${esc(s.name)}</span>
        <span class="pi-sector">${esc(s.sector)}</span>
      </span>
      ${s.demo_coverage ? `<span class="tag tag-demo" title="${s.fictional ? "Fictional company with a recorded demo case" : "Recorded demo case"}">Demo case</span>` : ""}
    </label>
  </li>`;
}

function updatePickerCount() {
  const n = $$("#picker-list input:checked").length;
  $("#picker-count").textContent = `${plural(n, "stock")} selected`;
}

function filterPicker(query) {
  const q = query.trim().toLowerCase();
  let shown = 0;
  $$("#picker-list li[data-search]").forEach((li) => {
    const match = !q || li.dataset.search.includes(q);
    li.hidden = !match;
    if (match) shown += 1;
  });
  $$("#picker-list .picker-group").forEach((g) => {
    const items = $$(`li[data-group="${g.dataset.groupId}"]`);
    g.hidden = items.length > 0 && items.every((li) => li.hidden);
  });
  $("#picker-empty").hidden = shown > 0;
}

async function openPicker() {
  const dialog = $("#picker");
  const list = $("#picker-list");
  $("#picker-error").hidden = true;
  $("#picker-search").value = "";
  list.innerHTML = `<li class="picker-empty">Loading stocks…</li>`;
  dialog.showModal();
  if (!state.catalog) {
    try {
      state.catalog = await api("GET", "/catalog");
    } catch (err) {
      list.innerHTML = `<li class="picker-empty">Could not load the stock list: ${esc(err.message)}</li>`;
      return;
    }
  }
  const chosen = new Set((state.stocks && state.stocks.tickers) || []);
  const covered = state.catalog.filter((s) => s.demo_coverage);
  const others = state.catalog.filter((s) => !s.demo_coverage);
  const group = (id, title, items) => items.length
    ? `<li class="picker-group" data-group-id="${id}">${esc(title)}</li>` +
      items.map((s) => pickerItemHtml(s, chosen.has(s.ticker), id)).join("")
    : "";
  list.innerHTML = group("demo", "With a demo investigation", covered) +
    group("nse", "NSE large caps (no demo data)", others) +
    `<li class="picker-empty" id="picker-empty" hidden>No stocks match your search.</li>`;
  updatePickerCount();
  $("#picker-search").focus();
}

async function savePicker() {
  const selected = new Set($$("#picker-list input:checked").map((el) => el.value));
  // Keep the current order for stocks already listed; add new ones in catalog order.
  const current = ((state.stocks && state.stocks.tickers) || []).filter((t) => selected.has(t));
  const added = (state.catalog || []).map((s) => s.ticker).filter((t) => selected.has(t) && !current.includes(t));
  const button = $("#picker-save");
  button.disabled = true;
  try {
    state.stocks = await api("PUT", "/watchlist", { tickers: [...current, ...added] });
    $("#picker").close();
    renderWatchlist();
    toast(`Watchlist saved: ${plural(state.stocks.tickers.length, "stock")}.`);
  } catch (err) {
    if (err.status === 401) return;
    const el = $("#picker-error");
    el.textContent = `Could not save: ${err.message}`;
    el.hidden = false;
  } finally {
    button.disabled = false;
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
  if (!state.user) return; // logged out: the login screen is showing; route after login
  const match = location.hash.match(/^#\/stock\/([^/]+)$/);
  const savedMatch = location.hash.match(/^#\/history\/(\d+)$/);
  if (savedMatch) {
    openSaved(savedMatch[1]);
    $("#investigation").focus({ preventScroll: true });
  } else if (location.hash === "#/live") {
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
  const authTab = event.target.closest("[data-auth-mode]");
  if (authTab) {
    setAuthMode(authTab.dataset.authMode);
    $("#auth-account").focus();
    return;
  }
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
  if (action.dataset.action === "history") {
    openHistory();
    return;
  }
  if (action.dataset.action === "history-close") {
    closeHistory();
    return;
  }
  if (action.dataset.action === "history-open") {
    const hash = `#/history/${action.dataset.historyId}`;
    closeHistory();
    if (location.hash === hash) route();
    else location.hash = hash;
    return;
  }
  if (action.dataset.action === "receipt") {
    downloadReceipt(action.dataset.ticker);
    toast("Receipt downloaded. The printable version opened in a new tab.");
  } else if (action.dataset.action === "simulate") {
    simulate(state.ticker);
  } else if (action.dataset.action === "live") {
    goLive();
  } else if (action.dataset.action === "retry") {
    route();
  } else if (action.dataset.action === "reload-stocks") {
    stocksReady = loadStocks();
  } else if (action.dataset.action === "logout") {
    logout();
  } else if (action.dataset.action === "picker") {
    openPicker();
  } else if (action.dataset.action === "picker-cancel") {
    $("#picker").close();
  }
});

document.addEventListener("submit", (event) => {
  if (event.target.id === "live-form") {
    event.preventDefault();
    submitLive(event.target);
  } else if (event.target.id === "auth-form") {
    event.preventDefault();
    submitAuth(event.target);
  } else if (event.target.id === "picker-form") {
    event.preventDefault();
    savePicker();
  }
});

document.addEventListener("change", (event) => {
  if (event.target.closest && event.target.closest("#picker-list")) updatePickerCount();
});

document.addEventListener("input", (event) => {
  if (event.target.id === "picker-search") filterPicker(event.target.value);
});

document.addEventListener("keydown", (event) => {
  if (!state.user || $("#picker").open) return; // no app shortcuts on the login screen or in the picker
  const drawer = $("#history-drawer");
  if (drawer && !drawer.hidden) {
    if (event.key === "Escape") {
      event.preventDefault();
      closeHistory();
    } else if (event.key === "Tab") {
      const focusable = $$("button, a[href]", drawer).filter((el) => !el.disabled);
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
    return; // number shortcuts are off while the drawer is open
  }
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
let stocksReady = Promise.resolve();

// Boot: logged in already (session cookie)? Otherwise show the login screen.
(async () => {
  try {
    const session = await api("GET", "/auth/session");
    if (session && session.account_id) enterApp(session);
    else showAuth("");
  } catch (err) {
    showAuth(err.status === 0 ? "The StockLens server could not be reached." : err.message);
  }
})();
