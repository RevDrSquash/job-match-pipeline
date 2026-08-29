"""Qualification-report LLM: JD + profile → structured MatchAnalysisReport.

Profile-derived output is personal information. Never log prompt or completion
text, and never put model output into exception args (docs/PRIVACY_AND_COMPLIANCE.md).
Prompt rules come from the resume-toolkit extract-job-signals / review-resume skills.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol, runtime_checkable

from app.analyze.schema import MatchAnalysisReport
from app.config import Settings, get_settings
from app.llm import (
    DEFAULT_GEMINI_API_BASE,
    LLMUsage,
    PermanentLLMError,
    RetryableLLMError,
    build_gemini_chat,
    structured_call,
)

logger = logging.getLogger(__name__)

ANALYSIS_SYSTEM_PROMPT = """\
You write a per-match qualification report for a job applicant. The report \
sits beside a cheap screening label: it is the full fit judgment plus a \
logistics checklist, not a resume rewrite and not a yes/no gate.

Return only JSON matching the schema.

Sections:
- verdict: 2–4 sentences on qualification fit only — skills, experience, \
domain, seniority. Logistics (location, relocation, work authorization, \
onsite/remote/hybrid, timezone, compensation, start date) must not appear \
in the verdict and must not change it.
- requirements: every explicit must-have / qualification from the JD as its \
own item. Use the JD's phrasing. status is met (profile evidence covers it), \
adjacent (related experience, not the same skill), missing (JD wants it, \
profile lacks it), or unclear (cannot tell from the profile). evidence is \
a short grounded citation from the profile, or empty when missing/unclear.
- nice_to_haves: preferred / bonus items, same shape, brief. Cap at the \
highest-weight items if the JD lists many.
- experience_alignment: itemize every explicit years-of-experience minimum \
in the JD as its own row — the overall career minimum and each skill- or \
domain-specific minimum (e.g. "5+ years Python"). Use exact JD skill \
phrasing. Tag kind required or preferred. Compare to dated work history; \
a skill listed only as a keyword with no dated role counts as zero years. \
overall is one sentence summarizing YOE fit.
- logistics: a checklist of five axes — location, arrangement, comp, \
authorization, timezone — each with jd, profile, and status \
(match|mismatch|unclear|not_stated). This is separate from the verdict.
- gaps_to_address: real candidate gaps that cannot be fixed by rewriting a \
resume (skills or tenure the profile does not have). Do not list \
presentation / keyword-placement issues here.
- emphasize: the strongest grounded evidence to lead with if the candidate \
applies. Only facts present in the profile.
- red_flags: knockout risks and JD oddities (visa, clearance, unrealistic \
scope, contradictory requirements, likely yes/no form filters).

Rules:
- Do not invent skills, employers, numbers, years, or experience.
- The MISSING skill bucket is an explicit do-not-claim list. Never treat a \
missing skill as met. Adjacent is not grounding for the JD's exact term.
- Use JD phrasing for requirement and YOE skill strings.
- Distinguish candidate gaps (gaps_to_address) from presentation gaps \
(omit those; this report is not a resume critique).
- Do not editorialize about whether to apply beyond the structured fields.
- Never include contact details or identifiers beyond the inputs.
"""


@runtime_checkable
class AnalysisLLM(Protocol):
    def analyze(self, *, user_text: str) -> tuple[MatchAnalysisReport, LLMUsage]:
        """Structured analysis. Raises RetryableLLMError on transient failures."""


def log_analysis_usage(usage: LLMUsage, *, match_id: str | None = None) -> None:
    """Log billed token counts and estimated cost. Never log profile or JD text."""
    logger.info(
        "analyze-match llm model=%s prompt_tokens=%s completion_tokens=%s "
        "cost_usd=%.6f match_id=%s",
        usage.model,
        usage.prompt_tokens,
        usage.completion_tokens,
        usage.cost_usd,
        match_id or "-",
    )


def build_analysis_user_text(
    *,
    job_title: str | None,
    job_doc: str,
    job_location: str | None,
    job_arrangement: str | None,
    job_comp: str,
    work_history_block: str,
    profile_doc: str,
    filters_text: str,
    buckets_text: str,
) -> str:
    return "\n".join(
        [
            f"Job title: {job_title or 'unknown'}",
            f"Job location: {job_location or 'unspecified'}",
            f"Job work arrangement: {job_arrangement or 'unspecified'}",
            f"Job compensation: {job_comp}",
            "",
            "Job description:",
            job_doc.strip() or "(no job description)",
            "",
            "Candidate work history (only allowed source of employers, titles, "
            "dates, numbers, and accomplishments):",
            work_history_block.strip() or "(no work history)",
            "",
            "Condensed profile:",
            profile_doc.strip() or "(none)",
            "",
            "Candidate logistics preferences:",
            filters_text.strip() or "(none stated)",
            "",
            "Skill buckets from matching (canonical):",
            buckets_text.strip(),
        ]
    )


def _api_key(settings: Settings) -> str:
    return (
        settings.llm_api_key
        or os.environ.get("GEMINI_API_KEY", "")
        or os.environ.get("GOOGLE_API_KEY", "")
    )


class GeminiAnalysisLLM:
    """Gemini structured qualification report. Model name comes from Settings."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        api_base: str = DEFAULT_GEMINI_API_BASE,
        input_usd_per_mtok: float = 1.50,
        output_usd_per_mtok: float = 9.00,
        timeout: float = 90.0,
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

    def analyze(self, *, user_text: str) -> tuple[MatchAnalysisReport, LLMUsage]:
        try:
            report, usage = structured_call(
                self._chat,
                MatchAnalysisReport,
                model_name=self._model_name,
                input_usd_per_mtok=self._input_usd_per_mtok,
                output_usd_per_mtok=self._output_usd_per_mtok,
                system_prompt=ANALYSIS_SYSTEM_PROMPT,
                user_text=user_text,
                provider="analysis llm",
            )
        except PermanentLLMError:
            raise PermanentLLMError("analysis llm permanent failure") from None
        except RetryableLLMError:
            raise RetryableLLMError("analysis llm retryable failure") from None
        try:
            return report.normalized(), usage
        except PermanentLLMError:
            raise PermanentLLMError("analysis llm invalid structured output") from None
        except Exception:
            raise PermanentLLMError("analysis llm invalid structured output") from None


def build_analysis_llm(settings: Settings | None = None) -> AnalysisLLM:
    settings = settings or get_settings()
    key = _api_key(settings)
    if not key:
        raise RetryableLLMError("llm_api_key is not configured")
    return GeminiAnalysisLLM(
        api_key=key,
        model=settings.analysis_model,
        api_base=settings.llm_api_base,
        input_usd_per_mtok=settings.analysis_input_usd_per_mtok,
        output_usd_per_mtok=settings.analysis_output_usd_per_mtok,
    )
