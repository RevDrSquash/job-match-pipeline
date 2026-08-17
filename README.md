# Job Match Pipeline

A job-matching pipeline: ingest postings from ATS providers, extract and canonicalize them with LLMs, match them against user profiles through a staged filtering funnel, and generate verified, fabrication-free tailored resumes. A human reviews and submits every application — automated submission is permanently out of scope.

## Documentation

Design docs live in [`docs/`](docs/README.md) and are the source of truth for all design decisions.

## Status

Local proof-of-concept scaffold. FastAPI handler stubs, `TaskQueue` abstraction (`QUEUE_IMPL=local|cloudtasks`), docker-compose (pgvector Postgres + app), and Alembic migrations for the full data model are in place. No GCP resources yet — everything runs locally with `QUEUE_IMPL=local`.

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

Smoke the stub chain (opt-in: each handler enqueues the next only when `follow_chain` is true; default is off):

```bash
curl -s -X POST http://localhost:8080/handlers/fetch-link-list \
  -H 'content-type: application/json' \
  -d '{"run_id":"local-1","follow_chain":true}'
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

## Conventions (baked into handlers)

- Handlers are idempotent HTTP POST endpoints.
- Return **2xx on permanent failure** (after logging); **5xx only for retryable errors**.
- See [`docs/TASKS_AND_HANDLERS.md`](docs/TASKS_AND_HANDLERS.md) for the full pipeline contracts.
