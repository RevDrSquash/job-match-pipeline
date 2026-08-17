"""Persist and update user profiles. No resume text in logs or events."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import PipelineEvent, User, UserFilter, UserProfile
from app.embeddings import Embedder
from app.privacy import PrivacySafeError, log_profile_access, safe_exc
from app.profile.filters import derive_default_filters
from app.profile.parse import ResumeParser
from app.profile.schema import ParsedResume, WorkHistoryEntry
from app.profile.synthesize import synthesize_profile_doc
from app.skills.linker import SkillLinker


@dataclass(frozen=True)
class ProfileBundle:
    user_id: uuid.UUID
    profile_version: int
    rematch_needed: bool
    work_history: list[dict[str, Any]]
    skill_ids: list[str]
    synthesized_doc: str | None
    embedding_dim: int | None
    filters: dict[str, Any]


@dataclass(frozen=True)
class IngestResult:
    bundle: ProfileBundle
    created_user: bool


def ingest_profile(
    session: Session,
    resume_text: str,
    *,
    input_kind: str,
    char_count: int,
    user_id: uuid.UUID | None = None,
    parser: ResumeParser,
    embedder: Embedder,
    linker: SkillLinker,
    settings: Settings | None = None,
) -> IngestResult:
    settings = settings or get_settings()
    log_profile_access(
        "ingest_start",
        input_kind=input_kind,
        char_count=char_count,
        has_user_id=user_id is not None,
    )
    parsed = parser.parse(resume_text)
    skill_ids = [hit.skill_id for hit in linker.link_spans(parsed.skill_spans)]
    if not skill_ids:
        skill_ids = [hit.skill_id for hit in linker.scan_text(resume_text)]
    synthesized = synthesize_profile_doc(parsed, skill_ids, linker)
    embedding = embedder.embed(synthesized, purpose="profile_embed")
    stored_history = [entry.to_stored() for entry in parsed.work_history]
    filters = derive_default_filters(parsed)

    user, created = _get_or_create_user(session, user_id, settings)
    profile = session.get(UserProfile, user.id)
    if profile is None:
        profile = UserProfile(
            user_id=user.id,
            work_history=stored_history,
            skill_ids=skill_ids,
            synthesized_doc=synthesized,
            embedding=embedding,
            profile_version=1,
            rematch_needed=True,
        )
        session.add(profile)
        version = 1
    else:
        profile.work_history = stored_history
        profile.skill_ids = skill_ids
        profile.synthesized_doc = synthesized
        profile.embedding = embedding
        profile.profile_version += 1
        profile.rematch_needed = True
        version = profile.profile_version

    _upsert_filters(session, user.id, filters)
    _record_event(session, user.id, "profile", "ingest")
    session.flush()
    log_profile_access(
        "ingest_ok",
        user_id=user.id,
        profile_version=version,
        skill_count=len(skill_ids),
        role_count=len(stored_history),
        created_user=created,
    )
    return IngestResult(
        bundle=_bundle(user.id, profile, session.get(UserFilter, user.id)),
        created_user=created,
    )


def show_profile(session: Session, user_id: uuid.UUID | None = None) -> ProfileBundle:
    user = _resolve_user(session, user_id)
    profile = session.get(UserProfile, user.id)
    if profile is None:
        raise PrivacySafeError("user has no profile")
    log_profile_access("show", user_id=user.id, profile_version=profile.profile_version)
    return _bundle(user.id, profile, session.get(UserFilter, user.id))


def edit_profile(
    session: Session,
    user_id: uuid.UUID,
    *,
    work_history: list[dict[str, Any]] | None = None,
    skill_ids: list[str] | None = None,
    synthesized_doc: str | None = None,
    title_families: list[str] | None = None,
    locations: list[str] | None = None,
    work_arrangement: list[str] | None = None,
    seniority_band: str | None = None,
    comp_floor: int | None = None,
    clear_comp_floor: bool = False,
    embedder: Embedder | None = None,
    linker: SkillLinker | None = None,
) -> ProfileBundle:
    """Apply a manual correction. Always bumps profile_version and rematch_needed."""
    profile = session.get(UserProfile, user_id)
    if profile is None:
        raise PrivacySafeError("user has no profile")
    filters = session.get(UserFilter, user_id)

    if work_history is not None:
        work_history = _prepare_edited_history(work_history)
        _validate_work_history(work_history)
        profile.work_history = work_history
        if synthesized_doc is None and linker is not None:
            parsed = _parsed_from_stored(work_history, skill_ids or profile.skill_ids or [])
            ids = skill_ids if skill_ids is not None else (profile.skill_ids or [])
            synthesized_doc = synthesize_profile_doc(parsed, ids, linker)
        if synthesized_doc is not None and embedder is not None:
            profile.embedding = embedder.embed(synthesized_doc, purpose="profile_embed")

    if skill_ids is not None:
        profile.skill_ids = skill_ids
    if synthesized_doc is not None:
        profile.synthesized_doc = synthesized_doc

    if filters is None:
        filters = UserFilter(user_id=user_id)
        session.add(filters)
    if title_families is not None:
        filters.title_families = title_families
    if locations is not None:
        filters.locations = locations
    if work_arrangement is not None:
        filters.work_arrangement = work_arrangement
    if seniority_band is not None:
        filters.seniority_band = seniority_band
    if clear_comp_floor:
        filters.comp_floor = None
    elif comp_floor is not None:
        filters.comp_floor = comp_floor

    profile.profile_version += 1
    profile.rematch_needed = True
    _record_event(session, user_id, "profile", "edit")
    session.flush()
    log_profile_access(
        "edit",
        user_id=user_id,
        profile_version=profile.profile_version,
        rematch_needed=True,
    )
    return _bundle(user_id, profile, filters)


def _prepare_edited_history(work_history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Manual edits default to user_asserted and keep span IDs on every bullet."""
    prepared: list[dict[str, Any]] = []
    for index, raw in enumerate(work_history):
        entry = dict(raw)
        entry.setdefault("source", "user_asserted")
        bullets_in = entry.get("bullets") or []
        bullets_out: list[dict[str, Any]] = []
        for j, bullet in enumerate(bullets_in):
            if isinstance(bullet, str):
                bullets_out.append({"span_id": f"wh:{index}:b:{j}", "text": bullet})
            else:
                item = dict(bullet)
                item.setdefault("span_id", f"wh:{index}:b:{j}")
                bullets_out.append(item)
        entry["bullets"] = bullets_out
        prepared.append(entry)
    return prepared


def _validate_work_history(work_history: list[dict[str, Any]]) -> None:
    try:
        for entry in work_history:
            WorkHistoryEntry.model_validate(entry)
    except ValueError as exc:
        raise safe_exc("invalid work_history payload", exc) from None


def _parsed_from_stored(work_history: list[dict[str, Any]], skill_ids: list[str]) -> ParsedResume:
    entries = [WorkHistoryEntry.model_validate(entry) for entry in work_history]
    return ParsedResume(work_history=entries, skill_spans=skill_ids)


def _get_or_create_user(
    session: Session,
    user_id: uuid.UUID | None,
    settings: Settings,
) -> tuple[User, bool]:
    if user_id is not None:
        user = session.get(User, user_id)
        if user is None:
            raise PrivacySafeError("user not found")
        return user, False
    now = datetime.now(tz=UTC)
    user = User(
        tier=settings.default_user_tier,
        quota_remaining=settings.default_quota_remaining,
        quota_reset_at=_next_month_start(now),
    )
    session.add(user)
    session.flush()
    return user, True


def _next_month_start(now: datetime) -> datetime:
    year, month = now.year, now.month + 1
    if month == 13:
        year, month = year + 1, 1
    return datetime(year, month, 1, tzinfo=UTC)


def _upsert_filters(session: Session, user_id: uuid.UUID, values: dict[str, Any]) -> None:
    row = session.get(UserFilter, user_id)
    if row is None:
        row = UserFilter(user_id=user_id)
        session.add(row)
    row.title_families = values.get("title_families")
    row.locations = values.get("locations")
    row.comp_floor = values.get("comp_floor")
    row.seniority_band = values.get("seniority_band")
    row.work_arrangement = values.get("work_arrangement")


def _resolve_user(session: Session, user_id: uuid.UUID | None) -> User:
    if user_id is not None:
        user = session.get(User, user_id)
        if user is None:
            raise PrivacySafeError("user not found")
        return user
    users = session.scalars(select(User)).all()
    if not users:
        raise PrivacySafeError("no users in database")
    if len(users) > 1:
        raise PrivacySafeError("multiple users; pass --user-id")
    return users[0]


def _record_event(session: Session, user_id: uuid.UUID, stage: str, action: str) -> None:
    session.add(PipelineEvent(user_id=user_id, job_id=None, stage=stage, score=None, action=action))


def _bundle(user_id: uuid.UUID, profile: UserProfile, filters: UserFilter | None) -> ProfileBundle:
    embedding = profile.embedding
    dim = len(embedding) if embedding is not None else None
    return ProfileBundle(
        user_id=user_id,
        profile_version=profile.profile_version,
        rematch_needed=profile.rematch_needed,
        work_history=list(profile.work_history or []),
        skill_ids=list(profile.skill_ids or []),
        synthesized_doc=profile.synthesized_doc,
        embedding_dim=dim,
        filters={
            "title_families": filters.title_families if filters else None,
            "locations": filters.locations if filters else None,
            "comp_floor": filters.comp_floor if filters else None,
            "seniority_band": filters.seniority_band if filters else None,
            "work_arrangement": filters.work_arrangement if filters else None,
        },
    )


def bundle_to_dict(bundle: ProfileBundle) -> dict[str, Any]:
    return {
        "user_id": str(bundle.user_id),
        "profile_version": bundle.profile_version,
        "rematch_needed": bundle.rematch_needed,
        "work_history": bundle.work_history,
        "skill_ids": bundle.skill_ids,
        "synthesized_doc": bundle.synthesized_doc,
        "embedding_dim": bundle.embedding_dim,
        "filters": bundle.filters,
    }
