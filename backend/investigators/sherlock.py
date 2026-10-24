"""Sherlock (company): did the company itself announce something?

Locates NSE and BSE filings for the move window, opens each filing and traces
cited news back to it.
TinyFish tools: Search, Agent, Fetch, Browser.
Hands over: filing text, quoted snippet and official timestamp.
Filings are Tier 1; company news is Tier 2.
"""

from urllib.parse import urlparse

from backend import config
from backend.investigators.base import Investigator
from backend.models import Evidence, Move


def is_exchange_filing(url: str) -> bool:
    """True if the URL is on an exchange filing domain (e.g. nseindia.com)."""
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in config.EXCHANGE_FILING_DOMAINS)


class Sherlock(Investigator):
    name = "sherlock"

    async def gather_live(self, move: Move) -> list[Evidence]:
        raise NotImplementedError("sherlock: live mode not implemented yet")

    def enforce(self, evidence: Evidence) -> Evidence | None:
        """Exchange filings are Tier 1; everything else Sherlock finds is company news, Tier 2."""
        tier = 1 if is_exchange_filing(evidence.url) else 2
        return evidence.model_copy(update={"source_tier": tier})
