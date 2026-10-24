"""Pipeline: Move -> investigators (in parallel) -> consensus engine -> Card.

Synchronous to callers; asyncio runs inside. Call from sync code (e.g. a plain
`def` FastAPI route, which runs in a worker thread), not from inside a running
event loop; async callers can await investigate() directly.
"""

import asyncio

from backend import config
from backend.consensus.engine import build_card_from_reports
from backend.investigators.atlas import Atlas
from backend.investigators.echo import Echo
from backend.investigators.sherlock import Sherlock
from backend.investigators.skeptic import Skeptic
from backend.models import Card, Move, Report
from backend.tinyfish import replay
from backend.tinyfish.client import TinyFishClient


async def investigate(move: Move, include_skeptic: bool = False,
                      client: TinyFishClient | None = None) -> list[Report]:
    """Run Atlas, Sherlock and Echo in parallel; the full Skeptic only if requested.

    Without a challenge, config.DEFAULT_INCLUDE_EARLIER_HINTS adds the Skeptic's
    earlier-hint lookup only. All investigators share one client, so they share
    one TinyFish call budget.
    """
    client = client or TinyFishClient()
    investigators = [Atlas(client), Sherlock(client), Echo(client)]
    if include_skeptic:
        investigators.append(Skeptic(client, requested=True))
    elif config.DEFAULT_INCLUDE_EARLIER_HINTS:
        investigators.append(Skeptic(client, earlier_hints_only=True))
    return list(await asyncio.gather(*(i.run(move) for i in investigators)))


def run_investigators(move: Move, include_skeptic: bool = False,
                      client: TinyFishClient | None = None) -> list[Report]:
    """Synchronous wrapper around investigate()."""
    return asyncio.run(investigate(move, include_skeptic, client))


def card_for_move(move: Move, include_skeptic: bool = False,
                  client: TinyFishClient | None = None) -> Card:
    """Run the investigators and the consensus engine for a given Move (e.g. one the trigger flagged)."""
    return build_card_from_reports(move, run_investigators(move, include_skeptic, client))


def live_card(ticker: str, include_skeptic: bool = False) -> Card | None:
    """Build the Card for a ticker, or None if there is no case for it.

    The Move comes from the recorded case (replay.find_case). /simulate is the
    path where the trigger engine produces the Move instead.
    """
    found = replay.find_case(ticker)
    if found is None:
        return None
    _, move = found
    return card_for_move(move, include_skeptic)
