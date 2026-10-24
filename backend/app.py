"""FastAPI app: API routes and the static frontend."""

import json
from collections.abc import Callable
from datetime import datetime
from html import escape
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend import config
from backend.consensus.engine import build_card
from backend.models import IST, Card, Evidence, Move

app = FastAPI(title="StockLens")
app.mount("/static", StaticFiles(directory=config.FRONTEND_DIR), name="static")


# --- Demo portfolio ----------------------------------------------------------
# Hardcoded demo prices, not live quotes. The three fixture tickers use the
# first and last prices in fixtures/market_data.json.

HOLDINGS = [
    {"ticker": "ACMEINFRA", "name": "ACME Infra", "price": 1180.00, "prev_close": 1251.00},
    {"ticker": "RELIANCE", "name": "Reliance Industries", "price": 2948.40, "prev_close": 2931.15},
    {"ticker": "HDFCBANK", "name": "HDFC Bank", "price": 1712.65, "prev_close": 1718.90},
    {"ticker": "ORIONPHARMA", "name": "Orion Pharma", "price": 1463.00, "prev_close": 1502.00},
]
WATCHLIST = [
    {"ticker": "ZENITHPOLY", "name": "Zenith Polymers", "price": 520.00, "prev_close": 499.00},
    {"ticker": "TCS", "name": "Tata Consultancy Services", "price": 4105.20, "prev_close": 4089.75},
    {"ticker": "INFY", "name": "Infosys", "price": 1876.30, "prev_close": 1881.05},
]


# --- Fixtures ----------------------------------------------------------------

def _fixture_index() -> dict[str, Path]:
    """Map ticker -> consensus_input.json for every fixtures/case_* folder."""
    index = {}
    for path in sorted(config.FIXTURES_DIR.glob("case_*/consensus_input.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        index[data["move"]["ticker"].upper()] = path
    return index


FIXTURES = _fixture_index()


def fixture_card(ticker: str) -> Card | None:
    """Replay: build the Card from the ticker's recorded fixture, or None if there is none."""
    path = FIXTURES.get(ticker)
    if path is None:
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    move = Move(**data["move"])
    evidence = [Evidence(**e) for e in data["evidence"]]
    return build_card(move, evidence)


# Swap point: replace with the live pipeline later (same signature,
# ticker -> Card | None). The routes and frontend do not change.
card_source: Callable[[str], Card | None] = fixture_card


def get_card(ticker: str) -> Card:
    """The one place routes get a Card from. Raises 404 if no investigation exists."""
    card = card_source(ticker.upper())
    if card is None:
        raise HTTPException(
            status_code=404,
            detail=f"No investigation available for {ticker.upper()}: no flagged move on record.")
    return card


# --- Routes ------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"status": "ok", "mode": config.MODE}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(config.FRONTEND_DIR / "index.html")


def _stock_row(stock: dict) -> dict:
    change = stock["price"] - stock["prev_close"]
    return {
        "ticker": stock["ticker"],
        "name": stock["name"],
        "price": stock["price"],
        "change": round(change, 2),
        "change_pct": round(100 * change / stock["prev_close"], 2),
        "alert": stock["ticker"] in FIXTURES,
    }


@app.get("/stocks")
def stocks() -> dict:
    return {
        "holdings": [_stock_row(s) for s in HOLDINGS],
        "watchlist": [_stock_row(s) for s in WATCHLIST],
    }


@app.post("/investigate/{ticker}")
def investigate(ticker: str) -> Card:
    return get_card(ticker)


@app.get("/receipt/{ticker}", response_model=None)
def receipt(ticker: str, format: str = "json") -> JSONResponse | HTMLResponse:
    """Evidence Receipt: ?format=json (default) downloads a file, ?format=html is printable."""
    card = get_card(ticker)
    generated_at = datetime.now(IST)
    if format == "html":
        return HTMLResponse(_receipt_html(card, generated_at))
    body = {
        "receipt": "StockLens Evidence Receipt",
        "generated_at": generated_at.isoformat(),
        "mode": config.MODE,
        "tier_names": config.TIER_NAMES,
        **card.model_dump(mode="json"),
    }
    filename = f"stocklens-receipt-{card.ticker}.json"
    return JSONResponse(body, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.post("/challenge/{ticker}")
def challenge(ticker: str) -> dict:
    return {
        "ticker": ticker.upper(),
        "status": "not_connected",
        "message": "Skeptic runs once the investigators are connected.",
    }


# --- Receipt HTML ------------------------------------------------------------

def _fmt_time(value: datetime | None) -> str:
    return value.strftime("%d %b %Y, %H:%M IST") if value else "missing"


def _evidence_row(n: int, e: Evidence, status: str, reason: str) -> str:
    url = escape(e.url)
    return (
        f"<tr><td>{n}</td><td class='{status}'>{status}</td>"
        f"<td>{escape(e.source_name)}<br><small>{e.investigator}</small></td>"
        f"<td>Tier {e.source_tier}<br><small>{escape(config.TIER_NAMES[e.source_tier])}</small></td>"
        f"<td>{_fmt_time(e.first_public_at)}</td>"
        f"<td>{escape(e.claim)}<br><a href='{url}'>{url}</a></td>"
        f"<td>{escape(reason)}</td></tr>"
    )


def _accepted_reason(e: Evidence, move: Move) -> str:
    """Why an accepted item was kept, following the timing rules in SPEC.md."""
    if e.investigator == "skeptic":
        return "Skeptic evidence (not held to the before-the-move rule)"
    if e.first_public_at < move.move_start_at:
        return "Public before the move started"
    return "Late Tier 2 report, counted only as corroboration of a pre-move filing"


def _receipt_html(card: Card, generated_at: datetime) -> str:
    m = card.move
    rows = [_evidence_row(i, e, "accepted", _accepted_reason(e, m))
            for i, e in enumerate(card.accepted_evidence, 1)]
    start = len(rows) + 1
    rows += [_evidence_row(i, r.evidence, "rejected", r.reason)
             for i, r in enumerate(card.rejected_evidence, start)]
    breakdown = "".join(
        f"<tr><td>{escape(s.rule)}</td><td class='num'>{s.points:+d}</td></tr>"
        for s in card.score_breakdown)
    lists = lambda items: "".join(f"<li>{escape(x)}</li>" for x in items) or "<li>None</li>"
    explanation = card.leading_explanation or config.UNKNOWN_TEXT

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Evidence Receipt - {escape(card.ticker)}</title>
<style>
  body {{ font-family: system-ui, sans-serif; color: #111; margin: 2rem; }}
  h1 {{ margin-bottom: 0; }} .meta {{ color: #555; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: 0.9rem; }}
  th, td {{ border: 1px solid #ccc; padding: 6px 8px; text-align: left; vertical-align: top; }}
  th {{ background: #eef2f8; }} .num {{ text-align: right; }}
  .accepted {{ color: #11743a; font-weight: 600; }} .rejected {{ color: #b3261e; font-weight: 600; }}
  a {{ word-break: break-all; }} button {{ padding: 6px 14px; }}
  @media print {{ button {{ display: none; }} }}
</style></head><body>
<button onclick="window.print()">Print</button>
<h1>Evidence Receipt: {escape(card.ticker)}</h1>
<p class="meta">Generated {_fmt_time(generated_at)} &middot; mode: {config.MODE}</p>

<h2>Move</h2>
<p>{m.pct_change:+.2f}% from {_fmt_time(m.move_start_at)} over {m.window_minutes} min,
volume {m.volume_multiple}x normal. Sector {m.sector_pct_change:+.2f}%, index {m.index_pct_change:+.2f}%.</p>

<h2>Result</h2>
<p><strong>{card.confidence_label}</strong> ({card.confidence_score}/100): {escape(explanation)}</p>

<h2>Evidence</h2>
<table>
<tr><th>#</th><th>Status</th><th>Source</th><th>Tier</th><th>Public at</th><th>Claim / URL</th><th>Reason</th></tr>
{"".join(rows) or "<tr><td colspan='7'>No evidence found.</td></tr>"}
</table>

<h2>Score breakdown</h2>
<table>
<tr><th>Rule</th><th class="num">Points</th></tr>
{breakdown}
<tr><th>Total</th><th class="num">{card.confidence_score}</th></tr>
</table>

<h2>Alternatives</h2><ul>{lists(card.alternatives)}</ul>
<h2>Limits</h2><ul>{lists(card.limits)}</ul>
</body></html>"""
