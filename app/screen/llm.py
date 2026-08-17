"""Cheap LLM gate: condensed JD + condensed profile → structured verdict.

The condensed profile is personal information. Never log prompt or completion
text, and never put model output into exception args (docs/PRIVACY_AND_COMPLIANCE.md).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, get_settings
from app.extract.llm import (
    DEFAULT_GEMINI_API_BASE,
    LLMUsage,
    RetryableLLMError,
    gemini_generate_json,
)

logger = logging.getLogger(__name__)

GATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "verdict": {"type": "STRING", "enum": ["pass", "reject"]},
        "reason": {"type": "STRING"},
        "confidence": {"type": "NUMBER"},
    },
    "required": ["verdict", "reason", "confidence"],
}

GATE_SYSTEM_PROMPT = """\
You are a job-fit screening gate. Given a condensed job description and a \
condensed candidate profile, decide whether the candidate is a plausible fit \
worth generating a tailored resume.

Return only JSON matching the schema:
- verdict: "pass" or "reject"
- reason: one specific sentence a user can read in a screened-out view. Do \
not invent employers, skills, years, or numbers that are not in the inputs.
- confidence: 0.0–1.0

Rules:
- Do not invent skills or experience the profile does not contain.
- Reject when the profile clearly cannot meet the role.
- Pass when the profile is a reasonable fit, including adjacent experience.
- A single missing skill is not automatic grounds for reject.
- Never include resume text, contact details, or identifiers beyond the inputs.
"""


class GateDecision(BaseModel):
    """Structured cheap-gate output. Extra keys from the model are ignored."""

    model_config = ConfigDict(extra="ignore")

    verdict: str
    reason: str = ""
    confidence: float = Field(default=0.0)

    def normalized(self) -> GateDecision:
        verdict = (self.verdict or "").strip().lower()
        if verdict not in {"pass", "reject"}:
            raise RetryableLLMError("gate llm invalid verdict")
        reason = (self.reason or "").strip()
        try:
            confidence = float(self.confidence)
        except (TypeError, ValueError) as exc:
            raise RetryableLLMError("gate llm invalid confidence") from exc
        confidence = min(1.0, max(0.0, confidence))
        return GateDecision(verdict=verdict, reason=reason, confidence=confidence)


@runtime_checkable
class GateLLM(Protocol):
    def screen(
        self, *, job_doc: str, profile_doc: str
    ) -> tuple[GateDecision, LLMUsage]:
        """Structured gate. Raises RetryableLLMError on transient failures."""


def log_gate_usage(usage: LLMUsage, *, match_id: str | None = None) -> None:
    """Log billed token counts and estimated cost. Never log profile or JD text."""
    logger.info(
        "screen-job llm model=%s prompt_tokens=%s completion_tokens=%s "
        "cost_usd=%.6f match_id=%s",
        usage.model,
        usage.prompt_tokens,
        usage.completion_tokens,
        usage.cost_usd,
        match_id or "-",
    )


def _api_key(settings: Settings) -> str:
    return (
        settings.llm_api_key
        or os.environ.get("GEMINI_API_KEY", "")
        or os.environ.get("GOOGLE_API_KEY", "")
    )


class GeminiGateLLM:
    """Budget Gemini structured gate. Model name comes from Settings, not call sites."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        api_base: str = DEFAULT_GEMINI_API_BASE,
        input_usd_per_mtok: float = 0.30,
        output_usd_per_mtok: float = 2.50,
        timeout: float = 45.0,
    ) -> None:
        if not api_key:
            raise RetryableLLMError("llm_api_key is not configured")
        self._api_key = api_key
        self._model = model
        self._api_base = api_base.rstrip("/")
        self._input_usd_per_mtok = input_usd_per_mtok
        self._output_usd_per_mtok = output_usd_per_mtok
        self._timeout = timeout

    def screen(
        self, *, job_doc: str, profile_doc: str
    ) -> tuple[GateDecision, LLMUsage]:
        user_text = f"Job:\n{job_doc}\n\nProfile:\n{profile_doc}"
        try:
            data, usage = gemini_generate_json(
                api_key=self._api_key,
                model=self._model,
                api_base=self._api_base,
                system_prompt=GATE_SYSTEM_PROMPT,
                user_text=user_text,
                response_schema=GATE_RESPONSE_SCHEMA,
                input_usd_per_mtok=self._input_usd_per_mtok,
                output_usd_per_mtok=self._output_usd_per_mtok,
                timeout=self._timeout,
            )
        except RetryableLLMError:
            # Drop upstream args — generate_json errors can echo completion text.
            raise RetryableLLMError("gate llm retryable failure") from None
        try:
            decision = GateDecision.model_validate(data).normalized()
        except RetryableLLMError:
            raise RetryableLLMError("gate llm invalid structured output") from None
        except Exception:
            raise RetryableLLMError("gate llm invalid structured output") from None
        return decision, usage


def build_gate_llm(settings: Settings | None = None) -> GateLLM:
    settings = settings or get_settings()
    key = _api_key(settings)
    if not key:
        raise RetryableLLMError("llm_api_key is not configured")
    return GeminiGateLLM(
        api_key=key,
        model=settings.gate_model,
        api_base=settings.llm_api_base,
        input_usd_per_mtok=settings.gate_input_usd_per_mtok,
        output_usd_per_mtok=settings.gate_output_usd_per_mtok,
    )
