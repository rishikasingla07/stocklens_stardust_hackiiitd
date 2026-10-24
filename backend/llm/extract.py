"""LLM extraction: turns raw fetched pages into structured candidate Evidence (claim, timestamp, snippet).

Returns None instead of guessing whenever the reply cannot be trusted.
"""

import json
import logging
from pathlib import Path

from pydantic import ValidationError

from backend.llm.client import LLMError, complete
from backend.llm.explain import fill
from backend.models import Evidence, Move

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# Tier each prompt asks for, and the investigator that usually reads that kind of page.
KIND_TIER = {"filing": 1, "news": 2, "social": 3}
KIND_INVESTIGATOR = {"filing": "sherlock", "news": "sherlock", "social": "echo"}


def _parse_json(reply: str):
    """json.loads, tolerating a ```json fence around the reply."""
    text = reply.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return json.loads(text)


def _squash(text: str) -> str:
    return " ".join(text.split())


def extract_evidence(kind: str, page_text: str, ticker: str, move: Move, source_name: str,
                     source_url: str, evidence_id: str,
                     investigator: str | None = None) -> Evidence | None:
    """One Evidence item from a fetched page, or None if nothing reliable was found."""
    if kind not in KIND_TIER:
        raise ValueError(f"kind must be one of {list(KIND_TIER)}, got {kind!r}")
    investigator = investigator or KIND_INVESTIGATOR[kind]

    template = (PROMPTS_DIR / f"extract_{kind}.md").read_text(encoding="utf-8")
    # page_text goes in last so placeholder-like text inside the page is never filled.
    prompt = fill(template, {
        "evidence_id": evidence_id,
        "investigator": investigator,
        "move_start_at": move.move_start_at.isoformat(),
        "source_name": source_name,
        "source_url": source_url,
        "ticker": ticker,
    })
    prompt = fill(prompt, {"page_text": page_text})

    try:
        data = _parse_json(complete(prompt, json_mode=True))
    except (LLMError, ValueError) as exc:
        log.info("extract %s: no evidence (%s)", evidence_id, exc)
        return None
    if not isinstance(data, dict) or not data.get("first_public_at"):
        return None  # "null" reply, wrong shape, or missing timestamp

    data.pop("original_source", None)  # in the prompt, not in the Evidence model
    # Identity fields come from the caller, not from the page.
    data.update(id=evidence_id, source_name=source_name, url=source_url,
                investigator=investigator, source_tier=KIND_TIER[kind])
    if kind == "social":
        data["verified"] = False  # crowd context is never verified
    try:
        evidence = Evidence(**data)
    except ValidationError as exc:
        log.info("extract %s: invalid evidence (%s)", evidence_id, exc)
        return None

    # A snippet that is not copied word for word from the page cannot count as verified.
    if evidence.verified and _squash(evidence.snippet) not in _squash(page_text):
        evidence = evidence.model_copy(update={"verified": False})
    return evidence
