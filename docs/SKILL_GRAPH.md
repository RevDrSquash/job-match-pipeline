# Skill knowledge graph

Canonical, source-agnostic skill concepts used to link spans extracted from
job descriptions and resumes. Downstream matching compares concept IDs —
no string matching, no LLM — so "AWS" and "Amazon Web Services" are the
same node, and an unknown span stays unresolved rather than being forced
onto the nearest neighbour.

The graph is **our data model**. ESCO and O*NET are imported as versioned
source taxonomies with provenance and mapped into canonical concepts. The
schema leaves room for further sources, corpus-discovered concepts, and
occupation nodes; automatic graph expansion and labor-market demand are
out of scope (DEF-48).

## Provenance model

| Layer | Tables | Meaning |
| -- | -- | -- |
| Canonical | `concept`, `concept_alias`, `concept_edge` | Application-owned identity. `concept.id` is a deterministic UUIDv5 from the founding source ref — never a raw ESCO URI or O*NET string. |
| Source | `source_concept`, `source_edge` | Lossless copy of one external concept or assertion, keyed by `(source, source_version, external_id)`. |
| Mapping | `source_mapping` | Provenance-bearing link from a source concept onto a canonical concept (`mapping_type` / `mapping_method` / `confidence`). |

Canonical edges start with `IS_A` only. ESCO skill→skill broader relations
are promoted into `concept_edge`. Skill groups and O*NET category
assertions stay in `source_edge` — they are evidence, not hierarchy.
`jobs.skill_ids`, `user_profiles.skill_ids`, and
`matches.{matched,adjacent,missing}_skills` remain `Text[]` arrays holding
concept UUID strings.

Concept types in use: `skill`, `knowledge`, `technology`,
`technology_category`. The column accepts `occupation` later.

Alias types: `preferred`, `alt`, `curated` (from
`data/esco/alias_overrides.json`), `derived` (parenthetical bare forms).
`scan_text` excludes `derived` aliases and ambiguous short terms so a
prose mention of "python" does not silently claim the language skill.

## Source downloads and version pinning

Pinned versions are recorded on every `source_concept` row and defaulted
in the importers (`ESCO_VERSION`, `ONET_VERSION`).

**ESCO v1.2.1.** Manual portal download of the English CSV classification
bundle (the public API cannot pin a complete release or supply
relationships):

<https://esco.ec.europa.eu/en/use-esco/download>

Drop into `data/esco/` (gitignored except `alias_overrides.json` and the
README):

- `skills_en.csv` (required)
- `broaderRelationsSkillPillar_en.csv` (hierarchy; optional — absent means
  no canonical `IS_A` edges; a later run backfills them)
- optionally `skillSkillRelations_en.csv` (source-layer only)

See [`data/esco/README.md`](../data/esco/README.md). Attribution: ESCO ©
European Union, CC BY 4.0.

**Live graph (local PoC):** the current import was built without
`broaderRelationsSkillPillar_en.csv`, so `concept_edge` is empty. Visible
structure in the `/skills` explorer comes from the source layer (`source_edge`
O*NET technology → category, plus `source_mapping`). Rebuilding with that CSV
present populates canonical `IS_A`; the explorer already renders both layers
and needs no change when those edges appear.

**O*NET 31.0 Software Skills.** One-time low-volume fetch of the full
~31.8k-row file (not Hot Technologies only), cached at
`data/onet/software_skills_31_0.json`:

<https://www.onetcenter.org/dictionary/31.0/json/software_skills.html>

`workplace_example` values are deduplicated into source concepts.
Occupation associations, Element ID/Name, and hot-technology flags stay
in `raw_data` / `source_edge`. See [`data/onet/README.md`](../data/onet/README.md).
Attribution: O*NET 31.0 Database by USDOL/ETA, CC BY 4.0; O*NET® is a
USDOL/ETA trademark.

There is an official ESCO/O*NET **occupation** crosswalk. It does not map
skill concepts and is not used.

## Rebuild / reconcile

```bash
# Offline vectors (default EMBEDDING_PROVIDER=hashing):
python -m scripts.build_skill_graph

# Live linker-space vectors:
python -m scripts.build_skill_graph --embedding-provider gemini

# Exact / alias / trgm only:
python -m scripts.build_skill_graph --no-embeddings

# ESCO only:
python -m scripts.build_skill_graph --skip-onet
```

The orchestrator (`scripts/build_skill_graph.py`) runs three idempotent
importers:

1. **ESCO** — source rows + canonical `skill` / `knowledge` concepts
   (from ESCO `skillType`), aliases, `source_mapping` (`exact` / `import`),
   and promoted skill→skill `IS_A` edges.
2. **O*NET** — download/cache, then source technologies + category
   assertions. Categories are never auto-promoted.
3. **Reconcile** — map each O*NET technology onto an existing concept
   (normalized-label equality → alias equality → pg_trgm / embedding
   candidates with fixed thresholds and deterministic tie-break) or
   found a new canonical `technology` (Docker, Kubernetes, AWS, …).
   Never forces a match.

Re-running with the same pinned versions upserts in place. Bump
`--esco-version` / `--onet-version` when the source files change so the
new rows sit alongside the previous version rather than colliding.

After a rebuild that changes concept identity (first graph build, or a
source version that merges/splits nodes), rewrite stored arrays:

```bash
python -m scripts.backfill_skill_ids
python -m scripts.backfill_skill_ids --dry-run
```

`scripts/backfill_skill_ids.py` is the documented migration path from
legacy stored IDs:

- official ESCO URIs → `source_concept(source='esco')` → `source_mapping`
- leftover `esco:<slug>` / current `seed:<slug>` placeholders → normalized
  label against `concept_alias` (via the in-repo seed record)
- already-canonical concept UUIDs are kept
- unmappable IDs are logged and dropped

A second run is a no-op.

## Linking policy

Callers go through `app.skills.SkillLinker` only (`link_spans` /
`scan_text`). Production uses `PostgresSkillLinker` over the graph
tables, wired by `linker_from_session`. The in-memory linker +
`linker_from_records` remain for unit tests, evals, and the profile-ingest
empty-graph seed fallback (`seed:<slug>` IDs). `extract-job` does **not**
get that fallback: an empty `concept` table is a retryable 503
(`skills_taxonomy_missing`) checked before any LLM spend.

Per span:

1. Normalize + compound-expand in Python (`normalize.py` / `enrich.py`).
2. Exact `concept_alias.normalized_alias` lookup.
3. `pg_trgm` candidates above `SKILL_LINK_TRGM_THRESHOLD`, ordered by
   similarity desc then `concept_id`.
4. pgvector cosine over `concept.embedding` using the existing two-tier
   high-confidence / threshold / margin policy (`skill_link_params`).
5. Unresolved otherwise. Never force a match.

`scan_text` generates token-bounded n-grams, then one batched
`normalized_alias = ANY(...)` query excluding `derived` aliases and
`AMBIGUOUS_SCAN_TERMS`.

**Embedding trust.** If stored `concept.embedding_model` does not match
the active span embedder, the vector stage is **disabled and logged**.
Exact + trgm still run. This is a behavior change from the old in-memory
linker, which rebuilt hashing vectors on mismatch — mixing embedding
spaces is worse than skipping the fallback. Rebuild with
`scripts/build_skill_graph.py --embedding-provider …` to restore vectors.

Matching (`app/match/skills.py`) is unchanged: set operations on
canonical IDs, no LLM. The hardcoded sibling-adjacency table stays until
a tested subsumption policy over `concept_edge` lands
(`docs/OPEN_ISSUES.md` §9).

Offline eval labels keep stable `seed:<slug>` IDs. Canonical UUIDs are
deterministic (UUIDv5) but not fixture-friendly across source-version
bumps; see `evals/sets/v1/skill_linking/labels.json`.
