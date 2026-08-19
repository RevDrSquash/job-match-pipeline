"""ORM models for the pipeline data model (see docs/TASKS_AND_HANDLERS.md)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

EMBEDDING_DIM = 768


class Skill(Base):
    """Canonical skill taxonomy entry (ESCO for the PoC; O*NET-swappable).

    Ids are opaque strings chosen by the loader (ESCO concept URIs today).
    Embeddings support span-level similarity fallback in the linker.
    """

    __tablename__ = "skills"
    __table_args__ = (Index("ix_skills_canonical_label", "canonical_label"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    canonical_label: Mapped[str] = mapped_column(Text, nullable=False)
    alt_labels: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}"
    )
    description: Mapped[str | None] = mapped_column(Text)
    embedding = mapped_column(Vector(EMBEDDING_DIM))
    # Which model produced ``embedding`` (e.g. gemini-embedding-001). Null when
    # the row has no vector or it came from the offline hashing stand-in.
    embedding_model: Mapped[str | None] = mapped_column(Text)


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    ats_provider: Mapped[str | None] = mapped_column(Text)
    board_token: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(Text)
    discovered_via: Mapped[str | None] = mapped_column(Text)

    jobs: Mapped[list[Job]] = relationship(back_populates="company")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("url_hash", name="uq_jobs_url_hash"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    url_hash: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)
    ats_provider: Mapped[str | None] = mapped_column(Text)
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="SET NULL")
    )
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    title: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    work_arrangement: Mapped[str | None] = mapped_column(Text)
    department: Mapped[str | None] = mapped_column(Text)
    employment_type: Mapped[str | None] = mapped_column(Text)
    comp_min: Mapped[int | None] = mapped_column(Integer)
    comp_max: Mapped[int | None] = mapped_column(Integer)
    raw_jd: Mapped[str | None] = mapped_column(Text)
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    seniority: Mapped[str | None] = mapped_column(Text)
    hard_requirements: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    nice_to_haves: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    skill_ids: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    synthesized_doc: Mapped[str | None] = mapped_column(Text)
    embedding = mapped_column(Vector(EMBEDDING_DIM))

    company: Mapped[Company | None] = relationship(back_populates="jobs")


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tier: Mapped[str | None] = mapped_column(Text)
    quota_remaining: Mapped[int | None] = mapped_column(Integer)
    quota_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    profile: Mapped[UserProfile | None] = relationship(back_populates="user", uselist=False)
    filters: Mapped[UserFilter | None] = relationship(back_populates="user", uselist=False)


class UserProfile(Base):
    __tablename__ = "user_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    # work_history entries carry per-entry provenance: {"source": "parsed"|"user_asserted", ...}
    work_history: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    skill_ids: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    synthesized_doc: Mapped[str | None] = mapped_column(Text)
    embedding = mapped_column(Vector(EMBEDDING_DIM))
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    rematch_needed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    user: Mapped[User] = relationship(back_populates="profile")


class UserFilter(Base):
    __tablename__ = "user_filters"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    title_families: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    locations: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    comp_floor: Mapped[int | None] = mapped_column(Integer)
    seniority_band: Mapped[str | None] = mapped_column(Text)
    work_arrangement: Mapped[list[str] | None] = mapped_column(ARRAY(Text))

    user: Mapped[User] = relationship(back_populates="filters")


class Match(Base):
    __tablename__ = "matches"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    cycle_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rerank_score: Mapped[float | None] = mapped_column(Float)
    qualification_label: Mapped[str | None] = mapped_column(Text)
    screen_reason: Mapped[str | None] = mapped_column(Text)
    matched_skills: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    adjacent_skills: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    missing_skills: Mapped[list[str] | None] = mapped_column(ARRAY(Text))

    generations: Mapped[list[Generation]] = relationship(back_populates="match")


class Generation(Base):
    __tablename__ = "generations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    match_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("matches.id", ondelete="CASCADE"), nullable=False
    )
    resume_doc: Mapped[str | None] = mapped_column(Text)
    claim_source_map: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    verify_status: Mapped[str | None] = mapped_column(Text)
    verify_failures: Mapped[list[str] | None] = mapped_column(ARRAY(Text))

    match: Mapped[Match] = relationship(back_populates="generations")


class PipelineEvent(Base):
    """Training-set event log. user_id is intentionally not FK-constrained so linkage
    can be stripped (SET NULL) on anonymization without deleting rows."""

    __tablename__ = "pipeline_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="SET NULL")
    )
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    score: Mapped[float | None] = mapped_column(Float)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Token/cost/latency and cycle counters. Never store resume or JD text.
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
