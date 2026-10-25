"""Tests for backend/llm/: client, explain (with offline fallback) and extract. The LLM is always mocked."""

import json

import pytest

from backend import config
from backend.consensus.engine import build_card
from backend.llm import client, explain, extract
from backend.models import Evidence, Move

FAKE_KEY = "test-key-not-real"


def card_for(case: str):
    data = json.loads((config.FIXTURES_DIR / case / "consensus_input.json").read_text())
    return build_card(Move(**data["move"]), [Evidence(**e) for e in data["evidence"]])


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.setattr(config, "LLM_API_KEY", "")


def fake_llm(monkeypatch, module, reply: str) -> list[str]:
    """Make module.complete return `reply`; returns the list of prompts it received."""
    prompts = []

    def complete(prompt, json_mode=False):
        prompts.append(prompt)
        return reply

    monkeypatch.setattr(module, "complete", complete)
    return prompts


# --- client ------------------------------------------------------------------

def test_client_without_key_raises_unavailable(no_key):
    with pytest.raises(client.LLMUnavailable):
        client.complete("hello")


@pytest.mark.parametrize("provider, model, payload", [
    ("anthropic", "claude-opus-5-5",
     {"content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "hi"}]}),
    ("openai", "some-openai-model", {"choices": [{"message": {"content": "hi"}}]}),
    ("gemini", "some-gemini-model", {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}),
])
def test_client_reads_each_provider_reply(monkeypatch, provider, model, payload):
    monkeypatch.setattr(config, "LLM_API_KEY", FAKE_KEY)
    monkeypatch.setattr(config, "LLM_PROVIDER", provider)
    monkeypatch.setattr(config, "LLM_MODEL", model)

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    monkeypatch.setattr(client.httpx, "post", lambda *a, **k: FakeResponse())
    assert client.complete("hello") == "hi"


# --- explain: offline fallback -----------------------------------------------

def test_offline_high_names_cause_and_timing(no_key):
    card = card_for("case_high")
    text = explain.explain(card)
    assert text == explain.offline_explanation(card, "plain")
    assert "High confidence (100 out of 100)" in text
    assert card.leading_explanation in text
    assert "14 minutes before the move began" in text
    assert "not investment advice" in text


def test_offline_medium_market_leads(no_key):
    card = card_for("case_market_wide")
    text = explain.explain(card, tone="expert")
    assert card.confidence_label == "Medium"
    assert text.startswith("Medium confidence, score 50")
    assert "Market explanation leads" in text
    assert "Score breakdown:" in text
    assert "not investment advice" in text


def test_offline_unknown_lists_why_each_candidate_failed(no_key):
    card = card_for("case_unknown")
    text = explain.explain(card)
    assert text.startswith("Cause not confidently identified")
    for r in card.rejected_evidence:
        assert r.reason in text
    assert "not investment advice" in text


def test_offline_text_states_no_wrong_confidence_number():
    for case in ("case_high", "case_market_wide", "case_unknown"):
        card = card_for(case)
        for tone in explain.TONES:
            numbers = explain.stated_confidence_numbers(explain.offline_explanation(card, tone))
            assert all(n == card.confidence_score for n in numbers)


# --- explain: LLM path -------------------------------------------------------

def test_good_llm_reply_is_used(monkeypatch):
    reply = "High confidence (100 out of 100). The company cut its guidance in a filing."
    fake_llm(monkeypatch, explain, reply)
    assert explain.explain(card_for("case_high")) == reply


def test_wrong_confidence_number_falls_back_to_offline(monkeypatch):
    card = card_for("case_high")
    fake_llm(monkeypatch, explain, "We are 95% confident the guidance cut caused the fall.")
    assert explain.explain(card) == explain.offline_explanation(card, "plain")


def test_unknown_reply_must_open_with_unknown_sentence(monkeypatch):
    card = card_for("case_unknown")
    fake_llm(monkeypatch, explain, "Probably an export order win.")
    assert explain.explain(card) == explain.offline_explanation(card, "plain")


def test_llm_error_falls_back_to_offline(monkeypatch):
    def broken(prompt, json_mode=False):
        raise client.LLMError("boom")

    monkeypatch.setattr(explain, "complete", broken)
    card = card_for("case_market_wide")
    assert explain.explain(card) == explain.offline_explanation(card, "plain")


def test_prompt_has_every_placeholder_filled(monkeypatch):
    prompts = fake_llm(monkeypatch, explain, "fine")
    explain.explain(card_for("case_high"), tone="plain")
    prompt = prompts[0]
    for name in ("ticker", "confidence_label", "confidence_score", "leading_explanation",
                 "move_summary", "accepted_evidence", "rejected_evidence", "alternatives",
                 "score_breakdown", "limits", "search_summary", "tone_instructions"):
        assert "{" + name + "}" not in prompt
    assert "100 out of 100" in prompt  # tone section's own {confidence_score}
    assert "retail investor" in prompt  # plain tone, not expert


def test_fill_explanation_returns_copy(no_key):
    card = card_for("case_high")
    filled = explain.fill_explanation(card, "expert")
    assert filled.explanation_text == explain.offline_explanation(card, "expert")
    assert card.explanation_text == ""


# --- extract -----------------------------------------------------------------

MOVE = card_for("case_high").move
PAGE = "Posted 22 Oct 2026 10:34 IST. We downgrade to SELL after the guidance revision."


def run_extract(monkeypatch, kind: str, reply) -> Evidence | None:
    fake_llm(monkeypatch, extract, reply if isinstance(reply, str) else json.dumps(reply))
    return extract.extract_evidence(kind, PAGE, "ACMEINFRA", MOVE, "Broker desk",
                                    "https://brokerdesk.example/n", "ev-1")


def llm_evidence(**overrides) -> dict:
    data = {
        "id": "ev-1", "claim": "Broker X says it downgrades ACME Infra to Sell",
        "source_name": "Broker desk", "source_tier": 3, "url": "https://brokerdesk.example/n",
        "first_public_at": "2026-10-22T10:34:00+05:30",
        "snippet": "We downgrade to SELL after the guidance revision.",
        "investigator": "echo", "independence_group": None, "verified": False,
        "original_source": None,
    }
    data.update(overrides)
    return data


def test_extract_bad_json_returns_none(monkeypatch):
    assert run_extract(monkeypatch, "news", "not json {") is None


def test_extract_null_reply_returns_none(monkeypatch):
    assert run_extract(monkeypatch, "news", "null") is None


def test_extract_missing_timestamp_returns_none(monkeypatch):
    assert run_extract(monkeypatch, "news", llm_evidence(first_public_at=None)) is None


def test_extract_timestamp_without_offset_returns_none(monkeypatch):
    assert run_extract(monkeypatch, "news", llm_evidence(first_public_at="2026-10-22T10:34:00")) is None


def test_extract_social_forced_to_tier3_unverified(monkeypatch):
    e = run_extract(monkeypatch, "social", llm_evidence(source_tier=1, verified=True))
    assert e.source_tier == 3
    assert e.verified is False
    assert e.investigator == "echo"


def test_extract_news_keeps_verified_only_for_exact_snippet(monkeypatch):
    good = run_extract(monkeypatch, "news", llm_evidence(source_tier=2, verified=True))
    assert good.source_tier == 2 and good.verified is True
    made_up = run_extract(monkeypatch, "news",
                          llm_evidence(verified=True, snippet="A quote that is not on the page."))
    assert made_up.verified is False


def test_extract_accepts_fenced_json(monkeypatch):
    reply = "```json\n" + json.dumps(llm_evidence()) + "\n```"
    assert run_extract(monkeypatch, "social", reply).id == "ev-1"
