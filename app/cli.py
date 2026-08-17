"""PoC CLI. Profile ingestion is a CLI until the UI issue lands (docs/UI.md §2).

Usage:
  jobmatch profile ingest <resume-file>
  jobmatch profile show [--user-id UUID]
  jobmatch profile edit <user-id> [corrections...]
  jobmatch match run --mode incremental|dirty
  jobmatch evals run [--suite NAME]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from pathlib import Path

import httpx

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
        if args.command == "match":
            return _dispatch_match(args)
        if args.command == "evals":
            return _dispatch_evals(args)
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

    match = sub.add_parser("match", help="Trigger a match-batch cycle")
    match_sub = match.add_subparsers(dest="match_command", required=True)
    run = match_sub.add_parser(
        "run",
        help="POST /handlers/match-batch (Cloud Scheduler stand-in for the PoC)",
    )
    run.add_argument(
        "--mode",
        choices=("incremental", "dirty"),
        required=True,
        help="incremental: jobs since last cycle × all users; dirty: full corpus × rematch_needed",
    )
    run.add_argument(
        "--since",
        default=None,
        help="ISO-8601 watermark override for incremental mode",
    )
    run.add_argument(
        "--dirty-cap",
        type=int,
        default=None,
        dest="dirty_profile_cap",
        help="Max dirty profiles to process this run",
    )
    run.add_argument(
        "--base-url",
        default=None,
        help="Handler base URL (default: LOCAL_QUEUE_BASE_URL)",
    )
    run.add_argument(
        "--cycle-at",
        default=None,
        help="ISO-8601 cycle timestamp (tests / idempotent redelivery)",
    )

    evals = sub.add_parser("evals", help="Run the four non-negotiable eval suites")
    evals_sub = evals.add_subparsers(dest="evals_command", required=True)
    evals_run = evals_sub.add_parser(
        "run",
        help="Execute extraction, skill linking, retrieval, and/or fabrication",
    )
    evals_run.add_argument(
        "--suite",
        action="append",
        dest="suites",
        default=None,
        metavar="NAME",
        help=(
            "Suite to run (repeatable): extraction, skill_linking, retrieval, "
            "fabrication. Default: all four"
        ),
    )
    evals_run.add_argument(
        "--set-version",
        default=None,
        help="Eval set version directory under evals/sets (default: latest)",
    )
    evals_run.add_argument(
        "--sets-dir",
        type=Path,
        default=None,
        help="Override evals/sets root",
    )
    evals_run.add_argument(
        "--results-dir",
        type=Path,
        default=None,
        help="Directory for timestamped JSON + summary (default: evals/results)",
    )
    evals_run.add_argument(
        "--offline",
        action="store_true",
        help="Force heuristic extraction and the grounded eval generator (no LLM)",
    )
    evals_run.add_argument(
        "--plant-fabrication",
        action="store_true",
        help="Inject known-bad claims; fabrication suite must exit non-zero",
    )
    evals_run.add_argument(
        "--require-gemini-embeddings",
        action="store_true",
        help="Refuse the retrieval suite unless EMBEDDING_PROVIDER=gemini",
    )
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


def _dispatch_match(args: argparse.Namespace) -> int:
    if args.match_command != "run":
        return 2
    settings = get_settings()
    base = (args.base_url or settings.local_queue_base_url).rstrip("/")
    url = f"{base}/handlers/match-batch"
    payload: dict[str, object] = {"mode": args.mode}
    if args.since:
        payload["since"] = args.since
    if args.dirty_profile_cap is not None:
        payload["dirty_profile_cap"] = args.dirty_profile_cap
    if args.cycle_at:
        payload["cycle_at"] = args.cycle_at
    try:
        response = httpx.post(url, json=payload, timeout=120.0)
    except httpx.HTTPError as exc:
        print(f"error: match-batch request failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(response.text)
    if response.status_code >= 400:
        print(f"error: handler returned {response.status_code}", file=sys.stderr)
        return 1
    return 0


def _dispatch_evals(args: argparse.Namespace) -> int:
    if args.evals_command != "run":
        return 2
    from app.evals.runner import format_run, run_evals

    try:
        result = run_evals(
            suites=args.suites,
            sets_dir=args.sets_dir,
            results_dir=args.results_dir,
            set_version=args.set_version,
            offline=args.offline,
            plant_fabrication=args.plant_fabrication,
            require_gemini_embeddings=args.require_gemini_embeddings,
        )
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(format_run(result), end="")
    return result.exit_code


def _csv(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]
