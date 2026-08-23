"""Structured JD extraction: schema, prompt, Gemini client, usage logging."""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from app.llm import (
    DEFAULT_GEMINI_API_BASE,
    LLMUsage,
    RetryableLLMError,
    build_gemini_chat,
    structured_call,
)

logger = logging.getLogger(__name__)

# Empty / boilerplate-only JDs are a permanent failure — do not spend on them.
MIN_RAW_JD_CHARS = 40

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
normalization). Each item is one skill — never a comma- or slash-separated \
list. Include tools, languages, platforms, and named practices. \
Do not include generic soft-skill prose.
- parseable: false only if this is not a usable job description (empty, \
boilerplate-only, or not a job posting). When parseable is false, leave \
lists empty and omit other fields.

Do not fabricate employers, skills, years, or compensation.
"""


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
        chat_model: object | None = None,
    ) -> None:
        if chat_model is None and not api_key:
            raise RetryableLLMError("llm_api_key is not configured")
        self._model_name = model
        self._input_usd_per_mtok = input_usd_per_mtok
        self._output_usd_per_mtok = output_usd_per_mtok
        self._chat = chat_model or build_gemini_chat(
            api_key=api_key,
            model=model,
            api_base=api_base,
            timeout=timeout,
        )

    def extract_job(
        self, raw_jd: str, *, title: str | None = None
    ) -> tuple[JobExtraction, LLMUsage]:
        user_parts = []
        if title and title.strip():
            user_parts.append(f"Title: {title.strip()}")
        user_parts.append("Job description:")
        user_parts.append(raw_jd)
        return structured_call(
            self._chat,
            JobExtraction,
            model_name=self._model_name,
            input_usd_per_mtok=self._input_usd_per_mtok,
            output_usd_per_mtok=self._output_usd_per_mtok,
            system_prompt=EXTRACTION_SYSTEM_PROMPT,
            user_text="\n".join(user_parts),
            provider="extract llm",
        )
