# ESCO source data

ESCO classification files are downloaded locally and are not committed.
Download the English CSV bundle for pinned release 1.2.1 from:

<https://esco.ec.europa.eu/en/use-esco/download>

The graph importer uses:

- `skills_en.csv`
- `broaderRelationsSkillPillar_en.csv`
- optionally `skillSkillRelations_en.csv`

The portal download is manual. The canonical graph importer does not use the
public API because it cannot pin a complete release or provide all source
relationships.

`alias_overrides.json` is committed. It records curated everyday names for
official ESCO concept URIs (Postgres/psql, Python, TypeScript, and others).
The importer stores them as provenance-bearing `curated` concept aliases;
safe parenthetical bare forms are stored separately as `derived` aliases.

Docker, Kubernetes, Terraform, and AWS have no ESCO concept. They enter the
canonical graph through the pinned O*NET Software Skills import instead of
being attached to speculative ESCO concepts.

Build/rebuild: `python -m scripts.build_skill_graph`. After a rebuild that
changes concept identity, rewrite stored skill-id arrays with
`python -m scripts.backfill_skill_ids`. Full procedure:
[`docs/SKILL_GRAPH.md`](../../docs/SKILL_GRAPH.md).

Attribution: ESCO © European Union, CC BY 4.0. See the
[ESCO copyright notice](https://esco.ec.europa.eu/en/copyright-notice-esco-skills-competences).
