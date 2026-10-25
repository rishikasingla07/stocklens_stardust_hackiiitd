"""FastAPI app: API routes and the static frontend."""

import json
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
from functools import partial
from html import escape
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend import config, live_jobs, pipeline
from backend.consensus import market_context
from backend.consensus.engine import build_card
from backend.db import history, users
from backend.db.users import User
from backend.llm.explain import fill_explanation, offline_explanation
from backend.market import catalog
from backend.market import data as market_data
from backend.market import price_history
from backend.tinyfish import replay
from backend.models import IST, Card, Evidence, Move
from backend.tinyfish.client import TinyFishClient

log = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    history.init()  # create the database and tables if missing
    if config.MODE == "replay" and users.ensure_demo_account():
        log.info("created the demo account '%s' (replay mode only)", config.DEMO_ACCOUNT_ID)
    yield


app = FastAPI(title="StockLens", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=config.FRONTEND_DIR), name="static")


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 without echoing what was sent: FastAPI's default includes the input, e.g. a password."""
    errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg", "invalid"), "type": e.get("type", "")}
              for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


# --- Accounts and sessions ---------------------------------------------------
# Demo-grade auth, not production security. See backend/db/users.py.

NOT_LOGGED_IN = "Please log in to use StockLens."
BAD_LOGIN = "Account ID or password is incorrect."


def current_user(request: Request) -> User:
    """FastAPI dependency: the logged-in user, or 401."""
    user = users.user_for_session(request.cookies.get(config.AUTH_COOKIE_NAME))
    if user is None:
        raise HTTPException(status_code=401, detail=NOT_LOGGED_IN)
    return user


def _set_session_cookie(response: Response, user: User) -> None:
    response.set_cookie(
        config.AUTH_COOKIE_NAME, users.create_session(user),
        max_age=config.AUTH_SESSION_HOURS * 3600, httponly=True, samesite="lax",
        secure=config.AUTH_COOKIE_SECURE, path="/")


class SignupRequest(BaseModel):
    account_id: str = Field(min_length=config.AUTH_ACCOUNT_ID_MIN, max_length=config.AUTH_ACCOUNT_ID_MAX,
                            pattern=config.AUTH_ACCOUNT_ID_PATTERN)
    password: str = Field(min_length=config.AUTH_PASSWORD_MIN, max_length=config.AUTH_PASSWORD_MAX)


class LoginRequest(BaseModel):
    # Loose bounds on purpose: anything that does not match an account gets the same 401.
    account_id: str = Field(max_length=200)
    password: str = Field(max_length=1000)


@app.post("/auth/signup", status_code=201)
def signup(body: SignupRequest, response: Response) -> dict:
    """Create an account and log in. 409 if the account ID is taken."""
    try:
        user = users.create_user(body.account_id, body.password)
    except users.AccountExists:
        raise HTTPException(status_code=409, detail="That account ID is already taken.") from None
    _set_session_cookie(response, user)
    return {"account_id": user.account_id}


@app.post("/auth/login")
def login(body: LoginRequest, response: Response) -> dict:
    """Log in. The same 401 for an unknown account ID and a wrong password."""
    user = users.verify_login(body.account_id.strip(), body.password)
    if user is None:
        raise HTTPException(status_code=401, detail=BAD_LOGIN)
    _set_session_cookie(response, user)
    return {"account_id": user.account_id}


@app.post("/auth/logout")
def logout(request: Request, response: Response) -> dict:
    """End the session (if any) and clear the cookie."""
    users.delete_session(request.cookies.get(config.AUTH_COOKIE_NAME))
    response.delete_cookie(config.AUTH_COOKIE_NAME, path="/", httponly=True, samesite="lax",
                           secure=config.AUTH_COOKIE_SECURE)
    return {"ok": True}


@app.get("/me")
def me(user: User = Depends(current_user)) -> dict:
    return {"account_id": user.account_id}


@app.get("/auth/session")
def session_check(request: Request) -> dict:
    """Public: who is logged in, or null. Lets the page boot without a 401 when logged out."""
    user = users.user_for_session(request.cookies.get(config.AUTH_COOKIE_NAME))
    return {"account_id": user.account_id if user else None}


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


def pipeline_card(ticker: str) -> Card | None:
    """Pipeline Card with explanation_text filled; the fixture path is the fallback."""
    card = pipeline.live_card(ticker) or fixture_card(ticker)
    return with_explanation(card) if card is not None else None


# Swap point (ticker -> Card | None). The routes and frontend do not change.
card_source: Callable[[str], Card | None] = pipeline_card


def save_history(card: Card, source: str, mode: str | None = None,
                 db_path: Path | None = None, *, user_id: int) -> None:
    """Save a produced Card to the user's history. A storage problem never breaks the response."""
    try:
        history.save(card, source, mode or ("live" if config.MODE == "live" else "replay"), db_path,
                     user_id=user_id)
    except Exception as exc:
        log.warning("history: could not save %s card for %s (%s)", source, card.ticker, exc)


NO_DEMO_COVERAGE = "No investigation available in this demo"


def no_investigation_detail(ticker: str) -> str:
    entry = catalog.by_ticker().get(ticker)
    if entry is not None and not entry["demo_coverage"]:
        return f"{NO_DEMO_COVERAGE} for {ticker}: StockLens has no recorded evidence for it."
    return f"No investigation available for {ticker}: no flagged move on record."


def get_card(ticker: str) -> Card:
    """The one place routes get a Card from. Raises 404 if no investigation exists."""
    card = card_source(ticker.upper())
    if card is None:
        raise HTTPException(status_code=404, detail=no_investigation_detail(ticker.upper()))
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


def _recorded_quote(ticker: str) -> dict | None:
    """Delayed-feed price and trigger alert for a ticker with recorded market data, else None."""
    case = market_data.find_case(ticker)
    if case is None:
        return None
    quote = market_data.delayed_quote(case, ticker, datetime.now(IST))
    if quote is None:
        return None
    change = quote["price"] - quote["reference"]
    return {
        "price": quote["price"],
        "change": round(change, 2),
        "change_pct": round(100 * change / quote["reference"], 2),
        "alert": market_data.get_move(case, ticker) is not None,
    }


def _stock_row(stock: dict) -> dict:
    """One /stocks row. Recorded tickers: delayed-feed price, alert only if the trigger flags a move."""
    change = stock["price"] - stock["prev_close"]
    row = {
        "ticker": stock["ticker"],
        "name": stock["name"],
        "price": stock["price"],
        "change": round(change, 2),
        "change_pct": round(100 * change / stock["prev_close"], 2),
        "alert": False,
    }
    row.update(_recorded_quote(stock["ticker"]) or {})
    return row


@app.get("/stocks")
def stocks(user: User = Depends(current_user)) -> dict:
    return {
        "holdings": [_stock_row(s) for s in HOLDINGS],
        "watchlist": [_stock_row(s) for s in WATCHLIST],
    }


# --- Catalog and watchlist ---------------------------------------------------

@app.get("/catalog")
def catalog_list(user: User = Depends(current_user)) -> list[dict]:
    """Stocks the picker offers: ticker, name, sector, demo_coverage. Demo-coverage stocks first."""
    return [{k: s[k] for k in ("ticker", "name", "sector", "demo_coverage", "fictional")}
            for s in catalog.stocks()]


def _watchlist_body(tickers: list[str]) -> dict:
    """The watchlist with display rows. Only demo-coverage stocks have a (recorded) price."""
    known = catalog.by_ticker()
    rows = []
    for ticker in tickers:
        entry = known.get(ticker)
        if entry is None:
            continue  # dropped from the catalog since it was saved
        row = {k: entry[k] for k in ("ticker", "name", "sector", "demo_coverage", "fictional")}
        row.update({"price": None, "change": None, "change_pct": None, "alert": False})
        if entry["demo_coverage"]:
            row.update(_recorded_quote(ticker) or {})
        rows.append(row)
    return {"tickers": [r["ticker"] for r in rows], "stocks": rows}


class WatchlistRequest(BaseModel):
    tickers: list[str] = Field(max_length=config.WATCHLIST_MAX)


@app.get("/watchlist")
def watchlist_get(user: User = Depends(current_user)) -> dict:
    return _watchlist_body(users.get_watchlist(user.id))


@app.put("/watchlist")
def watchlist_put(body: WatchlistRequest, user: User = Depends(current_user)) -> dict:
    """Replace the user's watchlist. Every ticker must be in the catalog; duplicates are dropped."""
    known = catalog.by_ticker()
    tickers = list(dict.fromkeys(t.strip().upper() for t in body.tickers))
    unknown = [t for t in tickers if t not in known]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Not in the stock catalog: {', '.join(unknown[:10])}.")
    users.set_watchlist(user.id, tickers)
    return _watchlist_body(tickers)


@app.get("/cases")
def cases(user: User = Depends(current_user)) -> list[dict]:
    """The recorded demo cases and their current results, for the Compare cases cards.

    Built from recorded evidence through the same pipeline as /investigate, so the
    labels always match what Investigate shows. Nothing is saved and no LLM is called.
    """
    rows = []
    for ticker in FIXTURES:
        card = pipeline.live_card(ticker) or fixture_card(ticker)
        if card is None:
            continue
        rows.append({
            "ticker": card.ticker,
            "label": card.confidence_label,
            "score": card.confidence_score,
            "market_leads": market_context.analyse(card.move).market_leads,
            "leading_explanation": card.leading_explanation,
        })
    return rows


@app.post("/investigate/{ticker}")
def investigate(ticker: str, user: User = Depends(current_user)) -> Card:
    card = get_card(ticker)
    save_history(card, "investigate", user_id=user.id)
    return card


@app.get("/receipt/{ticker}", response_model=None)
def receipt(ticker: str, format: str = "json", move_start_at: datetime | None = None,
            user: User = Depends(current_user)) -> JSONResponse | HTMLResponse:
    """Evidence Receipt: ?format=json (default) downloads a file, ?format=html is printable.

    ?move_start_at=... gives the Receipt for one move from the price history instead
    of the ticker's recorded case.
    """
    card = move_card(ticker, move_start_at) if move_start_at else get_card(ticker)
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
def simulate(ticker: str, user: User = Depends(current_user)) -> dict:
    """Run the trigger engine on the ticker's recorded series; if it flags a move, investigate that Move."""
    ticker = ticker.upper()
    case = market_data.find_case(ticker)
    if case is None:
        raise HTTPException(status_code=404,
                            detail=f"No recorded market data for {ticker}, so the trigger cannot run.")
    move = market_data.get_move(case, ticker)
    card = with_explanation(pipeline.card_for_move(move)) if move is not None else None
    if card is not None:
        save_history(card, "simulate", user_id=user.id)
    return {
        "flagged": move is not None,
        "move": move.model_dump(mode="json") if move else None,
        "thresholds": trigger_thresholds(),
        "card": card.model_dump(mode="json") if card else None,
    }


# --- One-month price history and its unusual moves -------------------------
# RECORDED DEMO DATA, NOT REAL PRICES (fixtures/price_history.json). These are
# not the /history routes, which are saved investigations.

PRICE_HISTORY_NOTE = "Recorded demo data, not real prices."


def _series_or_404(ticker: str) -> price_history.Series:
    series = price_history.get_series(ticker)
    if series is None:
        raise HTTPException(status_code=404,
                            detail=f"No price history for {ticker.upper()} in this demo.")
    return series


def _has_recorded_evidence(move: Move) -> bool:
    found = replay.find_case(move.ticker)
    return found is not None and found[1].move_start_at == move.move_start_at


@app.get("/price-history/{ticker}")
def price_history_route(ticker: str, user: User = Depends(current_user)) -> dict:
    """One month of bars and the unusual moves the trigger rules find in it (oldest first)."""
    series = _series_or_404(ticker)
    moves = []
    for m in price_history.detect_moves(series):
        row = m.to_dict()
        row["has_recorded_evidence"] = _has_recorded_evidence(m.move)
        moves.append(row)
    return {
        "ticker": series.ticker,
        "name": series.name,
        "sector": series.sector,
        "note": PRICE_HISTORY_NOTE,
        "thresholds": {**trigger_thresholds(),
                       "max_window_minutes": config.TRIGGER_DEFAULT_WINDOW_MINUTES
                       * config.PRICE_HISTORY_WINDOW_FACTOR},
        "bars": [{"at": b.at.isoformat(), "price": b.price, "volume": b.volume} for b in series.bars],
        "moves": moves,
    }


class MoveRequest(BaseModel):
    move_start_at: datetime  # must be the start of a detected move; read as IST if no time zone


def move_card(ticker: str, move_start_at: datetime) -> Card:
    """The Card for one detected move: its Move goes through the unchanged pipeline and engine."""
    if move_start_at.tzinfo is None:
        move_start_at = move_start_at.replace(tzinfo=IST)
    series = _series_or_404(ticker)
    found = price_history.find_move(series, move_start_at)
    if found is None:
        raise HTTPException(status_code=404, detail=(
            f"No unusual move for {series.ticker} starts at {move_start_at.isoformat()}."))
    return with_explanation(pipeline.card_for_move(found.move))


@app.post("/investigate-move/{ticker}")
def investigate_move(ticker: str, body: MoveRequest, user: User = Depends(current_user)) -> Card:
    """Investigate one unusual move from the price history. Saved to history as "investigate"."""
    card = move_card(ticker, body.move_start_at)
    save_history(card, "investigate", user_id=user.id)
    return card


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

# Which user started each live job: only they can poll it.
_live_owners: dict[str, int] = {}


@app.get("/live/status")
def live_status(user: User = Depends(current_user)) -> dict:
    """Whether live runs are allowed, and this user's running job if there is one."""
    reason = live_jobs.disabled_reason()
    active = live_jobs.active_job()
    mine = active is not None and _live_owners.get(active.id) == user.id
    return {
        "enabled": reason is None,
        "message": reason,
        "active_job_id": active.id if mine else None,
        "busy": active is not None,
        "poll_seconds": config.LIVE_POLL_SECONDS,
        "max_calls": config.MAX_TINYFISH_CALLS_PER_INVESTIGATION,
    }


@app.post("/live/start")
def live_start(request: LiveStartRequest, user: User = Depends(current_user)) -> dict:
    """Start one live investigation in the background. 403 if live is off, 409 if one is running."""
    move = live_jobs.build_move(
        request.ticker, request.move_start_at, request.window_minutes, request.pct_change,
        request.volume_multiple, request.sector_pct_change, request.index_pct_change)
    try:
        job = live_jobs.start_job(request.company.strip(), move, with_explanation,
                                  live_client_factory,
                                  on_done=partial(save_history, source="live", mode="live",
                                                  db_path=config.DB_PATH,  # path fixed at start
                                                  user_id=user.id))
    except live_jobs.LiveDisabled as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except live_jobs.LiveBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    _live_owners[job.id] = user.id
    return {"job_id": job.id}


@app.get("/live/{job_id}")
def live_job(job_id: str, user: User = Depends(current_user)) -> dict:
    job = live_jobs.get_job(job_id)
    if job is None or _live_owners.get(job_id) != user.id:
        raise HTTPException(status_code=404, detail="No live investigation with that id.")
    return job.to_dict()


# --- History (SQLite) --------------------------------------------------------

@app.get("/history")
def history_list(ticker: str | None = None, limit: int = history.MAX_LIMIT,
                 user: User = Depends(current_user)) -> list[dict]:
    """This user's past investigations, newest first, without the stored Card (max 50)."""
    return history.list_history(user.id, ticker, limit)


@app.get("/history/{entry_id}")
def history_entry(entry_id: int, user: User = Depends(current_user)) -> Card:
    """One of this user's stored Cards, exactly as it was produced (nothing re-runs)."""
    card = history.get_card(entry_id, user.id)
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
