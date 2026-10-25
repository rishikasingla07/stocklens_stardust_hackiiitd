"""Pipeline: Move -> investigators (in parallel) -> consensus engine -> Card.

Synchronous to callers; asyncio runs inside. Call from sync code (e.g. a plain
`def` FastAPI route, which runs in a worker thread), not from inside a running
event loop; async callers can await investigate() directly.
"""

import asyncio

from backend.consensus.engine import build_card_from_reports
from backend.investigators.atlas import Atlas
from backend.investigators.echo import Echo
from backend.investigators.sherlock import Sherlock
from backend.models import Card, Move, Report
from backend.tinyfish import replay
from backend.tinyfish.client import TinyFishClient


async def investigate(move: Move, client: TinyFishClient | None = None) -> list[Report]:
    """Run Atlas, Sherlock and Echo in parallel on recorded evidence.

    Always replay, whatever STOCKLENS_MODE says: this pipeline serves Investigate,
    Simulate move and the Evidence Receipt for the recorded demo cases, which must
    stay deterministic and must never spend TinyFish credits. Live retrieval is
    only "Run live investigation" (backend/live_jobs.py), which asks Sherlock for
    live mode explicitly. All investigators share one client and call budget.
    """
    client = client or TinyFishClient()
    investigators = [Atlas(client, mode="replay"), Sherlock(client, mode="replay"),
                     Echo(client, mode="replay")]
    return list(await asyncio.gather(*(i.run(move) for i in investigators)))


def run_investigators(move: Move, client: TinyFishClient | None = None) -> list[Report]:
    """Synchronous wrapper around investigate()."""
    return asyncio.run(investigate(move, client))


def card_for_move(move: Move, client: TinyFishClient | None = None) -> Card:
    """Run the investigators and the consensus engine for a given Move (e.g. one the trigger flagged)."""
    return build_card_from_reports(move, run_investigators(move, client))


def live_card(ticker: str) -> Card | None:
    """Build the Card for a ticker, or None if there is no case for it.

    The Move comes from the recorded case (replay.find_case). /simulate is the
    path where the trigger engine produces the Move instead.
    """
    found = replay.find_case(ticker)
    if found is None:
        return None
    _, move = found
    return card_for_move(move)
