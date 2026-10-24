"""Live TinyFish client (web agents, search, fetch, browser, monitor). Used only to acquire evidence. Key read from .env.

search(), fetch() and agent() are live (docs.tinyfish.ai/search-api.md,
fetch-api.md, agent-api.md), authenticated with the X-API-Key header. Every
successful response is cached in fixtures/live_cache/ and an identical request
is served from there, free. browser(), monitor() and research() are still
placeholders.
The API key is never logged, cached or put in an error message.
"""

import json
import logging
from collections.abc import Callable
from pathlib import Path

import httpx

from backend import config
from backend.tinyfish import cache

log = logging.getLogger(__name__)

NOT_CONNECTED = "Live TinyFish not connected yet"


class CallBudgetExceeded(RuntimeError):
    """Raised instead of making a TinyFish call that would go over the budget."""


class TinyFishError(RuntimeError):
    """A live TinyFish call failed. The message is safe to show (no key, no traceback)."""


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


def _error_message(tool: str, response: httpx.Response) -> str:
    """'TinyFish search failed: HTTP 402 INSUFFICIENT_CREDITS: ...' from the documented error body."""
    code, message = "", ""
    try:
        error = response.json().get("error")
        if isinstance(error, dict):
            code, message = error.get("code", ""), error.get("message", "")
        elif isinstance(error, str):
            message = error
    except ValueError:
        pass
    detail = ": ".join(p for p in (code, message) if p)
    return f"TinyFish {tool} failed: HTTP {response.status_code}" + (f" {detail}" if detail else "")


def agent_answer(data: dict) -> dict:
    """The answer object from an Agent /run response, or TinyFishError."""
    if data.get("status") != "COMPLETED":
        error = data.get("error") if isinstance(data.get("error"), dict) else {}
        detail = ": ".join(p for p in (error.get("code"), error.get("message")) if p)
        raise TinyFishError(f"TinyFish agent run {data.get('status') or 'did not complete'}"
                            + (f": {detail}" if detail else ""))
    result = data.get("result")
    if isinstance(result, dict) and set(result) == {"result"}:
        result = result["result"]  # list or text answers sit under result.result
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except ValueError:
                pass
    if not isinstance(result, dict):
        raise TinyFishError("TinyFish agent returned no JSON object")
    return result


class TinyFishClient:
    """One client per investigation; all investigators share its budget.

    `transport` lets tests swap in httpx.MockTransport; `cache_dir` lets them
    use a temporary cache. A cache hit costs no budget.
    """

    def __init__(self, api_key: str | None = None, budget: CallBudget | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 cache_dir: Path | None = None):
        self.api_key = config.TINYFISH_API_KEY if api_key is None else api_key
        self.budget = budget or CallBudget()
        self.transport = transport
        self.cache_dir = cache_dir
        self.cache_files: list[Path] = []  # cache files read or written by this client
        self.cache_hits = 0

    @property
    def has_key(self) -> bool:
        return bool(self.api_key.strip())

    def _call(self, tool: str, detail: str) -> None:
        self.budget.spend(tool, detail)
        raise NotImplementedError(NOT_CONNECTED)

    async def _request(self, tool: str, method: str, url: str, detail: str,
                       params: dict | None = None, body: dict | None = None,
                       timeout: float | None = None,
                       cacheable: Callable[[dict], bool] | None = None) -> dict:
        """Cached, budgeted, authenticated request. Raises TinyFishError / CallBudgetExceeded.

        `cacheable` decides whether a 200 response is saved (e.g. not a FAILED agent run).
        """
        timeout = timeout or config.TINYFISH_TIMEOUT_SECONDS
        key = cache.cache_key(tool, method, url, params, body)
        cached = cache.load(key, self.cache_dir)
        if cached is not None:
            self.cache_hits += 1
            self.cache_files.append(cache.cache_path(key, self.cache_dir))
            log.info("TinyFish cache hit: %s %s", tool, detail)
            return cached
        if not self.has_key:
            raise TinyFishError("TINYFISH_API_KEY is not set in .env")

        self.budget.spend(tool, detail)
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=timeout) as http:
                response = await http.request(method, url, params=params, json=body,
                                              headers={"X-API-Key": self.api_key.strip()})
        except httpx.TimeoutException:
            raise TinyFishError(f"TinyFish {tool} timed out after {timeout:g} s") from None
        except httpx.HTTPError as exc:
            raise TinyFishError(f"TinyFish {tool} request failed ({type(exc).__name__})") from None

        if response.status_code >= 400:
            raise TinyFishError(_error_message(tool, response))
        try:
            data = response.json()
        except ValueError:
            raise TinyFishError(f"TinyFish {tool} returned a non-JSON response") from None

        if cacheable is None or cacheable(data):
            path = cache.save(key, {"tool": tool, "method": method, "url": url,
                                    "params": params, "body": body}, data, self.cache_dir)
            self.cache_files.append(path)
        return data

    async def search(self, query: str, after_date: str | None = None,
                     before_date: str | None = None, domain_type: str | None = None,
                     purpose: str | None = None) -> dict:
        """Web search. Dates are YYYY-MM-DD. Returns {query, results: [{title, url, snippet, ...}], ...}."""
        params = {"query": query, "after_date": after_date, "before_date": before_date,
                  "domain_type": domain_type, "purpose": purpose}
        params = {k: v for k, v in params.items() if v is not None}
        return await self._request("search", "GET", config.TINYFISH_SEARCH_URL, query,
                                   params=params)

    async def fetch(self, urls: str | list[str], format: str = "markdown",
                    purpose: str | None = None) -> dict:
        """Fetch up to 10 pages in one call. Returns {results: [{url, final_url, text, ...}], errors: [...]}."""
        urls = [urls] if isinstance(urls, str) else list(urls)
        body = {"urls": urls, "format": format}
        if purpose:
            body["purpose"] = purpose
        return await self._request("fetch", "POST", config.TINYFISH_FETCH_URL, " ".join(urls),
                                   body=body)

    async def agent(self, url: str, goal: str, output_schema: dict | None = None) -> dict:
        """Run a web agent on `url` towards `goal` (synchronous /run). Returns the answer object.

        The answer is `result` when it is a JSON object, or the object under
        `result.result`. A FAILED run, or a result that is not an object, raises
        TinyFishError and is not cached.
        """
        body: dict = {"url": url, "goal": goal,
                      "agent_config": {"max_duration_seconds": config.TINYFISH_AGENT_MAX_DURATION_SECONDS}}
        if output_schema is not None:
            body["output_schema"] = output_schema
        data = await self._request("agent", "POST", config.TINYFISH_AGENT_URL, url, body=body,
                                   timeout=config.TINYFISH_AGENT_TIMEOUT_SECONDS,
                                   cacheable=lambda d: d.get("status") == "COMPLETED")
        return agent_answer(data)

    async def browser(self, url: str, instructions: str = "") -> str:
        """Open a page in a real browser (for JS-heavy sites) and return its text."""
        self._call("browser", url)

    async def monitor(self, url: str) -> dict:
        """Watch a source for new filings (alerts and timing checks, not price moves)."""
        self._call("monitor", url)

    async def research(self, question: str) -> dict:
        """Multi-step research on a question."""
        self._call("research", question)
