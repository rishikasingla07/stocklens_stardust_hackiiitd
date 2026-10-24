"""Consensus engine: combines Reports, runs scoring and the six hard gates, and produces a Card.

Order: timing -> independence -> market context -> scoring (rules + gates).
The Card is returned without explanation_text; the LLM layer writes that later.
"""

from backend import config
from backend.consensus import independence, market_context, timing
from backend.consensus.scoring import ScoringInput, claims_agree, evaluate, strength_key
from backend.models import Card, Evidence, Move, RejectedEvidence, Report


def _readmit_late_corroboration(
    move: Move, accepted: list[Evidence], rejected: list[RejectedEvidence]
) -> tuple[list[Evidence], list[RejectedEvidence], list[Evidence]]:
    """Keep late verified Tier 2 reports that agree with a pre-move verified Tier 1 source."""
    if not config.ALLOW_LATE_TIER2_CORROBORATION:
        return accepted, rejected, []
    anchors = [e for e in accepted if e.source_tier == 1 and e.verified]
    readmitted, still_rejected = [], []
    for r in rejected:
        e = r.evidence
        late = e.first_public_at is not None and e.first_public_at >= move.move_start_at
        if late and e.source_tier == 2 and e.verified and any(claims_agree(a, e) for a in anchors):
            readmitted.append(e)
        else:
            still_rejected.append(r)
    return accepted + readmitted, still_rejected, readmitted


def build_card(move: Move, evidence: list[Evidence]) -> Card:
    supporting = [e for e in evidence if e.investigator != "skeptic"]
    skeptic_all = [e for e in evidence if e.investigator == "skeptic"]

    # 1. Timing (gate 5 exclusions happen here). Skeptic evidence argues against a
    #    cause, so it is not held to the before-the-move rule, only to gate 5.
    accepted, rejected = timing.split_by_timing(move, supporting)
    accepted, rejected, late_corroboration = _readmit_late_corroboration(move, accepted, rejected)
    skeptic = []
    for e in skeptic_all:
        problem = timing.timestamp_problem(e, move)
        if problem:
            rejected.append(RejectedEvidence(evidence=e, reason=problem))
        else:
            skeptic.append(e)
    excluded = sum(
        r.reason in (timing.MISSING_TIMESTAMP, timing.AMBIGUOUS_TIMESTAMP) for r in rejected)

    # 2. Independence groups over everything we kept.
    grouped = independence.assign_groups(accepted + skeptic)
    accepted, skeptic = grouped[:len(accepted)], grouped[len(accepted):]

    # 3. Market context decides whose evidence backs the leading explanation (gate 4).
    market = market_context.analyse(move)
    if market.market_leads:
        leading = [e for e in accepted if e.investigator == "atlas"]
        context = [e for e in accepted if e.investigator != "atlas"]
    else:
        leading = [e for e in accepted if e.investigator in ("sherlock", "echo")]
        context = [e for e in accepted if e.investigator == "atlas"]

    earliest = min((e.first_public_at for e in leading), default=None)
    hints = [e for e in skeptic if earliest is not None and e.first_public_at < earliest]
    hint_ids = {e.id for e in hints}
    counter = [e for e in skeptic if e.id not in hint_ids]
    strong_alternative = market.market_leads and any(
        e.investigator == "sherlock" and e.source_tier == 1 and e.verified for e in context)

    # 4. Scoring and gates.
    result = evaluate(ScoringInput(
        move=move, leading=leading, hints=hints, counter=counter, market=market,
        excluded_timestamps=excluded, strong_alternative=strong_alternative,
    ))

    leading_explanation = None
    if result.label != config.UNKNOWN_LABEL and leading:
        top = min(leading, key=strength_key)
        if market.market_leads:
            leading_explanation = f"{market.summary} Cause: {top.claim}"
        else:
            leading_explanation = top.claim

    alternatives = list(market.alternatives)
    kinds = {"atlas": "macro", "sherlock": "company", "echo": "crowd"}
    for e in context:
        alternatives.append(f"{kinds[e.investigator]}: {e.claim}")
    for e in counter:
        alternatives.append(f"counter-evidence: {e.claim}")

    limits = list(config.CARD_LIMITS) + result.notes
    if not market.market_leads:
        limits.append(market.summary)
    if late_corroboration:
        limits.append(
            f"{len(late_corroboration)} Tier 2 report(s) published after the move began are "
            "counted only as corroboration of a filing that was public before it.")

    return Card(
        ticker=move.ticker,
        move=move,
        confidence_score=result.score,
        confidence_label=result.label,
        leading_explanation=leading_explanation,
        accepted_evidence=accepted + skeptic,
        rejected_evidence=rejected,
        alternatives=alternatives,
        score_breakdown=result.breakdown,
        limits=limits,
    )


def build_card_from_reports(move: Move, reports: list[Report]) -> Card:
    return build_card(move, [e for report in reports for e in report.evidence])
