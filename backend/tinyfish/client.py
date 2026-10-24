"""Live TinyFish client (web agents, search, fetch, browser, monitor). Used only to acquire evidence. Key read from .env.

Placeholder: every method spends one call from the CallBudget and then raises
NotImplementedError until the live API is wired in.
"""

import logging

from backend import config

log = logging.getLogger(__name__)

NOT_CONNECTED = "Live TinyFish not connected yet"


class CallBudgetExceeded(RuntimeError):
    """Raised instead of making a TinyFish call that would go over the budget."""


class CallBudget:
    """Counts TinyFish calls for one investigation and refuses any beyond the limit."""

    def __init__(self, limit: int | None = None):
        self.limit = config.MAX_TINYFISH_CALLS_PER_INVESTIGATION if limit is None else limit
        self.calls: list[str] = []

    @property
    def used(self) -> int:
        return len(self.calls)

    def spend(self, tool: str, detail: str = "") -> None:
        """Record one call, or raise CallBudgetExceeded if the budget is used up."""
        if self.used >= self.limit:
            log.warning("TinyFish budget exhausted (%d calls); refused %s %s",
                        self.limit, tool, detail)
            raise CallBudgetExceeded(
                f"TinyFish call budget of {self.limit} used up; refused {tool} {detail}".strip())
        self.calls.append(f"{tool} {detail}".strip())
        log.info("TinyFish call %d/%d: %s %s", self.used, self.limit, tool, detail)


class TinyFishClient:
    """One client per investigation; all investigators share its budget."""

    def __init__(self, api_key: str | None = None, budget: CallBudget | None = None):
        self.api_key = config.TINYFISH_API_KEY if api_key is None else api_key
        self.budget = budget or CallBudget()

    def _call(self, tool: str, detail: str) -> None:
        self.budget.spend(tool, detail)
        raise NotImplementedError(NOT_CONNECTED)

    async def agent(self, goal: str, url: str | None = None) -> dict:
        """Run a web agent towards a goal, optionally starting at a URL."""
        self._call("agent", goal)

    async def search(self, query: str) -> list[dict]:
        """Web search; returns result items."""
        self._call("search", query)

    async def fetch(self, url: str) -> str:
        """Fetch a page and return its text."""
        self._call("fetch", url)

    async def browser(self, url: str, instructions: str = "") -> str:
        """Open a page in a real browser (for JS-heavy sites) and return its text."""
        self._call("browser", url)

    async def monitor(self, url: str) -> dict:
        """Watch a source for new filings (alerts and timing checks, not price moves)."""
        self._call("monitor", url)

    async def research(self, question: str) -> dict:
        """Multi-step research on a question."""
        self._call("research", question)
