# Job Match Pipeline

A job-matching pipeline: ingest postings from ATS providers, extract and canonicalize them with LLMs, match them against user profiles through a staged filtering funnel, and generate verified, fabrication-free tailored resumes. A human reviews and submits every application — automated submission is permanently out of scope.

## Documentation

Design docs live in [`docs/`](docs/README.md) and are the source of truth for all design decisions.

## Status

Local proof-of-concept. FastAPI handlers (`fetch-link-list` / `ingest-job` / `extract-job` / `match-batch` / `screen-job` / `generate-resume` / `verify-resume`), ATS adapters (Greenhouse, Lever, Ashby), ESCO skill linking, `TaskQueue` abstraction (`QUEUE_IMPL=local|cloudtasks`), docker-compose (pgvector Postgres + app + web UI), Alembic migrations, a `job-match-seed` CLI for the ~500-posting corpus, a `jobmatch` CLI for profile ingest and match cycles, `jobmatch evals run` for the four non-negotiable evals, and a local single-user Next.js UI (see [Local UI](#local-ui)). No GCP resources yet — everything runs locally with `QUEUE_IMPL=local`.

## Prerequisites

- Python 3.12+
- Docker + Docker Compose
- Node.js 20+ (only for running the UI dev server outside Docker)

## Quick start (Docker)

First-run sequence. Details: [Skill taxonomy](#skill-taxonomy-esco), [Seed corpus](#seed-corpus-500-postings), [Profile CLI](#profile-cli-poc).

```bash
cp .env.example .env
docker compose up --build
# In another terminal (or after db is healthy):
pip install -e '.[dev]'
alembic upgrade head
python -m scripts.load_esco
python -m app.seed --target 500
jobmatch profile ingest tests/fixtures/sample_resume.md --fallback-parser --json
jobmatch match run --mode incremental
```

- App: http://localhost:8080/health
- UI: http://localhost:3100 (the `web` compose service; see [Local UI](#local-ui))
- Handlers: `POST http://localhost:8080/handlers/{name}`
- Postgres: `localhost:5432` (db `jobmatch`, user/password `postgres`)

Schema migrations live in `alembic/versions/`. Run `alembic upgrade head` against the compose DB before exercising handlers that touch Postgres.

Handler names: `fetch-link-list`, `ingest-job`, `extract-job`, `match-batch`, `screen-job`, `generate-resume`, `verify-resume`. Profile ingest and match cycles are documented under [Profile CLI](#profile-cli-poc).

### Seed corpus (~500 postings)

Hand-picked boards live in [`config/seed_companies.json`](config/seed_companies.json). After Postgres is up and migrated:

```bash
pip install -e '.[dev]'
alembic upgrade head
python -m app.seed --target 500
# or: job-match-seed --target 500
```

The seed walks boards sequentially (low concurrency), upserts on `url_hash`, and stops near the target. Re-running is a no-op once the corpus is filled. After adding `jobs.raw_jd_html`, backfill display HTML for existing rows with `python -m app.seed --backfill-html` (one list request per known board; no new jobs). Postings that have left the board stay on the plain-text fallback. No LLM calls on this path.

Extract a seeded job (lazy; cached on `extracted_at`). **Load the ESCO taxonomy first** (see [Skill taxonomy](#skill-taxonomy-esco)) — `extract-job` refuses to run against an empty `skills` table (retryable 503, checked before any LLM spend):

```bash
# Hard prerequisite: python -m scripts.load_esco (once, idempotent).
# Live extraction needs LLM_API_KEY or GEMINI_API_KEY.
# EMBEDDING_PROVIDER=hashing (default) writes 768-d hashing vectors offline;
# set EMBEDDING_PROVIDER=gemini to use gemini-embedding-001 (768-d truncation).
curl -s -X POST http://localhost:8080/handlers/extract-job \
  -H 'content-type: application/json' \
  -d '{"job_id":"<job uuid>"}'
```

Re-POSTing the same `job_id` is a no-op. Permanent failures (unparseable JD) return 2xx; retryable LLM errors return 5xx. Token counts and estimated cost are logged on every extraction — never the JD text.

Screen a match written by `match-batch` (hard-req overlap recorded, then cheap LLM qualification label):

```bash
# Live gate needs LLM_API_KEY or GEMINI_API_KEY (GATE_MODEL, default gemini-3.5-flash-lite).
curl -s -X POST http://localhost:8080/handlers/screen-job \
  -H 'content-type: application/json' \
  -d '{"match_id":"<match uuid>"}'
```

Re-POSTing the same `match_id` is a no-op. `clearly_qualified` + remaining quota enqueues `generate-resume` and decrements `users.quota_remaining`. Every label is persisted (`qualification_label` / `screen_reason`). Profile text is never logged.

Generate a resume for a screened match (three skill buckets, cached work-history prefix, claim → source-span map), then verify it:

```bash
# Live generation needs LLM_API_KEY (GENERATION_MODEL, default gemini-3.1-pro-preview).
curl -s -X POST http://localhost:8080/handlers/generate-resume \
  -H 'content-type: application/json' \
  -d '{"match_id":"<match uuid>"}'

# Live verify stages 2–3 need VERIFY_API_KEY or ANTHROPIC_API_KEY
# (VERIFY_MODEL, default claude-sonnet-4-5 — different family than generate).
curl -s -X POST http://localhost:8080/handlers/verify-resume \
  -H 'content-type: application/json' \
  -d '{"generation_id":"<generation uuid>"}'
```

Re-POSTing the same generate `attempt` is a no-op. Verify runs a deterministic stage (employers / titles / dates / numbers / skill subset) plus JD-blind grounding and JD-aware coverage. Failure regenerates once with the named violations, then flags `needs_review`. Resume text is never logged.

Smoke a single board through the HTTP handlers:

```bash
curl -s -X POST http://localhost:8080/handlers/fetch-link-list \
  -H 'content-type: application/json' \
  -d '{"ats_provider":"greenhouse","board_token":"airtable","company_name":"Airtable"}'
```

`ENABLE_DEBUG_CAPTURE=true` turns on an in-memory receipt log and `GET /_debug/received` for local tests. Leave it false (the default) outside PoC/test so it cannot ship to Cloud Run.

## Profile CLI (PoC)

Resume ingestion has no UI (upload stays CLI-only this milestone) — ingest the test resume from the command line. This writes `users`, `user_profiles` (structured `work_history` with `source: parsed` and stable span IDs, linked `skill_ids`, synthesized JD-shaped doc, 768-dim embedding), and default `user_filters`.

```bash
# Offline: --fallback-parser needs no API key, and EMBEDDING_PROVIDER=hashing
# (the default) writes 768-d hashing vectors. Real ingest uses
# PROFILE_PARSER=gemini + LLM_API_KEY (same key as extract-job).

jobmatch profile ingest path/to/resume.pdf --fallback-parser --json
jobmatch profile show --user-id <uuid>
jobmatch profile edit <uuid> --comp-floor 140000
```

`python -m app` is the same entry point. `profile edit` bumps `profile_version` and sets `rematch_needed`; it does not trigger a rematch. There is no Cloud Scheduler in the PoC — run a cycle by POSTing the handler:

```bash
jobmatch match run --mode incremental
jobmatch match run --mode dirty
```

Incremental matches jobs ingested or extracted since the last completed cycle against all profiles that have a `user_filters` row. Dirty scans the full corpus for profiles with `rematch_needed` (capped per run) and clears the flag. Unextracted prefilter survivors enqueue `extract-job` and wait for the next cycle; the following cycle writes `matches` and enqueues `screen-job`.

## Local UI

Single-user Next.js app in [`frontend/`](frontend/) — match feed, job search at `/jobs`, profile editor, generation handoff, and an admin dashboard at `/admin`. It talks to the user-facing `/api/*` router on the FastAPI app (distinct from the internal `/handlers/*` workers); Next.js rewrites proxy `/api/*` to `API_BASE_URL` (default `http://localhost:8080`), so there is no CORS setup. Design: [`docs/UI.md`](docs/UI.md), "Local UI milestone".

**Via Docker:** `docker compose up --build` includes the `web` service — open http://localhost:3100.

**Dev server against a locally running FastAPI app** (see [Local development](#local-development-without-docker-for-the-app)):

```bash
cd frontend
npm install
npm run dev    # http://localhost:3100, proxies /api/* to http://localhost:8080
```

Set `API_BASE_URL` if the FastAPI app is somewhere other than `localhost:8080`.

The UI serves on **3100** rather than Next.js's default 3000 because Windows (Hyper-V/WSL dynamic port reservation) often places an excluded port range over 3000, making binds fail with `EACCES`. Override with `npm run dev -- -p <port>` if 3100 is taken.

There is no auth: the UI auto-selects the user when exactly one profile exists and offers a picker otherwise. Ingest a profile first (see [Profile CLI](#profile-cli-poc)) — with no users the UI has nothing to show, and an empty match feed usually means no match cycle has run yet (`jobmatch match run --mode incremental`).

Frontend checks:

```bash
cd frontend
npm run lint
npm run typecheck
```

## Eval harness

The four non-negotiable evals (`docs/EVALUATION.md`) hang off the CLI.
Sample labels live in [`evals/sets/v1/`](evals/sets/v1/); the labeling
workflow is in [`evals/README.md`](evals/README.md).

```bash
jobmatch evals run --offline
jobmatch evals run --suite fabrication --plant-fabrication --offline   # exit 1
```

Reports are timestamped JSON + a text summary under `evals/results/` and
record the set version, per-suite latency, token counts, and estimated
cost. Retrieval recall@K warns (or refuses with
`--require-gemini-embeddings`) unless `EMBEDDING_PROVIDER=gemini`.
Fabrication is a hard gate: any fabricated claim fails the suite.

### Local proof of concept (DEF-25)

One command seeds the corpus, ingests the test profile, cycles `match-batch` until extracts and screens drain through the local queue, runs the four eval suites, and writes [`docs/POC_RESULTS.md`](docs/POC_RESULTS.md):

```bash
# Hard prerequisite for the live path: python -m scripts.load_esco (the runner
# fails fast if the skills table is empty).
# Measurement run needs EMBEDDING_PROVIDER=gemini and LLM_API_KEY / GEMINI_API_KEY.
# VERIFY_API_KEY / ANTHROPIC_API_KEY is required for verify-resume stages 2–3.
jobmatch poc run --quota 3
jobmatch poc report   # rewrite the report from the current DB + latest eval JSON
```

The default profile is `tests/fixtures/sample_resume.md` (same persona as `evals/sets/v1`). Do not commit a real resume. `QUEUE_IMPL=local` is required — the runner POSTs handlers; it does not call generate/verify functions directly.

Skill linking uses the shared `skills` table (load it with `scripts/load_esco.py`, below). **The ESCO load is a hard prerequisite for the pipeline:** `extract-job` refuses to run against an empty `skills` table (retryable 503, checked before any LLM spend) because it would cache permanently skill-less extractions. The profile CLI alone still falls back to a small built-in seed taxonomy for offline use, but load ESCO before running any live extraction or match cycle. Job and profile documents must share the same `EMBEDDING_PROVIDER` — the two vector spaces are not comparable across providers.

Resume text is never written to application logs or exception traces. `profile show` prints the structured result to stdout for manual review.

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
`--embedding-provider hashing|gemini` to choose the taxonomy-vector
embedder (default: `EMBEDDING_PROVIDER`). `gemini` uses
`gemini-embedding-001` / `SEMANTIC_SIMILARITY` and skips rows that
already have a matching `embedding_model` so a free-tier backfill can
resume. Pass `--no-embeddings` to skip vectors (exact/alias linking
still works). Curated everyday aliases live in
`data/esco/alias_overrides.json` and are merged into `alt_labels` on
every load. See `scripts/load_esco.py` for flags.

## Conventions (baked into handlers)

- Handlers are idempotent HTTP POST endpoints.
- Return **2xx on permanent failure** (after logging); **5xx only for retryable errors**.
- See [`docs/TASKS_AND_HANDLERS.md`](docs/TASKS_AND_HANDLERS.md) for the full pipeline contracts.
