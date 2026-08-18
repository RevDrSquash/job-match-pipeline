"""ESCO attribution note — classification CSVs are not committed here.

Download ``skills_en.csv`` from https://esco.ec.europa.eu/en/use-esco/download
or let ``python -m scripts.load_esco`` fetch via the public ESCO API and write
a cache CSV into this directory (gitignored).

``alias_overrides.json`` *is* committed: curated everyday names for official
ESCO concept URIs (postgres/psql, Python, TypeScript, …). The loader merges
those aliases into ``alt_labels`` at upsert time; the file is the provenance
record. Docker / Kubernetes / Terraform / AWS have no ESCO concept and are
omitted on purpose.

Attribution: ESCO © European Union, CC BY 4.0.
See https://esco.ec.europa.eu/en/copyright-notice-esco-skills-competences
"""
