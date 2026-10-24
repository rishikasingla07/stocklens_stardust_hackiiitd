"""Filing extraction with the TinyFish Agent: reads one filing page into Evidence, no LLM key needed.

The Agent only reports what is on the page (claim, published_at, source_name,
snippet). Everything that decides trust is done here, in code:
- published_at is parsed to an IST datetime; missing, date-only or time-zone-less
  values are rejected (see config.EXCHANGE_PAGE_TIMES_ARE_IST). A time is never invented.
- url, investigator, id and tier come from our code, never from the Agent.
- verified is True only if the snippet appears word for word in a separate
  Fetch of the same page.
"""

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from pydantic import ValidationError

from backend import config
from backend.models import IST, Evidence, Move
from backend.tinyfish.client import CallBudgetExceeded, TinyFishClient, TinyFishError

# Only keywords from the documented supported subset (docs.tinyfish.ai/key-concepts/structured-output.md).
FILING_SCHEMA = {
    "type": "object",
    "properties": {
        "claim": {"type": "string"},
        "published_at": {"type": "string", "nullable": True},
        "source_name": {"type": "string"},
        "snippet": {"type": "string"},
        "time_source": {"type": "string", "nullable": True},
    },
    "required": ["claim", "published_at", "source_name", "snippet", "time_source"],
}

GOAL = (
    "This page is an official stock exchange filing or company announcement about {company}. "
    "Report ONLY facts that are visible on this page. Do not use outside knowledge and do not guess. "
    "claim: one sentence saying what the filing announces. "
    "published_at: the official time this filing became public, copied character for character "
    "exactly as written on the page, including any time zone. Use the first of these that the page "
    "or document shows: (1) the exchange broadcast or dissemination date and time; (2) the "
    "'exchange received' date and time; (3) a date and time stamped on the document itself; "
    "(4) only if none of those exists, the document date as written. Never add a time that is not "
    "shown; null if no date is shown at all. "
    "time_source: a short phrase saying where published_at came from, e.g. 'exchange dissemination "
    "time', 'exchange received time', 'time stamped on document', 'document date only'; null if "
    "published_at is null. "
    "source_name: who published the page, as shown on the page. "
    "snippet: a short exact quote (one or two sentences) copied verbatim from the page that "
    "supports the claim."
)

UTC_NAMES = {"UTC", "GMT", "Z"}
TZ_SUFFIX = re.compile(
    r"\s*(?:\(?\b(IST|UTC|GMT)\b\)?|(?<=\d)Z|([+-]\d{2}):?(\d{2}))\s*$", re.IGNORECASE)
HAS_TIME = re.compile(r"\d{1,2}:\d{2}")
ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T")
LABEL_PREFIX = re.compile(r"^[A-Za-z][A-Za-z ./&-]*:\s*(?=\w)")  # "Date and Time: ..."
# Day first: Indian exchanges write 22-10-2026 / 22/10/2026.
FORMATS = [
    "%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y %I:%M:%S %p", "%d-%b-%Y %I:%M %p",
    "%d %b %Y %H:%M:%S", "%d %b %Y %H:%M", "%d %b %Y %I:%M:%S %p", "%d %b %Y %I:%M %p",
    "%d %B %Y %H:%M:%S", "%d %B %Y %H:%M", "%d %B %Y %I:%M %p",
    "%b %d %Y %H:%M:%S", "%b %d %Y %H:%M", "%b %d %Y %I:%M %p",
    "%B %d %Y %H:%M:%S", "%B %d %Y %H:%M", "%B %d %Y %I:%M %p",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y %I:%M %p",
    "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y %I:%M %p",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
]


@dataclass
class ExtractResult:
    evidence: Evidence | None
    reason: str  # why it was rejected, or "kept"
    time_source: str | None = None  # the Agent's note on where published_at came from


def is_exchange_filing(url: str) -> bool:
    """True if the URL is on an exchange filing domain (e.g. nseindia.com)."""
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in config.EXCHANGE_FILING_DOMAINS)


def evidence_id(url: str) -> str:
    return "sherlock-live-" + hashlib.sha1(url.encode()).hexdigest()[:10]


def _squash(text: str) -> str:
    return " ".join(text.split())


def parse_published_at(value, exchange_page: bool) -> datetime | None:
    """A timezone-aware IST datetime, or None if missing, date-only or without a usable time zone."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = _squash(value.replace(",", " ").replace(" at ", " "))
    text = re.sub(r"\s*\b(hrs|hours)\b\.?", "", text, flags=re.IGNORECASE).strip()
    if not HAS_TIME.search(text):
        return None  # date only
    if not re.match(r"^\d", text) and LABEL_PREFIX.match(text) and not re.match(r"^[A-Za-z]+ \d", text):
        text = LABEL_PREFIX.sub("", text, count=1)

    # ISO 8601 with an offset, e.g. 2026-10-22T09:48:00+05:30 or ...Z
    if ISO_DATETIME.match(text):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            return parsed.astimezone(IST)
        return parsed.replace(tzinfo=IST) if exchange_page and config.EXCHANGE_PAGE_TIMES_ARE_IST else None

    tz = None
    match = TZ_SUFFIX.search(text)
    if match:
        name, hours, minutes = match.groups()
        if name:
            tz = IST if name.upper() == "IST" else timezone.utc
        elif hours:
            sign = -1 if hours.startswith("-") else 1
            tz = timezone(sign * timedelta(hours=abs(int(hours)), minutes=int(minutes)))
        else:
            tz = timezone.utc  # trailing Z
        text = text[:match.start()].strip()
    elif exchange_page and config.EXCHANGE_PAGE_TIMES_ARE_IST:
        tz = IST
    if tz is None:
        return None  # no usable time zone

    for fmt in FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=tz).astimezone(IST)
        except ValueError:
            continue
    return None


async def _verified(client: TinyFishClient, url: str, snippet: str) -> bool:
    """True only if the snippet appears word for word in a separate Fetch of the page."""
    try:
        fetched = await client.fetch(url, purpose="Check that a quoted snippet appears on the page.")
    except (TinyFishError, CallBudgetExceeded):
        return False  # cannot check, so it stays unverified
    texts = [page.get("text") or "" for page in fetched.get("results") or []]
    needle = _squash(snippet)
    return bool(needle) and any(needle in _squash(t) for t in texts)


async def extract_filing(client: TinyFishClient, url: str, company: str, move: Move,
                         progress: Callable[[str], None] | None = None) -> ExtractResult:
    """Like extract_filing_with_agent, but also says why an item was rejected.

    `progress`, if given, is told about each stage ("Checking the timestamp", ...).
    Raises TinyFishError / CallBudgetExceeded if the Agent call itself fails.
    """
    report = progress or (lambda message: None)
    answer = await client.agent(url, GOAL.format(company=company), output_schema=FILING_SCHEMA)
    report("Checking the timestamp")

    time_source = answer.get("time_source")
    time_source = time_source.strip() or None if isinstance(time_source, str) else None
    exchange_page = is_exchange_filing(url)
    # The date-only rule lives here, whatever the Agent's time_source says. Never invent a time.
    published = parse_published_at(answer.get("published_at"), exchange_page)
    if published is None:
        return ExtractResult(None, "no usable timestamp (missing, date only or no time zone)",
                             time_source)
    claim, snippet = answer.get("claim"), answer.get("snippet")
    if not isinstance(claim, str) or not claim.strip() or not isinstance(snippet, str) or not snippet.strip():
        return ExtractResult(None, "no claim or snippet", time_source)
    source_name = answer.get("source_name")
    if not isinstance(source_name, str) or not source_name.strip():
        source_name = "BSE corporate filing" if "bseindia" in url else (
            "NSE corporate filing" if exchange_page else urlparse(url).hostname or url)

    try:
        evidence = Evidence(
            id=evidence_id(url), claim=claim.strip(), source_name=source_name.strip(),
            source_tier=1 if exchange_page else 2, url=url, first_public_at=published,
            snippet=snippet.strip(), investigator="sherlock", verified=False,
        )
    except ValidationError:
        return ExtractResult(None, "invalid evidence", time_source)

    report("Verifying the quoted snippet")
    if await _verified(client, url, evidence.snippet):
        evidence = evidence.model_copy(update={"verified": True})
    return ExtractResult(evidence, "kept", time_source)


async def extract_filing_with_agent(client: TinyFishClient, url: str, company: str,
                                    move: Move) -> Evidence | None:
    """One Evidence item from a filing page via the TinyFish Agent, or None if it cannot be trusted."""
    return (await extract_filing(client, url, company, move)).evidence
