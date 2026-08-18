"""Anthropic verifier for stages 2 (JD-blind grounding) and 3 (coverage).

A different model family than the Gemini generator. Resume text is personal
information — never log prompt or completion text, and never put model
output into exception args. ZDR paperwork is deferred (OPEN_ISSUES §8).
"""

from __future__ import annotations

import logging
import os
from typing import Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, get_settings
from app.extract.llm import (
    LLMUsage,
    MalformedLLMOutputError,
    PermanentLLMError,
    RetryableLLMError,
    classify_llm_status,
    parse_json_object,
    usage_cost,
)
from app.privacy import safe_exc

logger = logging.getLogger(__name__)

DEFAULT_ANTHROPIC_API_BASE = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"

GROUNDING_SYSTEM_PROMPT = """\
You verify that a generated resume is grounded in the candidate's work \
history and nothing else. You are deliberately NOT given a job description. \
Do not infer what the candidate "should" have. An invented claim that would \
fit a typical job is still a fabrication.

Return only JSON:
- verdict: "pass" or "fail"
- violations: list of short named problems (empty if pass)
- reason: one sentence, no resume quotations

Fail on unsupported employers, titles, dates, numbers, skills, or semantic \
drift (e.g. "led" vs "contributed"). Pass only when every claim is supported.
"""

COVERAGE_SYSTEM_PROMPT = """\
You check whether a generated resume dropped or under-weighted experience \
that is relevant to the job and present in the work history. Do not reward \
fabricated content. Missing skills the user does not have are not coverage \
failures.

Return only JSON:
- verdict: "pass" or "fail"
- violations: list of short named drops or under-weights (empty if pass)
- reason: one sentence, no resume quotations
"""


class VerifyDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    verdict: str
    violations: list[str] = Field(default_factory=list)
    reason: str = ""

    def normalized(self) -> VerifyDecision:
        verdict = (self.verdict or "").strip().lower()
        if verdict not in {"pass", "fail"}:
            # temperature=0: a redelivery would pay for the same bad output.
            raise PermanentLLMError("verify llm invalid verdict")
        cleaned = [str(item).strip() for item in self.violations if str(item).strip()]
        return VerifyDecision(
            verdict=verdict,
            violations=cleaned,
            reason=(self.reason or "").strip(),
        )


@runtime_checkable
class VerifyLLM(Protocol):
    def ground(
        self, *, resume_doc: str, work_history_block: str
    ) -> tuple[VerifyDecision, LLMUsage]:
        """JD-blind grounding check."""

    def coverage(
        self,
        *,
        resume_doc: str,
        job_context: str,
        work_history_block: str,
    ) -> tuple[VerifyDecision, LLMUsage]:
        """JD-aware coverage check. Separate call from ground()."""


def log_verify_usage(
    usage: LLMUsage, *, stage: str, generation_id: str | None = None
) -> None:
    logger.info(
        "verify-resume llm stage=%s model=%s prompt_tokens=%s completion_tokens=%s "
        "cost_usd=%.6f generation_id=%s",
        stage,
        usage.model,
        usage.prompt_tokens,
        usage.completion_tokens,
        usage.cost_usd,
        generation_id or "-",
    )


def _api_key(settings: Settings) -> str:
    return (
        settings.verify_api_key
        or os.environ.get("ANTHROPIC_API_KEY", "")
        or os.environ.get("VERIFY_API_KEY", "")
    )


class AnthropicVerifyLLM:
    """Claude Messages API client. Model name comes from Settings."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        api_base: str = DEFAULT_ANTHROPIC_API_BASE,
        input_usd_per_mtok: float = 3.00,
        output_usd_per_mtok: float = 15.00,
        timeout: float = 60.0,
    ) -> None:
        if not api_key:
            raise RetryableLLMError("verify_api_key is not configured")
        self._api_key = api_key
        self._model = model
        self._api_base = api_base.rstrip("/")
        self._input_usd_per_mtok = input_usd_per_mtok
        self._output_usd_per_mtok = output_usd_per_mtok
        self._timeout = timeout

    def ground(
        self, *, resume_doc: str, work_history_block: str
    ) -> tuple[VerifyDecision, LLMUsage]:
        user_text = (
            "Work history (only source of truth):\n"
            f"{work_history_block}\n\n"
            "Generated resume:\n"
            f"{resume_doc}"
        )
        return self._complete(GROUNDING_SYSTEM_PROMPT, user_text)

    def coverage(
        self,
        *,
        resume_doc: str,
        job_context: str,
        work_history_block: str,
    ) -> tuple[VerifyDecision, LLMUsage]:
        user_text = (
            "Job context:\n"
            f"{job_context}\n\n"
            "Work history:\n"
            f"{work_history_block}\n\n"
            "Generated resume:\n"
            f"{resume_doc}"
        )
        return self._complete(COVERAGE_SYSTEM_PROMPT, user_text)

    def _complete(
        self, system: str, user_text: str
    ) -> tuple[VerifyDecision, LLMUsage]:
        url = f"{self._api_base}/v1/messages"
        payload = {
            "model": self._model,
            "max_tokens": 1024,
            "temperature": 0,
            "system": system,
            "messages": [{"role": "user", "content": user_text}],
        }
        last_malformed: MalformedLLMOutputError | None = None
        for attempt in (1, 2):
            try:
                response = httpx.post(
                    url,
                    headers={
                        "Content-Type": "application/json",
                        "x-api-key": self._api_key,
                        "anthropic-version": ANTHROPIC_VERSION,
                    },
                    json=payload,
                    timeout=self._timeout,
                )
            except httpx.HTTPError as exc:
                raise RetryableLLMError("verify llm retryable failure") from safe_exc(
                    "verify llm transport error", exc
                )

            classify_llm_status(response.status_code, provider="verify llm")
            try:
                return self._parse_body(response)
            except MalformedLLMOutputError as exc:
                # One in-process retry for a billed-but-malformed completion,
                # then permanent — never retry via the queue at full price.
                last_malformed = exc
                logger.warning(
                    "verify llm malformed output attempt=%s: %s", attempt, exc
                )
        assert last_malformed is not None
        raise last_malformed

    def _parse_body(self, response: httpx.Response) -> tuple[VerifyDecision, LLMUsage]:
        try:
            body = response.json()
            blocks = body.get("content") or []
            text = "".join(
                str(block.get("text") or "")
                for block in blocks
                if isinstance(block, dict)
            )
            usage_meta = body.get("usage") or {}
            prompt_tokens = int(usage_meta.get("input_tokens") or 0)
            completion_tokens = int(usage_meta.get("output_tokens") or 0)
        except (TypeError, ValueError, AttributeError):
            raise MalformedLLMOutputError("verify llm invalid response") from None

        usage = LLMUsage(
            model=self._model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=usage_cost(
                prompt_tokens,
                completion_tokens,
                input_usd_per_mtok=self._input_usd_per_mtok,
                output_usd_per_mtok=self._output_usd_per_mtok,
            ),
        )
        usage_note = (
            f"(prompt_tokens={usage.prompt_tokens} "
            f"completion_tokens={usage.completion_tokens} "
            f"cost_usd={usage.cost_usd:.6f})"
        )
        if not text.strip():
            raise MalformedLLMOutputError(f"verify llm empty content {usage_note}")
        try:
            data = parse_json_object(text)
            decision = VerifyDecision.model_validate(data).normalized()
        except PermanentLLMError:
            # Includes MalformedLLMOutputError from parse and the deterministic
            # invalid-verdict case. Sanitized: no completion text in args.
            raise MalformedLLMOutputError(
                f"verify llm invalid structured output {usage_note}"
            ) from None
        except Exception:
            raise MalformedLLMOutputError(
                f"verify llm invalid structured output {usage_note}"
            ) from None
        return decision, usage


def build_verify_llm(settings: Settings | None = None) -> VerifyLLM:
    settings = settings or get_settings()
    key = _api_key(settings)
    if not key:
        raise RetryableLLMError("verify_api_key is not configured")
    return AnthropicVerifyLLM(
        api_key=key,
        model=settings.verify_model,
        api_base=settings.verify_api_base,
        input_usd_per_mtok=settings.verify_input_usd_per_mtok,
        output_usd_per_mtok=settings.verify_output_usd_per_mtok,
    )
