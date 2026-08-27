#!/usr/bin/env python3
"""Build the canonical ESCO + O*NET skill knowledge graph.

Orchestrates the three importers in ``app/skills/importers/``:

1. **ESCO** (``esco.py``): parse the pinned v1.2.1 CSV classification bundle
   from ``data/esco/`` into the source layer, found canonical concepts and
   aliases, and promote skill→skill broader relationships to ``IS_A`` edges.
2. **O*NET** (``onet.py``): download/cache the pinned 31.0 Software Skills
   file into ``data/onet/`` (one-time, low-volume fetch) and load deduplicated
   technologies plus category assertions into the source layer only.
3. **Reconcile** (``reconcile.py``): conservatively map O*NET technologies
   onto existing canonical concepts (normalized label → alias → trgm →
   embedding) or create new canonical ``technology`` concepts (Docker,
   Kubernetes, AWS, …). Never forces a match.

Every step is an idempotent upsert keyed on pinned ``source_version`` values,
so re-running the script converges instead of duplicating.

Source files
------------
``data/esco/skills_en.csv`` is required. The full classification bundle
(including ``broaderRelationsSkillPillar_en.csv``) requires a manual download
from https://esco.ec.europa.eu/en/use-esco/download (classification / en /
csv); the portal emails a link after accepting the privacy notice. When the
broader-relations file is absent the graph is built without hierarchy edges
(a later run backfills them). The O*NET file downloads automatically.

Attribution: ESCO © European Union, CC BY 4.0 — https://esco.ec.europa.eu/.
O*NET 31.0 Database by USDOL/ETA, CC BY 4.0; O*NET® is a USDOL/ETA trademark.

Usage
-----
  # Build/refresh the whole graph (hashing embeddings, offline):
  python -m scripts.build_skill_graph

  # Live linker-space vectors (default: EMBEDDING_PROVIDER):
  python -m scripts.build_skill_graph --embedding-provider gemini

  # Exact/alias/trgm linking only (no vectors):
  python -m scripts.build_skill_graph --no-embeddings

  # ESCO only (skip the O*NET download + reconcile):
  python -m scripts.build_skill_graph --skip-onet

After a rebuild, run ``scripts/backfill_skill_ids.py`` to rewrite stored
skill-id arrays that predate the canonical graph.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import get_settings
from app.llm import RetryableLLMError
from app.skills.embeddings import Embedder, build_span_embedder
from app.skills.importers.esco import ESCO_VERSION, import_esco
from app.skills.importers.onet import (
    DEFAULT_ONET_CACHE,
    ONET_VERSION,
    download_software_skills,
    import_onet,
)
from app.skills.importers.reconcile import reconcile_onet

logger = logging.getLogger("build_skill_graph")

DEFAULT_ESCO_DIR = Path("data/esco")
ESCO_CONCEPTS_FILENAME = "skills_en.csv"
ESCO_BROADER_FILENAME = "broaderRelationsSkillPillar_en.csv"
ESCO_SKILL_RELATIONS_FILENAME = "skillSkillRelations_en.csv"
DEFAULT_ALIAS_OVERRIDES = DEFAULT_ESCO_DIR / "alias_overrides.json"
ESCO_PORTAL_URL = "https://esco.ec.europa.eu/en/use-esco/download"


@dataclass(frozen=True, slots=True)
class BuildPlan:
    """Resolved inputs for one build run (paths validated, versions pinned)."""

    esco_concepts: Path
    esco_broader: Path | None
    esco_skill_relations: Path | None
    alias_overrides: Path | None
    esco_version: str
    onet_cache: Path
    onet_version: str
    refresh_onet: bool
    skip_onet: bool


def resolve_plan(args: argparse.Namespace) -> BuildPlan:
    """Validate CLI paths into a concrete plan; fail fast on missing inputs."""
    esco_dir: Path = args.esco_dir
    concepts = args.esco_concepts or esco_dir / ESCO_CONCEPTS_FILENAME
    if not concepts.is_file():
        raise FileNotFoundError(
            f"ESCO concepts CSV not found: {concepts} — download the "
            f"classification bundle from {ESCO_PORTAL_URL} into {esco_dir}/"
        )

    broader = _optional_input(
        args.esco_broader, esco_dir / ESCO_BROADER_FILENAME, "ESCO broader relations"
    )
    if broader is None:
        logger.warning(
            "no %s — building the graph without hierarchy edges; download the "
            "full ESCO CSV bundle from %s to add them on a later run",
            ESCO_BROADER_FILENAME,
            ESCO_PORTAL_URL,
        )
    skill_relations = _optional_input(
        args.esco_skill_relations,
        esco_dir / ESCO_SKILL_RELATIONS_FILENAME,
        "ESCO skill-skill relations",
    )

    overrides: Path | None = args.alias_overrides
    if overrides is not None and not overrides.is_file():
        raise FileNotFoundError(f"alias overrides file not found: {overrides}")

    return BuildPlan(
        esco_concepts=concepts,
        esco_broader=broader,
        esco_skill_relations=skill_relations,
        alias_overrides=overrides,
        esco_version=args.esco_version,
        onet_cache=args.onet_cache,
        onet_version=args.onet_version,
        refresh_onet=args.refresh_onet,
        skip_onet=args.skip_onet,
    )


def _optional_input(explicit: Path | None, default: Path, label: str) -> Path | None:
    """Explicit paths must exist; the default is used only when present."""
    if explicit is not None:
        if not explicit.is_file():
            raise FileNotFoundError(f"{label} file not found: {explicit}")
        return explicit
    return default if default.is_file() else None


def build_graph(
    session: Session,
    plan: BuildPlan,
    *,
    embedder: Embedder | None,
) -> None:
    """Run ESCO import, O*NET import, and reconciliation in one session."""
    esco_result = import_esco(
        session,
        concepts_path=plan.esco_concepts,
        broader_relations_path=plan.esco_broader,
        skill_relations_path=plan.esco_skill_relations,
        alias_overrides_path=plan.alias_overrides,
        source_version=plan.esco_version,
        embedder=embedder,
    )
    logger.info(
        "ESCO %s: source_concepts=%s canonical_concepts=%s aliases=%s "
        "source_edges=%s canonical_edges=%s",
        plan.esco_version,
        esco_result.source_concepts,
        esco_result.canonical_concepts,
        esco_result.aliases,
        esco_result.source_edges,
        esco_result.canonical_edges,
    )

    if plan.skip_onet:
        logger.info("O*NET import skipped (--skip-onet)")
        return

    onet_path = download_software_skills(plan.onet_cache, refresh=plan.refresh_onet)
    onet_result = import_onet(
        session, source_path=onet_path, source_version=plan.onet_version
    )
    logger.info(
        "O*NET %s: source_concepts=%s technologies=%s categories=%s source_edges=%s",
        plan.onet_version,
        onet_result.source_concepts,
        onet_result.technologies,
        onet_result.categories,
        onet_result.source_edges,
    )

    reconcile_result = reconcile_onet(
        session, source_version=plan.onet_version, embedder=embedder
    )
    logger.info(
        "reconcile O*NET→canonical: existing=%s normalized_label=%s alias=%s "
        "trgm=%s semantic=%s created=%s unresolved=%s",
        reconcile_result.existing,
        reconcile_result.normalized_label,
        reconcile_result.alias,
        reconcile_result.trgm,
        reconcile_result.semantic,
        reconcile_result.created,
        reconcile_result.unresolved,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--esco-dir",
        type=Path,
        default=DEFAULT_ESCO_DIR,
        help=f"Directory holding the ESCO CSV bundle (default: {DEFAULT_ESCO_DIR})",
    )
    parser.add_argument(
        "--esco-concepts",
        type=Path,
        default=None,
        help=f"Explicit path to {ESCO_CONCEPTS_FILENAME} (default: under --esco-dir)",
    )
    parser.add_argument(
        "--esco-broader",
        type=Path,
        default=None,
        help=(
            f"Explicit path to {ESCO_BROADER_FILENAME}; the default under "
            "--esco-dir is optional — absent means no hierarchy edges"
        ),
    )
    parser.add_argument(
        "--esco-skill-relations",
        type=Path,
        default=None,
        help=(
            f"Explicit path to {ESCO_SKILL_RELATIONS_FILENAME} (optional; "
            "source-layer assertions only, never promoted)"
        ),
    )
    parser.add_argument(
        "--alias-overrides",
        type=Path,
        default=DEFAULT_ALIAS_OVERRIDES,
        help=(
            "Curated ESCO-URI alias file merged as curated aliases "
            f"(default: {DEFAULT_ALIAS_OVERRIDES})"
        ),
    )
    parser.add_argument(
        "--esco-version",
        default=ESCO_VERSION,
        help=f"Pinned ESCO source version recorded on rows (default: {ESCO_VERSION})",
    )
    parser.add_argument(
        "--onet-cache",
        type=Path,
        default=DEFAULT_ONET_CACHE,
        help=f"O*NET Software Skills cache path (default: {DEFAULT_ONET_CACHE})",
    )
    parser.add_argument(
        "--onet-version",
        default=ONET_VERSION,
        help=f"Pinned O*NET source version recorded on rows (default: {ONET_VERSION})",
    )
    parser.add_argument(
        "--refresh-onet",
        action="store_true",
        help="Ignore the O*NET cache and re-download (one-time fetch policy still applies)",
    )
    parser.add_argument(
        "--skip-onet",
        action="store_true",
        help="ESCO only: skip the O*NET download, import, and reconciliation",
    )
    parser.add_argument(
        "--no-embeddings",
        action="store_true",
        help="Skip concept embeddings (exact/alias/trgm linking only)",
    )
    parser.add_argument(
        "--embedding-provider",
        choices=("hashing", "gemini"),
        default=None,
        help=(
            "Span embedder for concept vectors (default: EMBEDDING_PROVIDER). "
            "Ignored when --no-embeddings is set. The linker's vector stage "
            "only runs when stored concept.embedding_model matches the "
            "active provider."
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    # Touch settings early so a missing DATABASE_URL fails before any work.
    settings = get_settings()
    _ = settings.database_url

    try:
        plan = resolve_plan(args)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1

    embedder: Embedder | None = None
    if not args.no_embeddings:
        provider = (
            args.embedding_provider or settings.embedding_provider or "hashing"
        ).strip().lower()
        try:
            embedder = build_span_embedder(settings, provider=provider)
        except RetryableLLMError as exc:
            logger.error("cannot build %s embedder: %s", provider, exc)
            return 1
        logger.info("embedding concepts with provider=%s", provider)

    from app.db.session import db_session

    try:
        with db_session() as session:
            build_graph(session, plan, embedder=embedder)
    except RetryableLLMError as exc:
        logger.error("embedding failed: %s", exc)
        return 1
    logger.info("skill graph build complete (idempotent)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
