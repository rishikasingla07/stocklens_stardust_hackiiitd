"""Tests for the investigators, replay loader and TinyFish client (replay mode, no network)."""

import asyncio
from datetime import datetime

import pytest

from backend import config
from backend.investigators.atlas import Atlas
from backend.investigators.base import Investigator
from backend.investigators.echo import Echo
from backend.investigators.sherlock import Sherlock
from backend.investigators.skeptic import NOT_REQUESTED, Skeptic
from backend.models import IST, Evidence
from backend.tinyfish import replay
from backend.tinyfish.client import CallBudget, CallBudgetExceeded, TinyFishClient


@pytest.fixture(autouse=True)
def replay_mode(monkeypatch):
    monkeypatch.setattr(config, "MODE", "replay")


def high_move():
    return replay.find_case("ACMEINFRA")[1]


def run(investigator, move=None):
    return asyncio.run(investigator.run(move or high_move()))


def fake_evidence(id: str, **overrides) -> Evidence:
    data = dict(id=id, claim="claim", source_name="Source", source_tier=1,
                url=f"https://{id}.example/x", snippet="snippet", investigator="atlas",
                verified=True, first_public_at=datetime(2026, 10, 22, 9, 0, tzinfo=IST))
    data.update(overrides)
    return Evidence(**data)


def use_fake_replay(monkeypatch, items: list[Evidence]):
    monkeypatch.setattr(replay, "load_evidence", lambda case, name: items)


# --- replay ------------------------------------------------------------------

def test_find_case_is_case_insensitive():
    folder, move = replay.find_case("acmeinfra")
    assert folder.name == "case_high"
    assert move.ticker == "ACMEINFRA"


def test_find_case_unknown_ticker():
    assert replay.find_case("NOPE") is None


def test_load_evidence_missing_file_is_empty():
    assert replay.load_evidence("case_does_not_exist", "atlas") == []


def test_load_evidence_reads_split_fixture():
    assert [e.id for e in replay.load_evidence("case_high", "sherlock")] == [
        "high-filing", "high-media"]


# --- role rules --------------------------------------------------------------

def test_each_investigator_sets_its_own_name(monkeypatch):
    use_fake_replay(monkeypatch, [fake_evidence("x", investigator="atlas")])
    for cls in (Atlas, Sherlock, Echo):
        report = run(cls())
        assert report.investigator == cls.name
        assert all(e.investigator == cls.name for e in report.evidence)
    report = run(Skeptic(requested=True))
    assert report.evidence[0].investigator == "skeptic"


def test_echo_is_always_tier3_and_unverified(monkeypatch):
    use_fake_replay(monkeypatch, [fake_evidence("posh", source_tier=1, verified=True)])
    [e] = run(Echo()).evidence
    assert (e.source_tier, e.verified) == (3, False)


@pytest.mark.parametrize("case", ["case_high", "case_unknown", "case_market_wide"])
def test_echo_fixtures_stay_tier3_unverified(case):
    move = replay.find_case({"case_high": "ACMEINFRA", "case_unknown": "ZENITHPOLY",
                             "case_market_wide": "ORIONPHARMA"}[case])[1]
    for e in run(Echo(), move).evidence:
        assert (e.source_tier, e.verified) == (3, False)


def test_sherlock_filings_tier1_news_tier2(monkeypatch):
    use_fake_replay(monkeypatch, [
        fake_evidence("filing", source_tier=2, url="https://www.nseindia.com/filing/1"),
        fake_evidence("bse", source_tier=3, url="https://bseindia.com/x"),
        fake_evidence("news", source_tier=1, url="https://marketdaily.example/story"),
        fake_evidence("lookalike", source_tier=1, url="https://notnseindia.com/x"),
    ])
    tiers = {e.id: e.source_tier for e in run(Sherlock()).evidence}
    assert tiers == {"filing": 1, "bse": 1, "news": 2, "lookalike": 2}


def test_atlas_drops_tier3(monkeypatch):
    use_fake_replay(monkeypatch, [fake_evidence("ok", source_tier=2),
                                  fake_evidence("blog", source_tier=3)])
    report = run(Atlas())
    assert [e.id for e in report.evidence] == ["ok"]
    assert "dropped blog" in report.notes


def test_skeptic_does_not_run_unless_requested(monkeypatch):
    def fail(*args):
        raise AssertionError("Skeptic loaded evidence without being asked")
    monkeypatch.setattr(replay, "load_evidence", fail)
    report = run(Skeptic())
    assert report.evidence == []
    assert report.notes == NOT_REQUESTED


def test_skeptic_requested_returns_counter_evidence():
    assert [e.id for e in run(Skeptic(requested=True)).evidence] == ["high-hint"]


def test_skeptic_reports_none_found():
    move = replay.find_case("ZENITHPOLY")[1]
    assert "none found" in run(Skeptic(requested=True), move).notes


def test_unknown_ticker_gives_empty_report():
    move = high_move().model_copy(update={"ticker": "NOPE"})
    report = run(Sherlock(), move)
    assert report.evidence == [] and "no recorded case" in report.notes


def test_live_mode_is_not_implemented_yet():
    with pytest.raises(NotImplementedError):
        run(Atlas(mode="live"))


def test_base_investigator_live_is_not_implemented():
    inv = Investigator(mode="live")
    inv.name = "atlas"
    with pytest.raises(NotImplementedError):
        run(inv)


# --- TinyFish client and budget ----------------------------------------------

def test_budget_default_comes_from_config():
    assert CallBudget().limit == config.MAX_TINYFISH_CALLS_PER_INVESTIGATION == 10


def test_budget_raises_when_exceeded():
    budget = CallBudget(limit=2)
    budget.spend("search", "a")
    budget.spend("fetch", "b")
    with pytest.raises(CallBudgetExceeded):
        budget.spend("agent", "c")
    assert budget.calls == ["search a", "fetch b"]


def test_budget_logs_each_call(caplog):
    caplog.set_level("INFO", logger="backend.tinyfish.client")
    CallBudget(limit=5).spend("search", "ACMEINFRA filings")
    assert "TinyFish call 1/5: search ACMEINFRA filings" in caplog.text


@pytest.mark.parametrize("method, args", [
    ("agent", ("goal",)), ("search", ("q",)), ("fetch", ("https://x",)),
    ("browser", ("https://x",)), ("monitor", ("https://x",)), ("research", ("q",)),
])
def test_client_methods_not_connected(method, args):
    client = TinyFishClient(api_key="", budget=CallBudget(limit=10))
    with pytest.raises(NotImplementedError, match="Live TinyFish not connected yet"):
        asyncio.run(getattr(client, method)(*args))
    assert client.budget.used == 1


def test_client_respects_budget():
    client = TinyFishClient(api_key="", budget=CallBudget(limit=1))
    with pytest.raises(NotImplementedError):
        asyncio.run(client.search("first"))
    with pytest.raises(CallBudgetExceeded):
        asyncio.run(client.search("second"))
