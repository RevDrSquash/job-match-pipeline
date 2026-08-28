# O*NET source data

O*NET source files are downloaded locally and are not committed.

The importer pins O*NET 31.0 and caches the full Software Skills JSON file as
`software_skills_31_0.json`. It is downloaded from:

<https://www.onetcenter.org/dl_files/database/db_31_0_json/software_skills.json>

The file contains all 31,821 occupation/software rows. Import deduplicates
`workplace_example` values into source concepts while preserving occupation
associations, Element ID/Name categories, and Hot Technology/In Demand flags
as provenance. Category assertions remain source edges and are not promoted
to the canonical hierarchy.

Build/rebuild: `python -m scripts.build_skill_graph` (downloads this file
on first run). Full procedure: [`docs/SKILL_GRAPH.md`](../../docs/SKILL_GRAPH.md).

This project includes information from the O*NET 31.0 Database by the U.S.
Department of Labor, Employment and Training Administration (USDOL/ETA).
Used under the [CC BY 4.0 license](https://creativecommons.org/licenses/by/4.0/).
O*NET® is a trademark of USDOL/ETA.
