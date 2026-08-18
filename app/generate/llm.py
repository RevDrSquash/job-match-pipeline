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
from app.extract.llm import (
    DEFAULT_GEMINI_API_BASE,
    LLMUsage,
    MalformedLLMOutputError,
    PermanentLLMError,
    RetryableLLMError,
    classify_llm_status,
    parse_json_object,
    usage_cost,
)
from app.generate.schema import GeneratedResume
from app.privacy import safe_exc

logger = logging.getLogger(__name__)

GENERATION_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "resume_doc": {"type": "STRING"},
        "employers": {"type": "ARRAY", "items": {"type": "STRING"}},
        "titles": {"type": "ARRAY", "items": {"type": "STRING"}},
        "date_ranges": {"type": "ARRAY", "items": {"type": "STRING"}},
        "claimed_skill_ids": {"type": "ARRAY", "items": {"type": "STRING"}},
        "claims": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "text": {"type": "STRING"},
                    "span_ids": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "kind": {"type": "STRING"},
                    "canonical_skill_id": {"type": "STRING"},
                },
                "required": ["text", "span_ids"],
            },
        },
    },
    "required": ["resume_doc", "claims"],
}

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
    ) -> None:
        if not api_key:
            raise RetryableLLMError("llm_api_key is not configured")
        self._api_key = api_key
        self._model = model
        self._api_base = api_base.rstrip("/")
        self._input_usd_per_mtok = input_usd_per_mtok
        self._output_usd_per_mtok = output_usd_per_mtok
        self._timeout = timeout

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
            data, usage = self._generate_json(
                cache_prefix=cache_prefix,
                user_text=job_context,
                cache_name=cache_name,
            )
        # Drop upstream args — errors can echo completion text (PI).
        except PermanentLLMError:
            raise PermanentLLMError("generate llm permanent failure") from None
        except Exception:
            raise RetryableLLMError("generate llm retryable failure") from None
        try:
            resume = GeneratedResume.model_validate(data)
        except Exception:
            # temperature=0: a redelivery would pay for the same bad output.
            raise PermanentLLMError("generate llm invalid structured output") from None
        if not (resume.resume_doc or "").strip():
            raise PermanentLLMError("generate llm empty resume")
        return resume, usage

    def _generate_json(
        self,
        *,
        cache_prefix: str,
        user_text: str,
        cache_name: str | None,
    ) -> tuple[dict[str, Any], LLMUsage]:
        payload: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": GENERATION_SYSTEM_PROMPT}]},
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseSchema": GENERATION_RESPONSE_SCHEMA,
            },
        }
        if cache_name:
            payload["cachedContent"] = cache_name
            payload["contents"] = [{"role": "user", "parts": [{"text": user_text}]}]
        else:
            # Implicit cache: identical work-history prefix, JD-only suffix.
            payload["contents"] = [
                {
                    "role": "user",
                    "parts": [
                        {"text": cache_prefix},
                        {"text": user_text},
                    ],
                }
            ]
        url = f"{self._api_base}/models/{self._model}:generateContent"
        last_malformed: MalformedLLMOutputError | None = None
        for attempt in (1, 2):
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
                raise safe_exc("generate llm transport error", exc) from None

            classify_llm_status(response.status_code)
            try:
                return self._parse_body(response)
            except MalformedLLMOutputError as exc:
                # One in-process retry for a billed-but-malformed completion,
                # then permanent — never retry via the queue at full price.
                last_malformed = exc
                logger.warning(
                    "generate llm malformed output attempt=%s: %s", attempt, exc
                )
        assert last_malformed is not None
        raise last_malformed

    def _parse_body(self, response: httpx.Response) -> tuple[dict[str, Any], LLMUsage]:
        try:
            body = response.json()
        except ValueError:
            raise MalformedLLMOutputError("llm response was not JSON") from None

        usage_meta = body.get("usageMetadata") or {}
        prompt_tokens = int(usage_meta.get("promptTokenCount") or 0)
        completion_tokens = int(usage_meta.get("candidatesTokenCount") or 0)
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
        candidates = body.get("candidates") or []
        if not candidates:
            raise MalformedLLMOutputError(f"llm returned no candidates {usage_note}")
        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        text = "".join(str(part.get("text") or "") for part in parts)
        if not text.strip():
            raise MalformedLLMOutputError(f"llm returned empty content {usage_note}")
        try:
            return parse_json_object(text), usage
        except MalformedLLMOutputError as exc:
            # parse_json_object messages carry positions, not content — safe.
            raise MalformedLLMOutputError(f"{exc} {usage_note}") from None

    def _cached_content_name(self, cache_prefix: str, cache_key: str | None) -> str | None:
        if not cache_key or not cache_prefix.strip():
            return None
        with _cache_lock:
            existing = _cache_names.get(cache_key)
        if existing:
            return existing
        url = f"{self._api_base}/cachedContents"
        payload = {
            "model": f"models/{self._model}",
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
        except httpx.HTTPError:
            logger.info("generate-resume cache create failed transport")
            return None
        if response.status_code >= 400:
            # Short prefixes and unsupported models fall back to implicit cache.
            logger.info(
                "generate-resume cache create skipped status=%s",
                response.status_code,
            )
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
