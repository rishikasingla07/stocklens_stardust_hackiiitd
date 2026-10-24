"""Tests for the API routes in backend/app.py."""

import json

import pytest
from fastapi.testclient import TestClient

from backend import app as app_module
from backend import config

client = TestClient(app_module.app)

CASES = ["case_high", "case_unknown", "case_market_wide"]


def load_case(name: str) -> dict:
    return json.loads((config.FIXTURES_DIR / name / "consensus_input.json").read_text())


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_index_serves_frontend():
    assert client.get("/").status_code == 200


def test_stocks_flag_every_fixture_ticker():
    data = client.get("/stocks").json()
    rows = data["holdings"] + data["watchlist"]
    assert 6 <= len(rows) <= 8
    alerted = {s["ticker"] for s in rows if s["alert"]}
    assert alerted == {load_case(c)["move"]["ticker"] for c in CASES}
    for s in rows:
        assert {"ticker", "price", "change", "change_pct", "alert"} <= s.keys()


@pytest.mark.parametrize("case", CASES)
def test_investigate_matches_fixture_expectation(case):
    data = load_case(case)
    r = client.post(f"/investigate/{data['move']['ticker']}")
    assert r.status_code == 200
    card = r.json()
    assert card["confidence_label"] == data["expected"]["confidence_label"]
    if "confidence_score" in data["expected"]:
        assert card["confidence_score"] == data["expected"]["confidence_score"]


def test_investigate_is_case_insensitive():
    assert client.post("/investigate/acmeinfra").status_code == 200


def test_investigate_without_fixture_returns_clear_message():
    r = client.post("/investigate/TCS")
    assert r.status_code == 404
    assert "No investigation available" in r.json()["detail"]


def test_receipt_json_is_a_download():
    r = client.get("/receipt/ACMEINFRA")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    body = r.json()
    assert body["ticker"] == "ACMEINFRA"
    assert body["score_breakdown"] and body["limits"]
    assert sum(s["points"] for s in body["score_breakdown"]) == body["confidence_score"]


def test_receipt_html_lists_every_evidence_item():
    r = client.get("/receipt/ZENITHPOLY?format=html")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    for e in load_case("case_unknown")["evidence"]:
        assert e["url"] in r.text
    assert config.UNKNOWN_TEXT in r.text


def test_receipt_without_fixture_is_404():
    assert client.get("/receipt/TCS").status_code == 404


def test_challenge_placeholder():
    r = client.post("/challenge/ACMEINFRA")
    assert r.status_code == 200
    assert "Skeptic runs once the investigators are connected" in r.json()["message"]


def test_card_source_can_be_swapped(monkeypatch):
    real = app_module.fixture_card("ACMEINFRA")
    swapped = real.model_copy(update={"confidence_score": 42, "confidence_label": "Low"})
    monkeypatch.setattr(app_module, "card_source", lambda ticker: swapped)
    card = client.post("/investigate/ANYTHING").json()
    assert card["confidence_score"] == 42
