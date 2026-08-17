"""Parse and upsert taxonomy rows into ``skills`` (shared by loaders)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import Skill
from app.skills.embeddings import Embedder, HashingEmbedder
from app.skills.linker import SkillRecord, skill_embedding_text


def upsert_skills(
    session: Session,
    records: Sequence[SkillRecord],
    *,
    embedder: Embedder | None = None,
    compute_embeddings: bool = True,
    batch_size: int = 500,
    commit: bool = True,
) -> int:
    """Idempotent upsert of taxonomy rows. Returns number of rows written."""
    if not records:
        return 0

    active_embedder = embedder
    if compute_embeddings and active_embedder is None:
        active_embedder = HashingEmbedder()

    written = 0
    for start in range(0, len(records), batch_size):
        chunk = list(records[start : start + batch_size])
        embeddings: list[list[float] | None]
        if active_embedder is not None and compute_embeddings:
            embeddings = active_embedder.embed([skill_embedding_text(r) for r in chunk])
        else:
            embeddings = [
                list(r.embedding) if r.embedding is not None else None for r in chunk
            ]

        rows = [
            {
                "id": record.id,
                "canonical_label": record.canonical_label,
                "alt_labels": list(record.alt_labels),
                "description": record.description,
                "embedding": emb,
            }
            for record, emb in zip(chunk, embeddings, strict=True)
        ]
        stmt = insert(Skill).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Skill.id],
            set_={
                "canonical_label": stmt.excluded.canonical_label,
                "alt_labels": stmt.excluded.alt_labels,
                "description": stmt.excluded.description,
                "embedding": stmt.excluded.embedding,
            },
        )
        session.execute(stmt)
        written += len(rows)

    if commit:
        session.commit()
    else:
        session.flush()
    return written


def load_skill_records(session: Session) -> list[SkillRecord]:
    """Load all ``skills`` rows as linker records."""
    rows = session.scalars(select(Skill)).all()
    return [
        SkillRecord(
            id=row.id,
            canonical_label=row.canonical_label,
            alt_labels=tuple(row.alt_labels or ()),
            description=row.description,
            embedding=tuple(row.embedding) if row.embedding is not None else None,
        )
        for row in rows
    ]


def records_from_mapping_rows(rows: Iterable[dict[str, object]]) -> list[SkillRecord]:
    """Build ``SkillRecord`` list from dict rows (used by CSV / API parsers)."""
    out: list[SkillRecord] = []
    for row in rows:
        skill_id = str(row["id"]).strip()
        label = str(row["canonical_label"]).strip()
        if not skill_id or not label:
            continue
        alts_raw = row.get("alt_labels") or ()
        if isinstance(alts_raw, str):
            alts = tuple(a.strip() for a in _split_alt_labels(alts_raw) if a.strip())
        else:
            alts = tuple(str(a).strip() for a in alts_raw if str(a).strip())  # type: ignore[arg-type]
        description = row.get("description")
        desc = str(description).strip() if description else None
        out.append(
            SkillRecord(
                id=skill_id,
                canonical_label=label,
                alt_labels=alts,
                description=desc or None,
            )
        )
    return out


def _split_alt_labels(raw: str) -> list[str]:
    if "|" in raw:
        return raw.split("|")
    if "\n" in raw:
        return raw.splitlines()
    if ";" in raw and ", " not in raw:
        return raw.split(";")
    return [raw] if raw.strip() else []
