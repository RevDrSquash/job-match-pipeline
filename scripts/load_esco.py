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
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx

from app.config import get_settings
from app.db.session import db_session
from app.skills.repository import records_from_mapping_rows, upsert_skills

logger = logging.getLogger("load_esco")

ESCO_API_BASE = "https://ec.europa.eu/esco/api"
ESCO_SKILLS_SCHEME = "http://data.europa.eu/esco/concept-scheme/skills"
DEFAULT_CACHE_CSV = Path("data/esco/skills_en.csv")
USER_AGENT = "job-match-pipeline/0.1 (ESCO taxonomy loader; local PoC)"


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


def main(argv: list[str] | None = None) -> int:
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
        help="Skip hashing embeddings (exact/alias linking only)",
    )
    parser.add_argument(
        "--max-skills",
        type=int,
        default=None,
        help="Cap rows when fetching via API (dev/smoke only)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    # Touch settings early so a missing DATABASE_URL fails before network work.
    _ = get_settings().database_url

    raw_rows = list(
        iter_load_rows(
            csv_path=args.csv,
            cache_csv=args.cache_csv,
            refresh=args.refresh,
            max_skills=args.max_skills,
        )
    )
    records = records_from_mapping_rows(raw_rows)
    logger.info("parsed %s skill records", len(records))
    if not records:
        logger.error("no skills to load")
        return 1

    with db_session() as session:
        written = upsert_skills(
            session,
            records,
            compute_embeddings=not args.no_embeddings,
        )
    logger.info("upserted %s skill rows (idempotent)", written)
    return 0


if __name__ == "__main__":
    sys.exit(main())
