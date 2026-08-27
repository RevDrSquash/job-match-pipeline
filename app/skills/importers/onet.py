"""O*NET Software Skills download, parsing, and source-graph import."""

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.skills.importers.common import (
    source_concept_id,
    upsert_source_concepts,
    upsert_source_edges,
)
from app.skills.normalize import normalize_label

ONET_SOURCE = "onet"
ONET_VERSION = "31.0"
ONET_SOFTWARE_SKILLS_URL = (
    "https://www.onetcenter.org/dl_files/database/"
    "db_31_0_json/software_skills.json"
)
DEFAULT_ONET_CACHE = Path("data/onet/software_skills_31_0.json")
USER_AGENT = "job-match-pipeline/0.1 (O*NET database importer; local PoC)"


@dataclass(frozen=True, slots=True)
class OnetSourceConcept:
    external_id: str
    name: str
    source_type: str
    raw_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OnetSourceRelation:
    subject_external_id: str
    predicate: str
    object_external_id: str
    raw_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OnetDataset:
    concepts: tuple[OnetSourceConcept, ...]
    relations: tuple[OnetSourceRelation, ...]

    @property
    def technologies(self) -> tuple[OnetSourceConcept, ...]:
        return tuple(row for row in self.concepts if row.source_type == "technology")

    @property
    def categories(self) -> tuple[OnetSourceConcept, ...]:
        return tuple(
            row for row in self.concepts if row.source_type == "technology_category"
        )


@dataclass(frozen=True, slots=True)
class OnetImportResult:
    source_concepts: int
    technologies: int
    categories: int
    source_edges: int


def download_software_skills(
    cache_path: Path = DEFAULT_ONET_CACHE,
    *,
    refresh: bool = False,
    url: str = ONET_SOFTWARE_SKILLS_URL,
    client: httpx.Client | None = None,
) -> Path:
    """Download O*NET once and atomically cache the pinned source file.

    There are deliberately no retries: this is a one-time, low-volume import,
    and retrying client errors would violate the repository's fetch policy.
    """
    if cache_path.exists() and not refresh:
        return cache_path
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    owns_client = client is None
    active_client = client or httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
        timeout=60.0,
    )
    try:
        response = active_client.get(url)
        response.raise_for_status()
        content = response.content
    finally:
        if owns_client:
            active_client.close()
    if not content:
        raise RuntimeError("O*NET Software Skills download was empty")

    temporary = cache_path.with_name(f".{cache_path.name}.tmp")
    temporary.write_bytes(content)
    temporary.replace(cache_path)
    return cache_path


def parse_software_skill_rows(path: Path) -> list[dict[str, str]]:
    """Read the official O*NET JSON or CSV distribution into normalized rows."""
    if path.suffix.casefold() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        raw_rows = payload.get("row") if isinstance(payload, dict) else payload
        if not isinstance(raw_rows, list):
            raise ValueError(f"{path} does not contain an O*NET row list")
        return [_normalize_row(row, path) for row in raw_rows if isinstance(row, dict)]

    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"CSV has no header: {path}")
        return [_normalize_row(row, path) for row in reader]


def deduplicate_software_skills(
    rows: Iterable[Mapping[str, object]],
) -> OnetDataset:
    """Collapse occupation rows into technology concepts and category assertions."""
    grouped: dict[str, list[dict[str, str]]] = {}
    for raw in rows:
        row = _normalize_mapping(raw)
        example = row["workplace_example"]
        key = normalize_label(example)
        if not key:
            continue
        grouped.setdefault(key, []).append(row)

    technologies: list[OnetSourceConcept] = []
    category_names: dict[str, set[str]] = {}
    relation_rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for normalized_name in sorted(grouped):
        associations = sorted(grouped[normalized_name], key=_association_sort_key)
        names = sorted(
            {row["workplace_example"] for row in associations},
            key=lambda value: (value.casefold(), value),
        )
        name = names[0]
        external_id = onet_example_external_id(normalized_name)
        occupation_associations = [
            {
                "onet_soc_code": row["onet_soc_code"],
                "title": row["title"],
                "element_id": row["element_id"],
                "element_name": row["element_name"],
                "hot_technology": _yes(row["hot_technology"]),
                "in_demand": _yes(row["in_demand"]),
            }
            for row in associations
        ]
        technologies.append(
            OnetSourceConcept(
                external_id=external_id,
                name=name,
                source_type="technology",
                raw_data={
                    "normalized_name": normalized_name,
                    "source_names": names,
                    "occupation_associations": occupation_associations,
                    "hot_technology": any(
                        association["hot_technology"]
                        for association in occupation_associations
                    ),
                    "in_demand": any(
                        association["in_demand"] for association in occupation_associations
                    ),
                },
            )
        )

        for association in occupation_associations:
            element_id = str(association["element_id"])
            element_name = str(association["element_name"])
            if not element_id:
                continue
            category_names.setdefault(element_id, set()).add(element_name)
            relation_rows.setdefault((external_id, element_id), []).append(association)

    categories = [
        OnetSourceConcept(
            external_id=onet_category_external_id(element_id),
            name=sorted(
                (name for name in names if name),
                key=lambda value: (value.casefold(), value),
            )[0]
            if any(names)
            else element_id,
            source_type="technology_category",
            raw_data={
                "element_id": element_id,
                "source_names": sorted(name for name in names if name),
            },
        )
        for element_id, names in sorted(category_names.items())
    ]
    relations = [
        OnetSourceRelation(
            subject_external_id=technology_id,
            predicate="IS_A",
            object_external_id=onet_category_external_id(element_id),
            raw_data={
                "assertion": "onet_content_model_category",
                "occupation_associations": sorted(
                    associations, key=_association_sort_key
                ),
            },
        )
        for (technology_id, element_id), associations in sorted(relation_rows.items())
    ]
    return OnetDataset(
        concepts=tuple([*technologies, *categories]),
        relations=tuple(relations),
    )


def parse_onet_software_skills(path: Path) -> OnetDataset:
    return deduplicate_software_skills(parse_software_skill_rows(path))


def import_onet(
    session: Session,
    *,
    source_path: Path,
    source_version: str = ONET_VERSION,
) -> OnetImportResult:
    """Idempotently load O*NET technologies/categories into the source layer."""
    dataset = parse_onet_software_skills(source_path)
    source_rows = [
        {
            "id": source_concept_id(ONET_SOURCE, source_version, row.external_id),
            "source": ONET_SOURCE,
            "source_version": source_version,
            "external_id": row.external_id,
            "name": row.name,
            "source_type": row.source_type,
            "raw_data": row.raw_data,
        }
        for row in dataset.concepts
    ]
    upsert_source_concepts(session, source_rows)
    edge_rows = [
        {
            "subject_id": source_concept_id(
                ONET_SOURCE, source_version, row.subject_external_id
            ),
            "predicate": row.predicate,
            "object_id": source_concept_id(
                ONET_SOURCE, source_version, row.object_external_id
            ),
            "confidence": 1.0,
            "raw_data": row.raw_data,
        }
        for row in dataset.relations
    ]
    upsert_source_edges(session, edge_rows)
    session.flush()
    return OnetImportResult(
        source_concepts=len(source_rows),
        technologies=len(dataset.technologies),
        categories=len(dataset.categories),
        source_edges=len(edge_rows),
    )


def onet_example_external_id(normalized_example: str) -> str:
    normalized = normalize_label(normalized_example)
    if not normalized:
        raise ValueError("O*NET workplace example must not be empty")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"software:{digest}"


def onet_category_external_id(element_id: str) -> str:
    cleaned = element_id.strip()
    if not cleaned:
        raise ValueError("O*NET Element ID must not be empty")
    return f"category:{cleaned}"


def _normalize_row(row: Mapping[str, object], path: Path) -> dict[str, str]:
    normalized = _normalize_mapping(row)
    missing = [
        key
        for key in ("onet_soc_code", "workplace_example", "element_id", "element_name")
        if not normalized[key]
    ]
    if missing:
        raise ValueError(f"{path} O*NET row missing required fields: {missing}")
    return normalized


def _normalize_mapping(row: Mapping[str, object]) -> dict[str, str]:
    canonical = {_canon_header(str(key)): value for key, value in row.items()}

    def value(*keys: str) -> str:
        for key in keys:
            raw = canonical.get(_canon_header(key))
            if raw is not None:
                return str(raw).strip()
        return ""

    return {
        "onet_soc_code": value("onetsoc_code", "O*NET-SOC Code"),
        "title": value("title"),
        "workplace_example": value(
            "workplace_example", "Workplace Example", "example"
        ),
        "element_id": value("element_id", "Element ID", "commodity_code"),
        "element_name": value("element_name", "Element Name", "commodity_title"),
        "hot_technology": value("hot_technology", "Hot Technology"),
        "in_demand": value("in_demand", "In Demand"),
    }


def _association_sort_key(row: Mapping[str, object]) -> tuple[str, ...]:
    return tuple(
        str(row.get(key, ""))
        for key in (
            "onet_soc_code",
            "title",
            "element_id",
            "element_name",
            "hot_technology",
            "in_demand",
        )
    )


def _canon_header(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _yes(value: str) -> bool:
    return value.strip().casefold() in {"y", "yes", "true", "1"}
