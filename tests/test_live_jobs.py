"""Live investigation jobs (/live/*) with TinyFish mocked: no network, no real key."""

import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import app as app_module
from backend import config, live_jobs
from backend.llm.explain import NOT_ADVICE
from backend.tinyfish.client import TinyFishClient
from tests.test_tinyfish_live import FILING_URL, NEWS_URL, FakeTinyFish, agent_result

SECRET = "tf-secret-key-must-never-leak"
client = TestClient(app_module.app)

FORM = {"company": "Live Test Ltd", "ticker": "LIVETEST", "move_start_at": "2026-10-22T10:02:00",
        "pct_change": -5.8, "volume_multiple": 3.1, "sector_pct_change": -0.7,
        "index_pct_change": -0.2}


@pytest.fixture(autouse=True)
def live_env(monkeypatch, tmp_path):
    """Live mode with a fake key; every job's TinyFish client talks to a FakeTinyFish."""
    monkeypatch.setattr(config, "MODE", "live")
    monkeypatch.setattr(config, "TINYFISH_API_KEY", SECRET)
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    live_jobs.reset_for_tests()
    yield
    live_jobs.reset_for_tests()


def use_fake(monkeypatch, tmp_path, fake=None) -> FakeTinyFish:
    fake = fake or FakeTinyFish()
    monkeypatch.setattr(app_module, "live_client_factory",
                        lambda: TinyFishClient(transport=httpx.MockTransport(fake),
                                               cache_dir=tmp_path / "cache"))
    return fake


def wait_for(job_id: str, seen: list[str] | None = None, timeout: float = 10) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        r = client.get(f"/live/{job_id}")
        if seen is not None:
            seen.append(r.text)
        body = r.json()
        if body["status"] in ("done", "failed") or time.monotonic() > deadline:
            return body
        time.sleep(0.05)


def start(form=None) -> httpx.Response:
    return client.post("/live/start", json=form or FORM)


# --- disabled ------------------------------------------------------------------

def test_live_off_gives_friendly_message(monkeypatch):
    monkeypatch.setattr(config, "MODE", "replay")
    r = start()
    assert r.status_code == 403
    assert "Live investigations are off" in r.json()["detail"]
    status = client.get("/live/status").json()
    assert status["enabled"] is False and "STOCKLENS_MODE=live" in status["message"]


def test_missing_key_gives_friendly_message(monkeypatch):
    monkeypatch.setattr(config, "TINYFISH_API_KEY", "")
    r = start()
    assert r.status_code == 403
    assert "TINYFISH_API_KEY" in r.json()["detail"]


def test_bad_form_is_rejected():
    assert start({**FORM, "ticker": "<script>"}).status_code == 422
    assert start({**FORM, "volume_multiple": -1}).status_code == 422


def test_unknown_job_is_404():
    assert client.get("/live/nope").status_code == 404


# --- a job runs to a Card ----------------------------------------------------------

def test_job_reaches_done_with_card(monkeypatch, tmp_path):
    fake = use_fake(monkeypatch, tmp_path)
    r = start()
    assert r.status_code == 200
    body = wait_for(r.json()["job_id"])

    assert body["status"] == "done" and body["error"] is None
    card = body["card"]
    assert card["ticker"] == "LIVETEST" and card["confidence_label"] != "Unknown"
    [filing] = [e for e in card["accepted_evidence"] if e["investigator"] == "sherlock"]
    assert filing["url"] == FILING_URL and filing["source_tier"] == 1
    assert filing["first_public_at"] == "2026-10-22T09:48:00+05:30"
    # One search, one Agent read, one Fetch to verify the snippet.
    assert (body["calls_used"], body["agent_calls"]) == (3, 1)
    assert len(fake.calls("agent.tinyfish.ai")) == 1
    for step in ("Searching for filings", "Reading filing 1", "Checking the timestamp",
                 "Scoring evidence", "Atlas, Echo: not connected live"):
        assert step in body["steps"]
    assert any("entered by the user" in limit for limit in card["limits"])
    assert any("TinyFish calls used: 3 of 10" in limit for limit in card["limits"])
    assert card["explanation_text"].strip()


def test_recorded_ticker_uses_replay_for_the_others(monkeypatch, tmp_path):
    use_fake(monkeypatch, tmp_path)
    body = wait_for(start({**FORM, "ticker": "ACMEINFRA"}).json()["job_id"])
    assert body["status"] == "done"
    assert any("recorded replay data" in s for s in body["steps"])
    investigators = {e["investigator"] for e in body["card"]["accepted_evidence"]}
    investigators |= {r["evidence"]["investigator"] for r in body["card"]["rejected_evidence"]}
    assert "echo" in investigators  # replay Echo item for ACMEINFRA


def test_rejected_page_gives_honest_unknown(monkeypatch, tmp_path):
    fake = FakeTinyFish(search_results=[{"url": FILING_URL}, {"url": NEWS_URL}],
                        agent_responses={FILING_URL: agent_result(FILING_URL, published_at="22-Oct-2026")})
    use_fake(monkeypatch, tmp_path, fake)
    body = wait_for(start().json()["job_id"])

    card = body["card"]
    assert body["status"] == "done"
    assert card["confidence_label"] == "Unknown" and card["leading_explanation"] is None
    assert card["accepted_evidence"] == [] and card["rejected_evidence"] == []  # nothing invented
    text = card["explanation_text"]
    assert text.startswith(config.UNKNOWN_TEXT)
    assert FILING_URL in text and "no usable timestamp" in text
    assert text.endswith(NOT_ADVICE)  # the page reasons go before the closing not-advice line
    assert any("Filing 1 rejected: no usable timestamp" in s for s in body["steps"])
    assert any(FILING_URL in limit for limit in card["limits"])


def test_api_error_gives_unknown_that_says_search_stopped(monkeypatch, tmp_path):
    use_fake(monkeypatch, tmp_path, FakeTinyFish(status=402))
    body = wait_for(start().json()["job_id"])
    assert body["status"] == "done"
    assert body["card"]["confidence_label"] == "Unknown"
    assert "stopped early" in body["card"]["explanation_text"]
    assert "INSUFFICIENT_CREDITS" in body["card"]["explanation_text"]


def test_unexpected_crash_ends_as_failed(monkeypatch, tmp_path):
    def broken():
        raise RuntimeError(f"cannot build client with {SECRET}")
    monkeypatch.setattr(app_module, "live_client_factory", broken)
    body = wait_for(start().json()["job_id"])
    assert body["status"] == "failed" and body["card"] is None
    assert body["error"].startswith("Live investigation failed")
    assert SECRET not in body["error"]


# --- one job at a time -------------------------------------------------------------

class BlockingTinyFish(FakeTinyFish):
    """Holds every request until released, so a job stays running."""

    def __init__(self):
        super().__init__()
        self.release = threading.Event()

    def __call__(self, request):
        self.release.wait(10)
        return super().__call__(request)


def test_only_one_live_job_at_a_time(monkeypatch, tmp_path):
    fake = use_fake(monkeypatch, tmp_path, BlockingTinyFish())
    first = start()
    assert first.status_code == 200
    job_id = first.json()["job_id"]
    assert client.get("/live/status").json()["active_job_id"] == job_id

    second = start({**FORM, "ticker": "OTHER"})
    assert second.status_code == 409
    assert "already running" in second.json()["detail"]

    # While the search is in flight, the running job already reports the call it spent.
    deadline = time.monotonic() + 3
    while client.get(f"/live/{job_id}").json()["calls_used"] < 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    running = client.get(f"/live/{job_id}").json()
    assert running["status"] == "running" and running["calls_used"] == 1
    assert "Searching for filings" in running["steps"]

    fake.release.set()
    assert wait_for(job_id)["status"] == "done"
    third = start()
    assert third.status_code == 200
    wait_for(third.json()["job_id"])


# --- the key never leaves the server ------------------------------------------------

def test_key_is_never_in_any_response(monkeypatch, tmp_path):
    fake = use_fake(monkeypatch, tmp_path)
    seen = [client.get("/live/status").text]
    r = start()
    seen.append(r.text)
    wait_for(r.json()["job_id"], seen)
    seen.append(client.get(f"/live/{r.json()['job_id']}").text)

    # Error paths too: an API error and a busy rejection.
    use_fake(monkeypatch, tmp_path, FakeTinyFish(status=401))
    r2 = start({**FORM, "ticker": "ERRCASE"})
    seen.append(r2.text)
    wait_for(r2.json()["job_id"], seen)

    assert fake.requests and all(req.headers["X-API-Key"] == SECRET for req in fake.requests)
    for text in seen:
        assert SECRET not in text


# --- history ---------------------------------------------------------------------

def test_finished_live_job_is_saved_to_history(monkeypatch, tmp_path):
    use_fake(monkeypatch, tmp_path)
    body = wait_for(start().json()["job_id"])
    assert body["status"] == "done"
    [row] = client.get("/history").json()
    assert (row["ticker"], row["source"], row["mode"]) == ("LIVETEST", "live", "live")
    stored = client.get(f"/history/{row['id']}").json()
    assert stored == body["card"]
    assert SECRET not in client.get(f"/history/{row['id']}").text


def test_live_job_belongs_to_the_user_who_started_it(monkeypatch, tmp_path):
    use_fake(monkeypatch, tmp_path)
    job_id = start().json()["job_id"]
    other = TestClient(app_module.app)
    assert other.post("/auth/signup", json={"account_id": "someone-else", "password": "pw-123456"}).status_code == 201
    assert other.get(f"/live/{job_id}").status_code == 404  # cannot watch another user's job
    assert other.get("/live/status").json()["active_job_id"] in (None,)  # never sees its id
    assert wait_for(job_id)["status"] == "done"
    assert other.get("/history").json() == []  # the finished Card went to the starter's history
    assert [r["source"] for r in client.get("/history").json()] == ["live"]
