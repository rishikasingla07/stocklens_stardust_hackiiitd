"""Investigation history (SQLite): saving, listing, filtering, reopening. Uses a temporary DB (conftest)."""

import sqlite3
import subprocess

from fastapi.testclient import TestClient

from backend import app as app_module
from backend import config
from backend.db import history

client = TestClient(app_module.app)


def offline(monkeypatch):
    monkeypatch.setattr(config, "MODE", "replay")
    monkeypatch.setattr(config, "LLM_API_KEY", "")


def test_startup_creates_database_and_table(temp_db):
    assert not temp_db.exists()
    with TestClient(app_module.app):  # runs the lifespan startup
        pass
    with sqlite3.connect(temp_db) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(investigations)")]
    assert columns == ["id", "ticker", "created_at", "source", "mode", "label", "score", "card_json"]


def test_investigate_saves_and_list_returns_it(monkeypatch):
    offline(monkeypatch)
    card = client.post("/investigate/ACMEINFRA").json()
    [row] = client.get("/history").json()
    assert (row["ticker"], row["source"], row["mode"]) == ("ACMEINFRA", "investigate", "replay")
    assert (row["label"], row["score"]) == (card["confidence_label"], card["confidence_score"]) == ("High", 90)
    assert row["created_at"].endswith("+05:30")  # IST
    assert "card_json" not in row


def test_every_source_is_saved(monkeypatch):
    offline(monkeypatch)
    client.post("/investigate/ZENITHPOLY")
    client.post("/simulate/ORIONPHARMA")
    client.post("/challenge/ACMEINFRA")
    sources = [(r["ticker"], r["source"]) for r in client.get("/history").json()]
    assert sources == [("ACMEINFRA", "challenge"), ("ORIONPHARMA", "simulate"),
                       ("ZENITHPOLY", "investigate")]  # newest first


def test_no_card_means_nothing_saved(monkeypatch):
    offline(monkeypatch)
    assert client.post("/investigate/TCS").status_code == 404
    assert client.get("/history").json() == []


def test_filter_by_ticker_and_limit(monkeypatch):
    offline(monkeypatch)
    for ticker in ("ACMEINFRA", "ZENITHPOLY", "ACMEINFRA"):
        client.post(f"/investigate/{ticker}")
    rows = client.get("/history", params={"ticker": "acmeinfra"}).json()
    assert [r["ticker"] for r in rows] == ["ACMEINFRA", "ACMEINFRA"]
    assert rows[0]["id"] > rows[1]["id"]
    assert len(client.get("/history", params={"limit": 1}).json()) == 1


def test_list_is_capped_at_50(monkeypatch):
    card = app_module.fixture_card("ACMEINFRA")
    for _ in range(55):
        history.save(card, "investigate", "replay")
    assert len(client.get("/history", params={"limit": 500}).json()) == 50


def test_reopen_returns_stored_card_without_rerunning(monkeypatch):
    offline(monkeypatch)
    original = client.post("/investigate/ACMEINFRA").json()
    entry_id = client.get("/history").json()[0]["id"]

    def must_not_run(*args, **kwargs):
        raise AssertionError("reopening must not re-run the investigation")
    monkeypatch.setattr(app_module, "card_source", must_not_run)
    monkeypatch.setattr(app_module.pipeline, "live_card", must_not_run)
    reopened = client.get(f"/history/{entry_id}")
    assert reopened.status_code == 200
    assert reopened.json() == original


def test_missing_id_is_404():
    r = client.get("/history/999999")
    assert r.status_code == 404
    assert "No saved investigation" in r.json()["detail"]


def test_api_keys_are_never_stored(monkeypatch, temp_db):
    monkeypatch.setattr(config, "TINYFISH_API_KEY", "tf-secret-key-123")
    monkeypatch.setattr(config, "LLM_API_KEY", "llm-secret-key-456")
    card = app_module.fixture_card("ACMEINFRA")
    leaky = card.model_copy(update={"limits": card.limits + ["error with tf-secret-key-123 and llm-secret-key-456"]})
    history.save(leaky, "live", "live")
    raw = temp_db.read_bytes()
    assert b"tf-secret-key-123" not in raw and b"llm-secret-key-456" not in raw


def test_storage_failure_does_not_break_investigate(monkeypatch):
    offline(monkeypatch)

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(history, "save", broken)
    assert client.post("/investigate/ACMEINFRA").status_code == 200


def test_db_files_are_git_ignored():
    for name in ("stocklens.db", "other.db"):
        result = subprocess.run(["git", "check-ignore", "-q", name], cwd=config.ROOT, capture_output=True)
        assert result.returncode == 0, f"{name} is not git-ignored"
