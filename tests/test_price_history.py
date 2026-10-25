"""One-month price history: generated data, unusual-move detection, routes, investigating a move."""

import importlib.util
import json
import shutil
from datetime import datetime, time

import pytest
from fastapi.testclient import TestClient

from backend import app as app_module
from backend import config
from backend.market import price_history as ph
from backend.models import IST, Move

client = TestClient(app_module.app)  # logged in by the conftest fixture

RECORDED = {"ACMEINFRA": "case_high", "ZENITHPOLY": "case_unknown", "ORIONPHARMA": "case_market_wide"}
EXPECTED_MAIN = {"ACMEINFRA": ("High", 100), "ZENITHPOLY": ("Unknown", 15), "ORIONPHARMA": ("Medium", 50)}
# Extra, smaller unusual moves planted by scripts/make_price_history.py (start, rough % change).
EXTRA = {
    "ACMEINFRA": [("2026-10-07T11:00:00+05:30", 3.2), ("2026-10-14T13:15:00+05:30", -2.9)],
    "ZENITHPOLY": [("2026-10-01T10:00:00+05:30", -3.0)],
    "ORIONPHARMA": [("2026-09-29T14:00:00+05:30", 2.8), ("2026-10-12T10:30:00+05:30", -3.1)],
}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(config, "MODE", "replay")
    monkeypatch.setattr(config, "LLM_API_KEY", "")


def recorded_move(ticker: str) -> Move:
    path = config.FIXTURES_DIR / RECORDED[ticker] / "consensus_input.json"
    return Move(**json.loads(path.read_text())["move"])


def moves(ticker: str) -> list[ph.UnusualMove]:
    return ph.detect_moves(ph.get_series(ticker))


# --- generated data ------------------------------------------------------------------

def test_generator_is_deterministic(tmp_path, monkeypatch):
    """Re-running the seeded script reproduces fixtures/price_history.json byte for byte."""
    shutil.copy(config.FIXTURES_DIR / "market_data.json", tmp_path / "market_data.json")
    spec = importlib.util.spec_from_file_location("make_price_history",
                                                  config.ROOT / "scripts" / "make_price_history.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    monkeypatch.setattr(script.config, "FIXTURES_DIR", tmp_path)
    script.main()
    assert (tmp_path / "price_history.json").read_bytes() == \
        (config.FIXTURES_DIR / "price_history.json").read_bytes()


def test_file_says_it_is_demo_data():
    data = json.loads(config.PRICE_HISTORY_FILE.read_text(encoding="utf-8"))
    assert data["description"].startswith("RECORDED DEMO DATA, NOT REAL PRICES")


@pytest.mark.parametrize("ticker", ph.tickers())
def test_bars_are_trading_hours_on_trading_days(ticker):
    bars = ph.get_series(ticker).bars
    days = {b.at.date() for b in bars}
    assert 20 <= len(days) <= 23  # about one month of trading days
    assert (max(days) - min(days)).days <= 30
    for b in bars:
        assert b.at.utcoffset() == IST.utcoffset(None)
        assert b.at.weekday() < 5 and b.at.date().isoformat() != "2026-10-02"
        assert time(9, 15) <= b.at.time() <= time(15, 30)
        assert b.price > 0 and b.volume > 0


@pytest.mark.parametrize("ticker", RECORDED)
def test_recorded_bars_appear_exactly(ticker):
    market = json.loads((config.FIXTURES_DIR / "market_data.json").read_text())
    recorded = market["cases"][RECORDED[ticker]][ticker]
    sector = {s["at"]: s["price"] for s in recorded["sector_series"]}
    index = {s["at"]: s["price"] for s in recorded["index_series"]}
    by_time = {b.at: b for b in ph.get_series(ticker).bars}
    for rb in recorded["stock"]:
        bar = by_time[datetime.fromisoformat(rb["at"])]
        assert (bar.price, bar.volume, bar.sector, bar.index) == (
            rb["price"], rb["volume"], sector[rb["at"]], index[rb["at"]])


# --- detection -------------------------------------------------------------------------

@pytest.mark.parametrize("ticker", RECORDED)
def test_recorded_move_is_found_exactly(ticker):
    found = [m.move for m in moves(ticker)]
    assert recorded_move(ticker) in found


@pytest.mark.parametrize("ticker", RECORDED)
def test_extra_moves_are_found(ticker):
    found = {m.move.move_start_at.isoformat(): m for m in moves(ticker)}
    for start, pct in EXTRA[ticker]:
        m = found[start]
        assert m.move.pct_change == pytest.approx(pct, abs=0.3)
        assert m.direction == ("up" if pct > 0 else "down")
        assert m.move.window_minutes == 30
    assert len(found) == len(EXTRA[ticker]) + 1  # the extras plus the recorded move, nothing else


def test_quiet_ticker_has_no_unusual_moves():
    assert moves("LOTUSFOODS") == []


@pytest.mark.parametrize("ticker", ph.tickers())
def test_every_detected_move_passes_the_trigger_rules_and_none_overlap(ticker):
    found = moves(ticker)
    for m in found:
        assert abs(m.move.pct_change) >= config.TRIGGER_MIN_PCT_CHANGE
        assert m.move.volume_multiple >= config.TRIGGER_MIN_VOLUME_MULTIPLE
        assert m.move.window_minutes <= config.TRIGGER_DEFAULT_WINDOW_MINUTES * config.PRICE_HISTORY_WINDOW_FACTOR
    for a, b in zip(found, found[1:]):
        assert a.end_at < b.move.move_start_at  # merged: one event per move, oldest first


def test_detection_follows_the_trigger_thresholds(monkeypatch):
    monkeypatch.setattr(config, "TRIGGER_MIN_PCT_CHANGE", 4.0)
    assert [m.move.move_start_at.isoformat() for m in moves("ACMEINFRA")] == ["2026-10-22T10:02:00+05:30"]
    monkeypatch.setattr(config, "TRIGGER_MIN_VOLUME_MULTIPLE", 50.0)
    assert moves("ACMEINFRA") == []


def test_overlapping_windows_merge_into_one_event():
    # Many 15-60 minute windows pass around ACMEINFRA's recorded fall; they become one event.
    windows = [w for w in ph._passing_windows(ph.get_series("ACMEINFRA"))
               if w[2].move_start_at.date().isoformat() == "2026-10-22"]
    assert len(windows) > 1
    [event] = [m for m in moves("ACMEINFRA") if m.move.move_start_at.date().isoformat() == "2026-10-22"]
    assert event.move.window_minutes == 42


# --- GET /price-history ----------------------------------------------------------------------

@pytest.mark.parametrize("ticker", RECORDED)
def test_route_returns_bars_and_moves(ticker):
    r = client.get(f"/price-history/{ticker.lower()}")
    assert r.status_code == 200
    body = r.json()
    assert body["ticker"] == ticker and body["note"] == "Recorded demo data, not real prices."
    assert len(body["bars"]) > 500 and set(body["bars"][0]) == {"at", "price", "volume"}
    starts = {m["start_at"]: m for m in body["moves"]}
    main = starts[recorded_move(ticker).move_start_at.isoformat()]
    assert main["has_recorded_evidence"] is True
    assert sum(m["has_recorded_evidence"] for m in body["moves"]) == 1
    assert set(main) >= {"start_at", "end_at", "pct_change", "volume_multiple", "direction", "window_minutes"}


def test_route_quiet_ticker_has_bars_and_no_moves():
    body = client.get("/price-history/LOTUSFOODS").json()
    assert len(body["bars"]) > 500 and body["moves"] == []


def test_route_without_price_history_is_404():
    r = client.get("/price-history/RELIANCE")
    assert r.status_code == 404
    assert r.json()["detail"] == "No price history for RELIANCE in this demo."


def test_route_needs_login(anon_client):
    assert anon_client.get("/price-history/ACMEINFRA").status_code == 401
    assert anon_client.post("/investigate-move/ACMEINFRA",
                            json={"move_start_at": "2026-10-22T10:02:00+05:30"}).status_code == 401


# --- POST /investigate-move ----------------------------------------------------------------

@pytest.mark.parametrize("ticker", RECORDED)
def test_investigating_the_recorded_move_matches_investigate(ticker):
    start = recorded_move(ticker).move_start_at.isoformat()
    card = client.post(f"/investigate-move/{ticker}", json={"move_start_at": start}).json()
    plain = client.post(f"/investigate/{ticker}").json()
    assert (card["confidence_label"], card["confidence_score"]) == EXPECTED_MAIN[ticker]
    assert (plain["confidence_label"], plain["confidence_score"]) == EXPECTED_MAIN[ticker]
    assert card["leading_explanation"] == plain["leading_explanation"]


@pytest.mark.parametrize("ticker, start", [(t, s) for t, extras in EXTRA.items() for s, _ in extras])
def test_older_move_gets_a_valid_unknown_card(ticker, start):
    r = client.post(f"/investigate-move/{ticker}", json={"move_start_at": start})
    assert r.status_code == 200
    card = r.json()
    assert card["confidence_label"] == "Unknown" and card["leading_explanation"] is None
    assert card["accepted_evidence"] == [] and card["rejected_evidence"] == []  # no borrowed evidence
    assert card["move"]["move_start_at"] == start
    assert card["explanation_text"].startswith(config.UNKNOWN_TEXT)
    assert sum(s["points"] for s in card["score_breakdown"]) == card["confidence_score"]


def test_investigated_move_is_saved_as_investigate():
    start = EXTRA["ACMEINFRA"][0][0]
    client.post("/investigate-move/ACMEINFRA", json={"move_start_at": start})
    [row] = client.get("/history").json()
    assert (row["ticker"], row["source"], row["label"]) == ("ACMEINFRA", "investigate", "Unknown")
    saved = client.get(f"/history/{row['id']}").json()
    assert saved["move"]["move_start_at"] == start


def test_unknown_move_start_is_404():
    r = client.post("/investigate-move/ACMEINFRA", json={"move_start_at": "2026-10-07T11:15:00+05:30"})
    assert r.status_code == 404 and "No unusual move" in r.json()["detail"]
    assert client.post("/investigate-move/RELIANCE",
                       json={"move_start_at": "2026-10-07T11:00:00+05:30"}).status_code == 404


def test_receipt_for_a_chosen_move():
    start = EXTRA["ACMEINFRA"][0][0]
    body = client.get("/receipt/ACMEINFRA", params={"move_start_at": start}).json()
    assert body["move"]["move_start_at"] == start and body["confidence_label"] == "Unknown"
    default = client.get("/receipt/ACMEINFRA").json()  # unchanged without the parameter
    assert (default["confidence_label"], default["confidence_score"]) == ("High", 100)


def test_main_demo_results_unchanged():
    for ticker, expected in EXPECTED_MAIN.items():
        card = client.post(f"/investigate/{ticker}").json()
        assert (card["confidence_label"], card["confidence_score"]) == expected
        sim = client.post(f"/simulate/{ticker}").json()["card"]
        assert (sim["confidence_label"], sim["confidence_score"]) == expected
