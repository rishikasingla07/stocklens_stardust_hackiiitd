"""Sherlock (company): did the company itself announce something?

Locates NSE and BSE filings for the move window, opens each filing and traces
cited news back to it.
TinyFish tools: Search, Agent, Fetch, Browser.
Hands over: filing text, quoted snippet and official timestamp.
Filings are Tier 1; company news is Tier 2.

Live path (MODE=live), TinyFish key only, no LLM key: one Search for filings
around the move day, then for up to SHERLOCK_MAX_AGENT_PAGES exchange-domain
results one Agent call to read the page and one Fetch to verify the quoted
snippet (backend/tinyfish/extract_agent.py). Pages without a usable timestamp
are rejected. Never invents evidence.
"""

from datetime import timedelta

from backend import config
from backend.investigators.base import Investigator
from backend.models import Evidence, Move, Report
from backend.tinyfish.client import CallBudgetExceeded, TinyFishClient, TinyFishError
from backend.tinyfish.extract_agent import extract_filing, is_exchange_filing


class Sherlock(Investigator):
    name = "sherlock"

    def __init__(self, client: TinyFishClient | None = None, mode: str | None = None,
                 company: str | None = None):
        super().__init__(client, mode)
        self.company = company  # search by company name if given, else by ticker

    async def gather_live(self, move: Move) -> Report:
        """Search for filings, then read each with the Agent. Always returns a Report, never raises."""
        if not self.client.has_key:
            return Report(investigator=self.name,
                          notes="live: TINYFISH_API_KEY not set; no evidence gathered")
        evidence: list[Evidence] = []
        notes: list[str] = []
        try:
            await self._search_and_extract(move, evidence, notes)
        except (TinyFishError, CallBudgetExceeded) as exc:
            notes.append(f"live stopped: {exc}")
        notes.append(f"calls used {self.client.budget.used}/{self.client.budget.limit}")
        return Report(investigator=self.name, evidence=evidence, notes="; ".join(notes))

    async def _search_and_extract(self, move: Move, evidence: list[Evidence],
                                  notes: list[str]) -> None:
        name = self.company or move.ticker
        day = move.move_start_at.date()
        found = await self.client.search(
            f"{name} corporate announcement filing NSE BSE",
            after_date=(day - timedelta(days=config.SHERLOCK_LOOKBACK_DAYS)).isoformat(),
            before_date=day.isoformat(),
            purpose=f"Find official NSE/BSE exchange filings by {name} around {day}.",
        )
        results = found.get("results") or []
        urls = list(dict.fromkeys(r["url"] for r in results
                                  if r.get("url") and is_exchange_filing(r["url"])))
        pages = urls[:config.SHERLOCK_MAX_AGENT_PAGES]
        notes.append(f"live: {len(results)} search result(s), {len(urls)} on exchange domains")
        if not pages:
            notes.append("no exchange filing found to read")
            return

        rejected: list[str] = []
        try:
            for url in pages:
                try:
                    result = await extract_filing(self.client, url, name, move)
                except TinyFishError as exc:
                    rejected.append(f"{url}: {exc}")
                    continue
                if result.evidence is None:
                    rejected.append(f"{url}: {result.reason}")
                else:
                    evidence.append(result.evidence)
        finally:
            notes.append(f"pages found {len(pages)}, extracted {len(evidence)}, "
                         f"rejected {len(rejected)}")
            notes.extend(f"rejected {r}" for r in rejected)

    def enforce(self, evidence: Evidence) -> Evidence | None:
        """Exchange filings are Tier 1; everything else Sherlock finds is company news, Tier 2."""
        tier = 1 if is_exchange_filing(evidence.url) else 2
        return evidence.model_copy(update={"source_tier": tier})
