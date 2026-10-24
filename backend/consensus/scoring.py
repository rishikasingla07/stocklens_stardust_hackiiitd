"""Scoring: applies the point rules from config.SCORING_POINTS, then the six hard gates from config.GATES.

The score_breakdown always sums to the final score: when a gate caps the label,
an extra entry brings the score down to the top of the capped label's band.
"""

from dataclasses import dataclass, field

from backend import config
from backend.consensus.independence import count_groups, similarity
from backend.consensus.market_context import MarketContext
from backend.models import Evidence, Move, ScoreItem


@dataclass
class ScoringInput:
    move: Move
    leading: list[Evidence]  # accepted evidence for the leading explanation
    hints: list[Evidence]  # Skeptic evidence that predates the leading evidence
    counter: list[Evidence]  # unresolved Skeptic counter-evidence
    market: MarketContext
    excluded_timestamps: int = 0  # items dropped for missing/ambiguous timestamps
    strong_alternative: bool = False  # e.g. a verified company filing while the market leads


@dataclass
class ScoreResult:
    score: int
    label: str
    breakdown: list[ScoreItem]
    gates_fired: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def claims_agree(a: Evidence, b: Evidence) -> bool:
    return max(similarity(a.claim, b.claim), similarity(a.snippet, b.snippet)) >= (
        config.CLAIM_AGREEMENT_SIMILARITY
    )


def strength_key(e: Evidence) -> tuple:
    """Sort key: lower tier first, verified first, then earliest."""
    return (e.source_tier, not e.verified, e.first_public_at)


def label_for(score: int) -> str:
    for label, minimum in config.LABEL_CUTOFFS:
        if score >= minimum:
            return label
    return config.UNKNOWN_LABEL


def label_ceiling(label: str) -> int:
    """Highest score that still maps to this label."""
    ceiling = config.SCORE_MAX
    for name, minimum in config.LABEL_CUTOFFS:
        if name == label:
            return ceiling
        ceiling = minimum - 1
    return ceiling  # Unknown


def _verified(evidence: list[Evidence], tier: int) -> list[Evidence]:
    return [e for e in evidence if e.source_tier == tier and e.verified]


def fired_rules(inp: ScoringInput) -> list[str]:
    """Names (keys of SCORING_POINTS) of every rule that fires, in config order."""
    leading = inp.leading
    tier1 = _verified(leading, 1)
    tier2 = _verified(leading, 2)
    top = min(leading, key=strength_key) if leading else None

    if tier1:
        anchor_groups = {e.independence_group for e in tier1}
    else:
        anchor_groups = {top.independence_group} if top else set()

    checks = {
        "primary_source_confirms": bool(tier1),
        "independent_tier2_corroboration": any(
            e is not top and e.independence_group not in anchor_groups for e in tier2),
        "timing_public_before_move": any(
            e.first_public_at < inp.move.move_start_at for e in leading),
        "move_unusual_vs_sector_and_index": inp.market.unusual,
        "filing_and_media_agree": any(claims_agree(a, b) for a in tier1 for b in tier2),
        "no_stronger_alternative": bool(leading) and not inp.counter and not inp.strong_alternative,
        "partly_anticipated": bool(inp.hints),
    }
    return [rule for rule in config.SCORING_POINTS if checks.get(rule)]


def _gate_fires(name: str, inp: ScoringInput) -> bool:
    leading = inp.leading
    if name == "no_evidence_before_move":
        return not any(e.first_public_at < inp.move.move_start_at for e in leading)
    if name == "only_tier3_or_unverified":
        return bool(leading) and all(e.source_tier == 3 or not e.verified for e in leading)
    if name == "single_independence_group":
        return count_groups(leading) == 1
    if name == "market_explains_move":
        return inp.market.market_leads
    if name == "missing_or_ambiguous_timestamp":
        return inp.excluded_timestamps > 0
    if name == "unresolved_skeptic_counter":
        return bool(inp.counter)
    raise ValueError(f"unknown gate {name!r}")


def evaluate(inp: ScoringInput) -> ScoreResult:
    breakdown = [
        ScoreItem(rule=config.RULE_LABELS.get(rule, rule), points=config.SCORING_POINTS[rule])
        for rule in fired_rules(inp)
    ]
    raw = sum(item.points for item in breakdown)
    score = max(config.SCORE_MIN, min(config.SCORE_MAX, raw))
    if score != raw:
        breakdown.append(ScoreItem(rule="Clamped to 0-100", points=score - raw))
    label = label_for(score)

    result = ScoreResult(score=score, label=label, breakdown=breakdown)
    for gate in config.GATES:
        if not _gate_fires(gate["name"], inp):
            continue
        result.gates_fired.append(gate["id"])
        result.notes.append(f"Gate {gate['id']}: {gate['description']}")
        if gate["effect"] not in ("force", "cap"):
            continue  # exclude (done in timing) and market_leads (done in engine)
        target = gate["label"]
        if gate["effect"] == "cap" and config.LABEL_RANK[result.label] <= config.LABEL_RANK[target]:
            continue  # caps only ever lower a label
        result.label = target
        ceiling = label_ceiling(target)
        if result.score > ceiling:
            result.breakdown.append(ScoreItem(
                rule=f"Gate {gate['id']} cap ({target})", points=ceiling - result.score))
            result.score = ceiling
    return result
