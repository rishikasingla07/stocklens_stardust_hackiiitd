"""Live investigation jobs: run Sherlock live in a background thread and build a Card.

One job at a time (TinyFish credits are tight). Each job records short steps as
they happen, so the frontend can show real progress by polling. Sherlock is the
only live investigator; the others use recorded replay data if the ticker has a
recorded case, otherwise they return nothing ("not connected live").
Evidence is never invented: if no usable filing is found, the Card is an honest
Unknown and its explanation lists why each page was rejected.
"""

import asyncio
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from backend import config
from backend.consensus.engine import build_card_from_reports
from backend.investigators.atlas import Atlas
from backend.investigators.echo import Echo
from backend.investigators.sherlock import Sherlock
from backend.investigators.skeptic import Skeptic
from backend.llm.explain import NOT_ADVICE
from backend.models import IST, Card, Move, Report
from backend.tinyfish import replay
from backend.tinyfish.client import TinyFishClient

NOT_CONNECTED = "not connected live"
OTHERS = ("atlas", "echo", "skeptic")


class LiveDisabled(Exception):
    """Live mode is off or the TinyFish key is missing. The message is safe to show."""


class LiveBusy(Exception):
    """Another live job is still running."""


@dataclass
class LiveJob:
    id: str
    company: str
    ticker: str
    status: str = "queued"  # queued | running | done | failed
    steps: list[str] = field(default_factory=list)
    card: Card | None = None
    error: str | None = None
    calls_used: int = 0
    agent_calls: int = 0
    others: str | None = None  # "replay" or "not connected live", once known
    client: TinyFishClient | None = field(default=None, repr=False)  # read live for call counts
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def step(self, message: str) -> None:
        with self._lock:
            self.steps.append(_redact(message))

    def to_dict(self) -> dict:
        """The GET /live/{job_id} body."""
        if self.client is not None:
            _count_calls(self, self.client)
        with self._lock:
            return {
                "job_id": self.id,
                "ticker": self.ticker,
                "status": self.status,
                "steps": list(self.steps),
                "card": self.card.model_dump(mode="json") if self.card else None,
                "error": self.error,
                "calls_used": self.calls_used,
                "agent_calls": self.agent_calls,
                "others": self.others,
            }


_jobs: "OrderedDict[str, LiveJob]" = OrderedDict()
_jobs_lock = threading.Lock()


def _redact(text: str) -> str:
    """Belt and braces: the API key never leaves the server, even inside an error message."""
    key = config.TINYFISH_API_KEY.strip()
    return text.replace(key, "***") if key else text


def disabled_reason() -> str | None:
    """Why live runs are not allowed right now, or None if they are."""
    if config.MODE != "live":
        return ("Live investigations are off. Set STOCKLENS_MODE=live and TINYFISH_API_KEY "
                "in .env and restart the server. Replay demos still work.")
    if not config.TINYFISH_API_KEY.strip():
        return ("Live investigations need a TinyFish key. Set TINYFISH_API_KEY in .env and "
                "restart the server.")
    return None


def active_job() -> LiveJob | None:
    with _jobs_lock:
        return next((j for j in _jobs.values() if j.status in ("queued", "running")), None)


def get_job(job_id: str) -> LiveJob | None:
    with _jobs_lock:
        return _jobs.get(job_id)


def build_move(ticker: str, move_start_at: datetime, window_minutes: int, pct_change: float,
               volume_multiple: float, sector_pct_change: float, index_pct_change: float) -> Move:
    """A Move from user-supplied numbers. A time without a time zone is read as IST."""
    if move_start_at.tzinfo is None:
        move_start_at = move_start_at.replace(tzinfo=IST)
    return Move(ticker=ticker.upper(), move_start_at=move_start_at, window_minutes=window_minutes,
                pct_change=pct_change, volume_multiple=volume_multiple,
                sector_pct_change=sector_pct_change, index_pct_change=index_pct_change)


def start_job(company: str, move: Move, explain: Callable[[Card], Card],
              client_factory: Callable[[], TinyFishClient] = TinyFishClient,
              on_done: Callable[[Card], None] | None = None) -> LiveJob:
    """Start one live job in a background thread. Raises LiveDisabled or LiveBusy.

    `on_done` is called with the finished Card (e.g. to save it to history).
    """
    reason = disabled_reason()
    if reason:
        raise LiveDisabled(reason)
    with _jobs_lock:
        busy = next((j for j in _jobs.values() if j.status in ("queued", "running")), None)
        if busy:
            raise LiveBusy(f"A live investigation for {busy.ticker} is already running. "
                           "Wait for it to finish, then try again.")
        job = LiveJob(id=uuid.uuid4().hex[:12], company=company, ticker=move.ticker)
        _jobs[job.id] = job
        while len(_jobs) > config.LIVE_JOBS_KEPT:
            oldest = next(iter(_jobs))
            if _jobs[oldest].status in ("queued", "running"):
                break
            _jobs.pop(oldest)
    threading.Thread(target=_run, args=(job, move, explain, client_factory, on_done),
                     name=f"live-{job.id}", daemon=True).start()
    return job


def _run(job: LiveJob, move: Move, explain: Callable[[Card], Card],
         client_factory: Callable[[], TinyFishClient],
         on_done: Callable[[Card], None] | None = None) -> None:
    """Background thread body. Never raises; failures end as status=failed with a safe message."""
    client = None
    try:
        job.status = "running"
        job.step(f"Starting live investigation for {job.company} ({move.ticker})")
        client = client_factory()
        job.client = client
        sherlock = Sherlock(client, mode="live", company=job.company, progress=job.step)
        replay_case = replay.find_case(move.ticker) is not None
        job.others = "replay" if replay_case else NOT_CONNECTED
        reports, others_note = asyncio.run(_investigate(sherlock, move, client, replay_case))
        job.step(others_note)

        job.step("Scoring evidence")
        card = build_card_from_reports(move, reports)
        card = card.model_copy(update={"limits": card.limits + _live_limits(sherlock, others_note, client)})
        job.step("Writing the explanation")
        card = explain(card)
        card = card.model_copy(update={"explanation_text": _with_page_outcomes(card, sherlock)})
        _count_calls(job, client)
        if on_done is not None:
            on_done(card)  # e.g. save to history; must not fail the job (the caller logs errors)
        job.card = card
        job.step(f"Done: {card.confidence_label}")
        job.status = "done"
    except Exception as exc:  # any failure must reach the user as a message, not a traceback
        if client is not None:
            _count_calls(job, client)
        job.error = _redact(f"Live investigation failed: {exc}")
        job.step("Failed")
        job.status = "failed"


async def _investigate(sherlock: Sherlock, move: Move, client: TinyFishClient,
                       replay_case: bool) -> tuple[list[Report], str]:
    """Live Sherlock, plus replay data for the others only if the ticker has a recorded case."""
    if not replay_case:
        others = [Report(investigator=name, notes=f"{name}: {NOT_CONNECTED}") for name in OTHERS]
        return [await sherlock.run(move), *others], "Atlas, Echo, Skeptic: not connected live"
    investigators = [sherlock, Atlas(client, mode="replay"), Echo(client, mode="replay")]
    if config.DEFAULT_INCLUDE_EARLIER_HINTS:
        investigators.append(Skeptic(client, mode="replay", earlier_hints_only=True))
    reports = list(await asyncio.gather(*(i.run(move) for i in investigators)))
    return reports, "Atlas, Echo, Skeptic: recorded replay data for this ticker (not live)"


def _count_calls(job: LiveJob, client: TinyFishClient) -> None:
    job.calls_used = client.budget.used
    job.agent_calls = sum(1 for call in client.budget.calls if call.startswith("agent"))


def _live_limits(sherlock: Sherlock, others_note: str, client: TinyFishClient) -> list[str]:
    """Card limits that say what was live, what was supplied by the user, and what each page gave."""
    limits = [
        "Live run: the move numbers (price change, volume multiple, sector and index change) "
        "were entered by the user, not taken from live market data.",
        f"Live run: only Sherlock searched live. {others_note}.",
    ]
    for url, outcome in sherlock.page_outcomes:
        if outcome != "kept":
            limits.append(f"Live filing page rejected: {url} ({outcome}).")
    if sherlock.stopped:
        limits.append(f"Live search stopped early: {sherlock.stopped}.")
    elif not sherlock.page_outcomes:
        limits.append("Live search found no NSE/BSE filing page to read.")
    limits.append(f"TinyFish calls used: {client.budget.used} of {client.budget.limit}.")
    return [_redact(limit) for limit in limits]


def _with_page_outcomes(card: Card, sherlock: Sherlock) -> str:
    """The explanation, plus a plain account of each live page that was rejected (or that none was found)."""
    rejected = [(url, why) for url, why in sherlock.page_outcomes if why != "kept"]
    parts = []
    if rejected:
        pages = "; ".join(f"{url} was rejected because of {why}" for url, why in rejected)
        parts.append(f"Live search: {len(rejected)} filing page(s) were read and set aside: {pages}.")
    if sherlock.stopped:
        parts.append(f"Live search stopped early: {sherlock.stopped}.")
    elif not sherlock.page_outcomes:
        parts.append("Live search: no NSE or BSE filing page was found for this company around the move.")
    if not parts:
        return card.explanation_text
    text = card.explanation_text.rstrip()
    # Keep the not-advice sentence last.
    tail = NOT_ADVICE if text.endswith(NOT_ADVICE) else ""
    body = text[:len(text) - len(tail)].rstrip() if tail else text
    return _redact(" ".join(p for p in [body, *parts, tail] if p))


def reset_for_tests() -> None:
    with _jobs_lock:
        _jobs.clear()
