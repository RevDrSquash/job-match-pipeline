"""Structured JD extraction: schema, prompt, Gemini client, usage logging."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# Empty / boilerplate-only JDs are a permanent failure — do not spend on them.
MIN_RAW_JD_CHARS = 40

# Gemini REST (no residency/ZDR constraint: job postings are not personal info).
DEFAULT_GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

EXTRACTION_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "parseable": {"type": "BOOLEAN"},
        "seniority": {"type": "STRING"},
        "hard_requirements": {"type": "ARRAY", "items": {"type": "STRING"}},
        "nice_to_haves": {"type": "ARRAY", "items": {"type": "STRING"}},
        "work_arrangement": {"type": "STRING"},
        "comp_min": {"type": "INTEGER"},
        "comp_max": {"type": "INTEGER"},
        "skill_spans": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": [
        "parseable",
        "hard_requirements",
        "nice_to_haves",
        "skill_spans",
    ],
}

EXTRACTION_SYSTEM_PROMPT = """\
You extract structured fields from a job posting. The posting is not personal \
information. Return only JSON that matches the schema.

Hard vs nice-to-have (this distinction is load-bearing — it drives a later \
deterministic gate):
- hard_requirements: must-haves. Required qualifications, required years of \
experience, required skills, items marked must / required / minimum / \
"you will" as a condition of hire.
- nice_to_haves: optional. Preferred, bonus, plus, ideally, a plus, \
"nice to have".
If the posting does not distinguish, put concrete qualifications in \
hard_requirements and stretch or optional items in nice_to_haves. \
Never invent requirements that are not in the posting.

Other fields:
- seniority: one of intern, junior, mid, senior, staff, principal, executive, \
unknown. Infer from title and requirements; do not invent a level the posting \
does not support.
- work_arrangement: one of remote, hybrid, onsite, unknown.
- comp_min / comp_max: annual cash compensation as integers in the posting's \
currency, or omit if not stated. Convert hourly/monthly to annual when the \
posting makes that possible; otherwise omit.
- skill_spans: short surface forms of skills as they appear (or a close \
normalization). Include tools, languages, platforms, and named practices. \
Do not include generic soft-skill prose.
- parseable: false only if this is not a usable job description (empty, \
boilerplate-only, or not a job posting). When parseable is false, leave \
lists empty and omit other fields.

Do not fabricate employers, skills, years, or compensation.
"""


class RetryableLLMError(Exception):
    """LLM/embed transport, 5xx/429, or config error — handler should return 5xx."""


class PermanentLLMError(Exception):
    """Non-transient LLM outcome — handler logs, records the event, returns 2xx.

    Covers request-level HTTP 400 (poison payload; retrying burns queue
    attempts for the same answer) and billed completions that stay malformed
    after one in-process retry (temperature is 0 — a repeat retry pays full
    price for the same bad output).
    """


class MalformedLLMOutputError(PermanentLLMError):
    """A billed completion that failed to parse — retried once in-process."""


def classify_llm_status(status_code: int, *, provider: str = "llm") -> None:
    """Raise on non-2xx statuses with retryable/permanent classification.

    408/429/5xx are transient. 401/403/404 are operator config errors (bad key
    or model name): they affect every task and bill no tokens, so they stay
    retryable rather than silently dropping work as permanent. Any other 4xx
    is a poison request — permanent.
    """
    if status_code < 400:
        return
    if status_code in {408, 429} or status_code >= 500:
        raise RetryableLLMError(f"{provider} HTTP {status_code}")
    if status_code in {401, 403, 404}:
        raise RetryableLLMError(f"{provider} HTTP {status_code} (config)")
    raise PermanentLLMError(f"{provider} HTTP {status_code}")


class JobExtraction(BaseModel):
    """Structured extraction from a raw JD. Extra keys from the model are ignored."""

    model_config = ConfigDict(extra="ignore")

    parseable: bool = True
    seniority: str | None = None
    hard_requirements: list[str] = Field(default_factory=list)
    nice_to_haves: list[str] = Field(default_factory=list)
    work_arrangement: str | None = None
    comp_min: int | None = None
    comp_max: int | None = None
    skill_spans: list[str] = Field(default_factory=list)

    def is_usable(self) -> bool:
        if not self.parseable:
            return False
        return bool(
            (self.seniority and self.seniority.strip() and self.seniority != "unknown")
            or self.hard_requirements
            or self.nice_to_haves
            or self.skill_spans
            or self.comp_min is not None
            or self.comp_max is not None
        )


@dataclass(frozen=True, slots=True)
class LLMUsage:
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float


@runtime_checkable
class JobLLM(Protocol):
    def extract_job(
        self, raw_jd: str, *, title: str | None = None
    ) -> tuple[JobExtraction, LLMUsage]:
        """Structured extraction. Raises RetryableLLMError on transient failures."""


def log_llm_usage(usage: LLMUsage, *, job_id: str | None = None) -> None:
    """Log billed token counts and estimated cost. Never log JD text."""
    logger.info(
        "extract-job llm model=%s prompt_tokens=%s completion_tokens=%s cost_usd=%.6f job_id=%s",
        usage.model,
        usage.prompt_tokens,
        usage.completion_tokens,
        usage.cost_usd,
        job_id or "-",
    )


def usage_cost(
    prompt_tokens: int,
    completion_tokens: int,
    *,
    input_usd_per_mtok: float,
    output_usd_per_mtok: float,
) -> float:
    return (prompt_tokens / 1_000_000) * input_usd_per_mtok + (
        completion_tokens / 1_000_000
    ) * output_usd_per_mtok


def parse_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        if stripped.endswith("```"):
            stripped = stripped[: -3]
        stripped = stripped.strip()
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        # JSONDecodeError messages carry positions, not content — safe to keep.
        raise MalformedLLMOutputError(f"unparseable LLM JSON: {exc.msg}") from None
    if not isinstance(data, dict):
        raise MalformedLLMOutputError("LLM JSON was not an object")
    return data


def gemini_generate_json(
    *,
    api_key: str,
    model: str,
    api_base: str,
    system_prompt: str,
    user_text: str,
    response_schema: dict[str, Any],
    input_usd_per_mtok: float,
    output_usd_per_mtok: float,
    timeout: float = 45.0,
) -> tuple[dict[str, Any], LLMUsage]:
    """Structured Gemini generateContent.

    Raises RetryableLLMError on transport/5xx/429/config statuses and
    PermanentLLMError on request-level 4xx. A billed-but-malformed completion
    is retried once in-process, then raised as PermanentLLMError so a poison
    prompt cannot burn spend on every queue redelivery.
    """
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": response_schema,
        },
    }
    url = f"{api_base.rstrip('/')}/models/{model}:generateContent"
    last_malformed: MalformedLLMOutputError | None = None
    for attempt in (1, 2):
        try:
            response = httpx.post(
                url,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": api_key,
                },
                json=payload,
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise RetryableLLMError(f"llm transport error: {type(exc).__name__}") from exc

        classify_llm_status(response.status_code)
        try:
            return _parse_gemini_body(
                response,
                model=model,
                input_usd_per_mtok=input_usd_per_mtok,
                output_usd_per_mtok=output_usd_per_mtok,
            )
        except MalformedLLMOutputError as exc:
            last_malformed = exc
            logger.warning(
                "llm malformed output model=%s attempt=%s: %s", model, attempt, exc
            )
    assert last_malformed is not None
    raise last_malformed


def _parse_gemini_body(
    response: httpx.Response,
    *,
    model: str,
    input_usd_per_mtok: float,
    output_usd_per_mtok: float,
) -> tuple[dict[str, Any], LLMUsage]:
    try:
        body = response.json()
    except ValueError:
        raise MalformedLLMOutputError("llm response was not JSON") from None

    usage_meta = body.get("usageMetadata") or {}
    prompt_tokens = int(usage_meta.get("promptTokenCount") or 0)
    completion_tokens = int(usage_meta.get("candidatesTokenCount") or 0)
    usage = LLMUsage(
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=usage_cost(
            prompt_tokens,
            completion_tokens,
            input_usd_per_mtok=input_usd_per_mtok,
            output_usd_per_mtok=output_usd_per_mtok,
        ),
    )

    # Malformed completions are still billed — carry usage in the message so
    # the retry warning logs token counts for the failed attempt too.
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
        raise MalformedLLMOutputError(f"{exc} {usage_note}") from None


class GeminiJobLLM:
    """Cheapest-adequate Gemini structured extraction (no PI / no ZDR)."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        api_base: str = DEFAULT_GEMINI_API_BASE,
        input_usd_per_mtok: float = 0.10,
        output_usd_per_mtok: float = 0.40,
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

    def extract_job(
        self, raw_jd: str, *, title: str | None = None
    ) -> tuple[JobExtraction, LLMUsage]:
        user_parts = []
        if title and title.strip():
            user_parts.append(f"Title: {title.strip()}")
        user_parts.append("Job description:")
        user_parts.append(raw_jd)
        data, usage = gemini_generate_json(
            api_key=self._api_key,
            model=self._model,
            api_base=self._api_base,
            system_prompt=EXTRACTION_SYSTEM_PROMPT,
            user_text="\n".join(user_parts),
            response_schema=EXTRACTION_RESPONSE_SCHEMA,
            input_usd_per_mtok=self._input_usd_per_mtok,
            output_usd_per_mtok=self._output_usd_per_mtok,
            timeout=self._timeout,
        )
        try:
            return JobExtraction.model_validate(data), usage
        except Exception:
            # temperature=0: a redelivery would pay for the same bad output.
            raise PermanentLLMError("extraction llm invalid structured output") from None
