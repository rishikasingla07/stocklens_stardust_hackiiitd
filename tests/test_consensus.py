"""Tests for the deterministic Consensus Engine (backend/consensus/)."""

import json
from datetime import datetime, timedelta

import pytest

from backend import config
from backend.consensus import independence, market_context, timing
from backend.consensus.engine import build_card
from backend.consensus.scoring import ScoringInput, evaluate
from backend.models import IST, Evidence, Move

MOVE_START = datetime(2026, 10, 22, 10, 2, tzinfo=IST)


def make_move(**overrides) -> Move:
    data = dict(ticker="TEST", move_start_at=MOVE_START, window_minutes=42, pct_change=-5.8,
                volume_multiple=3.1, sector_pct_change=-0.7, index_pct_change=-0.2)
    data.update(overrides)
    return Move(**data)


def make_evidence(id: str, minutes_from_start: float | None, **overrides) -> Evidence:
    data = dict(
        id=id, claim=f"claim {id}", source_name="Source", source_tier=2,
        url=f"https://{id}.example/story", snippet=f"snippet for {id}",
        investigator="sherlock", verified=True,
        first_public_at=None if minutes_from_start is None
        else MOVE_START + timedelta(minutes=minutes_from_start),
    )
    data.update(overrides)
    return Evidence(**data)


def load_case(name: str) -> tuple[Move, list[Evidence], dict]:
    data = json.loads((config.FIXTURES_DIR / name / "consensus_input.json").read_text())
    return Move(**data["move"]), [Evidence(**e) for e in data["evidence"]], data["expected"]


# --- timing ------------------------------------------------------------------

def test_late_news_is_rejected():
    move = make_move()
    early = make_evidence("early", -10)
    late = make_evidence("late", 14)
    accepted, rejected = timing.split_by_timing(move, [early, late])
    assert [e.id for e in accepted] == ["early"]
    assert [r.evidence.id for r in rejected] == ["late"]
    assert rejected[0].reason == "became public 14 minutes after the move began"


def test_evidence_at_exact_move_start_is_rejected():
    _, rejected = timing.split_by_timing(make_move(), [make_evidence("same", 0)])
    assert "not before" in rejected[0].reason


def test_missing_timestamp_is_excluded():
    accepted, rejected = timing.split_by_timing(make_move(), [make_evidence("nots", None)])
    assert accepted == []
    assert rejected[0].reason == "missing timestamp"


def test_midnight_on_move_day_is_ambiguous():
    item = make_evidence("dateonly", None, first_public_at=datetime(2026, 10, 22, tzinfo=IST))
    _, rejected = timing.split_by_timing(make_move(), [item])
    assert rejected[0].reason == timing.AMBIGUOUS_TIMESTAMP


# --- independence ------------------------------------------------------------

def test_ten_copies_are_one_independence_group():
    story = ("Acme Infra cut its FY27 revenue growth guidance to 6-8% from 10-12%, "
             "citing delays in two large orders, the company said in a filing.")
    copies = [
        make_evidence(f"copy{i}", -5, url=f"https://outlet{i}.example/news/{i}",
                      snippet=story if i % 2 else story + " (PTI)")
        for i in range(10)
    ]
    grouped = independence.assign_groups(copies)
    assert independence.count_groups(grouped) == 1


def test_existing_group_is_kept_and_distinct_stories_stay_apart():
    a = make_evidence("a", -5, independence_group="filing-123", snippet="Board approves buyback")
    b = make_evidence("b", -5, snippet="Analysts see weak monsoon hurting rural demand this year")
    grouped = independence.assign_groups([a, b])
    assert grouped[0].independence_group == "filing-123"
    assert independence.count_groups(grouped) == 2


# --- market context ----------------------------------------------------------

def test_sector_driven_move_makes_market_explanation_lead():
    ctx = market_context.analyse(make_move(pct_change=-2.6, sector_pct_change=-2.3))
    assert ctx.market_leads
    assert ctx.summary.startswith("Market explanation leads")

    move, evidence, expected = load_case("case_market_wide")
    card = build_card(move, evidence)
    assert expected["market_leads"]
    assert card.leading_explanation.startswith("Market explanation leads")
    assert card.confidence_label == expected["confidence_label"]
    assert "sector weakness: high" in card.alternatives


# --- gates -------------------------------------------------------------------

def test_no_evidence_gives_unknown():
    card = build_card(make_move(), [])
    assert card.confidence_label == "Unknown"
    assert card.leading_explanation is None


def test_case_unknown_fixture():
    move, evidence, expected = load_case("case_unknown")
    card = build_card(move, evidence)
    assert card.confidence_label == expected["confidence_label"] == "Unknown"
    assert card.leading_explanation is None
    reasons = {r.evidence.id: r.reason for r in card.rejected_evidence}
    assert reasons["unk-blog"] == "missing timestamp"


def test_tier3_only_is_capped_at_low():
    tier3 = [make_evidence(f"t3-{i}", -20 - i, source_tier=3, verified=False,
                           investigator="echo") for i in range(3)]
    card = build_card(make_move(), tier3)
    assert card.confidence_label == "Low"
    assert any(note.startswith("Gate 2") for note in card.limits)

    # Unverified Tier 1 evidence also counts as "only Tier 3 or unverified".
    move = make_move()
    unverified_t1 = make_evidence("t1", -20, source_tier=1, verified=False)
    result = evaluate(ScoringInput(
        move=move, leading=independence.assign_groups([unverified_t1]), hints=[], counter=[],
        market=market_context.analyse(move)))
    assert 2 in result.gates_fired
    assert result.label == "Low" and result.score <= 49


def test_one_group_is_capped_at_medium():
    claim = "Acme Infra revises FY27 revenue growth guidance down to 6-8%"
    filing = make_evidence("filing", -15, source_tier=1, claim=claim, independence_group="g1")
    copy = make_evidence("copy", -5, source_tier=2, claim=claim, independence_group="g1")
    card = build_card(make_move(), [filing, copy])
    # 25 + 20 timing + 15 unusual + 10 agree + 10 no alternative = 80 before the cap.
    assert card.confidence_label == "Medium"
    assert card.confidence_score == 74
    assert sum(item.points for item in card.score_breakdown) == card.confidence_score


def test_counter_evidence_gate_still_caps_at_medium():
    # Gate 6 stays in the consensus engine even though no investigator produces
    # counter-evidence any more; the engine still handles it if it arrives.
    move, evidence, _ = load_case("case_high")
    denial = make_evidence("denial", 30, investigator="skeptic",
                           claim="Company denies guidance change was material")
    card = build_card(move, evidence + [denial])
    assert card.confidence_label == "Medium"
    assert "counter-evidence: Company denies guidance change was material" in card.alternatives


def test_caps_never_raise_a_label():
    # One Tier 3 item: scores Low; gate 3 (cap at Medium) fires but must not raise it.
    item = make_evidence("t3", -20, source_tier=3, verified=False, investigator="echo")
    card = build_card(make_move(), [item])
    assert any(note.startswith("Gate 3") for note in card.limits)
    assert card.confidence_label == "Low"
    assert card.confidence_score == 45


# --- high-confidence case ----------------------------------------------------

def test_high_confidence_case_scores_100():
    move, evidence, expected = load_case("case_high")
    card = build_card(move, evidence)

    assert card.confidence_score == expected["confidence_score"] == 100
    assert card.confidence_label == "High"
    labels = config.RULE_LABELS
    assert {item.rule: item.points for item in card.score_breakdown} == {
        labels["primary_source_confirms"]: 25,
        labels["independent_tier2_corroboration"]: 20,
        labels["timing_public_before_move"]: 20,
        labels["move_unusual_vs_sector_and_index"]: 15,
        labels["filing_and_media_agree"]: 10,
        labels["no_stronger_alternative"]: 10,
    }
    rejected = {r.evidence.id: r.reason for r in card.rejected_evidence}
    assert rejected == {"high-broker": "became public 32 minutes after the move began"}
    assert card.leading_explanation.startswith("ACME Infra revises FY27")
    assert card.limits[0] == config.CARD_LIMITS[0]
    assert card.explanation_text == ""


@pytest.mark.parametrize("case", ["case_high", "case_unknown", "case_market_wide"])
def test_fixture_expectations(case):
    move, evidence, expected = load_case(case)
    card = build_card(move, evidence)
    assert card.confidence_label == expected["confidence_label"]
    if "confidence_score" in expected:
        assert card.confidence_score == expected["confidence_score"]
