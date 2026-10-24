"""LLM explanation: writes plain-language explanation_text for an already-decided Card.

The LLM only puts the Card into words. If it is unavailable, fails, or states a
confidence number that is not the card's score, offline_explanation() is used.
"""

import logging
import re
from datetime import datetime
from pathlib import Path

from backend import config
from backend.consensus.timing import describe_gap
from backend.llm.client import LLMError, complete
from backend.models import Card, Evidence, Move

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
TONES = ("plain", "expert")
NOT_ADVICE = "This is an evidence-based attribution, not proof, and not investment advice."
INVESTIGATOR_ROLES = {
    "atlas": "Atlas (market, sector and policy news)",
    "sherlock": "Sherlock (company filings and news)",
    "echo": "Echo (crowd posts and blogs)",
    "skeptic": "Skeptic (evidence against the cause)",
}

# Phrases that state a confidence number: "90 out of 100", "90/100", "85% confidence",
# "confidence score of 90", "score is 90". Move sizes like "-5.8%" do not match.
CONFIDENCE_NUMBER = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:/\s*100\b|out\s+of\s+100\b)"
    r"|(\d+(?:\.\d+)?)\s*(?:%|percent)\s+(?:confidence|confident|probability|likely|likelihood|chance|certain)"
    r"|\b(?:confidence|probability|likelihood|chance)\s+(?:score\s+|level\s+)?(?:of|is|was|at|=|:)?\s*(\d+(?:\.\d+)?)"
    r"|\bscore\s+(?:of|is|was|at|=|:)\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


# --- Formatting helpers ------------------------------------------------------

def fill(template: str, values: dict[str, str]) -> str:
    """Replace each {name} with its value. Not str.format: prompts contain literal braces."""
    for name, value in values.items():
        template = template.replace("{" + name + "}", value)
    return template


def _clock(t: datetime, move: Move, expert: bool) -> str:
    """'9:48 am IST' (plain) or '09:48 IST' (expert), with the date if not the move's day."""
    text = t.strftime("%H:%M IST") if expert else t.strftime("%I:%M %p IST").lstrip("0").replace("AM", "am").replace("PM", "pm")
    if t.date() != move.move_start_at.date():
        text = t.strftime("%d %b ") + text
    return text


def _when(e: Evidence, move: Move, expert: bool) -> str:
    """'at 9:48 am IST, 14 minutes before the move began'."""
    t = e.first_public_at
    if t is None:
        return "with no timestamp"
    if t < move.move_start_at:
        relation = f"{describe_gap(move.move_start_at - t)} before the move began"
    elif t == move.move_start_at:
        relation = "exactly when the move began"
    else:
        relation = f"{describe_gap(t - move.move_start_at)} after the move began"
    return f"at {_clock(t, move, expert)}, {relation}"


def _source(e: Evidence) -> str:
    status = "verified" if e.verified else "unverified"
    return f"{e.source_name} (Tier {e.source_tier}, {status})"


def _move_summary(m: Move) -> str:
    return (f"{m.pct_change:+.1f}% over {m.window_minutes} minutes from "
            f"{m.move_start_at.strftime('%d %b %Y %H:%M IST')}, on {m.volume_multiple}x normal volume; "
            f"sector {m.sector_pct_change:+.1f}%, index {m.index_pct_change:+.1f}%")


def _bullets(lines: list[str]) -> str:
    return "\n".join(f"- {line}" for line in lines) or "- None"


def _search_summary(card: Card) -> str:
    items = card.accepted_evidence + [r.evidence for r in card.rejected_evidence]
    who = sorted({e.investigator for e in items}, key=list(INVESTIGATOR_ROLES).index)
    roles = ", ".join(INVESTIGATOR_ROLES[i] for i in who) or "no investigator returned anything"
    return (f"Investigators with results: {roles}. {len(items)} item(s) found: "
            f"{len(card.accepted_evidence)} accepted, {len(card.rejected_evidence)} rejected. "
            f"Market check: {_move_summary(card.move)}.")


def tone_instructions(tone: str) -> str:
    """The text under '## plain' or '## expert' in plain_vs_expert.md."""
    text = (PROMPTS_DIR / "plain_vs_expert.md").read_text(encoding="utf-8")
    section = text.split(f"## {tone}", 1)[1]
    return section.split("\n## ", 1)[0].strip()


def build_prompt(card: Card, tone: str) -> str:
    m = card.move
    template = (PROMPTS_DIR / "explain.md").read_text(encoding="utf-8")
    # Tone goes in first so its own {confidence_score} is filled by the next pass.
    template = fill(template, {"tone_instructions": tone_instructions(tone)})
    return fill(template, {
        "ticker": card.ticker,
        "confidence_label": card.confidence_label,
        "confidence_score": str(card.confidence_score),
        "leading_explanation": card.leading_explanation or "None",
        "move_summary": _move_summary(m),
        "accepted_evidence": _bullets(
            [f"[{e.investigator}] {_source(e)}: {e.claim}. Public {_when(e, m, True)}."
             for e in card.accepted_evidence]),
        "rejected_evidence": _bullets(
            [f"[{r.evidence.investigator}] {_source(r.evidence)}: {r.evidence.claim}. "
             f"Rejected: {r.reason}." for r in card.rejected_evidence]),
        "alternatives": _bullets(card.alternatives),
        "score_breakdown": _bullets([f"{s.rule}: {s.points:+d}" for s in card.score_breakdown]),
        "limits": _bullets(card.limits),
        "search_summary": _search_summary(card),
    })


# --- Offline template --------------------------------------------------------

def offline_explanation(card: Card, tone: str = "plain") -> str:
    """Explanation built only from the Card, no LLM. Never gives investment advice."""
    expert = tone == "expert"
    m = card.move
    parts = []

    if card.confidence_label == config.UNKNOWN_LABEL:
        parts.append(f"{config.UNKNOWN_TEXT}.")
        parts.append(_search_summary(card) if expert else
                     f"StockLens checked {len(card.accepted_evidence) + len(card.rejected_evidence)} "
                     f"item(s) about the {m.pct_change:+.1f}% move.")
        for r in card.rejected_evidence:
            parts.append(f"{_source(r.evidence)} said \"{r.evidence.claim}\" but was not counted: "
                         f"{r.reason}.")
        for e in card.accepted_evidence:
            parts.append(f"{_source(e)} said \"{e.claim}\" {_when(e, m, expert)}, "
                         "but the evidence was not strong enough to name a cause.")
        if card.alternatives:
            parts.append("Also considered: " + "; ".join(card.alternatives) + ".")
        parts += [lim for lim in card.limits if lim.startswith("Gate")]
    else:
        if expert:
            parts.append(f"{card.confidence_label} confidence, score {card.confidence_score}. "
                         f"Move: {_move_summary(m)}.")
        else:
            parts.append(f"{card.confidence_label} confidence ({card.confidence_score} out of 100).")
        parts.append(f"Leading explanation: {card.leading_explanation}")
        for e in card.accepted_evidence:
            if e.investigator == "skeptic":
                parts.append(f"Skeptic found {_source(e)}: \"{e.claim}\", public {_when(e, m, expert)}.")
            elif e.first_public_at >= m.move_start_at:
                parts.append(f"{_source(e)} reported \"{e.claim}\" {_when(e, m, expert)}, "
                             "counted only as corroboration of the earlier filing.")
            else:
                parts.append(f"{_source(e)} reported \"{e.claim}\" {_when(e, m, expert)}.")
        for r in card.rejected_evidence:
            parts.append(f"Not counted: {r.evidence.source_name}, because it {r.reason}."
                         if r.reason.startswith("became") else
                         f"Not counted: {r.evidence.source_name} ({r.reason}).")
        if expert:
            rules = ", ".join(f"{s.rule} {s.points:+d}" for s in card.score_breakdown)
            parts.append(f"Score breakdown: {rules} = {card.confidence_score}.")
            if card.alternatives:
                parts.append("Alternatives: " + "; ".join(card.alternatives) + ".")
            parts += [lim for lim in card.limits if lim not in config.CARD_LIMITS]

    parts.append(NOT_ADVICE)
    return " ".join(p if p.endswith(".") else p + "." for p in parts)


# --- LLM path ----------------------------------------------------------------

def stated_confidence_numbers(text: str) -> list[float]:
    """Every confidence number the text states."""
    return [float(next(g for g in match.groups() if g)) for match in CONFIDENCE_NUMBER.finditer(text)]


def _reply_problem(reply: str, card: Card) -> str | None:
    """Why an LLM reply cannot be used, or None if it is fine."""
    if not reply.strip():
        return "empty reply"
    wrong = [n for n in stated_confidence_numbers(reply) if n != card.confidence_score]
    if wrong:
        return f"states confidence {wrong[0]:g}, card score is {card.confidence_score}"
    if card.confidence_label == config.UNKNOWN_LABEL and not reply.strip().startswith(config.UNKNOWN_TEXT):
        return "Unknown card reply does not open with the Unknown sentence"
    return None


def explain(card: Card, tone: str = "plain") -> str:
    """explanation_text for the Card: the LLM's words if usable, otherwise the offline template."""
    if tone not in TONES:
        raise ValueError(f"tone must be one of {TONES}, got {tone!r}")
    try:
        reply = complete(build_prompt(card, tone)).strip()
    except LLMError as exc:
        log.info("explain: using offline text (%s)", exc)
        return offline_explanation(card, tone)
    problem = _reply_problem(reply, card)
    if problem:
        log.warning("explain: LLM reply rejected (%s); using offline text", problem)
        return offline_explanation(card, tone)
    return reply


def fill_explanation(card: Card, tone: str = "plain") -> Card:
    """A copy of the Card with explanation_text set."""
    return card.model_copy(update={"explanation_text": explain(card, tone)})
