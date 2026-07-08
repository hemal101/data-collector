"""Minimal, dependency-light LLM provider abstraction.

The point is Phase 7 works *today* with a heuristic, and automatically upgrades
to a real model the moment an API key is available - no code change needed.

Provider is chosen from the environment:
    OPENAI_API_KEY     -> OpenAI chat completions   (OPENAI_MODEL, default gpt-4o-mini)
    ANTHROPIC_API_KEY  -> Anthropic messages        (ANTHROPIC_MODEL, default claude-3-5-haiku-latest)

Returns ``None`` when no key is configured, which callers treat as "use the
heuristic". Uses httpx directly so we don't pull in vendor SDKs.
"""

from __future__ import annotations

import json
import os

import httpx


class LLM:
    def __init__(self, provider: str, model: str) -> None:
        self.provider = provider
        self.model = model

    def complete_json(self, system: str, user: str, timeout: float = 40.0) -> dict | None:
        """Ask the model for a JSON object and parse it. None on any failure."""
        try:
            if self.provider == "openai":
                text = self._openai(system, user, timeout)
            elif self.provider == "anthropic":
                text = self._anthropic(system, user, timeout)
            else:
                return None
            return _extract_json(text)
        except Exception:  # noqa: BLE001 - never let enrichment crash on the LLM
            return None

    def _openai(self, system: str, user: str, timeout: float) -> str:
        key = os.environ["OPENAI_API_KEY"]
        r = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    def _anthropic(self, system: str, user: str, timeout: float) -> str:
        key = os.environ["ANTHROPIC_API_KEY"]
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": self.model,
                "max_tokens": 500,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()["content"][0]["text"]


def get_llm() -> LLM | None:
    """Return an LLM if a provider key is configured, else None."""
    if os.environ.get("OPENAI_API_KEY"):
        return LLM("openai", os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
    if os.environ.get("ANTHROPIC_API_KEY"):
        return LLM("anthropic", os.environ.get("ANTHROPIC_MODEL", "claude-3-5-haiku-latest"))
    return None


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None
