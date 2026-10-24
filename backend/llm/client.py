"""Tiny provider-agnostic LLM call: complete(prompt) -> reply text. Provider is set in .env."""

import httpx

from backend import config


class LLMError(Exception):
    """The LLM call failed (network, HTTP error or an unexpected reply shape)."""


class LLMUnavailable(LLMError):
    """No LLM is configured (no key, unknown provider or no model)."""


def _anthropic(prompt: str, json_mode: bool) -> tuple[str, dict, dict]:
    # No JSON mode flag here; the prompts already ask for JSON only.
    url = "https://api.anthropic.com/v1/messages"
    headers = {"x-api-key": config.LLM_API_KEY, "anthropic-version": "2023-06-01"}
    body = {
        "model": config.LLM_MODEL,
        "max_tokens": config.LLM_MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }
    return url, headers, body


def _openai(prompt: str, json_mode: bool) -> tuple[str, dict, dict]:
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {config.LLM_API_KEY}"}
    body = {
        "model": config.LLM_MODEL,
        "max_completion_tokens": config.LLM_MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    return url, headers, body


def _gemini(prompt: str, json_mode: bool) -> tuple[str, dict, dict]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.LLM_MODEL}:generateContent"
    headers = {"x-goog-api-key": config.LLM_API_KEY}
    generation = {"maxOutputTokens": config.LLM_MAX_TOKENS}
    if json_mode:
        generation["responseMimeType"] = "application/json"
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": generation}
    return url, headers, body


def _reply_text(provider: str, data: dict) -> str:
    """Pull the reply text out of each provider's response JSON."""
    if provider == "anthropic":
        # Skip thinking blocks; keep only text.
        return "".join(b["text"] for b in data["content"] if b["type"] == "text")
    if provider == "openai":
        return data["choices"][0]["message"]["content"] or ""
    return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])


PROVIDERS = {"anthropic": _anthropic, "openai": _openai, "gemini": _gemini}


def complete(prompt: str, json_mode: bool = False) -> str:
    """Send one prompt, return the reply text. Raises LLMUnavailable or LLMError."""
    if not config.LLM_API_KEY:
        raise LLMUnavailable("LLM_API_KEY is not set")
    provider = config.LLM_PROVIDER
    if provider not in PROVIDERS:
        raise LLMUnavailable(f"unknown LLM_PROVIDER {provider!r}")
    if not config.LLM_MODEL:
        raise LLMUnavailable(f"LLM_MODEL must be set for provider {provider!r}")

    url, headers, body = PROVIDERS[provider](prompt, json_mode)
    try:
        response = httpx.post(url, headers=headers, json=body, timeout=config.LLM_TIMEOUT_SECONDS)
        response.raise_for_status()
        return _reply_text(provider, response.json())
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"{provider} call failed: {exc}") from exc
