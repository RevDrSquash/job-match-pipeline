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

from app.config import Settings
from app.llm import (
    PermanentLLMError,
    RetryableLLMError,
    build_gemini_chat,
    structured_call,
)
from app.privacy import PrivacySafeError
from app.profile.schema import LlmParsePayload

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
    """JSON completion via Gemini structured output (same key as extract-job).

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
        chat_model: object | None = None,
    ) -> None:
        if chat_model is None and not api_key:
            raise PrivacySafeError("LLM_API_KEY is not set")
        self._model = model
        self._input_usd_per_mtok = input_usd_per_mtok
        self._output_usd_per_mtok = output_usd_per_mtok
        self._chat = chat_model or build_gemini_chat(
            api_key=api_key,
            model=model,
            api_base=api_base,
            timeout=timeout_s,
        )

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
        try:
            parsed, usage = structured_call(
                self._chat,
                LlmParsePayload,
                model_name=self._model,
                input_usd_per_mtok=self._input_usd_per_mtok,
                output_usd_per_mtok=self._output_usd_per_mtok,
                system_prompt=system,
                user_text=user,
                provider="profile llm",
            )
        except (PermanentLLMError, RetryableLLMError) as exc:
            raise PrivacySafeError(str(exc)) from None
        log_llm_usage(
            purpose=purpose,
            model=usage.model,
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            cost_usd=usage.cost_usd,
        )
        return LlmResult(
            text=parsed.model_dump_json(),
            model=usage.model,
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            cost_usd=usage.cost_usd,
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
