# Job Match Pipeline

A job-matching pipeline: ingest postings from ATS providers, extract and canonicalize them with LLMs, match them against user profiles through a staged filtering funnel, and generate verified, fabrication-free tailored resumes. A human reviews and submits every application — automated submission is permanently out of scope.

## Documentation

Design docs live in [`docs/`](docs/README.md) and are the source of truth for all design decisions.

## Status

Local proof-of-concept. FastAPI handlers (`fetch-link-list` / `ingest-job` / `extract-job` are real; later stages are stubs), ATS adapters (Greenhouse, Lever, Ashby), ESCO skill linking, `TaskQueue` abstraction (`QUEUE_IMPL=local|cloudtasks`), docker-compose (pgvector Postgres + app), Alembic migrations, and a `job-match-seed` CLI for the ~500-posting corpus. No GCP resources yet — everything runs locally with `QUEUE_IMPL=local`.

## Prerequisites

- Python 3.12+
- Docker + Docker Compose

## Quick start (Docker)

```bash
cp .env.example .env
docker compose up --build
# In another terminal (or after db is healthy):
pip install -e '.[dev]'
alembic upgrade head
```

- App: http://localhost:8080/health
- Handlers: `POST http://localhost:8080/handlers/{name}`
- Postgres: `localhost:5432` (db `jobmatch`, user/password `postgres`)

Schema migrations live in `alembic/versions/`. Run `alembic upgrade head` against the compose DB before exercising handlers that touch Postgres.

Handler names: `fetch-link-list`, `ingest-job`, `extract-job`, `match-batch`, `screen-job`, `generate-resume`, `verify-resume`.

### Seed corpus (~500 postings)

Hand-picked boards live in [`config/seed_companies.json`](config/seed_companies.json). After Postgres is up and migrated:

```bash
pip install -e '.[dev]'
alembic upgrade head
python -m app.seed --target 500
# or: job-match-seed --target 500
```

The seed walks boards sequentially (low concurrency), upserts on `url_hash`, and stops near the target. Re-running is a no-op once the corpus is filled. No LLM calls on this path.

Extract a seeded job (lazy; cached on `extracted_at`):

```bash
# Live extraction needs LLM_API_KEY or GEMINI_API_KEY.
# EMBEDDING_PROVIDER=hashing (default) writes 768-d hashing vectors offline;
# set EMBEDDING_PROVIDER=gemini to use text-embedding-004.
curl -s -X POST http://localhost:8080/handlers/extract-job \
  -H 'content-type: application/json' \
  -d '{"job_id":"<job uuid>"}'
```

Re-POSTing the same `job_id` is a no-op. Permanent failures (unparseable JD) return 2xx; retryable LLM errors return 5xx. Token counts and estimated cost are logged on every extraction — never the JD text.

Smoke a single board through the HTTP handlers:

```bash
curl -s -X POST http://localhost:8080/handlers/fetch-link-list \
  -H 'content-type: application/json' \
  -d '{"ats_provider":"greenhouse","board_token":"airtable","company_name":"Airtable"}'
```

`ENABLE_DEBUG_CAPTURE=true` turns on an in-memory receipt log and `GET /_debug/received` for local tests. Leave it false (the default) outside PoC/test so it cannot ship to Cloud Run.

## Local development (without Docker for the app)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env

# Optional: start only Postgres
docker compose up db -d

uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

`QUEUE_IMPL` selects the queue backend (see [`docs/INFRASTRUCTURE.md`](docs/INFRASTRUCTURE.md)):

| Value | Behavior |
| -- | -- |
| `local` (default) | Background HTTP POST to `LOCAL_QUEUE_BASE_URL/handlers/{name}` with retries |
| `cloudtasks` | Creates a GCP Cloud Tasks task targeting `CLOUD_TASKS_HANDLER_BASE_URL` |

This queue abstraction is the only environment-specific code path.

## Tests and lint

```bash
pip install -e '.[dev]'
docker compose up db -d   # schema tests need Postgres + pgvector
alembic upgrade head
pytest
ruff check .
```

## Skill taxonomy (ESCO)

Canonical skill linking is shared by `extract-job` and profile parsing
(`app/skills/`). The PoC taxonomy is **ESCO** (~14k skills); the linker is
taxonomy-agnostic so O*NET can replace it later.

**License:** ESCO data is published by the European Commission under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) (see the
[ESCO copyright notice](https://esco.ec.europa.eu/en/copyright-notice-esco-skills-competences)).
Attribute “ESCO © European Union” when redistributing derived data. The skills
pillar also incorporates O*NET (USDOL/ETA, CC BY 4.0) and Canadian glossary
elements — credit those sources as required by the notice.

Load into Postgres (idempotent upsert on skill id):

```bash
# Preferred: official CSV from https://esco.ec.europa.eu/en/use-esco/download
# (classification / en / csv → skills_en.csv)
python -m scripts.load_esco --csv /path/to/skills_en.csv

# Or fetch via the public ESCO API and cache data/esco/skills_en.csv
python -m scripts.load_esco
```

Re-running the loader updates existing rows; it does not duplicate. Pass
`--no-embeddings` to skip the PoC hashing embeddings (exact/alias linking
still works). See `scripts/load_esco.py` for flags.

## Conventions (baked into handlers)

- Handlers are idempotent HTTP POST endpoints.
- Return **2xx on permanent failure** (after logging); **5xx only for retryable errors**.
- See [`docs/TASKS_AND_HANDLERS.md`](docs/TASKS_AND_HANDLERS.md) for the full pipeline contracts.
