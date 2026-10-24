"""Skeptic (against): what argues against the leading cause?

Hunts for denials, conflicts and earlier mentions.
TinyFish tools: Search, Fetch.
Full run only when the user taps "Challenge This".
Hands over: counter-evidence, or a clean "none found".

Default investigations (config.DEFAULT_INCLUDE_EARLIER_HINTS) run only the
earlier-hint lookup: Skeptic items public before the move started.
"""

from backend.investigators.base import Investigator
from backend.models import Evidence, Move, Report
from backend.tinyfish.client import TinyFishClient

NOT_REQUESTED = "Skeptic not run: it runs only when the user taps Challenge This."
NONE_FOUND = "none found"
EARLIER_HINTS_ONLY = "earlier-hint lookup only (default run)"


class Skeptic(Investigator):
    name = "skeptic"

    def __init__(self, client: TinyFishClient | None = None, mode: str | None = None,
                 requested: bool = False, earlier_hints_only: bool = False):
        super().__init__(client, mode)
        self.requested = requested
        self.earlier_hints_only = earlier_hints_only

    async def run(self, move: Move) -> Report:
        """Gather nothing (and spend no calls) unless asked for a challenge or the hint lookup."""
        if self.requested:
            report = await super().run(move)
            if not report.evidence:
                report.notes = "; ".join(n for n in (report.notes, NONE_FOUND) if n)
            return report
        if not self.earlier_hints_only:
            return Report(investigator=self.name, notes=NOT_REQUESTED)
        try:
            report = await super().run(move)
        except NotImplementedError as exc:
            return Report(investigator=self.name, notes=f"{EARLIER_HINTS_ONLY}: skipped ({exc})")
        report.evidence = [e for e in report.evidence
                           if e.first_public_at is not None and e.first_public_at < move.move_start_at]
        report.notes = "; ".join(n for n in (report.notes, EARLIER_HINTS_ONLY) if n)
        return report

    async def gather_live(self, move: Move) -> list[Evidence]:
        raise NotImplementedError("skeptic: live mode not implemented yet")
