"""Skeptic (against): what argues against the leading cause?

Hunts for denials, conflicts and earlier mentions.
TinyFish tools: Search, Fetch.
Only runs when the user taps "Challenge This".
Hands over: counter-evidence, or a clean "none found".
"""

from backend.investigators.base import Investigator
from backend.models import Evidence, Move, Report
from backend.tinyfish.client import TinyFishClient

NOT_REQUESTED = "Skeptic not run: it runs only when the user taps Challenge This."
NONE_FOUND = "none found"


class Skeptic(Investigator):
    name = "skeptic"

    def __init__(self, client: TinyFishClient | None = None, mode: str | None = None,
                 requested: bool = False):
        super().__init__(client, mode)
        self.requested = requested

    async def run(self, move: Move) -> Report:
        """Gather nothing (and spend no calls) unless the user asked for a challenge."""
        if not self.requested:
            return Report(investigator=self.name, notes=NOT_REQUESTED)
        report = await super().run(move)
        if not report.evidence:
            report.notes = "; ".join(n for n in (report.notes, NONE_FOUND) if n)
        return report

    async def gather_live(self, move: Move) -> list[Evidence]:
        raise NotImplementedError("skeptic: live mode not implemented yet")
