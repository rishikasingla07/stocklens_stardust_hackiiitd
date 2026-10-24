"""Trigger engine -> investigation: /stocks alert flags, /simulate, and the delayed-feed quote."""

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from backend import app as app_module
from backend import config
from backend.market import data as market_data
from backend.models import IST, Move

client = TestClient(app_module.app)

RECORDED = {"ACMEINFRA": "case_high", "ZENITHPOLY": "case_unknown",
            "ORIONPHARMA": "case_market_wide"}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Replay data and no LLM key: explanations come from the offline template."""
    monkeypatch.setattr(config, "MODE", "replay")
    monkeypatch.setattr(config, "LLM_API_KEY", "")


def fixture_move(case: str) -> Move:
    path = config.FIXTURES_DIR / case / "consensus_input.json"
    return Move(**json.loads(path.read_text())["move"])


def stock_rows() -> dict[str, dict]:
    data = client.get("/stocks").json()
    return {s["ticker"]: s for s in data["holdings"] + data["watchlist"]}


# --- /stocks -----------------------------------------------------------------

def test_stocks_alert_only_where_trigger_flags_a_move():
    rows = stock_rows()
    assert {t for t, s in rows.items() if s["alert"]} == set(RECORDED)
    for ticker in ("RELIANCE", "HDFCBANK", "TCS", "INFY"):
        assert rows[ticker]["alert"] is False


def test_stocks_alerts_follow_trigger_thresholds(monkeypatch):
    # ORIONPHARMA moved -2.6%; at a 3% threshold the trigger no longer flags it.
    monkeypatch.setattr(config, "TRIGGER_MIN_PCT_CHANGE", 3.0)
    rows = stock_rows()
    assert {t for t, s in rows.items() if s["alert"]} == {"ACMEINFRA", "ZENITHPOLY"}
    monkeypatch.setattr(config, "TRIGGER_MIN_PCT_CHANGE", 50.0)
    assert not any(s["alert"] for s in stock_rows().values())


def test_stocks_prices_come_from_delayed_feed():
    acme = stock_rows()["ACMEINFRA"]
    # Last recorded bar 1180.00 against the day's first bar 1251.00.
    assert acme["price"] == 1180.0
    assert acme["change"] == -71.0
    assert acme["change_pct"] == round(100 * -71 / 1251, 2)


def test_stocks_response_shape_unchanged():
    data = client.get("/stocks").json()
    assert set(data) == {"holdings", "watchlist"}
    for s in data["holdings"] + data["watchlist"]:
        assert set(s) == {"ticker", "name", "price", "change", "change_pct", "alert"}


def test_delayed_quote_hides_bars_newer_than_the_delay():
    # 15-minute delay: at 10:31 the feed shows the 10:16 bar (1221.00), not 10:30.
    quote = market_data.delayed_quote("case_high", "ACMEINFRA",
                                      datetime(2026, 10, 22, 10, 31, tzinfo=IST))
    assert quote == {"price": 1221.0, "reference": 1251.0}
    assert market_data.delayed_quote("case_high", "ACMEINFRA",
                                     datetime(2026, 10, 22, 9, 0, tzinfo=IST)) is None


def test_market_data_find_case():
    assert market_data.find_case("acmeinfra") == "case_high"
    assert market_data.find_case("TCS") is None


# --- /simulate ---------------------------------------------------------------

def test_simulate_flags_acmeinfra_and_matches_investigate():
    body = client.post("/simulate/ACMEINFRA").json()
    assert body["flagged"] is True
    assert Move(**body["move"]) == fixture_move("case_high")
    assert body["thresholds"] == {
        "min_pct_change": config.TRIGGER_MIN_PCT_CHANGE,
        "min_volume_multiple": config.TRIGGER_MIN_VOLUME_MULTIPLE,
        "default_window_minutes": config.TRIGGER_DEFAULT_WINDOW_MINUTES,
    }
    card = body["card"]
    investigated = client.post("/investigate/ACMEINFRA").json()
    assert (card["confidence_score"], card["confidence_label"]) == (
        investigated["confidence_score"], investigated["confidence_label"]) == (90, "High")
    assert card["explanation_text"] == investigated["explanation_text"]
    assert card["explanation_text"].strip()


@pytest.mark.parametrize("ticker", RECORDED)
def test_simulate_every_demo_case_matches_investigate(ticker):
    card = client.post(f"/simulate/{ticker}").json()["card"]
    investigated = client.post(f"/investigate/{ticker}").json()
    assert card["confidence_label"] == investigated["confidence_label"]
    assert card["confidence_score"] == investigated["confidence_score"]
    assert card["leading_explanation"] == investigated["leading_explanation"]


def test_simulate_uses_the_move_the_trigger_produced(monkeypatch):
    seen = []
    real = app_module.pipeline.card_for_move

    def spy(move, include_skeptic=False, client=None):
        seen.append(move)
        return real(move, include_skeptic, client)

    monkeypatch.setattr(app_module.pipeline, "card_for_move", spy)
    client.post("/simulate/ORIONPHARMA")
    assert seen == [market_data.get_move("case_market_wide", "ORIONPHARMA")]


def test_simulate_not_flagged_starts_no_investigation(monkeypatch):
    monkeypatch.setattr(config, "TRIGGER_MIN_VOLUME_MULTIPLE", 10.0)
    monkeypatch.setattr(app_module.pipeline, "card_for_move",
                        lambda *a, **k: pytest.fail("investigation started without a flag"))
    r = client.post("/simulate/ACMEINFRA")
    assert r.status_code == 200
    body = r.json()
    assert (body["flagged"], body["move"], body["card"]) == (False, None, None)
    assert body["thresholds"]["min_volume_multiple"] == 10.0


@pytest.mark.parametrize("ticker", ["TCS", "NOPE"])
def test_simulate_unknown_ticker_is_404(ticker):
    r = client.post(f"/simulate/{ticker}")
    assert r.status_code == 404
    assert "No recorded market data" in r.json()["detail"]


def test_simulate_is_case_insensitive():
    assert client.post("/simulate/acmeinfra").json()["flagged"] is True
