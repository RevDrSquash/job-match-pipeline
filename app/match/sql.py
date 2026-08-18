"""Shared match-batch SQL: ATS metadata prefilter + optional vector recall.

One statement joins ``jobs`` to ``user_filters`` / ``user_profiles``. Vector
similarity is computed in the same query (the reason for pgvector). Unextracted
jobs still survive the metadata join so they can be dispatched to extract-job.
"""

from __future__ import annotations

from sqlalchemy import bindparam, text
from sqlalchemy.sql.elements import TextClause

# ATS-metadata predicates. NULL / empty filter arrays mean "no constraint".
# Missing job-side metadata also passes — dropping on absent ATS fields is the
# main source of invisible false negatives (docs/EVALUATION.md, docs/UI.md §4).
METADATA_PREDICATE = """
              AND (
                  uf.locations IS NULL
                  OR cardinality(uf.locations) = 0
                  OR j.location IS NULL
                  OR EXISTS (
                      SELECT 1 FROM unnest(uf.locations) AS loc(name)
                      WHERE position(lower(loc.name) in lower(j.location)) > 0
                         OR position(lower(j.location) in lower(loc.name)) > 0
                  )
              )
              AND (
                  uf.comp_floor IS NULL
                  OR j.comp_min IS NULL
                  OR j.comp_min >= uf.comp_floor
              )
              AND (
                  uf.work_arrangement IS NULL
                  OR cardinality(uf.work_arrangement) = 0
                  OR j.work_arrangement IS NULL
                  OR j.work_arrangement = ANY(uf.work_arrangement)
              )
              AND (
                  uf.title_families IS NULL
                  OR cardinality(uf.title_families) = 0
                  OR j.title IS NULL
                  OR EXISTS (
                      SELECT 1 FROM unnest(uf.title_families) AS tf(family)
                      WHERE j.title ILIKE
                            '%' || split_part(replace(tf.family, '/', ' '), ' ', 1) || '%'
                  )
              )
              AND (
                  uf.seniority_band IS NULL
                  OR uf.seniority_band = ''
                  OR j.seniority IS NULL
                  OR lower(j.seniority) = ANY(string_to_array(lower(uf.seniority_band), ','))
              )
"""

# Incremental: newly ingested *or* newly extracted since the last cycle.
# The extracted_at arm is what makes two-cycle lazy extraction work — a job
# ingested in cycle N is extracted after cycle N and recalled in cycle N+1.
JOB_SINCE_PREDICATE = """
              AND (
                  CAST(:since AS timestamptz) IS NULL
                  OR j.ingested_at > CAST(:since AS timestamptz)
                  OR j.extracted_at > CAST(:since AS timestamptz)
              )
"""


def candidate_query() -> TextClause:
    """Prefilter + skill overlap + cosine similarity for a set of users.

    Bindings: ``user_ids`` (expanding sequence of UUIDs), ``since`` (timestamptz
    or NULL). Incremental passes the last-cycle watermark; dirty passes NULL
    so the full corpus is scanned.
    """
    sql = f"""
            SELECT
                up.user_id,
                j.id AS job_id,
                j.title,
                j.extracted_at,
                j.synthesized_doc,
                j.skill_ids AS job_skill_ids,
                up.skill_ids AS profile_skill_ids,
                up.synthesized_doc AS profile_doc,
                (
                    SELECT count(*)::int
                    FROM unnest(COALESCE(j.skill_ids, ARRAY[]::text[])) AS js(skill)
                    INNER JOIN unnest(COALESCE(up.skill_ids, ARRAY[]::text[])) AS us(skill)
                        USING (skill)
                ) AS skill_overlap,
                CASE
                    WHEN j.extracted_at IS NOT NULL
                     AND j.embedding IS NOT NULL
                     AND up.embedding IS NOT NULL
                    THEN 1 - (j.embedding <=> up.embedding)
                    ELSE NULL
                END AS similarity
            FROM jobs j
            JOIN user_profiles up ON up.user_id IN :user_ids
            JOIN user_filters uf ON uf.user_id = up.user_id
            WHERE (j.expires_at IS NULL OR j.expires_at > now())
{JOB_SINCE_PREDICATE}
{METADATA_PREDICATE}
            ORDER BY up.user_id, j.embedding <=> up.embedding NULLS LAST
    """
    return text(sql).bindparams(bindparam("user_ids", expanding=True))
