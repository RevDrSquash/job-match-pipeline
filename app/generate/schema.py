"""Claim → source-span contract shared by generate-resume and verify-resume.

Span IDs come from profile ingest (`wh:{role}` for role-level facts,
`wh:{role}:b:{n}` for bullets). The generator must not invent span IDs.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ClaimKind = Literal[
    "employer",
    "title",
    "date_range",
    "number",
    "skill",
    "accomplishment",
]


class Claim(BaseModel):
    """One resume claim and the profile spans that support it."""

    model_config = ConfigDict(extra="ignore")

    text: str
    span_ids: list[str] = Field(default_factory=list)
    kind: ClaimKind = "accomplishment"
    canonical_skill_id: str | None = None


class ClaimSourceMap(BaseModel):
    """Structured mapping stored on ``generations.claim_source_map``."""

    model_config = ConfigDict(extra="ignore")

    attempt: int = 1
    claims: list[Claim] = Field(default_factory=list)
    claimed_skill_ids: list[str] = Field(default_factory=list)
    employers: list[str] = Field(default_factory=list)
    titles: list[str] = Field(default_factory=list)
    date_ranges: list[str] = Field(default_factory=list)

    def to_stored(self) -> dict[str, Any]:
        return self.model_dump()


class GeneratedResume(BaseModel):
    """Structured generator output. Extra keys from the model are ignored."""

    model_config = ConfigDict(extra="ignore")

    resume_doc: str
    claims: list[Claim] = Field(default_factory=list)
    claimed_skill_ids: list[str] = Field(default_factory=list)
    employers: list[str] = Field(default_factory=list)
    titles: list[str] = Field(default_factory=list)
    date_ranges: list[str] = Field(default_factory=list)

    def to_claim_map(self, *, attempt: int) -> ClaimSourceMap:
        return ClaimSourceMap(
            attempt=attempt,
            claims=list(self.claims),
            claimed_skill_ids=list(self.claimed_skill_ids),
            employers=list(self.employers),
            titles=list(self.titles),
            date_ranges=list(self.date_ranges),
        )
