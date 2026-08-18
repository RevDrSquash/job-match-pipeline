#!/usr/bin/env python3
"""Load the ESCO skills taxonomy into the ``skills`` table.

ESCO (European Skills, Competences, Qualifications and Occupations) is the
PoC taxonomy (~14k skill concepts). The linker itself stays taxonomy-agnostic;
this script is the only ESCO-specific code path.

License / attribution
---------------------
ESCO classification data is published by the European Commission and is
generally available under Creative Commons Attribution 4.0 International
(CC BY 4.0) unless otherwise indicated. The skills pillar hierarchy also
incorporates elements of O*NET (USDOL/ETA, CC BY 4.0; O*NET® is a trademark
of USDOL/ETA) and the Government of Canada's Skills and Knowledge Checklist.

When redistributing derived data, attribute:

  ESCO © European Union, CC BY 4.0 — https://esco.ec.europa.eu/
  https://esco.ec.europa.eu/en/copyright-notice-esco-skills-competences

Sources
-------
1. Official CSV package (preferred): download ``skills_en.csv`` from
   https://esco.ec.europa.eu/en/use-esco/download (classification / en / csv).
   The portal emails a download link after accepting the privacy notice.
2. Public ESCO Web Services API (default when ``--csv`` is omitted): pages
   skills from ``https://ec.europa.eu/esco/api/`` and optionally caches a
   CSV under ``data/esco/`` for idempotent reloads without re-fetching.

Usage
-----
  # From a portal CSV (no network):
  python -m scripts.load_esco --csv /path/to/skills_en.csv

  # Fetch via API, cache CSV, upsert (idempotent):
  python -m scripts.load_esco

  # Skip embedding computation (exact/alias linking only):
  python -m scripts.load_esco --no-embeddings

  # Live linker-space vectors (default: EMBEDDING_PROVIDER):
  python -m scripts.load_esco --embedding-provider gemini

Curated aliases
---------------
``data/esco/alias_overrides.json`` maps official ESCO URIs to everyday
names (postgres/psql, Python, TypeScript, …). The loader merges those
into ``alt_labels`` at upsert time; the JSON file is the provenance
record. Unknown URIs and aliases that collide with another concept are
skipped (logged), so a taxonomy bump cannot silently retarget a span.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import db_session
from app.extract.llm import RetryableLLMError
from app.skills.embeddings import GeminiSpanEmbedder, build_span_embedder
from app.skills.linker import SkillRecord
from app.skills.normalize import normalize_label
from app.skills.repository import load_skill_records, records_from_mapping_rows, upsert_skills

logger = logging.getLogger("load_esco")

ESCO_API_BASE = "https://ec.europa.eu/esco/api"
ESCO_SKILLS_SCHEME = "http://data.europa.eu/esco/concept-scheme/skills"
DEFAULT_CACHE_CSV = Path("data/esco/skills_en.csv")
DEFAULT_ALIAS_OVERRIDES = Path("data/esco/alias_overrides.json")
USER_AGENT = "job-match-pipeline/0.1 (ESCO taxonomy loader; local PoC)"


@dataclass(frozen=True, slots=True)
class AliasOverride:
    """One curated URI → everyday-name mapping from ``alias_overrides.json``."""

    uri: str
    aliases: tuple[str, ...]
    preferred_label: str | None = None


def parse_skills_csv(path: Path) -> list[dict[str, object]]:
    """Parse an official-style ``skills_*.csv`` (or the API cache we write)."""
    rows: list[dict[str, object]] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"CSV has no header: {path}")
        field_map = {_canon_header(name): name for name in reader.fieldnames if name}
        uri_key = _require_field(field_map, ("concepturi", "concept_uri", "uri", "id"))
        label_key = _require_field(
            field_map, ("preferredlabel", "conceptpt", "canonical_label", "title")
        )
        type_key = field_map.get("concepttype") or field_map.get("concept_type")
        alt_key = field_map.get("altlabels") or field_map.get("alt_labels")
        desc_key = (
            field_map.get("description")
            or field_map.get("definition")
            or field_map.get("scopenote")
        )

        for raw in reader:
            concept_type = (raw.get(type_key) or "").strip().upper() if type_key else ""
            # Official CSV marks skills/knowledge as SK and groups as SG. Our
            # API-written cache uses SK for every row.
            if concept_type in {"SG", "SKILLGROUP", "SKILL_GROUP"}:
                continue
            skill_id = (raw.get(uri_key) or "").strip()
            label = (raw.get(label_key) or "").strip()
            if not skill_id or not label:
                continue
            rows.append(
                {
                    "id": skill_id,
                    "canonical_label": label,
                    "alt_labels": (raw.get(alt_key) or "") if alt_key else "",
                    "description": (raw.get(desc_key) or "") if desc_key else "",
                }
            )
    return rows


def _canon_header(name: str) -> str:
    return "".join(ch for ch in name.casefold() if ch.isalnum() or ch == "_")


def _require_field(field_map: dict[str, str], candidates: tuple[str, ...]) -> str:
    for candidate in candidates:
        if candidate in field_map:
            return field_map[candidate]
    raise ValueError(
        f"CSV missing required column (tried {candidates}); have {sorted(field_map)}"
    )


def fetch_skills_from_api(
    *,
    page_size: int = 100,
    max_skills: int | None = None,
    sleep_s: float = 0.05,
) -> list[dict[str, object]]:
    """Page the public ESCO skills scheme. Low concurrency; no 4xx retries.

    The ESCO API's ``offset`` parameter is a *page number*, not a record
    offset: ``offset=N`` returns records ``N*limit .. N*limit+limit-1``.
    Keep ``limit`` constant across requests so page boundaries stay aligned.

    Some ESCO records are malformed server-side and make the whole page 500
    (e.g. "More than one value found for field 'hasSkillType'"). On a 5xx we
    re-fetch that page's records one at a time and skip only the bad ones.
    """
    rows: list[dict[str, object]] = []
    total: int | None = None
    page = 0
    with httpx.Client(
        base_url=ESCO_API_BASE,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=60.0,
    ) as client:
        while True:
            response = _get_skill_page(client, limit=page_size, offset=page)
            if response.status_code >= 500:
                logger.warning(
                    "ESCO API %s on page %s (%s); re-fetching its records one at a time",
                    response.status_code,
                    page,
                    response.text[:120],
                )
                batch, total = _fetch_page_records_individually(
                    client,
                    start=page * page_size,
                    count=page_size,
                    total=total,
                    sleep_s=sleep_s,
                )
            elif response.status_code >= 400:
                raise RuntimeError(
                    f"ESCO API error {response.status_code} at page={page}: "
                    f"{response.text[:200]}"
                )
            else:
                payload = response.json()
                embedded = payload.get("_embedded") or {}
                batch = [
                    _skill_from_api(embedded[item["uri"]]) for item in payload.get("concepts", [])
                ]
                total = int(payload.get("total") or 0)
                if not batch:
                    break
            rows.extend(batch)
            if total is None:
                raise RuntimeError("ESCO API total unknown after first page failed entirely")
            logger.info("fetched %s / %s skills from ESCO API", min(len(rows), total), total)
            if (page + 1) * page_size >= total:
                break
            if max_skills is not None and len(rows) >= max_skills:
                break
            page += 1
            if sleep_s > 0:
                time.sleep(sleep_s)
    if max_skills is not None:
        rows = rows[:max_skills]
    return rows


def _get_skill_page(client: httpx.Client, *, limit: int, offset: int) -> httpx.Response:
    return client.get(
        "/resource/skill",
        params={
            "isInScheme": ESCO_SKILLS_SCHEME,
            "language": "en",
            "limit": limit,
            "offset": offset,
        },
    )


def _fetch_page_records_individually(
    client: httpx.Client,
    *,
    start: int,
    count: int,
    total: int | None,
    sleep_s: float,
) -> tuple[list[dict[str, object]], int | None]:
    """Fetch records ``start .. start+count-1`` with ``limit=1``, skipping 5xx.

    With ``limit=1`` the page number equals the record index, so this recovers
    every healthy record from a page whose bulk fetch 500s on one bad record.
    """
    rows: list[dict[str, object]] = []
    for index in range(start, start + count):
        if total is not None and index >= total:
            break
        response = _get_skill_page(client, limit=1, offset=index)
        if response.status_code >= 500:
            logger.warning(
                "skipping malformed ESCO record at index %s (API %s: %s)",
                index,
                response.status_code,
                response.text[:120],
            )
        elif response.status_code >= 400:
            raise RuntimeError(
                f"ESCO API error {response.status_code} at record index={index}: "
                f"{response.text[:200]}"
            )
        else:
            payload = response.json()
            embedded = payload.get("_embedded") or {}
            rows.extend(
                _skill_from_api(embedded[item["uri"]]) for item in payload.get("concepts", [])
            )
            total = int(payload.get("total") or 0) or total
        if sleep_s > 0:
            time.sleep(sleep_s)
    return rows, total


def _skill_from_api(resource: dict) -> dict[str, object]:
    preferred = resource.get("preferredLabel") or {}
    label = preferred.get("en") or resource.get("title") or ""
    alt = resource.get("alternativeLabel") or {}
    alt_en = alt.get("en") or []
    if isinstance(alt_en, str):
        alt_list = [alt_en]
    else:
        alt_list = [str(a) for a in alt_en]
    description = ""
    desc = resource.get("description") or {}
    en_desc = desc.get("en") if isinstance(desc, dict) else None
    if isinstance(en_desc, dict):
        description = str(en_desc.get("literal") or "")
    elif isinstance(en_desc, str):
        description = en_desc
    return {
        "id": resource["uri"],
        "canonical_label": label,
        "alt_labels": " | ".join(alt_list),
        "description": description,
    }


def write_skills_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["conceptUri", "conceptType", "preferredLabel", "altLabels", "description"],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "conceptUri": row["id"],
                    "conceptType": "SK",
                    "preferredLabel": row["canonical_label"],
                    "altLabels": row.get("alt_labels") or "",
                    "description": row.get("description") or "",
                }
            )


def load_alias_overrides(path: Path) -> list[AliasOverride]:
    """Parse the versioned curated-alias file (official ESCO URIs → names)."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = payload.get("overrides")
    if not isinstance(raw, list):
        raise ValueError(f"{path} missing overrides list")
    out: list[AliasOverride] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"{path} overrides[{index}] must be an object")
        uri = str(item.get("uri") or "").strip()
        aliases_raw = item.get("aliases")
        if not uri:
            raise ValueError(f"{path} overrides[{index}] missing uri")
        if not isinstance(aliases_raw, list) or not aliases_raw:
            raise ValueError(f"{path} overrides[{index}] aliases must be a non-empty list")
        aliases = tuple(str(alias).strip() for alias in aliases_raw if str(alias).strip())
        if not aliases:
            raise ValueError(f"{path} overrides[{index}] aliases empty after trim")
        preferred = item.get("preferred_label")
        out.append(
            AliasOverride(
                uri=uri,
                aliases=aliases,
                preferred_label=str(preferred).strip() if preferred else None,
            )
        )
    return out


def apply_alias_overrides(
    records: Sequence[SkillRecord],
    overrides: Sequence[AliasOverride],
) -> list[SkillRecord]:
    """Union curated aliases onto matching records; colliding forms lose.

    Official labels always win. An alias that normalizes to another concept's
    existing label or alias is skipped. Unknown URIs are logged and ignored so
    a newer ESCO drop cannot fail the load.
    """
    merged: dict[str, SkillRecord] = {record.id: record for record in records}
    occupied: dict[str, str] = {}
    for record in records:
        for label in (record.canonical_label, *record.alt_labels):
            key = normalize_label(label)
            if key:
                occupied.setdefault(key, record.id)

    seen_uris: set[str] = set()
    added = 0
    for override in overrides:
        if override.uri in seen_uris:
            logger.warning(
                "duplicate alias override for %s; skipping later entry", override.uri
            )
            continue
        seen_uris.add(override.uri)

        record = merged.get(override.uri)
        if record is None:
            logger.warning(
                "alias override URI not in loaded taxonomy (skipped): %s", override.uri
            )
            continue
        if (
            override.preferred_label
            and normalize_label(override.preferred_label)
            != normalize_label(record.canonical_label)
        ):
            logger.warning(
                "alias override preferred_label %r does not match taxonomy label %r for %s",
                override.preferred_label,
                record.canonical_label,
                override.uri,
            )

        existing_norm = {
            normalize_label(label)
            for label in (record.canonical_label, *record.alt_labels)
            if normalize_label(label)
        }
        extra: list[str] = []
        for alias in override.aliases:
            key = normalize_label(alias)
            if not key or key in existing_norm:
                continue
            owner = occupied.get(key)
            if owner is not None and owner != record.id:
                logger.warning(
                    "skipping curated alias %r for %s; already claimed by %s",
                    alias,
                    override.uri,
                    owner,
                )
                continue
            extra.append(alias)
            existing_norm.add(key)
            occupied[key] = record.id

        if extra:
            added += len(extra)
            merged[override.uri] = SkillRecord(
                id=record.id,
                canonical_label=record.canonical_label,
                alt_labels=(*record.alt_labels, *extra),
                description=record.description,
                embedding=record.embedding,
                embedding_model=record.embedding_model,
            )

    logger.info("merged %s curated aliases onto %s skill records", added, len(records))
    return [merged[record.id] for record in records]


def iter_load_rows(
    *,
    csv_path: Path | None,
    cache_csv: Path,
    refresh: bool,
    max_skills: int | None,
) -> Iterator[dict[str, object]]:
    if csv_path is not None:
        logger.info("reading skills CSV from %s", csv_path)
        yield from parse_skills_csv(csv_path)
        return

    if cache_csv.exists() and not refresh:
        logger.info("reading cached skills CSV from %s", cache_csv)
        yield from parse_skills_csv(cache_csv)
        return

    logger.info("fetching skills from ESCO API (will cache to %s)", cache_csv)
    rows = fetch_skills_from_api(max_skills=max_skills)
    write_skills_csv(cache_csv, rows)
    yield from rows


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Path to skills_en.csv from the ESCO download portal",
    )
    parser.add_argument(
        "--cache-csv",
        type=Path,
        default=DEFAULT_CACHE_CSV,
        help=f"Cache path when fetching via API (default: {DEFAULT_CACHE_CSV})",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore cache and re-fetch from the ESCO API",
    )
    parser.add_argument(
        "--no-embeddings",
        action="store_true",
        help="Skip embeddings (exact/alias linking only)",
    )
    parser.add_argument(
        "--embedding-provider",
        choices=("hashing", "gemini"),
        default=None,
        help=(
            "Span embedder for taxonomy vectors (default: EMBEDDING_PROVIDER). "
            "Ignored when --no-embeddings is set. gemini uses batchEmbedContents "
            "with SEMANTIC_SIMILARITY; already-embedded rows with a matching "
            "embedding_model are skipped so a free-tier backfill can resume."
        ),
    )
    parser.add_argument(
        "--alias-overrides",
        type=Path,
        default=DEFAULT_ALIAS_OVERRIDES,
        help=(
            "Curated ESCO-URI alias file merged into alt_labels at upsert "
            f"(default: {DEFAULT_ALIAS_OVERRIDES})"
        ),
    )
    parser.add_argument(
        "--max-skills",
        type=int,
        default=None,
        help="Cap rows when fetching via API (dev/smoke only)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def partition_for_embed(
    records: Sequence[SkillRecord],
    existing: Mapping[str, SkillRecord],
    *,
    model: str,
) -> tuple[list[SkillRecord], list[SkillRecord]]:
    """Split rows into (needs_embed, already_embedded_with_model).

    Already-embedded rows keep their stored vectors so an interrupted gemini
    backfill can resume without re-spending tokens. Label/alias fields on
    those rows still refresh from the incoming record.
    """
    needs_embed: list[SkillRecord] = []
    already: list[SkillRecord] = []
    for record in records:
        prev = existing.get(record.id)
        if (
            prev is not None
            and prev.embedding is not None
            and prev.embedding_model == model
        ):
            already.append(
                SkillRecord(
                    id=record.id,
                    canonical_label=record.canonical_label,
                    alt_labels=record.alt_labels,
                    description=record.description,
                    embedding=prev.embedding,
                    embedding_model=prev.embedding_model,
                )
            )
        else:
            needs_embed.append(record)
    return needs_embed, already


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    # Touch settings early so a missing DATABASE_URL fails before network work.
    settings = get_settings()
    _ = settings.database_url
    provider = (args.embedding_provider or settings.embedding_provider or "hashing")
    provider = provider.strip().lower()

    raw_rows = list(
        iter_load_rows(
            csv_path=args.csv,
            cache_csv=args.cache_csv,
            refresh=args.refresh,
            max_skills=args.max_skills,
        )
    )
    records = apply_alias_overrides(
        records_from_mapping_rows(raw_rows),
        load_alias_overrides(args.alias_overrides),
    )
    logger.info("parsed %s skill records", len(records))
    if not records:
        logger.error("no skills to load")
        return 1

    try:
        with db_session() as session:
            written = _upsert_records(
                session,
                records,
                provider=provider,
                compute_embeddings=not args.no_embeddings,
            )
    except RetryableLLMError as exc:
        logger.error("embedding failed: %s", exc)
        return 1
    logger.info("upserted %s skill rows (idempotent)", written)
    return 0


def _upsert_records(
    session: Session,
    records: list[SkillRecord],
    *,
    provider: str,
    compute_embeddings: bool,
) -> int:
    if not compute_embeddings:
        return upsert_skills(session, records, compute_embeddings=False)

    embedder = build_span_embedder(get_settings(), provider=provider)
    if not isinstance(embedder, GeminiSpanEmbedder):
        return upsert_skills(session, records, embedder=embedder, compute_embeddings=True)

    existing = {row.id: row for row in load_skill_records(session)}
    needs_embed, already = partition_for_embed(records, existing, model=embedder.model)
    logger.info(
        "gemini span-embed model=%s to_embed=%s already_embedded=%s",
        embedder.model,
        len(needs_embed),
        len(already),
    )
    written = 0
    if already:
        written += upsert_skills(
            session,
            already,
            compute_embeddings=False,
            batch_size=embedder.batch_size,
        )
    # Commit each API batch so a TPM-stalled run can resume from embedding_model.
    if needs_embed:
        written += upsert_skills(
            session,
            needs_embed,
            embedder=embedder,
            compute_embeddings=True,
            batch_size=embedder.batch_size,
        )
    return written


if __name__ == "__main__":
    sys.exit(main())
