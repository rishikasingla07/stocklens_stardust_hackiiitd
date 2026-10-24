"""Atlas (macro): did the market or sector cause this?

Finds policy, sector and macro events in the move window and opens official
release pages for exact dates.
TinyFish tools: Search, Research, Agent.
Hands over: a dated macro cause with a link. Tier 1 or 2.
"""

from backend.investigators.base import Investigator
from backend.models import Evidence, Move


class Atlas(Investigator):
    name = "atlas"

    async def gather_live(self, move: Move) -> list[Evidence]:
        raise NotImplementedError("atlas: live mode not implemented yet")

    def enforce(self, evidence: Evidence) -> Evidence | None:
        """Atlas hands over Tier 1 or 2 only; Tier 3 is dropped, never promoted."""
        return evidence if evidence.source_tier in (1, 2) else None
