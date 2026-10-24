"""Base Investigator interface: takes a Move, returns a Report. Investigators gather evidence only; they never score."""

from backend import config
from backend.models import Evidence, InvestigatorName, Move, Report
from backend.tinyfish import replay
from backend.tinyfish.client import TinyFishClient


class Investigator:
    """Gathers evidence for a Move and hands it over in a Report.

    Replay mode reads recorded fixtures; live mode calls gather_live(), which
    each subclass implements with TinyFish. Every item then passes through
    enforce(), which applies the investigator's role rules in code.
    """

    name: InvestigatorName

    def __init__(self, client: TinyFishClient | None = None, mode: str | None = None):
        self.client = client or TinyFishClient()
        self.mode = mode

    async def run(self, move: Move) -> Report:
        mode = self.mode or config.MODE
        if mode == "replay":
            raw, notes = self._load_replay(move)
        else:
            raw, notes = await self.gather_live(move), []
        evidence = []
        for item in raw:
            kept = self.enforce(item.model_copy(update={"investigator": self.name}))
            if kept is None:
                notes.append(f"dropped {item.id}: not allowed for {self.name}")
            else:
                evidence.append(kept)
        return Report(investigator=self.name, evidence=evidence, notes="; ".join(notes))

    def _load_replay(self, move: Move) -> tuple[list[Evidence], list[str]]:
        found = replay.find_case(move.ticker)
        if found is None:
            return [], [f"replay: no recorded case for {move.ticker}"]
        case_dir, _ = found
        return replay.load_evidence(case_dir, self.name), [f"replay: {case_dir.name}"]

    async def gather_live(self, move: Move) -> list[Evidence]:
        """Collect evidence with TinyFish. Subclasses implement this."""
        raise NotImplementedError(f"{self.name}: live mode not implemented yet")

    def enforce(self, evidence: Evidence) -> Evidence | None:
        """Apply this investigator's tier/verification rules. Return None to drop the item."""
        return evidence
