# Agent instructions

Job Match Pipeline: ingest job postings from ATS providers, extract and canonicalize them with LLMs, match them against user profiles through a staged filtering funnel, and generate verified, fabrication-free tailored resumes.

## Read the docs first

`docs/` is canonical for all design decisions — read the docs relevant to your task before writing code. `docs/README.md` is the index. The Linear issues reference specific sections; follow those references. If a design question isn't answered in `docs/`, check `docs/OPEN_ISSUES.md` before inventing an answer.

**The docs record the owner's decisions; they do not override them.** The authority order is: owner's current direction → docs → code. When the owner directs a change that contradicts a doc, that is a design change, not an error — update the doc and the code together. Push back only on substance: if a doc records a reason for the old decision or a dependency the change would break, surface that trade-off in one line as input to the owner's call, then follow their direction.

Current milestone: local proof of concept only. No GCP resources, no Terraform — everything runs against docker-compose with `QUEUE_IMPL=local`. The UI is a local single-user Next.js app (`frontend/`) talking to the user-facing `/api/*` router; see `docs/UI.md`, "Local UI milestone". No auth, no public endpoints.

## Hard rules (never violate)

1. **Never fabricate resume content.** The generator must never invent skills, employers, numbers, or experience. The "missing skills" bucket exists precisely so the model knows what NOT to claim. The fabrication eval is a hard gate with target zero.
2. **No code reuse from OpenPostings** (github.com/Masterjx9/OpenPostings). It has no LICENSE file. Reading it to understand which ATS endpoints exist is fine; copying code is not. See `docs/POSTING_SOURCES.md`.
3. **No automated application submission.** Permanently out of scope, not deferred. A human reviews and submits every application.
4. **No personal information in logs or error traces** — no resume text, no work history, ever. Job postings are not personal information; user data is. See `docs/PRIVACY_AND_COMPLIANCE.md`.
5. **No LLM calls in the ingest path** (`fetch-link-list`, `ingest-job`). Extraction is deliberately lazy — it runs in `extract-job` on first prefilter hit. See the rationale in `docs/TASKS_AND_HANDLERS.md`.
6. **Keep external fetch volume trivial.** The ATS ToS review hasn't been done; the seed uses public documented APIs at low concurrency. Never add aggressive fetching, retries against 4xx, or user-agent spoofing.

## Conventions

* **Handlers:** every pipeline handler is an idempotent HTTP POST endpoint at `/handlers/{name}`. Return 2xx on permanent failure (after logging); 5xx only for genuinely retryable errors — a 5xx on a poison message burns LLM spend on every retry.
* **`pipeline_events`:** every handler writes a row regardless of outcome. This table is the future training set and the project's main defensible asset; never skip it, never make it deletable-by-cascade.
* **`TaskQueue`:** the only code that differs between environments. Everything else must be environment-agnostic. Don't add Cloud Tasks emulators or environment branches elsewhere.
* **Idempotency everywhere:** at-least-once delivery makes duplicates certain. Dedup on `url_hash` for jobs, `extracted_at IS NULL` guards for extraction, no-op on redelivery for screening/generation.
* **Embeddings are 768-dim** (`vector(768)`); use the same embedding model for job and profile documents. Record the model choice in `docs/OPEN_ISSUES.md` §6.
* **Log token counts and cost for every LLM call.** Real token counts are the cost model's biggest open question and resolve a known ~10× discrepancy (`docs/OPEN_ISSUES.md` §1).
* **Skill canonicalization** goes through the shared linking module over the canonical ESCO + O*NET graph (`docs/SKILL_GRAPH.md`). Don't do string matching on skill names outside it.

## Workflow

* Stack: Python, FastAPI, Postgres + pgvector via docker-compose, Alembic migrations, pytest, ruff.
* Run `pytest` and `ruff check` before finishing any task. Schema changes always go through an Alembic migration, never manual DDL.
* Prompts are code: any prompt change must re-run the eval suite once it exists (`docs/EVALUATION.md`, Operational discipline).
* **If your implementation diverges from a design doc, update the doc in the same change** — the repo docs must stay true to the actual design, whether the divergence came from implementation reality or an owner decision. Deferred decisions and known inconsistencies go in `docs/OPEN_ISSUES.md`.
* Secrets come from env vars (`.env`, gitignored). Never commit keys, and never hardcode model names deep in call sites — keep them in config.

### Dev servers and host ports

Never end a turn with a background process still running. Compose owns **3100** (`web`), **8080** (`app`), and **5433** (`db`). Do not bind those with `next dev`, `npm run dev`, or `uvicorn`.

* Start a temporary UI with `python -m scripts.dev web` (first free port in 3200–3209). Start a temporary API with `python -m scripts.dev api` (8180–8189). Both print the URL.
* Stop everything you started before the turn ends: `python -m scripts.dev stop`. `--keep` on the launcher is the only exception, and only when the user asked for a server to outlive the turn.
* When a bind fails or compose reports `ports are not available` / `port is already allocated`, run `python -m scripts.dev ports` before guessing. Do not scan `netstat` by hand first.
* Prefer `python -m scripts.dev up [--build]` over a raw `docker compose up` so occupied compose ports are diagnosed instead of failing as a bare bind error. Raw `docker compose up` still works.

Project Cursor hooks enforce the same rules for agent shells (not the user's own terminal): `npm run dev` / `next dev` on 3100 is denied; `uvicorn` on 8080 asks. Hooks need `python3` on PATH.
