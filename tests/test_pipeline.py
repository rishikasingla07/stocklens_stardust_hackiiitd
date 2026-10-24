"""End-to-end replay tests: ticker -> investigators -> consensus engine -> Card."""

import pytest

from backend import config, pipeline
from backend.tinyfish import replay
from backend.tinyfish.client import CallBudget, TinyFishClient


@pytest.fixture(autouse=True)
def replay_mode(monkeypatch):
    monkeypatch.setattr(config, "MODE", "replay")


def test_high_case_with_challenge_is_high_90():
    # The fixture's expected 90 includes the Skeptic's earlier hint (-10).
    card = pipeline.live_card("ACMEINFRA", include_skeptic=True)
    assert (card.confidence_label, card.confidence_score) == ("High", 90)
    assert card.leading_explanation.startswith("ACME Infra revises FY27")


def test_high_case_default_run_includes_earlier_hint():
    # DEFAULT_INCLUDE_EARLIER_HINTS: the default run keeps the -10 earlier hint.
    card = pipeline.live_card("ACMEINFRA")
    assert (card.confidence_label, card.confidence_score) == ("High", 90)


def test_high_case_without_earlier_hints_has_no_hint_penalty(monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_INCLUDE_EARLIER_HINTS", False)
    card = pipeline.live_card("ACMEINFRA")
    assert (card.confidence_label, card.confidence_score) == ("High", 100)


def test_unknown_case_is_unknown():
    card = pipeline.live_card("ZENITHPOLY")
    assert card.confidence_label == "Unknown"
    assert card.leading_explanation is None


def test_market_wide_case_market_leads():
    card = pipeline.live_card("ORIONPHARMA")
    assert card.leading_explanation.startswith("Market explanation leads")
    assert (card.confidence_label, card.confidence_score) == ("Medium", 50)


def test_unknown_ticker_returns_none():
    assert pipeline.live_card("NOPE") is None


def test_echo_in_cards_is_tier3_unverified():
    for ticker in ("ACMEINFRA", "ZENITHPOLY", "ORIONPHARMA"):
        card = pipeline.live_card(ticker, include_skeptic=True)
        echo = [e for e in card.accepted_evidence + [r.evidence for r in card.rejected_evidence]
                if e.investigator == "echo"]
        assert echo and all(e.source_tier == 3 and not e.verified for e in echo)


def test_full_skeptic_only_runs_when_requested(monkeypatch):
    move = replay.find_case("ACMEINFRA")[1]
    default = pipeline.run_investigators(move)
    assert [r.investigator for r in default] == ["atlas", "sherlock", "echo", "skeptic"]
    assert "earlier-hint lookup only" in default[-1].notes
    monkeypatch.setattr(config, "DEFAULT_INCLUDE_EARLIER_HINTS", False)
    assert [r.investigator for r in pipeline.run_investigators(move)] == ["atlas", "sherlock", "echo"]
    challenged = pipeline.run_investigators(move, include_skeptic=True)
    assert [r.investigator for r in challenged] == ["atlas", "sherlock", "echo", "skeptic"]


def test_replay_spends_no_tinyfish_calls():
    client = TinyFishClient(api_key="", budget=CallBudget(limit=0))
    move = replay.find_case("ACMEINFRA")[1]
    reports = pipeline.run_investigators(move, include_skeptic=True, client=client)
    assert len(reports) == 4 and client.budget.used == 0
