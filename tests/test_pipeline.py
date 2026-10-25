"""End-to-end replay tests: ticker -> investigators -> consensus engine -> Card."""

import pytest

from backend import config, pipeline
from backend.tinyfish import replay
from backend.tinyfish.client import CallBudget, TinyFishClient


@pytest.fixture(autouse=True)
def replay_mode(monkeypatch):
    monkeypatch.setattr(config, "MODE", "replay")


def test_high_case_is_high_100():
    card = pipeline.live_card("ACMEINFRA")
    assert (card.confidence_label, card.confidence_score) == ("High", 100)
    assert card.leading_explanation.startswith("ACME Infra revises FY27")


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
        card = pipeline.live_card(ticker)
        echo = [e for e in card.accepted_evidence + [r.evidence for r in card.rejected_evidence]
                if e.investigator == "echo"]
        assert echo and all(e.source_tier == 3 and not e.verified for e in echo)


def test_pipeline_runs_atlas_sherlock_echo_only():
    move = replay.find_case("ACMEINFRA")[1]
    assert [r.investigator for r in pipeline.run_investigators(move)] == ["atlas", "sherlock", "echo"]


def test_replay_spends_no_tinyfish_calls():
    client = TinyFishClient(api_key="", budget=CallBudget(limit=0))
    move = replay.find_case("ACMEINFRA")[1]
    reports = pipeline.run_investigators(move, client=client)
    assert len(reports) == 3 and client.budget.used == 0


def test_live_mode_still_uses_recorded_evidence_for_demo_cases(monkeypatch):
    # Regression: with STOCKLENS_MODE=live, Sherlock used to search live inside the
    # Investigate pipeline, lost the recorded filing and turned ACMEINFRA Unknown.
    import httpx

    def no_network(request):
        raise AssertionError(f"the recorded pipeline must not call TinyFish: {request.url}")
    monkeypatch.setattr(config, "MODE", "live")
    monkeypatch.setattr(config, "TINYFISH_API_KEY", "")  # if it went live anyway: no network, just empty
    client = TinyFishClient(api_key="fake", budget=CallBudget(limit=10),
                            transport=httpx.MockTransport(no_network))
    move = replay.find_case("ACMEINFRA")[1]
    reports = pipeline.run_investigators(move, client=client)
    assert [len(r.evidence) for r in reports] == [0, 2, 1]  # atlas, sherlock (recorded), echo
    assert client.budget.used == 0
    expected = {"ACMEINFRA": ("High", 100), "ZENITHPOLY": ("Unknown", 15), "ORIONPHARMA": ("Medium", 50)}
    for ticker, result in expected.items():
        card = pipeline.live_card(ticker)
        assert (card.confidence_label, card.confidence_score) == result
