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
const INVESTIGATORS = {
  atlas: "Atlas (macro)",
  sherlock: "Sherlock (company)",
  echo: "Echo (crowd)",
  skeptic: "Skeptic (against)",
};

const state = {
  mode: loadMode(),
  stocks: null, // {holdings, watchlist}
  card: null,
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

const timeFmt = new Intl.DateTimeFormat("en-IN", {
  timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false,
});
const dateFmt = new Intl.DateTimeFormat("en-IN", {
  timeZone: "Asia/Kolkata", day: "numeric", month: "short", year: "numeric",
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
  else text = `${Math.round(abs / 1440)} day${Math.round(abs / 1440) === 1 ? "" : "s"}`;
  if (minutes < 0) return `${text} before the move`;
  if (minutes > 0) return `${text} after the move began`;
  return "exactly when the move began";
}

async function api(method, path) {
  const res = await fetch(path, { method, headers: { Accept: "application/json" } });
  let body = null;
  try { body = await res.json(); } catch { /* non-JSON body */ }
  if (!res.ok) {
    const err = new Error((body && body.detail) || `Request failed (${res.status})`);
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

// --- Plain / Expert ----------------------------------------------------------

function applyMode() {
  document.body.classList.toggle("mode-plain", state.mode === "plain");
  document.body.classList.toggle("mode-expert", state.mode === "expert");
  document.querySelectorAll(".mode-toggle button").forEach((btn) => {
    btn.setAttribute("aria-pressed", String(btn.dataset.mode === state.mode));
  });
  const breakdown = $("details.breakdown");
  if (breakdown) breakdown.open = state.mode === "expert";
}

// --- My Stocks ---------------------------------------------------------------

function stockRow(stock) {
  const cls = signClass(stock.change);
  const numbers = `
    <span>
      <span class="stock-ticker">${esc(stock.ticker)}</span>
      <span class="stock-name">${esc(stock.name)}</span>
    </span>
    <span>
      <span class="stock-price">${esc(fmtPrice(stock.price))}</span>
      <span class="stock-change ${cls}">${esc(fmtSigned(stock.change, 2, ""))} (${esc(fmtSigned(stock.change_pct))})</span>
    </span>`;
  if (stock.alert) {
    return `<li><button type="button" class="stock-row" data-ticker="${esc(stock.ticker)}"
        aria-label="Investigate ${esc(stock.ticker)}, move flagged">
      ${numbers}
      <span class="stock-status"><span class="badge-flag">Move flagged</span>
        <span class="investigate-hint">Investigate →</span></span>
    </button></li>`;
  }
  return `<li><div class="stock-row">
      ${numbers}
      <span class="stock-status"><span class="no-investigation">No investigation available</span></span>
    </div></li>`;
}

function renderStockList(el, stocks) {
  el.innerHTML = stocks.length
    ? stocks.map(stockRow).join("")
    : `<li class="muted">No stocks.</li>`;
}

async function loadStocks() {
  try {
    state.stocks = await api("GET", "/stocks");
    renderStockList($("#holdings-list"), state.stocks.holdings || []);
    renderStockList($("#watchlist-list"), state.stocks.watchlist || []);
  } catch (err) {
    const msg = `<li class="muted">Could not load stocks: ${esc(err.message)}</li>`;
    $("#holdings-list").innerHTML = msg;
    $("#watchlist-list").innerHTML = msg;
  }
}

function stockName(ticker) {
  if (!state.stocks) return "";
  const all = [...(state.stocks.holdings || []), ...(state.stocks.watchlist || [])];
  const match = all.find((s) => s.ticker === ticker);
  return match ? match.name : "";
}

// --- Investigation card ------------------------------------------------------

/** Accepted + rejected evidence in one list, numbered in time order (missing timestamps last). */
function numberedEvidence(card) {
  const items = [
    ...card.accepted_evidence.map((e) => ({ e, status: "accepted", reason: "" })),
    ...card.rejected_evidence.map((r) => ({ e: r.evidence, status: "rejected", reason: r.reason })),
  ];
  items.sort((a, b) => {
    const ta = a.e.first_public_at ? Date.parse(a.e.first_public_at) : Infinity;
    const tb = b.e.first_public_at ? Date.parse(b.e.first_public_at) : Infinity;
    return ta - tb;
  });
  items.forEach((item, i) => { item.n = i + 1; });
  return items;
}

function moveStats(move) {
  const stat = (label, value, cls = "", sub = "") =>
    `<li><span class="stat-label">${esc(label)}</span><span class="stat-value ${cls}">${esc(value)}</span>${
      sub ? `<span class="stat-sub">${esc(sub)}</span>` : ""}</li>`;
  const began = new Date(move.move_start_at);
  return `<ul class="move-stats">
    ${stat("Move", fmtSigned(move.pct_change, 1), signClass(move.pct_change))}
    ${stat("Window", `${move.window_minutes} min`)}
    ${stat("Volume", `${Number(move.volume_multiple).toFixed(1)}×`, "", "normal volume")}
    ${stat("Sector", fmtSigned(move.sector_pct_change, 1), signClass(move.sector_pct_change))}
    ${stat("Index", fmtSigned(move.index_pct_change, 1), signClass(move.index_pct_change))}
    ${stat("Move began", `${timeFmt.format(began)} IST`, "", dateFmt.format(began))}
  </ul>`;
}

function headerBlock(card) {
  const name = stockName(card.ticker);
  const marketLeads = (card.leading_explanation || "").startsWith(MARKET_PREFIX);
  return `<section class="panel">
    <div class="card-head">
      <div class="card-title">
        <h1>${esc(card.ticker)}</h1>
        ${name ? `<div class="muted">${esc(name)}</div>` : ""}
      </div>
      <div class="chip chip-${esc(card.confidence_label)}" aria-label="Confidence ${esc(card.confidence_label)}, ${esc(card.confidence_score)} out of 100">
        ${esc(card.confidence_label)} <small>${esc(card.confidence_score)}/100</small>
      </div>
    </div>
    ${marketLeads ? `<p><span class="tag tag-market">Market-wide move</span></p>` : ""}
    ${moveStats(card.move)}
  </section>`;
}

function explanationBlock(card) {
  if (card.confidence_label === "Unknown") {
    const rejected = card.rejected_evidence;
    const rows = rejected.length
      ? rejected.map((r) => `<li><strong>${esc(r.evidence.source_name)}</strong>: ${esc(r.evidence.claim)}
          <br><span class="reason">Why it failed: ${esc(r.reason)}</span></li>`).join("")
      : `<li>No evidence was found.</li>`;
    return `<section class="panel unknown-card">
      <h2>${esc(UNKNOWN_TEXT)}</h2>
      <p>No evidence that was public before the move explains it. Here is what we found and why each item failed:</p>
      <ul class="plain-list">${rows}</ul>
      ${actionsBlock(card)}
    </section>`;
  }
  const text = card.explanation_text || card.leading_explanation || UNKNOWN_TEXT;
  return `<section class="panel">
    <div class="explanation-label">Why it moved</div>
    <p class="explanation">${esc(text)}</p>
    ${actionsBlock(card)}
  </section>`;
}

function actionsBlock(card) {
  return `<div class="actions">
      <button type="button" class="btn" data-action="receipt" data-ticker="${esc(card.ticker)}">Evidence Receipt</button>
      <button type="button" class="btn btn-ghost" data-action="challenge" data-ticker="${esc(card.ticker)}">Challenge This</button>
    </div>
    <div class="challenge-result" id="challenge-result" role="status" aria-live="polite" hidden></div>`;
}

function timelineBlock(card, items) {
  const start = Date.parse(card.move.move_start_at);
  const timed = items.filter((it) => it.e.first_public_at);
  const missing = items.filter((it) => !it.e.first_public_at);

  // Compressed (log) time scale so a hint from days earlier and news minutes
  // later both stay readable. The move start is always in the middle.
  const minutes = timed.map((it) => (Date.parse(it.e.first_public_at) - start) / 60000);
  const maxAbs = Math.max(60, ...minutes.map(Math.abs));
  const scale = (m) => Math.sign(m) * Math.log1p(Math.abs(m)) / Math.log1p(maxAbs);

  const placed = timed.map((it, i) => ({ ...it, minutes: minutes[i], x: 50 + 45 * scale(minutes[i]) }));
  // Dots closer than about one dot width (in % of the track) go up a lane.
  const trackPx = Math.max(200, $("#card-root").clientWidth - 80);
  const minGap = Math.max(5.5, (48 / trackPx) * 100);
  const lanes = [];
  [...placed].sort((a, b) => a.x - b.x).forEach((p) => {
    let lane = lanes.findIndex((lastX) => p.x - lastX >= minGap);
    if (lane === -1) { lane = lanes.length; lanes.push(p.x); } else { lanes[lane] = p.x; }
    p.lane = lane;
  });
  const laneCount = Math.max(1, lanes.length);

  const dots = placed.map((p) => `
    <button type="button" class="dot dot-${p.status}" data-n="${p.n}"
      style="left:${p.x.toFixed(2)}%; bottom:calc(2.3rem - 1.1rem + ${p.lane} * 2.7rem)"
      aria-label="Evidence ${p.n}, ${esc(p.status)}, ${esc(p.e.source_name)}, ${esc(describeGap(p.minutes))}">
      <span>${p.n}</span>
    </button>`).join("");

  const missingList = missing.length
    ? `<div class="timeline-missing"><h3>Missing timestamp</h3><ul class="plain-list">
        ${missing.map((it) => `<li><strong>#${it.n} ${esc(it.e.source_name)}</strong>: ${esc(it.e.claim)}
          <span class="reason">(missing timestamp)</span></li>`).join("")}
      </ul></div>`
    : "";

  return `<section class="panel">
    <h2>Evidence timeline</h2>
    <p class="muted">Green was accepted, red was rejected. Hover or tap a dot for details.</p>
    <div class="timeline">
      <div class="timeline-track" style="height:calc(4.6rem + ${laneCount} * 2.7rem)">
        <div class="timeline-line"></div>
        <div class="timeline-marker" style="left:50%">
          <span class="timeline-marker-label">Move start ${esc(timeFmt.format(new Date(start)))} IST</span>
        </div>
        <span class="timeline-side left">← before the move</span>
        <span class="timeline-side right">after the move →</span>
        ${dots}
      </div>
    </div>
    <div class="timeline-detail" id="timeline-detail" aria-live="polite">
      ${timed.length ? `<span class="muted">Hover or tap a numbered dot.</span>`
                     : `<span class="muted">No timestamped evidence to place on the timeline.</span>`}
    </div>
    <p class="timeline-note">The time axis is compressed so that items days apart and minutes apart both fit.</p>
    ${missingList}
  </section>`;
}

function detailHtml(item, card) {
  const e = item.e;
  const start = Date.parse(card.move.move_start_at);
  const gap = e.first_public_at ? describeGap((Date.parse(e.first_public_at) - start) / 60000) : "";
  return `<div><span class="status status-${item.status}">#${item.n} ${esc(item.status)}</span>
      · <strong>${esc(e.source_name)}</strong> · ${esc(TIER_NAMES[e.source_tier] || `Tier ${e.source_tier}`)}</div>
    <div class="evidence-claim">${esc(e.claim)}</div>
    <div class="muted">${esc(fmtWhen(e.first_public_at))}${gap ? ` · ${esc(gap)}` : ""}</div>
    ${item.reason ? `<div class="reason">Rejected: ${esc(item.reason)}</div>` : ""}`;
}

function evidenceListBlock(items) {
  if (!items.length) return "";
  const rows = items.map(({ e, status, reason, n }) => `
    <li class="evidence-item">
      <span class="evidence-num ${status}" aria-hidden="true">${n}</span>
      <div class="evidence-meta">
        <span class="status status-${status}">${esc(status)}</span>
        <strong>${esc(e.source_name)}</strong>
        <span>${esc(fmtWhen(e.first_public_at))}</span>
        <span class="tag expert-only">${esc(TIER_NAMES[e.source_tier] || `Tier ${e.source_tier}`)}</span>
        <span class="tag expert-only">Group: ${esc(e.independence_group || "none")}</span>
        <span class="tag expert-only">${esc(INVESTIGATORS[e.investigator] || e.investigator)}</span>
        <span class="tag expert-only">${e.verified ? "Verified" : "Unverified"}</span>
      </div>
      <div class="evidence-claim">${esc(e.claim)}</div>
      <div>
        ${reason ? `<div class="reason">Rejected: ${esc(reason)}</div>` : ""}
        <div class="expert-only muted">“${esc(e.snippet)}”</div>
        <a class="evidence-link expert-only" href="${esc(safeUrl(e.url))}" target="_blank" rel="noopener noreferrer">${esc(e.url)}</a>
      </div>
    </li>`).join("");
  return `<section class="panel">
    <h2>Evidence</h2>
    <ol class="evidence-list">${rows}</ol>
  </section>`;
}

function breakdownBlock(card) {
  const rows = card.score_breakdown.map((s) => `
    <tr><td>${esc(s.rule)}</td>
      <td class="pts ${s.points >= 0 ? "pos" : "neg"}">${esc(fmtSigned(s.points, 0, ""))}</td></tr>`).join("");
  return `<section class="panel">
    <details class="breakdown" ${state.mode === "expert" ? "open" : ""}>
      <summary>Score breakdown: ${esc(card.confidence_score)}/100</summary>
      <table class="score-table">
        <tbody>${rows || `<tr><td colspan="2" class="muted">No scoring rules fired.</td></tr>`}</tbody>
        <tfoot><tr><th>Total</th><th class="pts">${esc(card.confidence_score)}</th></tr></tfoot>
      </table>
    </details>
  </section>`;
}

function alternativesBlock(card) {
  if (!card.alternatives.length) return "";
  return `<section class="panel">
    <h2>Alternatives considered</h2>
    <ul class="plain-list">${card.alternatives.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>
  </section>`;
}

function limitsBlock(card) {
  return `<footer class="limits">
    <h2>Limits</h2>
    <ul class="plain-list">${card.limits.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>
  </footer>`;
}

function renderCard(card) {
  const items = numberedEvidence(card);
  const root = $("#card-root");
  root.innerHTML = `<div class="stack">
    ${headerBlock(card)}
    ${explanationBlock(card)}
    ${timelineBlock(card, items)}
    ${evidenceListBlock(items)}
    ${breakdownBlock(card)}
    ${alternativesBlock(card)}
  </div>
  ${limitsBlock(card)}`;

  const detail = $("#timeline-detail");
  root.querySelectorAll(".dot").forEach((dot) => {
    const item = items.find((it) => it.n === Number(dot.dataset.n));
    const show = () => {
      root.querySelectorAll(".dot.active").forEach((d) => d.classList.remove("active"));
      dot.classList.add("active");
      detail.innerHTML = detailHtml(item, card);
    };
    dot.addEventListener("mouseenter", show);
    dot.addEventListener("focus", show);
    dot.addEventListener("click", show);
  });
}

function renderMessage(title, body) {
  $("#card-root").innerHTML = `<section class="panel message-panel">
    <h2>${esc(title)}</h2>
    <p class="muted">${esc(body)}</p>
    <p><a class="btn" href="#/">Back to My Stocks</a></p>
  </section>`;
}

async function investigate(ticker) {
  state.card = null;
  $("#card-root").innerHTML = `<div class="loading">Investigating ${esc(ticker)}…</div>`;
  try {
    const card = await api("POST", `/investigate/${encodeURIComponent(ticker)}`);
    if (location.hash !== `#/stock/${encodeURIComponent(ticker)}`) return; // user navigated away
    state.card = card;
    renderCard(card);
  } catch (err) {
    if (err.status === 404) {
      renderMessage("No investigation available", `${ticker} has no flagged move on record.`);
    } else {
      renderMessage("Could not load the investigation", err.message);
    }
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

async function challenge(ticker, button) {
  const out = $("#challenge-result");
  button.disabled = true;
  try {
    const res = await api("POST", `/challenge/${encodeURIComponent(ticker)}`);
    out.innerHTML = `<strong>Skeptic:</strong> ${esc(res.message)}
      ${res.status ? `<span class="tag">${esc(res.status)}</span>` : ""}`;
  } catch (err) {
    out.innerHTML = `<span class="reason">Challenge failed: ${esc(err.message)}</span>`;
  } finally {
    out.hidden = false;
    button.disabled = false;
  }
}

// --- Routing -----------------------------------------------------------------

function route() {
  const match = location.hash.match(/^#\/stock\/([^/]+)$/);
  const stocksView = $("#stocks-view");
  const cardView = $("#card-view");
  if (match) {
    const ticker = decodeURIComponent(match[1]).toUpperCase();
    stocksView.hidden = true;
    cardView.hidden = false;
    document.title = `${ticker} · StockLens`;
    window.scrollTo(0, 0);
    investigate(ticker);
  } else {
    cardView.hidden = true;
    stocksView.hidden = false;
    document.title = "StockLens";
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
  const stockBtn = event.target.closest("button.stock-row[data-ticker]");
  if (stockBtn) {
    location.hash = `#/stock/${encodeURIComponent(stockBtn.dataset.ticker)}`;
    return;
  }
  const action = event.target.closest("[data-action]");
  if (action?.dataset.action === "receipt") {
    downloadReceipt(action.dataset.ticker);
    toast("Receipt downloaded. The printable version opened in a new tab.");
  } else if (action?.dataset.action === "challenge") {
    challenge(action.dataset.ticker, action);
  }
});

window.addEventListener("hashchange", route);
applyMode();
loadStocks();
route();
