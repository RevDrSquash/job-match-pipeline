"""FastAPI router for user-facing /api/* endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from app.api.service import (
    MatchView,
    admin_metrics,
    get_generation,
    get_profile,
    list_matches,
    list_users,
    patch_profile,
    record_match_event,
    trigger_generate,
)
from app.db.session import db_session
from app.privacy import PrivacySafeError
from app.profile.deps import build_profile_deps


class ProfilePatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: uuid.UUID
    work_history: list[dict[str, Any]] | None = None
    skill_ids: list[str] | None = None
    synthesized_doc: str | None = None
    title_families: list[str] | None = None
    locations: list[str] | None = None
    work_arrangement: list[str] | None = None
    seniority_band: str | None = None
    comp_floor: int | None = None
    clear_comp_floor: bool = False


class MatchEventBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: uuid.UUID
    action: Literal["viewed", "skipped", "generate_requested", "marked_applied", "outcome"]
    reason_code: str | None = None
    reason_text: str | None = None
    applied_at: datetime | None = None
    outcome: Literal["interview", "rejected"] | None = None


def create_api_router() -> APIRouter:
    router = APIRouter(prefix="/api", tags=["api"])

    @router.get("/users")
    def api_list_users() -> dict[str, list[dict[str, Any]]]:
        with db_session() as session:
            return {"users": list_users(session)}

    @router.get("/profile")
    def api_get_profile(user_id: Annotated[uuid.UUID, Query()]) -> dict[str, Any]:
        try:
            with db_session() as session:
                return get_profile(session, user_id)
        except PrivacySafeError as exc:
            raise _client_error(exc) from None

    @router.patch("/profile")
    def api_patch_profile(body: ProfilePatchBody, request: Request) -> dict[str, Any]:
        settings = request.app.state.settings
        try:
            with db_session() as session:
                deps = build_profile_deps(settings, session, allow_fallback=True)
                return patch_profile(
                    session,
                    body.user_id,
                    deps=deps,
                    work_history=body.work_history,
                    skill_ids=body.skill_ids,
                    synthesized_doc=body.synthesized_doc,
                    title_families=body.title_families,
                    locations=body.locations,
                    work_arrangement=body.work_arrangement,
                    seniority_band=body.seniority_band,
                    comp_floor=body.comp_floor,
                    clear_comp_floor=body.clear_comp_floor,
                )
        except PrivacySafeError as exc:
            raise _client_error(exc) from None

    @router.get("/matches")
    def api_list_matches(
        user_id: Annotated[uuid.UUID, Query()],
        view: Annotated[MatchView, Query()] = "matched",
    ) -> dict[str, list[dict[str, Any]]]:
        with db_session() as session:
            return {"matches": list_matches(session, user_id, view=view)}

    @router.get("/generations/{generation_id}")
    def api_get_generation(generation_id: uuid.UUID) -> dict[str, Any]:
        try:
            with db_session() as session:
                return get_generation(session, generation_id)
        except PrivacySafeError as exc:
            raise _client_error(exc) from None

    @router.get("/admin/metrics")
    def api_admin_metrics() -> dict[str, Any]:
        with db_session() as session:
            return admin_metrics(session)

    @router.post("/matches/{match_id}/events")
    def api_match_event(match_id: uuid.UUID, body: MatchEventBody) -> dict[str, Any]:
        try:
            with db_session() as session:
                return record_match_event(
                    session,
                    match_id,
                    user_id=body.user_id,
                    action=body.action,
                    reason_code=body.reason_code,
                    reason_text=body.reason_text,
                    applied_at=body.applied_at,
                    outcome=body.outcome,
                )
        except PrivacySafeError as exc:
            raise _client_error(exc) from None

    @router.post("/matches/{match_id}/generate")
    def api_trigger_generate(match_id: uuid.UUID, request: Request) -> dict[str, Any]:
        queue = request.app.state.queue
        try:
            with db_session() as session:
                return trigger_generate(session, match_id, queue)
        except PrivacySafeError as exc:
            raise _client_error(exc) from None

    return router


def _client_error(exc: PrivacySafeError) -> HTTPException:
    message = str(exc)
    if "not found" in message or "no profile" in message or "no users" in message:
        return HTTPException(status_code=404, detail=message)
    return HTTPException(status_code=400, detail=message)
