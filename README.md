# Job Match Pipeline

A job-matching pipeline: ingest postings from ATS providers, extract and canonicalize them with LLMs, match them against user profiles through a staged filtering funnel, and generate verified, fabrication-free tailored resumes. A human reviews and submits every application — automated submission is permanently out of scope.

## Documentation

Design docs live in [`docs/`](docs/README.md). **The repo is canonical:** the docs were drafted in Linear, but the copies here are the source of truth. Linear documents are a snapshot for discussion — if the two diverge, the repo wins.

## Status

Pre-scaffold. Current milestone: local proof of concept — the full pipeline running on ~500 seed postings with the four non-negotiable evals from [docs/EVALUATION.md](docs/EVALUATION.md) passing against hand-labeled data, before any GCP spend.
