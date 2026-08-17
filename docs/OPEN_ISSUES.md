# Open issues from doc review

Findings from the pre-scaffold review of the design docs (2026-08-16). None of these block the local proof of concept; they are recorded here so they don't get lost or derail the build.

## 1. Cheap-gate cost figures disagree by ~10× (load-bearing for pricing, not for the PoC)

Two docs give incompatible per-call costs for the `screen-job` LLM gate:

* **Tasks and Handlers** (`screen-job`): "a separate **~$0.005** gate call saves the full generation cost on reject."
* **Cost Model** (per-user table): cheap LLM gate at ~100 calls/day → **~$0.50–1.50/user/mo**, which is ~3,000 calls/mo → **~$0.0002–0.0005 per call**.

Why it matters: at $0.005/call the gate costs ~$15/user/mo, which breaks the "screening is effectively free, generous on every tier" claim and the blended COGS tables (screening would exceed generation on every tier). At $0.0005/call the Cost Model stands.

The architectural decisions (separate gate call, gate placement before generation) hold under either figure, so the PoC is unaffected. **Resolution:** measure real token counts and cost per gate call during the PoC — already listed as an open cost question — then correct whichever doc is wrong.

## 2. Recall@K eval set size vs. the 500-posting seed corpus

Evaluation Strategy specifies "a fixed corpus (a few thousand postings) with exhaustive relevance labels" for retrieval recall@K, but the PoC seed corpus is ~500 postings, and DEF-14 requires all four non-negotiable evals running. The Bootstrapping section already sanctions starting smaller ("a few hundred real postings... enough for evals 1–3 immediately").

**Resolution:** run recall@K against the ~500-posting seed as the first cut; treat the few-thousand-posting exhaustively-labeled corpus as a follow-up before scale-up. No doc change needed beyond this note.

## 3. Queue list omits `fetch-link-list` and `match-batch`

The Queues table in Tasks and Handlers lists five queues but both Scheduler-triggered handlers (`fetch-link-list`, `match-batch`) are absent. Presumably Cloud Scheduler invokes them directly over HTTP (OIDC) without a queue in between, but this is never stated. Irrelevant locally (the local `TaskQueue` posts to handlers directly); decide and document before the Terraform work.

## 4. Schema sketch is not migration-ready

**Resolved in DEF-16.** The initial Alembic migration adds primary keys on `matches`, `generations`, and `pipeline_events`; moves work-history provenance (`source: parsed | user_asserted`) inside each `work_history` JSONB entry on `user_profiles`; and keeps `pipeline_events.user_id` as a nullable column with no FK so user linkage can be stripped on anonymization.

Original sketch gaps (for history):

* Primary keys on `matches`, `generations`, `pipeline_events` (`generations.match_id` implies `matches` needs an `id`)
* `user_profiles.source` is annotated "per work_history entry" but placed at table level — provenance belongs inside the structured `work_history` entries, not as a table column
* `pipeline_events` needs to support the deletion/anonymization cascade from Privacy and Compliance (keep user linkage strippable)

## 5. ATS ingest ToS review also covers the seed fetch

DEF-14 lists ATS-endpoint ToS review as a blocker "before building." Strictly, even fetching the ~500 seed postings uses those endpoints. The seed fetch targets public, documented JSON APIs intended for consumption (Greenhouse boards API, Lever postings API) at trivial volume, which is a defensible interim posture — but the review should happen before steady-state ingest, and this note is the acknowledgment that the PoC front-runs it slightly.

## 6. Unpinned technical choices to make during scaffold (defaults suggested)

Not inconsistencies — just decisions the docs deliberately leave open that the scaffold has to pick something for:

* **Embedding model** — **Resolved for the PoC in §7.** Docs fix the dimension (768) but not the model. Job and profile documents use the same model.
* **Skill taxonomy** — **PoC pick: ESCO.** The linker (`app/skills`) is the only place that matches skill-name strings. The PoC ships a seed of common software-engineering concepts with `esco:<slug>` IDs. Swapping the seed for a downloaded ESCO CSV (official concept URIs) is a data change, not a call-site change.
* **Migration tooling** — docs say "schema migration" without naming a tool; Alembic is the default for a FastAPI/Postgres stack.

## 7. Embedding model (PoC)

**Choice:** OpenAI `text-embedding-3-small` with `dimensions=768`.

Same model for job documents (`extract-job`) and profile documents (`profile ingest`). Configured via `EMBEDDING_MODEL` / `EMBEDDING_DIM` in `app/config.py` — not hardcoded at call sites.

Why this one: native 768-dim output (the schema constraint), a single HTTP API, no local GPU, and an OpenAI-compatible wire format so a Vertex/Gemini endpoint can be swapped in later without changing callers.

When `EMBEDDING_API_KEY` / `LLM_API_KEY` is unset, the client falls back to a deterministic hash embedder so the CLI and tests can write a real `vector(768)` without calling a vendor. That stand-in is **not** for matching quality.

Revisit Vertex `text-embedding-004` (`outputDimensionality=768`) when GCP is wired up — same dimension, in-family with the rest of the stack.
