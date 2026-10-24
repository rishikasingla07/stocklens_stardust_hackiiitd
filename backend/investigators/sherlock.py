"""Sherlock (company): did the company itself announce something?

Locates NSE and BSE filings for the move window, opens each filing and traces
cited news back to it.
TinyFish tools: Search, Agent, Fetch, Browser.
Hands over: filing text, quoted snippet and official timestamp.
Filings are Tier 1; company news is Tier 2.

Live path (MODE=live): one Search for filings around the move day, one Fetch
of up to SHERLOCK_MAX_FETCHES exchange-domain pages, then LLM extraction of
each page. Pages without a timestamp are skipped. Never invents evidence.
"""

import asyncio
import hashlib
from datetime import timedelta
from urllib.parse import urlparse

from backend import config
from backend.investigators.base import Investigator
from backend.llm.extract import extract_evidence
from backend.models import Evidence, Move, Report
from backend.tinyfish.client import CallBudgetExceeded, TinyFishClient, TinyFishError


def is_exchange_filing(url: str) -> bool:
    """True if the URL is on an exchange filing domain (e.g. nseindia.com)."""
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in config.EXCHANGE_FILING_DOMAINS)


def filing_source_name(url: str) -> str:
    """'NSE corporate filing' or 'BSE corporate filing', from the URL's domain."""
    host = (urlparse(url).hostname or "").lower()
    return "BSE corporate filing" if "bseindia" in host else "NSE corporate filing"


def _evidence_id(url: str) -> str:
    return "sherlock-live-" + hashlib.sha1(url.encode()).hexdigest()[:10]


class Sherlock(Investigator):
    name = "sherlock"

    def __init__(self, client: TinyFishClient | None = None, mode: str | None = None,
                 company: str | None = None):
        super().__init__(client, mode)
        self.company = company  # search by company name if given, else by ticker

    async def gather_live(self, move: Move) -> Report:
        """Search, fetch and extract real exchange filings. Always returns a Report, never raises."""
        if not self.client.has_key:
            return Report(investigator=self.name,
                          notes="live: TINYFISH_API_KEY not set; no evidence gathered")
        evidence: list[Evidence] = []
        notes: list[str] = []
        try:
            await self._search_fetch_extract(move, evidence, notes)
        except (TinyFishError, CallBudgetExceeded) as exc:
            notes.append(f"live stopped: {exc}")
        notes.append(f"calls used {self.client.budget.used}/{self.client.budget.limit}")
        return Report(investigator=self.name, evidence=evidence, notes="; ".join(notes))

    async def _search_fetch_extract(self, move: Move, evidence: list[Evidence],
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
        to_fetch = urls[:config.SHERLOCK_MAX_FETCHES]
        notes.append(f"live: {len(results)} search result(s), {len(urls)} on exchange domains")
        if not to_fetch:
            notes.append("no exchange filing found to fetch")
            return

        fetched = await self.client.fetch(
            to_fetch, purpose=f"Read the filing text and its official timestamp for {name}.")
        pages = fetched.get("results") or []
        errors = fetched.get("errors") or []
        rejected = 0
        for page in pages:
            url = page.get("final_url") or page.get("url") or ""
            text = page.get("text") or ""
            if not text.strip() or not is_exchange_filing(url):
                rejected += 1  # empty page, or redirected off the exchange
                continue
            item = await asyncio.to_thread(
                extract_evidence, "filing", text, name, move,
                filing_source_name(url), url, _evidence_id(url), self.name)
            if item is None:
                rejected += 1  # no timestamp, nothing relevant, or LLM unavailable
            else:
                evidence.append(item)
        notes.append(f"fetched {len(pages)} of {len(to_fetch)} page(s), {len(errors)} fetch "
                     f"error(s), rejected {rejected} (no timestamp, not relevant or unreadable), "
                     f"kept {len(evidence)}")

    def enforce(self, evidence: Evidence) -> Evidence | None:
        """Exchange filings are Tier 1; everything else Sherlock finds is company news, Tier 2."""
        tier = 1 if is_exchange_filing(evidence.url) else 2
        return evidence.model_copy(update={"source_tier": tier})
