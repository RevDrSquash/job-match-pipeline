"""LLM client with token/cost logging and no user-content leakage.

Call sites pass prompts; this module never writes prompt or completion text
to logs. Only model, token counts, estimated USD, and purpose are recorded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.config import Settings
from app.privacy import PrivacySafeError, safe_exc

logger = logging.getLogger(__name__)

# USD per 1M tokens. Order-of-magnitude; used only for the cost log line.
# Keep in sync with whatever `llm_model` / `embedding_model` we actually call.
_PRICE_PER_MILLION: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "text-embedding-3-small": (0.02, 0.0),
    "text-embedding-3-large": (0.13, 0.0),
    "text-embedding-004": (0.025, 0.0),
}


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int = 0) -> float:
    input_rate, output_rate = _PRICE_PER_MILLION.get(model, (0.0, 0.0))
    return (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000


def log_llm_usage(
    *,
    purpose: str,
    model: str,
    input_tokens: int,
    output_tokens: int = 0,
) -> float:
    cost = estimate_cost_usd(model, input_tokens, output_tokens)
    logger.info(
        "llm_call purpose=%s model=%s input_tokens=%d output_tokens=%d cost_usd=%.6f",
        purpose,
        model,
        input_tokens,
        output_tokens,
        cost,
    )
    return cost


@dataclass(frozen=True)
class LlmResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


class LlmClient(Protocol):
    def complete_json(self, *, system: str, user: str, purpose: str) -> LlmResult: ...


class OpenAICompatibleClient:
    """Chat completions against an OpenAI-compatible `/chat/completions` endpoint."""

    def __init__(self, settings: Settings, *, timeout_s: float = 60.0) -> None:
        if not settings.llm_api_key:
            raise PrivacySafeError("LLM_API_KEY is not set")
        self._settings = settings
        self._timeout_s = timeout_s

    def complete_json(self, *, system: str, user: str, purpose: str) -> LlmResult:
        url = self._settings.llm_base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self._settings.llm_model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        headers = {
            "Authorization": f"Bearer {self._settings.llm_api_key}",
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(timeout=self._timeout_s) as client:
                response = client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise safe_exc("LLM request failed", exc) from None

        if response.status_code >= 400:
            raise PrivacySafeError(f"LLM request failed (HTTP {response.status_code})")

        try:
            body: dict[str, Any] = response.json()
            text = body["choices"][0]["message"]["content"]
            usage = body.get("usage") or {}
            input_tokens = int(usage.get("prompt_tokens") or 0)
            output_tokens = int(usage.get("completion_tokens") or 0)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise safe_exc("LLM response missing content", exc) from None

        if not isinstance(text, str) or not text.strip():
            raise PrivacySafeError("LLM response missing content")

        cost = log_llm_usage(
            purpose=purpose,
            model=self._settings.llm_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        return LlmResult(
            text=text,
            model=self._settings.llm_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
        )


class FakeLlmClient:
    """Test double: returns a canned JSON string. Never used in production."""

    def __init__(self, text: str) -> None:
        self._text = text

    def complete_json(self, *, system: str, user: str, purpose: str) -> LlmResult:
        cost = log_llm_usage(purpose=purpose, model="fake", input_tokens=0, output_tokens=0)
        return LlmResult(
            text=self._text,
            model="fake",
            input_tokens=0,
            output_tokens=0,
            cost_usd=cost,
        )


def get_llm_client(settings: Settings) -> LlmClient:
    impl = settings.llm_impl.strip().lower()
    if impl == "openai":
        return OpenAICompatibleClient(settings)
    if impl == "fake":
        raise PrivacySafeError("llm_impl=fake requires an injected FakeLlmClient")
    raise PrivacySafeError(f"unknown llm_impl {impl!r}")
