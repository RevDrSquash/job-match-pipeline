"""PoC CLI. Profile ingestion is a CLI until the UI issue lands (docs/UI.md §2).

Usage:
  jobmatch profile ingest <resume-file>
  jobmatch profile show [--user-id UUID]
  jobmatch profile edit <user-id> [corrections...]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from pathlib import Path

from app.config import Settings, get_settings
from app.db.session import db_session
from app.privacy import PrivacySafeError
from app.profile.deps import build_profile_deps
from app.profile.service import (
    bundle_to_dict,
    edit_profile,
    ingest_profile,
    show_profile,
)
from app.profile.text import read_resume_file


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "profile":
            return _dispatch_profile(args)
        parser.print_help()
        return 2
    except PrivacySafeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobmatch", description="Job Match Pipeline CLI")
    sub = parser.add_subparsers(dest="command")

    profile = sub.add_parser("profile", help="Ingest, show, and edit a user profile")
    profile_sub = profile.add_subparsers(dest="profile_command", required=True)

    ingest = profile_sub.add_parser("ingest", help="Parse a resume into a structured profile")
    ingest.add_argument("resume_file", type=Path, help="PDF, markdown, or text resume")
    ingest.add_argument("--user-id", type=uuid.UUID, default=None, help="Update this user")
    ingest.add_argument(
        "--fallback-parser",
        action="store_true",
        help="Use the offline structured parser instead of the LLM",
    )
    ingest.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Print the result as JSON",
    )

    show = profile_sub.add_parser("show", help="Dump the structured profile for review")
    show.add_argument("--user-id", type=uuid.UUID, default=None)

    edit = profile_sub.add_parser(
        "edit",
        help="Apply a correction; bumps profile_version and sets rematch_needed",
    )
    edit.add_argument("user_id", type=uuid.UUID)
    edit.add_argument(
        "--work-history-json",
        type=Path,
        default=None,
        help="Replacement work_history JSON array (each entry needs source + span IDs)",
    )
    edit.add_argument("--skill-ids", default=None, help="Comma-separated canonical skill IDs")
    edit.add_argument("--comp-floor", type=int, default=None)
    edit.add_argument("--clear-comp-floor", action="store_true")
    edit.add_argument("--seniority-band", default=None)
    edit.add_argument("--add-location", action="append", default=None)
    edit.add_argument("--add-title-family", action="append", default=None)
    edit.add_argument("--work-arrangement", default=None, help="Comma-separated values")
    return parser


def _dispatch_profile(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.profile_command == "ingest":
        return _cmd_ingest(args, settings)
    if args.profile_command == "show":
        return _cmd_show(args)
    if args.profile_command == "edit":
        return _cmd_edit(args, settings)
    return 2


def _cmd_ingest(args: argparse.Namespace, settings: Settings) -> int:
    path: Path = args.resume_file
    if not path.is_file():
        print("error: resume file not found", file=sys.stderr)
        return 1
    text, kind = read_resume_file(path)
    with db_session() as session:
        deps = build_profile_deps(settings, session, allow_fallback=args.fallback_parser)
        result = ingest_profile(
            session,
            text,
            input_kind=kind,
            char_count=len(text),
            user_id=args.user_id,
            parser=deps.parser,
            embedder=deps.embedder,
            linker=deps.linker,
            settings=settings,
        )
        payload = bundle_to_dict(result.bundle)
    if args.as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"user_id={payload['user_id']}")
        print(f"created_user={result.created_user}")
        print(f"profile_version={payload['profile_version']}")
        print(f"rematch_needed={payload['rematch_needed']}")
        print(f"roles={len(payload['work_history'])}")
        print(f"skills={len(payload['skill_ids'])}")
        print(f"embedding_dim={payload['embedding_dim']}")
        print("review with: jobmatch profile show --user-id", payload["user_id"])
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    with db_session() as session:
        bundle = show_profile(session, args.user_id)
    print(json.dumps(bundle_to_dict(bundle), indent=2))
    return 0


def _cmd_edit(args: argparse.Namespace, settings: Settings) -> int:
    work_history = None
    if args.work_history_json is not None:
        work_history = json.loads(args.work_history_json.read_text(encoding="utf-8"))
        if not isinstance(work_history, list):
            raise PrivacySafeError("work-history-json must be a JSON array")
    skill_ids = _csv(args.skill_ids)
    arrangements = _csv(args.work_arrangement)
    with db_session() as session:
        deps = build_profile_deps(settings, session, allow_fallback=True)
        current = show_profile(session, args.user_id)
        locations = current.filters.get("locations")
        if args.add_location:
            locations = list(locations or [])
            for loc in args.add_location:
                if loc not in locations:
                    locations.append(loc)
        title_families = current.filters.get("title_families")
        if args.add_title_family:
            title_families = list(title_families or [])
            for family in args.add_title_family:
                if family not in title_families:
                    title_families.append(family)
        bundle = edit_profile(
            session,
            args.user_id,
            work_history=work_history,
            skill_ids=skill_ids,
            title_families=title_families if args.add_title_family else None,
            locations=locations if args.add_location else None,
            work_arrangement=arrangements,
            seniority_band=args.seniority_band,
            comp_floor=args.comp_floor,
            clear_comp_floor=args.clear_comp_floor,
            embedder=deps.embedder,
            linker=deps.linker,
        )
        print(json.dumps(bundle_to_dict(bundle), indent=2))
    return 0


def _csv(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]
