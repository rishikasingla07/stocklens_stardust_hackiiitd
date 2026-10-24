"""FastAPI app: API routes and the static frontend."""

import json
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
from functools import partial
from html import escape
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend import config, live_jobs, pipeline
from backend.consensus.engine import build_card
from backend.db import history
from backend.llm.explain import fill_explanation, offline_explanation
from backend.market import data as market_data
from backend.models import IST, Card, Evidence, Move
from backend.tinyfish.client import TinyFishClient

log = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    history.init()  # create the database and table if missing
    yield


app = FastAPI(title="StockLens", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=config.FRONTEND_DIR), name="static")


# --- Demo portfolio ----------------------------------------------------------
# Hardcoded demo prices, not live quotes. Tickers with recorded series in
# fixtures/market_data.json are overridden in /stocks by the delayed feed, and
# only those can be flagged (by the trigger engine). The prices below are
# fallbacks for them.

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


def with_explanation(card: Card) -> Card:
    """Fill explanation_text: LLM if usable, otherwise the offline template, silently."""
    try:
        return fill_explanation(card)
    except Exception as exc:  # any LLM-side failure must not break the card
        log.info("explanation: using offline text (%s)", exc)
        return card.model_copy(update={"explanation_text": offline_explanation(card)})


def pipeline_card(ticker: str, include_skeptic: bool = False) -> Card | None:
    """Pipeline Card with explanation_text filled; the fixture path is the fallback."""
    card = pipeline.live_card(ticker, include_skeptic=include_skeptic) or fixture_card(ticker)
    return with_explanation(card) if card is not None else None


# Swap points (ticker -> Card | None). The routes and frontend do not change.
card_source: Callable[[str], Card | None] = pipeline_card
challenge_source: Callable[[str], Card | None] = partial(pipeline_card, include_skeptic=True)


def save_history(card: Card, source: str, mode: str | None = None,
                 db_path: Path | None = None) -> None:
    """Save a produced Card to history. A storage problem never breaks the response."""
    try:
        history.save(card, source, mode or ("live" if config.MODE == "live" else "replay"), db_path)
    except Exception as exc:
        log.warning("history: could not save %s card for %s (%s)", source, card.ticker, exc)


def get_card(ticker: str, source: Callable[[str], Card | None] | None = None) -> Card:
    """The one place routes get a Card from. Raises 404 if no investigation exists."""
    card = (source or card_source)(ticker.upper())
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


def trigger_thresholds() -> dict:
    """The trigger engine's thresholds, as shown to the user."""
    return {
        "min_pct_change": config.TRIGGER_MIN_PCT_CHANGE,
        "min_volume_multiple": config.TRIGGER_MIN_VOLUME_MULTIPLE,
        "default_window_minutes": config.TRIGGER_DEFAULT_WINDOW_MINUTES,
    }


def _stock_row(stock: dict) -> dict:
    """One /stocks row. Recorded tickers: delayed-feed price, alert only if the trigger flags a move."""
    price, reference, alert = stock["price"], stock["prev_close"], False
    case = market_data.find_case(stock["ticker"])
    if case is not None:
        quote = market_data.delayed_quote(case, stock["ticker"], datetime.now(IST))
        if quote is not None:
            price, reference = quote["price"], quote["reference"]
        alert = market_data.get_move(case, stock["ticker"]) is not None
    change = price - reference
    return {
        "ticker": stock["ticker"],
        "name": stock["name"],
        "price": price,
        "change": round(change, 2),
        "change_pct": round(100 * change / reference, 2),
        "alert": alert,
    }


@app.get("/stocks")
def stocks() -> dict:
    return {
        "holdings": [_stock_row(s) for s in HOLDINGS],
        "watchlist": [_stock_row(s) for s in WATCHLIST],
    }


@app.post("/investigate/{ticker}")
def investigate(ticker: str) -> Card:
    card = get_card(ticker)
    save_history(card, "investigate")
    return card


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


@app.post("/simulate/{ticker}")
def simulate(ticker: str) -> dict:
    """Run the trigger engine on the ticker's recorded series; if it flags a move, investigate that Move."""
    ticker = ticker.upper()
    case = market_data.find_case(ticker)
    if case is None:
        raise HTTPException(status_code=404,
                            detail=f"No recorded market data for {ticker}, so the trigger cannot run.")
    move = market_data.get_move(case, ticker)
    card = with_explanation(pipeline.card_for_move(move)) if move is not None else None
    if card is not None:
        save_history(card, "simulate")
    return {
        "flagged": move is not None,
        "move": move.model_dump(mode="json") if move else None,
        "thresholds": trigger_thresholds(),
        "card": card.model_dump(mode="json") if card else None,
    }


# --- Live investigation (Sherlock live, background job) ---------------------

class LiveStartRequest(BaseModel):
    """User-supplied move: we have no live market data, so the numbers come from the form."""
    company: str = Field(min_length=1, max_length=100)
    ticker: str = Field(min_length=1, max_length=20, pattern=r"^[A-Za-z0-9&._-]+$")
    move_start_at: datetime  # ISO; read as IST if it has no time zone
    pct_change: float = Field(ge=-100, le=1000)
    volume_multiple: float = Field(ge=0, le=1000)
    sector_pct_change: float = Field(ge=-100, le=1000)
    index_pct_change: float = Field(ge=-100, le=1000)
    window_minutes: int = Field(default=config.TRIGGER_DEFAULT_WINDOW_MINUTES, ge=1, le=375)


# Swap point for tests: builds the TinyFish client for each live job.
live_client_factory: Callable[[], TinyFishClient] = TinyFishClient


@app.get("/live/status")
def live_status() -> dict:
    """Whether live runs are allowed, and the running job if there is one."""
    reason = live_jobs.disabled_reason()
    active = live_jobs.active_job()
    return {
        "enabled": reason is None,
        "message": reason,
        "active_job_id": active.id if active else None,
        "poll_seconds": config.LIVE_POLL_SECONDS,
        "max_calls": config.MAX_TINYFISH_CALLS_PER_INVESTIGATION,
    }


@app.post("/live/start")
def live_start(request: LiveStartRequest) -> dict:
    """Start one live investigation in the background. 403 if live is off, 409 if one is running."""
    move = live_jobs.build_move(
        request.ticker, request.move_start_at, request.window_minutes, request.pct_change,
        request.volume_multiple, request.sector_pct_change, request.index_pct_change)
    try:
        job = live_jobs.start_job(request.company.strip(), move, with_explanation,
                                  live_client_factory,
                                  on_done=partial(save_history, source="live", mode="live",
                                                  db_path=config.DB_PATH))  # path fixed at start
    except live_jobs.LiveDisabled as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except live_jobs.LiveBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {"job_id": job.id}


@app.get("/live/{job_id}")
def live_job(job_id: str) -> dict:
    job = live_jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No live investigation with that id.")
    return job.to_dict()


@app.post("/challenge/{ticker}")
def challenge(ticker: str) -> Card:
    """Re-run the investigation with the full Skeptic. Same Card shape as /investigate."""
    card = get_card(ticker, challenge_source)
    save_history(card, "challenge")
    return card


# --- History (SQLite) --------------------------------------------------------

@app.get("/history")
def history_list(ticker: str | None = None, limit: int = history.MAX_LIMIT) -> list[dict]:
    """Past investigations, newest first, without the stored Card (max 50)."""
    return history.list_history(ticker, limit)


@app.get("/history/{entry_id}")
def history_entry(entry_id: int) -> Card:
    """The stored Card for one past investigation, exactly as it was produced (nothing re-runs)."""
    card = history.get_card(entry_id)
    if card is None:
        raise HTTPException(status_code=404, detail=f"No saved investigation with id {entry_id}.")
    return card


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
