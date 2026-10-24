"""Live TinyFish client, cache and live Sherlock, with httpx mocked (no network, no real key)."""

import asyncio
import importlib.util
import json
from datetime import datetime

import httpx
import pytest

from backend import config
from backend.investigators import sherlock as sherlock_module
from backend.investigators.sherlock import Sherlock
from backend.models import IST, Evidence, Move
from backend.tinyfish import cache
from backend.tinyfish.client import CallBudget, CallBudgetExceeded, TinyFishClient, TinyFishError

FAKE_KEY = "test-key-not-real"
MOVE = Move(ticker="ACMEINFRA", move_start_at=datetime(2026, 10, 22, 10, 2, tzinfo=IST),
            window_minutes=42, pct_change=-5.8, volume_multiple=3.1,
            sector_pct_change=-0.7, index_pct_change=-0.2)
FILING_URL = "https://nsearchives.nseindia.com/corporate/ACMEINFRA_22102026.pdf"
BSE_URL = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/acme.pdf"
NEWS_URL = "https://marketdaily.example/acme"


class FakeTinyFish:
    """httpx handler that answers search and fetch like the documented API, and records calls."""

    def __init__(self, search_results=None, fetch_results=None, status=200):
        self.requests: list[httpx.Request] = []
        self.search_results = search_results if search_results is not None else [
            {"position": 1, "title": "Filing", "url": FILING_URL, "snippet": "..."},
            {"position": 2, "title": "BSE", "url": BSE_URL, "snippet": "..."},
            {"position": 3, "title": "News", "url": NEWS_URL, "snippet": "..."},
        ]
        self.fetch_results = fetch_results
        self.status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {
                "code": "INSUFFICIENT_CREDITS", "message": "Wallet balance too low"}})
        if request.url.host == "api.search.tinyfish.ai":
            return httpx.Response(200, json={"query": request.url.params["query"],
                                             "results": self.search_results,
                                             "total_results": len(self.search_results), "page": 0})
        urls = json.loads(request.content)["urls"]
        results = self.fetch_results if self.fetch_results is not None else [
            {"url": u, "final_url": u, "title": "t", "format": "markdown",
             "text": f"Filing text for {u}. Date: 22-Oct-2026 09:48:00"} for u in urls]
        return httpx.Response(200, json={"results": results, "errors": []})


def make_client(tmp_path, fake=None, limit=10, key=FAKE_KEY) -> tuple[TinyFishClient, FakeTinyFish]:
    fake = fake or FakeTinyFish()
    client = TinyFishClient(api_key=key, budget=CallBudget(limit=limit),
                            transport=httpx.MockTransport(fake), cache_dir=tmp_path / "cache")
    return client, fake


def filing_evidence(url: str) -> Evidence:
    return Evidence(id="x", claim="ACME Infra revises FY27 guidance", source_name="NSE corporate filing",
                    source_tier=1, url=url, snippet="The Board has revised the guidance",
                    first_public_at=datetime(2026, 10, 22, 9, 48, tzinfo=IST),
                    investigator="sherlock", verified=True)


# --- client ------------------------------------------------------------------

def test_search_uses_documented_endpoint_and_header(tmp_path):
    client, fake = make_client(tmp_path)
    data = asyncio.run(client.search("ACME filing", after_date="2026-10-20",
                                     before_date="2026-10-22"))
    [req] = fake.requests
    assert req.method == "GET" and str(req.url).startswith(config.TINYFISH_SEARCH_URL)
    assert req.headers["X-API-Key"] == FAKE_KEY
    assert req.url.params["query"] == "ACME filing"
    assert req.url.params["after_date"] == "2026-10-20"
    assert "domain_type" not in req.url.params  # None params are not sent
    assert data["results"][0]["url"] == FILING_URL


def test_fetch_posts_urls_as_json(tmp_path):
    client, fake = make_client(tmp_path)
    data = asyncio.run(client.fetch([FILING_URL, BSE_URL]))
    [req] = fake.requests
    assert req.method == "POST" and str(req.url).rstrip("/") == config.TINYFISH_FETCH_URL
    assert json.loads(req.content) == {"urls": [FILING_URL, BSE_URL], "format": "markdown"}
    assert len(data["results"]) == 2
    assert client.budget.used == 1  # one batch fetch is one call


def test_cache_hit_avoids_second_call(tmp_path):
    client, fake = make_client(tmp_path)
    first = asyncio.run(client.search("same query"))
    second = asyncio.run(client.search("same query"))
    assert first == second
    assert len(fake.requests) == 1
    assert client.budget.used == 1 and client.cache_hits == 1

    # A fresh client (new investigation) also reuses the cache, for free.
    again, fake2 = make_client(tmp_path)
    asyncio.run(again.search("same query"))
    assert fake2.requests == [] and again.budget.used == 0


def test_cache_file_never_contains_the_key(tmp_path):
    client, _ = make_client(tmp_path)
    asyncio.run(client.search("q"))
    [path] = (tmp_path / "cache").glob("*.json")
    assert FAKE_KEY not in path.read_text(encoding="utf-8")


def test_budget_stops_the_11th_call(tmp_path):
    client, fake = make_client(tmp_path, limit=10)
    for i in range(10):
        asyncio.run(client.search(f"query {i}"))
    with pytest.raises(CallBudgetExceeded):
        asyncio.run(client.search("query 10"))
    assert len(fake.requests) == 10


def test_missing_key_makes_no_request(tmp_path):
    client, fake = make_client(tmp_path, key="")
    with pytest.raises(TinyFishError, match="TINYFISH_API_KEY is not set"):
        asyncio.run(client.search("q"))
    assert fake.requests == [] and client.budget.used == 0


def test_http_error_is_clear_and_hides_key(tmp_path):
    client, _ = make_client(tmp_path, fake=FakeTinyFish(status=402))
    with pytest.raises(TinyFishError) as exc:
        asyncio.run(client.search("q"))
    assert str(exc.value) == ("TinyFish search failed: HTTP 402 INSUFFICIENT_CREDITS: "
                              "Wallet balance too low")
    assert FAKE_KEY not in str(exc.value)
    assert list((tmp_path / "cache").glob("*.json")) == []  # errors are not cached


def test_timeout_is_a_clear_error(tmp_path):
    def slow(request):
        raise httpx.ReadTimeout("timed out", request=request)
    client = TinyFishClient(api_key=FAKE_KEY, transport=httpx.MockTransport(slow),
                            cache_dir=tmp_path)
    with pytest.raises(TinyFishError, match="timed out"):
        asyncio.run(client.fetch(FILING_URL))


# --- live Sherlock -----------------------------------------------------------

def run_sherlock(client, company=None):
    return asyncio.run(Sherlock(client, mode="live", company=company).run(MOVE))


def test_sherlock_live_keeps_filings_and_skips_none(tmp_path, monkeypatch):
    calls = []

    def fake_extract(kind, text, name, move, source_name, url, evidence_id, investigator):
        calls.append((kind, url, source_name))
        return filing_evidence(url).model_copy(update={"id": evidence_id}) if url == FILING_URL else None

    monkeypatch.setattr(sherlock_module, "extract_evidence", fake_extract)
    client, fake = make_client(tmp_path)
    report = run_sherlock(client, company="ACME Infra")

    fetch_body = json.loads(fake.requests[1].content)
    assert fetch_body["urls"] == [FILING_URL, BSE_URL]  # exchange domains only, at most 2
    assert calls == [("filing", FILING_URL, "NSE corporate filing"),
                     ("filing", BSE_URL, "BSE corporate filing")]
    [e] = report.evidence
    assert (e.url, e.source_tier, e.investigator) == (FILING_URL, 1, "sherlock")
    assert e.first_public_at == datetime(2026, 10, 22, 9, 48, tzinfo=IST)
    assert "3 search result(s), 2 on exchange domains" in report.notes
    assert "fetched 2 of 2 page(s)" in report.notes
    assert "rejected 1" in report.notes
    assert "calls used 2/10" in report.notes


def test_sherlock_live_missing_key_gives_empty_report(tmp_path):
    client, fake = make_client(tmp_path, key="")
    report = run_sherlock(client)
    assert report.evidence == []
    assert "TINYFISH_API_KEY not set" in report.notes
    assert fake.requests == []


def test_sherlock_live_budget_runs_out(tmp_path, monkeypatch):
    monkeypatch.setattr(sherlock_module, "extract_evidence", lambda *a: pytest.fail("no fetch"))
    client, _ = make_client(tmp_path, limit=1)  # search allowed, fetch refused
    report = run_sherlock(client)
    assert report.evidence == []
    assert "live stopped: TinyFish call budget of 1 used up" in report.notes


def test_sherlock_live_api_error_gives_empty_report(tmp_path):
    client, _ = make_client(tmp_path, fake=FakeTinyFish(status=402))
    report = run_sherlock(client)
    assert report.evidence == [] and "HTTP 402" in report.notes


def test_sherlock_live_no_exchange_results_fetches_nothing(tmp_path):
    fake = FakeTinyFish(search_results=[{"url": NEWS_URL, "title": "News"}])
    client, _ = make_client(tmp_path, fake=fake)
    report = run_sherlock(client)
    assert len(fake.requests) == 1 and report.evidence == []
    assert "no exchange filing found" in report.notes


def test_sherlock_live_rejects_redirect_off_exchange(tmp_path, monkeypatch):
    monkeypatch.setattr(sherlock_module, "extract_evidence", lambda *a: pytest.fail("not called"))
    fake = FakeTinyFish(search_results=[{"url": FILING_URL}],
                        fetch_results=[{"url": FILING_URL, "final_url": NEWS_URL, "text": "x"}])
    client, _ = make_client(tmp_path, fake=fake)
    assert run_sherlock(client).evidence == []


# --- cache helpers -----------------------------------------------------------

@pytest.fixture
def fake_fixtures(tmp_path, monkeypatch):
    case = tmp_path / "fixtures" / "case_live"
    case.mkdir(parents=True)
    (case / "consensus_input.json").write_text("{}")
    monkeypatch.setattr(config, "FIXTURES_DIR", tmp_path / "fixtures")
    return case


def test_save_replay_evidence_and_copy_response(tmp_path, fake_fixtures):
    target = cache.save_replay_evidence("case_live", "sherlock", [filing_evidence(FILING_URL)])
    assert json.loads(target.read_text())[0]["url"] == FILING_URL
    with pytest.raises(FileExistsError):
        cache.save_replay_evidence("case_live", "sherlock", [filing_evidence(FILING_URL)])

    source = cache.save("abc123", {"tool": "search"}, {"results": []}, tmp_path / "cache")
    copied = cache.copy_to_case(source, "case_live")
    assert copied == fake_fixtures / config.REPLAY_SUBDIR / "responses" / "abc123.json"
    assert json.loads(copied.read_text())["response"] == {"results": []}


def test_copy_to_case_requires_a_real_case(fake_fixtures):
    with pytest.raises(FileNotFoundError):
        cache.save_replay_evidence("case_missing", "sherlock", [])


# --- demo script -------------------------------------------------------------

def load_script():
    spec = importlib.util.spec_from_file_location(
        "live_sherlock", config.ROOT / "scripts" / "live_sherlock.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_refuses_in_replay_mode(monkeypatch, capsys):
    script = load_script()
    monkeypatch.setattr(config, "MODE", "replay")
    monkeypatch.setattr(config, "TINYFISH_API_KEY", FAKE_KEY)
    assert script.main(["INFY", "2026-10-22T10:02"]) == 1
    assert "Refusing to run: STOCKLENS_MODE is 'replay'" in capsys.readouterr().out


def test_demo_refuses_without_key(monkeypatch, capsys):
    script = load_script()
    monkeypatch.setattr(config, "MODE", "live")
    monkeypatch.setattr(config, "TINYFISH_API_KEY", "")
    assert script.main(["INFY", "2026-10-22T10:02"]) == 1
    assert "TINYFISH_API_KEY is not set" in capsys.readouterr().out


def test_demo_move_defaults_to_ist():
    move = load_script().build_move("Infosys Ltd", "2026-10-22T10:02", 30)
    assert move.ticker == "INFOSYSLTD"
    assert move.move_start_at == datetime(2026, 10, 22, 10, 2, tzinfo=IST)
