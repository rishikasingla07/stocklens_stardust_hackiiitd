"""Echo (crowd): what is the crowd saying?

Reads public posts and commentary.
TinyFish tools: Search, Fetch, Browser.
Always Tier 3, always verified=False. Context only, never proof.
"""

from backend.investigators.base import Investigator
from backend.models import Evidence, Move


class Echo(Investigator):
    name = "echo"

    async def gather_live(self, move: Move) -> list[Evidence]:
        raise NotImplementedError("echo: live mode not implemented yet")

    def enforce(self, evidence: Evidence) -> Evidence | None:
        """Crowd evidence is always Tier 3 and never verified, whatever the source claims."""
        return evidence.model_copy(update={"source_tier": 3, "verified": False})
