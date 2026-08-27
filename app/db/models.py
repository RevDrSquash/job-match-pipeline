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
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

EMBEDDING_DIM = 768


class Concept(Base):
    """Source-independent skill-graph concept owned by this application."""

    __tablename__ = "concept"
    __table_args__ = (
        Index("ix_concept_normalized_name", "normalized_name"),
        Index(
            "ix_concept_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_where=text("embedding IS NOT NULL"),
        ),
    )

    # Importers assign deterministic UUIDv5 IDs; source identifiers never become
    # canonical IDs.
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False)
    concept_type: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="active"
    )
    embedding = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    aliases: Mapped[list[ConceptAlias]] = relationship(
        back_populates="concept", cascade="all, delete-orphan", passive_deletes=True
    )
    outgoing_edges: Mapped[list[ConceptEdge]] = relationship(
        back_populates="subject",
        cascade="all, delete-orphan",
        foreign_keys="ConceptEdge.subject_id",
        passive_deletes=True,
    )
    incoming_edges: Mapped[list[ConceptEdge]] = relationship(
        back_populates="object",
        cascade="all, delete-orphan",
        foreign_keys="ConceptEdge.object_id",
        passive_deletes=True,
    )
    source_mappings: Mapped[list[SourceMapping]] = relationship(
        back_populates="concept", cascade="all, delete-orphan", passive_deletes=True
    )


class ConceptAlias(Base):
    """A normalized surface form that can resolve to a canonical concept."""

    __tablename__ = "concept_alias"
    __table_args__ = (
        Index("ix_concept_alias_normalized_alias", "normalized_alias"),
        Index(
            "ix_concept_alias_normalized_alias_trgm",
            "normalized_alias",
            postgresql_using="gin",
            postgresql_ops={"normalized_alias": "gin_trgm_ops"},
        ),
    )

    concept_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("concept.id", ondelete="CASCADE"),
        primary_key=True,
    )
    normalized_alias: Mapped[str] = mapped_column(Text, primary_key=True)
    alias: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="en"
    )
    alias_type: Mapped[str] = mapped_column(String(32), nullable=False)
    provenance: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    concept: Mapped[Concept] = relationship(back_populates="aliases")


class ConceptEdge(Base):
    """An application-owned interpretation of a canonical graph relationship."""

    __tablename__ = "concept_edge"
    __table_args__ = (
        Index("ix_concept_edge_object_predicate", "object_id", "predicate"),
    )

    subject_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("concept.id", ondelete="CASCADE"),
        primary_key=True,
    )
    predicate: Mapped[str] = mapped_column(String(64), primary_key=True)
    object_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("concept.id", ondelete="CASCADE"),
        primary_key=True,
    )
    confidence: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="1.0"
    )
    provenance: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    subject: Mapped[Concept] = relationship(
        back_populates="outgoing_edges", foreign_keys=[subject_id]
    )
    object: Mapped[Concept] = relationship(
        back_populates="incoming_edges", foreign_keys=[object_id]
    )


class SourceConcept(Base):
    """Lossless representation of one external taxonomy concept or example."""

    __tablename__ = "source_concept"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "source_version",
            "external_id",
            name="uq_source_concept_source_version_external_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_version: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )

    mappings: Mapped[list[SourceMapping]] = relationship(
        back_populates="source_concept",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    outgoing_edges: Mapped[list[SourceEdge]] = relationship(
        back_populates="subject",
        cascade="all, delete-orphan",
        foreign_keys="SourceEdge.subject_id",
        passive_deletes=True,
    )
    incoming_edges: Mapped[list[SourceEdge]] = relationship(
        back_populates="object",
        cascade="all, delete-orphan",
        foreign_keys="SourceEdge.object_id",
        passive_deletes=True,
    )


class SourceMapping(Base):
    """Provenance-bearing mapping from a source concept to a canonical concept."""

    __tablename__ = "source_mapping"
    __table_args__ = (Index("ix_source_mapping_concept_id", "concept_id"),)

    source_concept_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("source_concept.id", ondelete="CASCADE"),
        primary_key=True,
    )
    concept_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("concept.id", ondelete="CASCADE"),
        primary_key=True,
    )
    mapping_type: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="1.0"
    )
    mapping_method: Mapped[str] = mapped_column(String(64), nullable=False)

    source_concept: Mapped[SourceConcept] = relationship(back_populates="mappings")
    concept: Mapped[Concept] = relationship(back_populates="source_mappings")


class SourceEdge(Base):
    """Relationship asserted by a source, without canonical promotion."""

    __tablename__ = "source_edge"
    __table_args__ = (
        Index("ix_source_edge_object_predicate", "object_id", "predicate"),
    )

    subject_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("source_concept.id", ondelete="CASCADE"),
        primary_key=True,
    )
    predicate: Mapped[str] = mapped_column(String(64), primary_key=True)
    object_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("source_concept.id", ondelete="CASCADE"),
        primary_key=True,
    )
    confidence: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="1.0"
    )
    raw_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )

    subject: Mapped[SourceConcept] = relationship(
        back_populates="outgoing_edges", foreign_keys=[subject_id]
    )
    object: Mapped[SourceConcept] = relationship(
        back_populates="incoming_edges", foreign_keys=[object_id]
    )


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
    raw_jd_html: Mapped[str | None] = mapped_column(Text)
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
