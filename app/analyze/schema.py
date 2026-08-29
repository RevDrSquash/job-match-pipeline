"""Structured qualification report stored on match_analyses.analysis."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.llm import PermanentLLMError

_REQUIREMENT_STATUSES = frozenset({"met", "adjacent", "missing", "unclear"})
_EXPERIENCE_KINDS = frozenset({"required", "preferred"})
_EXPERIENCE_STATUSES = frozenset({"met", "short", "unclear", "not_stated"})
_LOGISTICS_AXES = frozenset(
    {"location", "arrangement", "comp", "authorization", "timezone"}
)
_LOGISTICS_STATUSES = frozenset({"match", "mismatch", "unclear", "not_stated"})


def _canon(value: str) -> str:
    return (value or "").strip().lower().replace(" ", "_").replace("-", "_")


def _pick(value: str, allowed: frozenset[str], fallback: str) -> str:
    key = _canon(value)
    return key if key in allowed else fallback


class RequirementItem(BaseModel):
    """One JD requirement (or nice-to-have) with profile evidence."""

    model_config = ConfigDict(extra="ignore")

    requirement: str = ""
    status: str = "unclear"
    evidence: str = ""

    def normalized(self) -> RequirementItem:
        return RequirementItem(
            requirement=(self.requirement or "").strip(),
            status=_pick(self.status, _REQUIREMENT_STATUSES, "unclear"),
            evidence=(self.evidence or "").strip(),
        )


class ExperienceAsk(BaseModel):
    """One explicit YOE minimum from the JD vs the profile."""

    model_config = ConfigDict(extra="ignore")

    skill: str = ""
    required_years: float | None = None
    profile_years: float | None = None
    kind: str = "required"
    status: str = "unclear"

    def normalized(self) -> ExperienceAsk:
        return ExperienceAsk(
            skill=(self.skill or "").strip(),
            required_years=self.required_years,
            profile_years=self.profile_years,
            kind=_pick(self.kind, _EXPERIENCE_KINDS, "required"),
            status=_pick(self.status, _EXPERIENCE_STATUSES, "unclear"),
        )


class ExperienceAlignment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    overall: str = ""
    items: list[ExperienceAsk] = Field(default_factory=list)

    def normalized(self) -> ExperienceAlignment:
        return ExperienceAlignment(
            overall=(self.overall or "").strip(),
            items=[item.normalized() for item in self.items],
        )


class LogisticsItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    axis: str = "location"
    jd: str = ""
    profile: str = ""
    status: str = "unclear"

    def normalized(self) -> LogisticsItem:
        return LogisticsItem(
            axis=_pick(self.axis, _LOGISTICS_AXES, "location"),
            jd=(self.jd or "").strip(),
            profile=(self.profile or "").strip(),
            status=_pick(self.status, _LOGISTICS_STATUSES, "unclear"),
        )


class MatchAnalysisReport(BaseModel):
    """Paid per-match qualification report (docs/OPEN_ISSUES.md §16)."""

    model_config = ConfigDict(extra="ignore")

    verdict: str = ""
    requirements: list[RequirementItem] = Field(default_factory=list)
    nice_to_haves: list[RequirementItem] = Field(default_factory=list)
    experience_alignment: ExperienceAlignment = Field(
        default_factory=ExperienceAlignment
    )
    logistics: list[LogisticsItem] = Field(default_factory=list)
    gaps_to_address: list[str] = Field(default_factory=list)
    emphasize: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)

    def normalized(self) -> MatchAnalysisReport:
        verdict = (self.verdict or "").strip()
        if not verdict:
            raise PermanentLLMError("analysis llm empty verdict")
        return MatchAnalysisReport(
            verdict=verdict,
            requirements=[item.normalized() for item in self.requirements if item],
            nice_to_haves=[item.normalized() for item in self.nice_to_haves if item],
            experience_alignment=self.experience_alignment.normalized(),
            logistics=[item.normalized() for item in self.logistics],
            gaps_to_address=_clean_strings(self.gaps_to_address),
            emphasize=_clean_strings(self.emphasize),
            red_flags=_clean_strings(self.red_flags),
        )

    def to_stored(self) -> dict[str, Any]:
        return self.model_dump()


def _clean_strings(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        text = str(value).strip()
        if text:
            out.append(text)
    return out
