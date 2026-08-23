"""Best-available Gemini generator with work-history prompt caching.

Resume text is personal information. Never log prompt or completion text,
and never put model output into exception args (docs/PRIVACY_AND_COMPLIANCE.md).
ZDR/no-training vendor terms are a production blocker; paperwork is deferred.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Protocol, runtime_checkable

import httpx

from app.config import Settings, get_settings
from app.generate.schema import GeneratedResume
from app.llm import (
    DEFAULT_GEMINI_API_BASE,
    LLMUsage,
    PermanentLLMError,
    RetryableLLMError,
    build_gemini_chat,
    structured_call,
)
from app.privacy import safe_exc

logger = logging.getLogger(__name__)

GENERATION_SYSTEM_PROMPT = """\
You write a tailored resume strictly grounded in the candidate's work history.

The work-history block is cached and is the only allowed source of facts.
Every claim must map to one or more span_id values from that block.

Return only JSON matching the schema:
- resume_doc: a complete paste-ready resume in Markdown
- employers / titles / date_ranges: exact values copied from the work history
- claimed_skill_ids: canonical ids from the MATCHED or ADJACENT buckets only
- claims: every factual statement with kind (employer|title|date_range|number|\
skill|accomplishment) and the supporting span_ids

Rules:
- Do not invent employers, titles, dates, skills, numbers, or experience.
- Do not find-replace skill terms. Terminology is context; choose a surface \
form that is honest. The form that satisfies both without substitution is \
like "AWS (Amazon Web Services)".
- MATCHED skills: surface prominently, prefer the JD's phrasing when it does \
not change the claim.
- ADJACENT skills: frame the bridge honestly. Do not claim the JD skill.
- MISSING skills: do not invent under any circumstances. Do not claim them.
- If a regenerate list of violations is provided, fix those violations only \
by removing or correcting unsupported claims — never by adding new facts.
- Never include contact details that are not in the work history.
"""


@runtime_checkable
class GenerateLLM(Protocol):
    def generate(
        self,
        *,
        cache_prefix: str,
        job_context: str,
        cache_key: str | None = None,
        violations: list[str] | None = None,
    ) -> tuple[GeneratedResume, LLMUsage]:
        """Structured generation. Raises RetryableLLMError on transient failures."""


def log_generate_usage(usage: LLMUsage, *, match_id: str | None = None) -> None:
    """Log billed token counts and estimated cost. Never log resume text."""
    logger.info(
        "generate-resume llm model=%s prompt_tokens=%s completion_tokens=%s "
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


def build_job_context(
    *,
    job_title: str | None,
    job_doc: str,
    buckets_text: str,
    violations: list[str] | None = None,
) -> str:
    parts = [
        f"Job title: {job_title or 'unknown'}",
        "",
        "Job description:",
        job_doc.strip() or "(no job description)",
        "",
        "Skill buckets:",
        buckets_text,
    ]
    if violations:
        parts.extend(
            [
                "",
                "Previous verification failed. Fix only these named violations.",
                "Do not add new facts to paper over a gap.",
                *[f"- {item}" for item in violations],
            ]
        )
    return "\n".join(parts)


def generation_messages(*, cache_prefix: str, user_text: str, cached: bool) -> list[dict[str, Any]]:
    """System + user messages. Implicit cache: prefix then JD as two user parts."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": GENERATION_SYSTEM_PROMPT},
    ]
    if cached:
        messages.append({"role": "user", "content": user_text})
        return messages
    messages.append(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": cache_prefix},
                {"type": "text", "text": user_text},
            ],
        }
    )
    return messages


_cache_lock = threading.Lock()
_cache_names: dict[str, str] = {}


class GeminiGenerateLLM:
    """Frontier Gemini structured generation with work-history prompt caching."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        api_base: str = DEFAULT_GEMINI_API_BASE,
        input_usd_per_mtok: float = 1.25,
        output_usd_per_mtok: float = 10.00,
        timeout: float = 90.0,
        chat_model: object | None = None,
    ) -> None:
        if chat_model is None and not api_key:
            raise RetryableLLMError("llm_api_key is not configured")
        self._api_key = api_key
        self._model_name = model
        self._api_base = api_base.rstrip("/")
        self._input_usd_per_mtok = input_usd_per_mtok
        self._output_usd_per_mtok = output_usd_per_mtok
        self._timeout = timeout
        self._chat = chat_model

    def generate(
        self,
        *,
        cache_prefix: str,
        job_context: str,
        cache_key: str | None = None,
        violations: list[str] | None = None,
    ) -> tuple[GeneratedResume, LLMUsage]:
        # violations are folded into job_context by the caller; the argument
        # stays on the protocol so fakes can assert regenerate-once behavior.
        _ = violations
        cache_name = self._cached_content_name(cache_prefix, cache_key)
        try:
            resume, usage = structured_call(
                self._chat_for(cache_name),
                GeneratedResume,
                model_name=self._model_name,
                input_usd_per_mtok=self._input_usd_per_mtok,
                output_usd_per_mtok=self._output_usd_per_mtok,
                messages=generation_messages(
                    cache_prefix=cache_prefix,
                    user_text=job_context,
                    cached=bool(cache_name),
                ),
                provider="generate llm",
            )
        except PermanentLLMError as exc:
            logger.warning("generate llm permanent failure cause=%s", exc)
            raise PermanentLLMError("generate llm permanent failure") from None
        except Exception as exc:
            logger.warning(
                "generate llm retryable failure cause=%s: %s", type(exc).__name__, exc
            )
            raise RetryableLLMError("generate llm retryable failure") from None
        if not (resume.resume_doc or "").strip():
            raise PermanentLLMError("generate llm empty resume")
        return resume, usage

    def _chat_for(self, cache_name: str | None) -> object:
        if self._chat is not None and not cache_name:
            return self._chat
        return build_gemini_chat(
            api_key=self._api_key,
            model=self._model_name,
            api_base=self._api_base,
            timeout=self._timeout,
            cached_content=cache_name,
        )

    def _cached_content_name(self, cache_prefix: str, cache_key: str | None) -> str | None:
        if not cache_key or not cache_prefix.strip():
            return None
        with _cache_lock:
            existing = _cache_names.get(cache_key)
        if existing is not None:
            # "" is a negative entry: creation already failed for this key
            # (e.g. prefix below the cache minimum) — don't burn a request
            # per generate call re-attempting it.
            return existing or None
        url = f"{self._api_base}/cachedContents"
        payload = {
            "model": f"models/{self._model_name}",
            "systemInstruction": {"parts": [{"text": GENERATION_SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": cache_prefix}]}],
            "ttl": "3600s",
        }
        try:
            response = httpx.post(
                url,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": self._api_key,
                },
                json=payload,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            logger.info("generate-resume cache create failed transport")
            _ = safe_exc("generate-resume cache create failed", exc)
            return None
        if response.status_code >= 400:
            # Short prefixes and unsupported models fall back to implicit cache.
            logger.info(
                "generate-resume cache create skipped status=%s",
                response.status_code,
            )
            if 400 <= response.status_code < 500 and response.status_code != 429:
                with _cache_lock:
                    _cache_names[cache_key] = ""
            return None
        try:
            name = (response.json() or {}).get("name")
        except ValueError:
            return None
        if not isinstance(name, str) or not name:
            return None
        with _cache_lock:
            _cache_names[cache_key] = name
        logger.info("generate-resume cache created key=%s", cache_key)
        return name


def build_generate_llm(settings: Settings | None = None) -> GenerateLLM:
    settings = settings or get_settings()
    key = _api_key(settings)
    if not key:
        raise RetryableLLMError("llm_api_key is not configured")
    return GeminiGenerateLLM(
        api_key=key,
        model=settings.generation_model,
        api_base=settings.llm_api_base,
        input_usd_per_mtok=settings.generation_input_usd_per_mtok,
        output_usd_per_mtok=settings.generation_output_usd_per_mtok,
    )
