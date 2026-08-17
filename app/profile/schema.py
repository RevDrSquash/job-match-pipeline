"""Structured profile parse types. Span IDs are assigned after parse."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class WorkBullet(BaseModel):
    model_config = ConfigDict(extra="ignore")

    span_id: str
    text: str


class WorkHistoryEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    employer: str
    title: str
    start_date: str | None = None
    end_date: str | None = None
    is_current: bool = False
    location: str | None = None
    source: Literal["parsed", "user_asserted"] = "parsed"
    kind: Literal["role"] = "role"
    bullets: list[WorkBullet] = Field(default_factory=list)

    def to_stored(self) -> dict[str, Any]:
        return self.model_dump()


class ParsedResume(BaseModel):
    model_config = ConfigDict(extra="ignore")

    work_history: list[WorkHistoryEntry] = Field(default_factory=list)
    skill_spans: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    seniority: str | None = None
    title_families: list[str] = Field(default_factory=list)
    work_arrangement: list[str] = Field(default_factory=list)
    comp_floor: int | None = None
    summary: str | None = None


class LlmParsePayload(BaseModel):
    """Shape we ask the model to emit. Span IDs are *not* trusted from the model."""

    model_config = ConfigDict(extra="ignore")

    class Role(BaseModel):
        model_config = ConfigDict(extra="ignore")

        employer: str
        title: str
        start_date: str | None = None
        end_date: str | None = None
        location: str | None = None
        bullets: list[str] = Field(default_factory=list)

    work_history: list[Role] = Field(default_factory=list)
    skill_spans: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    seniority: str | None = None
    title_families: list[str] = Field(default_factory=list)
    work_arrangement: list[str] = Field(default_factory=list)
    comp_floor: int | None = None
    summary: str | None = None
