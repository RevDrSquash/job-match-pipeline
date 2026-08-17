"""LLM client for profile parse with token/cost logging and no content leakage.

A resume is entirely personal information (docs/PRIVACY_AND_COMPLIANCE.md), so
this module never writes prompt or completion text to logs or exception args.
Only model, token counts, estimated USD, and purpose are recorded.

Uses the same Gemini API key/base as extract-job (app/config.py). ZDR /
no-training vendor terms are a production blocker tracked in the privacy doc;
the PoC default remains the offline fallback parser for real resumes until
those terms are in place.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.config import Settings
from app.privacy import PrivacySafeError, safe_exc

logger = logging.getLogger(__name__)


def log_llm_usage(
    *,
    purpose: str,
    model: str,
    input_tokens: int,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
) -> None:
    logger.info(
        "llm_call purpose=%s model=%s input_tokens=%d output_tokens=%d cost_usd=%.6f",
        purpose,
        model,
        input_tokens,
        output_tokens,
        cost_usd,
    )


@dataclass(frozen=True)
class LlmResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


class LlmClient(Protocol):
    def complete_json(self, *, system: str, user: str, purpose: str) -> LlmResult: ...


class GeminiProfileLLM:
    """JSON completion via Gemini ``generateContent`` (same API as extract-job).

    Errors are always re-raised as PrivacySafeError: response bodies and
    upstream exception args may echo resume text and must never surface.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        api_base: str,
        input_usd_per_mtok: float = 0.10,
        output_usd_per_mtok: float = 0.40,
        timeout_s: float = 60.0,
    ) -> None:
        if not api_key:
            raise PrivacySafeError("LLM_API_KEY is not set")
        self._api_key = api_key
        self._model = model
        self._api_base = api_base.rstrip("/")
        self._input_usd_per_mtok = input_usd_per_mtok
        self._output_usd_per_mtok = output_usd_per_mtok
        self._timeout_s = timeout_s

    @classmethod
    def from_settings(cls, settings: Settings, *, api_key: str) -> GeminiProfileLLM:
        return cls(
            api_key=api_key,
            model=settings.profile_parse_model,
            api_base=settings.llm_api_base,
            input_usd_per_mtok=settings.profile_parse_input_usd_per_mtok,
            output_usd_per_mtok=settings.profile_parse_output_usd_per_mtok,
        )

    def complete_json(self, *, system: str, user: str, purpose: str) -> LlmResult:
        url = f"{self._api_base}/models/{self._model}:generateContent"
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
            },
        }
        try:
            with httpx.Client(timeout=self._timeout_s) as client:
                response = client.post(
                    url,
                    headers={
                        "Content-Type": "application/json",
                        "x-goog-api-key": self._api_key,
                    },
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise safe_exc("LLM request failed", exc) from None

        if response.status_code >= 400:
            # Never include the response body — error payloads can echo input.
            raise PrivacySafeError(f"LLM request failed (HTTP {response.status_code})")

        try:
            body = response.json()
            candidates = body.get("candidates") or []
            parts = ((candidates[0].get("content") or {}).get("parts")) or []
            text = "".join(str(part.get("text") or "") for part in parts)
            usage = body.get("usageMetadata") or {}
            input_tokens = int(usage.get("promptTokenCount") or 0)
            output_tokens = int(usage.get("candidatesTokenCount") or 0)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise safe_exc("LLM response missing content", exc) from None

        if not text.strip():
            raise PrivacySafeError("LLM response missing content")

        cost = (input_tokens / 1_000_000) * self._input_usd_per_mtok + (
            output_tokens / 1_000_000
        ) * self._output_usd_per_mtok
        log_llm_usage(
            purpose=purpose,
            model=self._model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
        )
        return LlmResult(
            text=text,
            model=self._model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
        )


class FakeLlmClient:
    """Test double: returns a canned JSON string. Never used in production."""

    def __init__(self, text: str) -> None:
        self._text = text

    def complete_json(self, *, system: str, user: str, purpose: str) -> LlmResult:
        log_llm_usage(purpose=purpose, model="fake", input_tokens=0, output_tokens=0)
        return LlmResult(
            text=self._text,
            model="fake",
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
        )
