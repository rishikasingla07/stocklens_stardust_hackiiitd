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
from backend.tinyfish import cache, extract_agent
from backend.tinyfish.client import (CallBudget, CallBudgetExceeded, TinyFishClient, TinyFishError,
                                     agent_answer)
from backend.tinyfish.extract_agent import extract_filing_with_agent, parse_published_at

FAKE_KEY = "test-key-not-real"
MOVE = Move(ticker="ACMEINFRA", move_start_at=datetime(2026, 10, 22, 10, 2, tzinfo=IST),
            window_minutes=42, pct_change=-5.8, volume_multiple=3.1,
            sector_pct_change=-0.7, index_pct_change=-0.2)
FILING_URL = "https://nsearchives.nseindia.com/corporate/ACMEINFRA_22102026.pdf"
BSE_URL = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/acme.pdf"
NEWS_URL = "https://marketdaily.example/acme"


SNIPPET = "The Board has revised the revenue growth guidance for FY27 to 6-8%"


def agent_result(url: str, **overrides) -> dict:
    """What the Agent reads off a filing page (documented /run COMPLETED shape)."""
    answer = {"claim": "ACME Infra revises FY27 revenue growth guidance",
              "published_at": "22-Oct-2026 09:48:00", "source_name": "NSE", "snippet": SNIPPET,
              "time_source": "exchange dissemination time"}
    answer.update(overrides)
    return {"run_id": "run-1", "status": "COMPLETED", "started_at": "2026-10-24T10:00:00Z",
            "finished_at": "2026-10-24T10:00:20Z", "num_of_steps": 3, "result": answer,
            "error": None}


class FakeTinyFish:
    """httpx handler that answers search, fetch and agent like the documented APIs, and records calls."""

    def __init__(self, search_results=None, fetch_results=None, status=200,
                 agent_responses=None, fetch_status=200):
        self.requests: list[httpx.Request] = []
        self.search_results = search_results if search_results is not None else [
            {"position": 1, "title": "Filing", "url": FILING_URL, "snippet": "..."},
            {"position": 2, "title": "BSE", "url": BSE_URL, "snippet": "..."},
            {"position": 3, "title": "News", "url": NEWS_URL, "snippet": "..."},
        ]
        self.fetch_results = fetch_results
        self.status = status
        self.agent_responses = agent_responses or {}  # url -> /run response body
        self.fetch_status = fetch_status

    def calls(self, host: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == host]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {
                "code": "INSUFFICIENT_CREDITS", "message": "Wallet balance too low"}})
        if request.url.host == "agent.tinyfish.ai":
            url = json.loads(request.content)["url"]
            return httpx.Response(200, json=self.agent_responses.get(url, agent_result(url)))
        if request.url.host == "api.fetch.tinyfish.ai" and self.fetch_status != 200:
            return httpx.Response(self.fetch_status, json={"error": {"code": "INTERNAL_ERROR",
                                                                     "message": "boom"}})
        if request.url.host == "api.search.tinyfish.ai":
            return httpx.Response(200, json={"query": request.url.params["query"],
                                             "results": self.search_results,
                                             "total_results": len(self.search_results), "page": 0})
        urls = json.loads(request.content)["urls"]
        results = self.fetch_results if self.fetch_results is not None else [
            {"url": u, "final_url": u, "title": "t", "format": "markdown",
             "text": f"Filing text for {u}. Date: 22-Oct-2026 09:48:00. {SNIPPET} on account of "
                     "delays."} for u in urls]
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


# --- agent() -------------------------------------------------------------------

def test_agent_posts_documented_body_and_header(tmp_path):
    client, fake = make_client(tmp_path)
    answer = asyncio.run(client.agent(FILING_URL, "read it", output_schema={"type": "object"}))
    [req] = fake.requests
    assert req.method == "POST" and str(req.url) == config.TINYFISH_AGENT_URL
    assert req.headers["X-API-Key"] == FAKE_KEY
    body = json.loads(req.content)
    assert (body["url"], body["goal"], body["output_schema"]) == (FILING_URL, "read it", {"type": "object"})
    assert body["agent_config"] == {"max_duration_seconds": config.TINYFISH_AGENT_MAX_DURATION_SECONDS}
    assert answer["published_at"] == "22-Oct-2026 09:48:00"


def test_agent_answer_object_result():
    assert agent_answer({"status": "COMPLETED", "result": {"claim": "c"}}) == {"claim": "c"}


def test_agent_answer_under_result_result():
    assert agent_answer({"status": "COMPLETED", "result": {"result": {"claim": "c"}}}) == {"claim": "c"}
    assert agent_answer({"status": "COMPLETED", "result": {"result": '{"claim": "c"}'}}) == {"claim": "c"}


def test_agent_answer_rejects_failed_or_non_object():
    with pytest.raises(TinyFishError, match="FAILED: SITE_BLOCKED: blocked"):
        agent_answer({"status": "FAILED", "result": None,
                      "error": {"code": "SITE_BLOCKED", "message": "blocked", "category": "AGENT_FAILURE"}})
    with pytest.raises(TinyFishError, match="no JSON object"):
        agent_answer({"status": "COMPLETED", "result": {"result": "just text"}})


def test_failed_agent_run_is_not_cached(tmp_path):
    failed = {"run_id": "r", "status": "FAILED", "result": None,
              "error": {"code": "TASK_FAILED", "message": "no", "category": "AGENT_FAILURE"}}
    client, fake = make_client(tmp_path, fake=FakeTinyFish(agent_responses={FILING_URL: failed}))
    with pytest.raises(TinyFishError):
        asyncio.run(client.agent(FILING_URL, "g"))
    assert list((tmp_path / "cache").glob("*.json")) == []


def test_agent_cache_stops_a_repeat_call(tmp_path):
    client, fake = make_client(tmp_path)
    first = asyncio.run(client.agent(FILING_URL, "same goal"))
    second = asyncio.run(client.agent(FILING_URL, "same goal"))
    assert first == second
    assert len(fake.requests) == 1 and client.budget.used == 1 and client.cache_hits == 1


def test_agent_budget_stops_the_11th_call(tmp_path):
    client, fake = make_client(tmp_path, limit=10)
    for i in range(10):
        asyncio.run(client.agent(FILING_URL, f"goal {i}"))
    with pytest.raises(CallBudgetExceeded):
        asyncio.run(client.agent(FILING_URL, "goal 10"))
    assert len(fake.requests) == 10


def test_agent_uses_its_own_timeout(tmp_path):
    def slow(request):
        raise httpx.ReadTimeout("timed out", request=request)
    client = TinyFishClient(api_key=FAKE_KEY, transport=httpx.MockTransport(slow), cache_dir=tmp_path)
    with pytest.raises(TinyFishError, match=f"agent timed out after {config.TINYFISH_AGENT_TIMEOUT_SECONDS} s"):
        asyncio.run(client.agent(FILING_URL, "g"))


def test_filing_schema_uses_only_supported_keywords():
    supported = {"anyOf", "enum", "format", "items", "maxItems", "maximum", "minItems", "minimum",
                 "nullable", "properties", "propertyOrdering", "required", "type"}

    def keywords(node):
        for key, value in node.items():
            yield key
            if key == "properties":
                for child in value.values():
                    yield from keywords(child)
    assert set(keywords(extract_agent.FILING_SCHEMA)) <= supported


# --- extract_filing_with_agent --------------------------------------------------

def extract(client, url=FILING_URL):
    return asyncio.run(extract_filing_with_agent(client, url, "ACME Infra", MOVE))


def test_extract_builds_verified_tier1_evidence(tmp_path):
    client, fake = make_client(tmp_path)
    e = extract(client)
    assert (e.url, e.source_tier, e.investigator, e.verified) == (FILING_URL, 1, "sherlock", True)
    assert e.first_public_at == datetime(2026, 10, 22, 9, 48, tzinfo=IST)
    assert e.id == extract_agent.evidence_id(FILING_URL)
    assert len(fake.calls("agent.tinyfish.ai")) == 1 and len(fake.calls("api.fetch.tinyfish.ai")) == 1


@pytest.mark.parametrize("published_at", [None, "", "22-Oct-2026", "2026-10-22", "22 October 2026"])
def test_date_only_or_missing_published_at_returns_none(tmp_path, published_at):
    fake = FakeTinyFish(agent_responses={FILING_URL: agent_result(FILING_URL, published_at=published_at)})
    client, _ = make_client(tmp_path, fake=fake)
    assert extract(client) is None


def test_time_without_zone_rejected_off_exchange(tmp_path):
    client, _ = make_client(tmp_path)
    assert extract(client, url=NEWS_URL) is None  # "22-Oct-2026 09:48:00" has no zone


def test_parse_published_at_rules(monkeypatch):
    ist_948 = datetime(2026, 10, 22, 9, 48, tzinfo=IST)
    assert parse_published_at("2026-10-22T04:18:00Z", False) == ist_948
    assert parse_published_at("22-Oct-2026 09:48 IST", False) == ist_948
    assert parse_published_at("22-Oct-2026 09:48:00", True) == ist_948
    monkeypatch.setattr(config, "EXCHANGE_PAGE_TIMES_ARE_IST", False)
    assert parse_published_at("22-Oct-2026 09:48:00", True) is None


def test_agent_cannot_change_url_tier_or_identity(tmp_path):
    sneaky = agent_result(NEWS_URL, published_at="22-Oct-2026 09:48 IST")
    sneaky["result"].update({"source_url": FILING_URL, "url": FILING_URL, "source_tier": 1, "tier": 1,
                             "investigator": "atlas", "id": "evil", "verified": True})
    client, _ = make_client(tmp_path, fake=FakeTinyFish(agent_responses={NEWS_URL: sneaky}))
    e = extract(client, url=NEWS_URL)
    assert (e.url, e.source_tier, e.investigator) == (NEWS_URL, 2, "sherlock")
    assert e.id == extract_agent.evidence_id(NEWS_URL)


def test_verified_only_when_snippet_is_in_fetched_text(tmp_path):
    other = [{"url": FILING_URL, "final_url": FILING_URL, "text": "A different page entirely."}]
    client, _ = make_client(tmp_path, fake=FakeTinyFish(fetch_results=other))
    assert extract(client).verified is False


def test_verify_skipped_when_fetch_fails(tmp_path):
    client, _ = make_client(tmp_path, fake=FakeTinyFish(fetch_status=500))
    e = extract(client)
    assert e is not None and e.verified is False


def test_verify_skipped_when_budget_spent(tmp_path):
    client, fake = make_client(tmp_path, limit=1)  # the agent call uses the whole budget
    e = extract(client)
    assert e is not None and e.verified is False
    assert fake.calls("api.fetch.tinyfish.ai") == []


# --- live Sherlock (Agent path) ---------------------------------------------------

def run_sherlock(client, company=None):
    return asyncio.run(Sherlock(client, mode="live", company=company).run(MOVE))


ARCHIVE_URL = "https://nsearchives.nseindia.com/corporate/ADV_ACMEINFRA_22OCT2026.pdf"
DATE_ONLY = {"published_at": "22-Oct-2026", "time_source": "document date only"}


def test_date_only_page_rejected_and_second_page_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LLM_API_KEY", "")  # no LLM key needed
    fake = FakeTinyFish(
        search_results=[{"url": ARCHIVE_URL}, {"url": NEWS_URL}, {"url": BSE_URL}, {"url": FILING_URL}],
        agent_responses={ARCHIVE_URL: agent_result(ARCHIVE_URL, **DATE_ONLY)})
    client, _ = make_client(tmp_path, fake=fake)
    sherlock = Sherlock(client, mode="live", company="ACME Infra")
    report = asyncio.run(sherlock.run(MOVE))

    agent_urls = [json.loads(r.content)["url"] for r in fake.calls("agent.tinyfish.ai")]
    assert agent_urls == [ARCHIVE_URL, BSE_URL]  # nsearchives subdomain counts; stops after BSE
    [e] = report.evidence
    assert (e.url, e.source_tier, e.investigator, e.verified) == (BSE_URL, 1, "sherlock", True)
    assert e.first_public_at == datetime(2026, 10, 22, 9, 48, tzinfo=IST)
    assert sherlock.time_sources[e.id] == "exchange dissemination time"
    assert "4 search result(s), 3 on exchange domains" in report.notes
    assert "pages found 3, read 2, extracted 1, rejected 1" in report.notes
    assert (f"rejected {ARCHIVE_URL}: no usable timestamp (missing, date only or no time zone) "
            "(time_source: 'document date only')") in report.notes
    assert "calls used 4/10" in report.notes  # search + 2 agent + 1 verify fetch


def test_sherlock_reads_at_most_max_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SHERLOCK_MAX_PAGES", 3)
    urls = [f"https://nsearchives.nseindia.com/corporate/f{i}.pdf" for i in range(5)]
    fake = FakeTinyFish(search_results=[{"url": u} for u in urls],
                        agent_responses={u: agent_result(u, **DATE_ONLY) for u in urls})
    client, _ = make_client(tmp_path, fake=fake)
    report = run_sherlock(client)
    assert len(fake.calls("agent.tinyfish.ai")) == 3 and report.evidence == []
    assert "pages found 3, read 3, extracted 0, rejected 3" in report.notes


def test_time_source_is_passed_through(tmp_path):
    fake = FakeTinyFish(agent_responses={FILING_URL: agent_result(
        FILING_URL, time_source="exchange received time")})
    client, _ = make_client(tmp_path, fake=fake)
    result = asyncio.run(extract_agent.extract_filing(client, FILING_URL, "ACME Infra", MOVE))
    assert result.time_source == "exchange received time" and result.evidence is not None


def test_goal_and_schema_ask_for_time_source(tmp_path):
    client, fake = make_client(tmp_path)
    extract(client)
    body = json.loads(fake.calls("agent.tinyfish.ai")[0].content)
    assert "time_source" in body["output_schema"]["required"]
    for phrase in ("broadcast or dissemination", "exchange received", "stamped on the document",
                   "document date"):
        assert phrase in body["goal"]


def test_exchange_domains_include_subdomains():
    assert extract_agent.is_exchange_filing(ARCHIVE_URL)
    assert extract_agent.is_exchange_filing("https://www.bseindia.com/x.pdf")
    assert not extract_agent.is_exchange_filing("https://nseindia.com.evil.example/x.pdf")


def test_sherlock_live_missing_key_gives_empty_report(tmp_path):
    client, fake = make_client(tmp_path, key="")
    report = run_sherlock(client)
    assert report.evidence == []
    assert "TINYFISH_API_KEY not set" in report.notes
    assert fake.requests == []


def test_sherlock_live_budget_runs_out(tmp_path):
    client, fake = make_client(tmp_path, limit=1)  # search allowed, agent refused
    report = run_sherlock(client)
    assert report.evidence == [] and fake.calls("agent.tinyfish.ai") == []
    assert "live stopped: TinyFish call budget of 1 used up" in report.notes


def test_sherlock_live_failed_agent_run_rejects_that_page(tmp_path):
    failed = {"run_id": "r", "status": "FAILED", "result": None,
              "error": {"code": "SITE_BLOCKED", "message": "blocked", "category": "AGENT_FAILURE"}}
    client, _ = make_client(tmp_path, fake=FakeTinyFish(agent_responses={FILING_URL: failed}))
    report = run_sherlock(client)
    assert [e.url for e in report.evidence] == [BSE_URL]  # moved on to the next page
    assert "SITE_BLOCKED" in report.notes


def test_sherlock_live_api_error_gives_empty_report(tmp_path):
    client, _ = make_client(tmp_path, fake=FakeTinyFish(status=402))
    report = run_sherlock(client)
    assert report.evidence == [] and "HTTP 402" in report.notes


def test_sherlock_live_no_exchange_results_reads_nothing(tmp_path):
    fake = FakeTinyFish(search_results=[{"url": NEWS_URL, "title": "News"}])
    client, _ = make_client(tmp_path, fake=fake)
    report = run_sherlock(client)
    assert len(fake.requests) == 1 and report.evidence == []
    assert "no exchange filing found" in report.notes


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
