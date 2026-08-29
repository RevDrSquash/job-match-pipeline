# Infrastructure

## Platform choice

**GCP: Cloud Tasks + Cloud Run + Cloud SQL.**

Why this over the alternatives considered:

| Option | Verdict |
| -- | -- |
| **Railway** | Rejected. Horizontal replicas are manual — no queue-depth autoscaling. Would need Judoscale or a DIY autoscaler. No dedicated worker service type. |
| **AWS SQS + Lambda** | Viable and close. Rejected mainly on Cloud Tasks' cleaner per-queue rate limiting and Cloud Run's 60-min timeout vs Lambda's 15. |
| **Step Functions / Temporal** | Overkill. The workflow is a handful of job types with simple transitions, not complex state machines. Durable execution isn't needed. |
| **Azure Container Apps + KEDA** | Good native queue autoscaling, but no reason to prefer it over GCP here. |
| **Celery** | Weak observability, and durable recursion is DIY. |

The decisive features of Cloud Tasks for this workload:

* **Per-queue rate limiting** — `max-dispatches-per-second` and `max-concurrent-dispatches` are exactly the knobs needed for LLM and source-API rate limits. Handler returning 429/503 causes the queue to back off automatically.
* **Durable delivery with retry/backoff** to plain HTTP endpoints
* **Headroom** — caps are 500 dispatches/sec and 5,000 concurrent tasks *per queue*. Steady state is ~0.12/sec, so ~0.02% of one queue's ceiling.

Note: Cloud Tasks delivers each task to exactly one target. That's fine here — we want work distribution, not broadcast. (Pub/Sub would be the choice if multiple services needed to react to the same event.)

## Components

| Component | Purpose |
| -- | -- |
| **Cloud Scheduler** | Cron triggers: ingest kickoff, ~5-min match batch, hourly dirty-profile re-match |
| **Cloud Tasks** | One queue per job type, each independently rate-limited |
| **Cloud Run (handlers)** | Pipeline handlers. Split by job type so concurrency/memory tune independently |
| **Cloud Run (frontend)** | Server-rendered web app. Separate service — see below |
| **Cloud Load Balancer** | Single domain, path-split to frontend and API |
| **Cloud CDN** | Edge caching in front of the frontend service |
| **Cloud SQL (Postgres + pgvector)** | Jobs, users, skills, embeddings, match log |
| **Artifact Registry** | Container images |
| **Secret Manager** | LLM keys, job-board API keys |
| **Service accounts** | Cloud Tasks dispatches with OIDC; Cloud Run validates. Handlers never publicly reachable (`--no-allow-unauthenticated`) |

### Frontend hosting: separate Cloud Run service, not static assets

**Decided.** The frontend runs as its own Cloud Run service rather than as a static bundle on a CDN.

The deciding argument is **SEO**, not convenience. Public job search is a plausible organic acquisition channel, and server-rendered pages index far better than a client-rendered SPA. That outweighs the CDN cost savings.

**Caveat on the SEO argument:** public search pages are thin by design — we withhold JD body text and the source link (see UI Design). Thin, near-duplicate pages at corpus scale risk being treated as low quality rather than indexed. Likely means indexing search and category pages rather than one page per posting. Decide deliberately if organic traffic is actually part of the plan.

**This is not a choice against caching.** Cloud CDN sits in front of Cloud Run, so the static bundle still gets edge-cached. We're choosing the rendering model, not giving up the CDN.

#### Single domain via load balancer

Serve both services under one domain, path-split:

| Path | Backend |
| -- | -- |
| `/api/*` | API service |
| `/*` | Frontend service |

Two Cloud Run services on separate domains would mean CORS configuration plus cross-domain cookie handling for OAuth sessions. One domain keeps session cookies first-party and removes CORS from the picture entirely.

#### Set `min-instances: 1` on the frontend only

Cold start on the landing page is the worst possible place for one: it is the first impression and sits directly in the "Do I qualify?" conversion funnel. Roughly $10–15/mo for a small always-on instance — cheap relative to its position in the funnel.

Leave the pipeline handler services at `min-instances: 0`. They are queue-driven and nobody is waiting on them.

### Storage rationale

Cloud SQL Postgres with `pgvector`. The matching query needs relational filters *and* vector similarity in the same statement — that's the whole match step. 1M jobs with 768-dim vectors is ~3GB; trivially within range.

* **Firestore** — rejected, wrong query shape
* **AlloyDB** — only if pgvector's ANN index stops keeping up, which won't happen at 1M rows
* **BigQuery** — possible cheap archive later, not OLTP

## Infrastructure as code

**Terraform.** The Google provider is the most mature of any cloud's, and it has by far the most training data — coding agents write it far more reliably than Pulumi or Config Connector.

* **Pulumi** — same providers, Python instead of HCL. Nicer for dynamic resource generation, but agents are worse at it and this infra is mostly static config. Revisit only if we start generating resources dynamically.
* **Config Connector** — skip, assumes GKE.
* **gcloud CLI** — bootstrap only. Not idempotent; don't build real infra with shell scripts.

### Module split

Two modules, **separate state files**:

1. `stateful/` — Artifact Registry, Cloud SQL, Secret Manager, service accounts, IAM. Slow-changing, applied rarely.
2. `runtime/` — Cloud Run services, Cloud Tasks queues, Cloud Scheduler jobs. Fast-changing; queue rate limits will be tuned constantly.

The split exists so a Cloud Run tweak can never put the database in the plan.

### Bootstrap sequence

1. `gcloud`: enable APIs, create GCS state bucket (chicken-and-egg — this one by hand), create deploy service account
2. `terraform apply` on `stateful/`
3. Build + push image to Artifact Registry
4. `terraform apply` on `runtime/`

### Critical: don't let Terraform manage the image tag

Deploy images via `gcloud run deploy` or CI. Set `ignore_changes` on the Cloud Run image field. Otherwise every code deploy dirties infra state and every `terraform apply` threatens to roll the app back.

## Agent-assisted IaC

Terraform's plan/apply cycle gives a coding agent a real feedback loop: write HCL → `terraform plan` → read errors → fix. Safer than an agent calling GCP APIs directly, because `plan` shows exactly what will change before anything happens.

Two requirements:

* **Point the agent at current provider docs, not its memory.** GCP resource schemas churn and models confidently emit deprecated arguments. Use the Terraform MCP server or paste the relevant `google_cloud_run_v2_service` / `google_cloud_tasks_queue` reference pages into context.
* **Gate on** `plan`**, always.** Agent writes and plans freely; a human reads the diff and runs `apply`. Never unattended — Terraform will replace a database if an immutable field changed.

## Local development

The whole system is "HTTP handlers + a queue + Postgres." Every piece has a local equivalent.

### The one abstraction that matters

A `TaskQueue` interface with a single method:

```python
enqueue(queue_name: str, payload: dict, delay: int | None = None) -> None
```

Two implementations:

* **Local** — POSTs to `http://localhost:8080/handlers/{name}` in a background task, with a retry loop
* **Cloud Tasks** — creates a task targeting the Cloud Run URL

~50 lines, and it is the *only* thing that differs between environments. Handlers are plain HTTP endpoints either way and don't know which they're running under. (Cloud Tasks emulators exist but the interface is less work and gives deterministic tests.)

Handlers never enqueue directly mid-transaction: they wrap the queue in an environment-agnostic `BufferedTaskQueue` and flush after commit, so an immediately-delivered local task can't race the parent transaction and hit `not_found` (see `TASKS_AND_HANDLERS.md`, Conventions).

### Local stack

* FastAPI, one POST endpoint per job type
* `docker-compose`: pgvector Postgres image + app container. The app service loads host `.env` via `env_file` (keys, `EMBEDDING_PROVIDER`); compose `environment:` still wins for `DATABASE_URL` / `QUEUE_IMPL` / `LOCAL_QUEUE_BASE_URL`.
* Same schema as Cloud SQL
* `QUEUE_IMPL` env var selects the implementation
* Seed with a few hundred real postings, run the full pipeline locally

Host ports are owned by compose and must not be bound by a leftover `next dev` / `uvicorn` (that collision is how `docker compose up` fails with `ports are not available` / `port is already allocated`):

| Host port | Owner | Process |
| -- | -- | -- |
| 3100 | compose `web` | container 3000; 3100 avoids a Hyper-V excluded range over 3000 |
| 8080 | compose `app` | |
| 5433 | compose `db` | container 5432; 5433 avoids a native Windows Postgres on 5432 |
| 3200–3209 | agent UI | `python -m scripts.dev web` (first free port) |
| 8180–8189 | agent API | `python -m scripts.dev api` (first free port) |

`python -m scripts.dev ports` is the port doctor: free vs expected compose container vs foreign process, with PID, command line, and the kill command. `python -m scripts.dev up` preflights the three compose ports, then runs `docker compose up`. Plain `docker compose up` is unchanged. Auto-increment on the agent ranges also covers Hyper-V `EACCES` on a single fixed port. Cursor project hooks keep agent shells off 3100/8080; they require `python3` on PATH (on Windows, copy `python.exe` to `python3.exe` in the same directory).

### What changes on deploy

Same container image. `QUEUE_IMPL=cloudtasks`. Different DB connection string. That's it.

## Known footguns

**Connection pool exhaustion.** Cloud Run scales to many instances, each opening a pool, and Cloud SQL runs out of connections. Keep per-instance pools small (2–5) and cap `max-concurrent-dispatches` accordingly, or front with PgBouncer.

**Poison messages.** Return **2xx on permanent failures** (malformed JD, dead link) after logging. Only 5xx for genuinely retryable errors. Otherwise a bad task retries to `maxAttempts`, burning LLM spend each time.

**Handler timeouts.** Cloud Run's default request timeout is 5 minutes — raise it for resume generation, and ensure the Cloud Tasks dispatch deadline matches, or the queue will retry a task that is still running.

**At-least-once delivery.** Duplicates are certain. Unique constraint on job URL hash; idempotent handlers throughout. Deterministic Cloud Tasks names (hash of the natural key) are the target redelivery-dedup once Terraform lands — see `docs/OPEN_ISSUES.md` §3.

**Two different auth postures on Cloud Run.** The frontend service is public by necessity; every pipeline handler must stay `--no-allow-unauthenticated`. Easy to get wrong when both live in the same Terraform module — the frontend is the *only* service with public ingress, and public endpoints need rate limiting and bot protection (see UI Design).

## Rollout order

1. Local end-to-end on ~500 seed postings — this is where extraction and matching quality gets validated, which is the real project risk
2. Terraform the infra once the pipeline shape is stable
3. Deploy, run steady-state ingest
4. Scale up ingest coverage last, after per-job costs are measured
