"""Tests for the API routes in backend/app.py."""

import json

import pytest
from fastapi.testclient import TestClient

from backend import app as app_module
from backend import config

client = TestClient(app_module.app)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Replay data and no LLM key: explanations come from the offline template."""
    monkeypatch.setattr(config, "MODE", "replay")
    monkeypatch.setattr(config, "LLM_API_KEY", "")

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
    assert card["explanation_text"].strip()


def test_investigate_demo_cases():
    acme = client.post("/investigate/ACMEINFRA").json()
    assert (acme["confidence_label"], acme["confidence_score"]) == ("High", 100)
    assert client.post("/investigate/ZENITHPOLY").json()["confidence_label"] == "Unknown"
    orion = client.post("/investigate/ORIONPHARMA").json()
    assert orion["leading_explanation"].startswith("Market explanation leads")


def test_llm_failure_falls_back_to_offline_explanation(monkeypatch):
    from backend.llm import explain
    from backend.llm.client import LLMError

    def broken(*args, **kwargs):
        raise LLMError("boom")
    monkeypatch.setattr(config, "LLM_API_KEY", "set-but-broken")
    monkeypatch.setattr(explain, "complete", broken)
    card = client.post("/investigate/ACMEINFRA").json()
    assert card["explanation_text"] == explain.offline_explanation(
        app_module.Card.model_validate(card))


def test_unexpected_explain_error_falls_back_silently(monkeypatch):
    def crash(card, tone="plain"):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(app_module, "fill_explanation", crash)
    r = client.post("/investigate/ACMEINFRA")
    assert r.status_code == 200 and r.json()["explanation_text"].strip()


def test_fixture_path_is_fallback_when_pipeline_has_no_case(monkeypatch):
    monkeypatch.setattr(app_module.pipeline, "live_card", lambda ticker: None)
    card = client.post("/investigate/ACMEINFRA").json()
    assert card["confidence_score"] == 100 and card["explanation_text"].strip()


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


def test_cases_returns_real_results_for_compare_cards():
    rows = {r["ticker"]: r for r in client.get("/cases").json()}
    assert (rows["ACMEINFRA"]["label"], rows["ACMEINFRA"]["score"], rows["ACMEINFRA"]["market_leads"]) == ("High", 100, False)
    assert (rows["ZENITHPOLY"]["label"], rows["ZENITHPOLY"]["score"]) == ("Unknown", 15)
    assert rows["ZENITHPOLY"]["leading_explanation"] is None
    assert (rows["ORIONPHARMA"]["label"], rows["ORIONPHARMA"]["score"], rows["ORIONPHARMA"]["market_leads"]) == ("Medium", 50, True)
    assert client.get("/history").json() == []  # reading the cases saves nothing


def test_cases_match_investigate_in_live_mode(monkeypatch):
    monkeypatch.setattr(config, "MODE", "live")
    monkeypatch.setattr(config, "TINYFISH_API_KEY", "")
    for row in client.get("/cases").json():
        card = client.post(f"/investigate/{row['ticker']}").json()
        assert (card["confidence_label"], card["confidence_score"]) == (row["label"], row["score"])
    acme = client.post("/investigate/ACMEINFRA").json()
    assert (acme["confidence_label"], acme["confidence_score"]) == ("High", 100)


def test_challenge_route_is_gone():
    assert client.post("/challenge/ACMEINFRA").status_code == 404


def test_card_source_can_be_swapped(monkeypatch):
    real = app_module.fixture_card("ACMEINFRA")
    swapped = real.model_copy(update={"confidence_score": 42, "confidence_label": "Low"})
    monkeypatch.setattr(app_module, "card_source", lambda ticker: swapped)
    card = client.post("/investigate/ANYTHING").json()
    assert card["confidence_score"] == 42
