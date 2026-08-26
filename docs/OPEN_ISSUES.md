# Open issues from doc review

Findings from the pre-scaffold review of the design docs (2026-08-16). None of these block the local proof of concept; they are recorded here so they don't get lost or derail the build.

## 1. Cheap-gate cost figures disagree by ~10× (load-bearing for pricing, not for the PoC)

Two docs give incompatible per-call costs for the `screen-job` LLM gate:

* **Tasks and Handlers** (`screen-job`): "a separate **~$0.005** gate call saves the full generation cost on reject."
* **Cost Model** (per-user table): cheap LLM gate at ~100 calls/day → **~$0.50–1.50/user/mo**, which is ~3,000 calls/mo → **~$0.0002–0.0005 per call**.

Why it matters: at $0.005/call the gate costs ~$15/user/mo, which breaks the "screening is effectively free, generous on every tier" claim and the blended COGS tables (screening would exceed generation on every tier). At $0.0005/call the Cost Model stands.

The architectural decisions (separate gate call, gate placement before generation) hold under either figure, so the PoC is unaffected. **Resolution:** measure real token counts and cost per gate call during the PoC — already listed as an open cost question — then correct whichever doc is wrong.

**PoC instrumentation (DEF-22):** `screen-job` now logs `prompt_tokens` / `completion_tokens` / `cost_usd` on every gate call (`GATE_MODEL`, default `gemini-3.5-flash-lite`) and persists the same fields on `pipeline_events.details`. Token counts come from LangChain `AIMessage.usage_metadata` (`input_tokens` / `output_tokens`) via `app/llm/usage.py`; estimated USD still uses the per-stage rates in `app/config.py`.

**Resolution path (DEF-25):** `jobmatch poc run` writes the measured distribution to [`docs/POC_RESULTS.md`](POC_RESULTS.md) and this section is updated from that live `EMBEDDING_PROVIDER=gemini` run — not from a single fixture call. Until that report has a non-zero `screen-job` `n`, both docs stay as estimates.

## 2. Recall@K eval set size vs. the 500-posting seed corpus

Evaluation Strategy specifies "a fixed corpus (a few thousand postings) with exhaustive relevance labels" for retrieval recall@K, but the PoC seed corpus is ~500 postings, and DEF-14 requires all four non-negotiable evals running. The Bootstrapping section already sanctions starting smaller ("a few hundred real postings... enough for evals 1–3 immediately").

**Resolution:** run recall@K against the ~500-posting seed as the first cut; treat the few-thousand-posting exhaustively-labeled corpus as a follow-up before scale-up. No doc change needed beyond this note.

**Harness (DEF-24):** `jobmatch evals run --suite retrieval` ships with a
tiny sample corpus in `evals/sets/v1/retrieval/` so the runner works without
a labeled seed. Replace that corpus with exhaustive labels on the ~500
seed postings before treating the number as a quality signal. The runner
warns (and can refuse with `--require-gemini-embeddings`) when
`EMBEDDING_PROVIDER=hashing`.

## 3. Queue list omits `fetch-link-list` and `match-batch`

The Queues table in Tasks and Handlers lists five queues but both Scheduler-triggered handlers (`fetch-link-list`, `match-batch`) are absent. Presumably Cloud Scheduler invokes them directly over HTTP (OIDC) without a queue in between, but this is never stated. Irrelevant locally (the local `TaskQueue` posts to handlers directly); decide and document before the Terraform work.

**Deterministic task names (deferred).** Docs used to describe named-task redelivery-dedup as current behavior. Neither `LocalTaskQueue` nor `CloudTasksQueue` sets a task name; correctness rests on handler idempotency (`extracted_at IS NULL`, `skipped_screened`, `skipped_existing`) plus in-process `job_id` dedup in `match-batch`. Named tasks are a cost optimization, not a correctness requirement. Before Terraform: add an optional `dedup_key` to `TaskQueue.enqueue`, set `task.name` from it in `CloudTasksQueue`, no-op locally.

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

* **Embedding model** — docs fix the dimension (768) but not the model.
  **Skill-span linking:** deterministic feature-hashing embedder in
  `app/skills/embeddings.py` (`HashingEmbedder`, 768-d) is the offline
  default. Live taxonomy backfill (`scripts/load_esco.py
  --embedding-provider gemini`, defaulting to `EMBEDDING_PROVIDER`) uses
  `GeminiSpanEmbedder`: `gemini-embedding-001` via `batchEmbedContents`,
  `taskType=SEMANTIC_SIMILARITY` (symmetric span↔label — not the document
  embedder's `RETRIEVAL_DOCUMENT`), Matryoshka-truncated to 768 and
  L2-normalized. Stored rows record `skills.embedding_model` so a
  an   interrupted backfill can skip already-embedded concepts. Runtime linker
  builders (`app/skills/factory.py`, used by profile ingest / extract-job /
  generate / verify) pick the span embedder from `EMBEDDING_PROVIDER` and
  trust stored vectors only when `skills.embedding_model` matches; a
  mismatch falls back to in-memory hashing so tests and offline runs stay
  on the hashing space. Similarity uses a two-tier rule: link when
  `best >= high_confidence` regardless of margin, otherwise require
  `best >= threshold` and `best - second >= margin`. Hashing defaults
  stay `0.72 / 0.72 / 0` (back-compat). Gemini defaults are **measured**
  (2026-08-18, `scripts/calibrate_link_threshold.py --provider gemini`
  over the skill_linking eval labels plus
  `evals/sets/v1/skill_linking/calibration_spans.json` negatives):
  `high_confidence 0.90 / threshold 0.85 / margin 0.05`. The
  `gemini-embedding-001` SEMANTIC_SIMILARITY score space is compressed —
  true paraphrase positives scored 0.867–0.958 (median 0.921) while
  must-refuse spans (generic terms over sibling-dense neighborhoods:
  "relational databases", "a modern JavaScript framework") reached 0.896
  with margins ≤ 0.043 — so the cutoffs sit on a narrow ledge; re-run the
  calibration script whenever the embedding model or the taxonomy changes.
  Override via `SKILL_LINK_HIGH_CONFIDENCE` / `SKILL_LINK_THRESHOLD` /
  `SKILL_LINK_MARGIN`.
  **Job/profile documents (DEF-20, updated 2026-08):** Google
  `gemini-embedding-001` Matryoshka-truncated to 768
  (`outputDimensionality=768`, L2-normalized client-side because reduced-dim
  vectors come back unnormalized) when `EMBEDDING_PROVIDER=gemini`. This is
  the setting for retrieval-quality evals. The original DEF-20 pick,
  `text-embedding-004`, was **shut down by Google on 2026-01-14**.
  `gemini-embedding-001` is also available on Vertex AI, which keeps the
  later Canada-residency move (Vertex `northamerica-northeast1`) an
  auth/endpoint change rather than a re-embed. Offline default is
  `EMBEDDING_PROVIDER=hashing` (same `HashingEmbedder`) so extract-job can
  write vectors without an API key — not for matching quality. Job and
  profile documents must use the same provider — the two spaces are not
  comparable. Switching providers later would require re-embedding;
  `extracted_at` is a permanent cache.
  Profile ingest shares the extract-job `DocumentEmbedder` (see §7).
  **Extraction LLM:** `gemini-3.5-flash-lite` (configurable via
  `EXTRACTION_MODEL`). Current GA budget tier; the earlier pick
  `gemini-2.5-flash-lite` retires with the 2.5 series (~Oct 2026). No
  residency/ZDR constraint for postings.
* **Skill taxonomy** — **ESCO** chosen for the PoC (CSV distribution + public
  API; see `scripts/load_esco.py` and README). Linker stays behind
  `app/skills.SkillLinker` with no ESCO types outside the loader. O*NET remains
  the named alternative. The profile CLI and tests fall back to a small
  in-repo seed (`app/skills/taxonomy.py`, `esco:<slug>` placeholder IDs, pure
  data) when the `skills` table is empty; swapping the seed for the loaded
  ESCO CSV is a data change, not a call-site change. **`extract-job` does not
  get the seed fallback:** a loaded `skills` table is a hard prerequisite — an
  empty table is a retryable config error checked before the LLM call
  (`TASKS_AND_HANDLERS.md`, extract-job), because extraction results are
  cached permanently and would otherwise be skill-less forever. The
  `jobmatch poc run` live path fails fast on the same check.
* **Migration tooling** — docs say "schema migration" without naming a tool; Alembic is the default for a FastAPI/Postgres stack.

## 7. Profile ingest LLM/embedding choices (PoC)

The profile-ingest branch originally picked OpenAI `text-embedding-3-small` (`dimensions=768`) here. That pick was **superseded at merge time** by the DEF-20 decision recorded in §6 (now `gemini-embedding-001` @ 768 / `EMBEDDING_PROVIDER=hashing` offline), one provider shared by job and profile documents (the two vector spaces are not comparable across providers).

Where profile ingest landed after the merge:

* **Embeddings:** profile documents go through the same `DocumentEmbedder` as `extract-job` (`app/extract/embed.py`), so the shared-provider invariant is enforced by construction. `EMBEDDING_PROVIDER=hashing` (default) writes deterministic 768-d vectors offline; that stand-in is **not** for matching quality. `gemini-embedding-001` caps input at 2,048 tokens and truncates silently, so the synthesized profile doc is trimmed to that budget (oldest roles' bullets dropped first; job docs already cap at 500) and the embedder logs an error if an over-cap doc ever slips through.
* **Parse LLM:** Gemini via the same `LLM_API_KEY` / `LLM_API_BASE` as extraction (`PROFILE_PARSE_MODEL`, default `gemini-3.5-flash-lite`), with an offline structured parser as `PROFILE_PARSER=fallback`. Unlike job postings, **resume text is personal information** — ZDR/no-training vendor terms (docs/PRIVACY_AND_COMPLIANCE.md) are a production blocker for any parse vendor; until then the fallback parser is the safe default for real resumes.
* **Skill linking:** the shared `app/skills` linker over the `skills` table (provider-aware builder in `app/skills/factory.py`; see §6), with the in-repo seed fallback described in §6.

## 8. generate-resume / verify-resume model split (DEF-23)

Docs require a **different model family** for verify stages 2–3 than the generator. The repo was Gemini-only; the PoC split is:

* **Generator:** `GENERATION_MODEL` default `gemini-3.1-pro-preview` (same `LLM_API_KEY` / `LLM_API_BASE` as extract/profile). Best-available Gemini pro tier the API actually serves — `gemini-3.5-pro` does not exist as of Aug 2026 (the 3.5 family tops out at flash). Note: free-tier API keys have zero quota for pro-tier models (429 `limit: 0`) and `gemini-2.5-pro` is closed to new users (404); on a free-tier key set `GENERATION_MODEL=gemini-3.5-flash`, the best model such keys can call. Free-tier `gemini-3.5-flash` is capped at 20 generate requests (metric `generate_content_free_tier_requests`, observed Aug 2026) — one fabrication-suite run costs 5, so budget roughly two full eval runs plus one small pipeline drain per day, or use a paid key for measurement runs. Generation pricing config (`generation_*_usd_per_mtok`) reflects pro-tier list prices and overstates flash costs. ZDR/no-training terms are a production blocker (privacy doc). Work-history prompt caching uses Gemini `cachedContents` when the prefix is long enough, otherwise an implicit identical prefix.
* **Verifier:** `VERIFY_MODEL` default `claude-sonnet-4-5` (`VERIFY_API_KEY` or `ANTHROPIC_API_KEY`, `VERIFY_API_BASE`). Anthropic is a different family, which is the load-bearing requirement. ZDR paperwork is equally deferred — do not send real resumes until those terms exist.
* Token/cost rates live in `app/config.py` and are logged on every call (`OPEN_ISSUES.md` §1). Defaults are list-price placeholders, not measured. Completion transport is LangChain (`app/llm/`); verify stages 2–3 use `ChatAnthropic.with_structured_output` (tool-calling under the hood), which is stricter than the previous free-form JSON parse.

Self-verification within Gemini is intentionally not offered as a fallback: a missing Anthropic key is a retryable config error, not a silent downgrade to the generator family.

## 9. ESCO hierarchy / subsumption-aware skill buckets (deferred follow-up)

Generic↔specific skill matching is not implemented: a JD asking for
"relational databases" is satisfied by PostgreSQL on a profile, but not vice
versa. Doing this properly needs the ESCO broader/narrower relations loaded
(the current loader takes only the flat skill concepts) and a subsumption rule
in the `skill_buckets` logic: a job skill counts as matched when the profile
holds it **or a descendant**; the converse direction (profile generic, job
specific) is adjacent at best. Until then, generic-vs-specific pairs land in
the **missing** bucket — conservative and fabrication-safe, but it understates
matches.

Two invariants make the future layer possible; keep them:

* The linker must canonicalize generic spans to **generic concepts, never to a
  specific product** — the calibrated margin rule enforces this by refusing
  near-tied sibling neighborhoods (MySQL vs PostgreSQL for an ambiguous span),
  and `calibration_spans.json` pins it with `skill_id: null` negatives.
* Docker / Kubernetes / Terraform and similar tools have **no ESCO concept at
  all** (checked against the 2026 skills pillar); they stay unlinked until a
  taxonomy supplement is designed. That is documented behavior, not a linking
  regression. See §12 for the recall-ceiling this creates on `matched_skills`.

## 10. User deletion / anonymization path is unimplemented

`docs/PRIVACY_AND_COMPLIANCE.md` ("Deletion — design for it now") requires a cascade that strips or deletes user-side rows, including `pipeline_events` linkage. The schema is ready — `pipeline_events.user_id` is nullable with no FK (DEF-16, §4) so anonymization can null the column without deleting the training row — but there is no delete or anonymize path in the app. Fine for the local PoC (no real user data). A working path is required before any real resume is stored.

## 11. Deferred from the local UI milestone

The local UI cut (`docs/UI.md`, "Local UI milestone") ships the match feed, profile editor, generation handoff, and admin dashboard, but defers two designed features that need decisions before v1:

* **PDF / docx resume export.** The handoff screen offers markdown download and a paste-ready context block only. Real export needs the templating decision already listed in UI.md's open questions (do users choose a template, or do we impose one?) plus a rendering dependency; don't pick a library before picking a templating answer.
* **Email digest one-click outcome links.** The digest's one-click buttons ("got interview" / "rejected" on applied jobs) are the highest-yield placement for the most valuable label, but they require an authenticated-by-token link design — a signed, expiring, single-purpose token that can write one `outcome` event without a session. That token scheme (scope, expiry, revocation on account deletion) is undesigned; the local milestone has no auth at all, so nothing here constrains it yet. Design it together with the auth/session work, not before.

## 12. ESCO coverage gaps cap `matched_skills` recall

Official ESCO has no concept for much of the modern software stack (Docker, Kubernetes, AWS, GCP, React, GraphQL, Rust, Terraform — see `data/esco/README.md`). Those spans drop on both job and profile sides, so Jaccard overlap (30% of the local rerank score) and `matched_skills` have a recall ceiling even after compound-span splitting. A curated taxonomy supplement (local IDs that do not collide with ESCO URIs) is the future option; do not invent speculative links.

`docker-compose.yml` loads the host `.env` via `env_file` so in-container handlers inherit `EMBEDDING_PROVIDER` / `LLM_API_KEY`. Compose `environment:` still overrides `DATABASE_URL` / `QUEUE_IMPL` / `LOCAL_QUEUE_BASE_URL`. Without that `env_file`, the container defaulted to `EMBEDDING_PROVIDER=hashing`, distrusted the stored `gemini-embedding-001` taxonomy vectors, and zeroed the similarity fallback — which produced tiny disjoint skill sets on the first local run.

## 13. `matches` table should be renamed (`match_results`)

The `matches` table stores per-cycle **evaluation snapshots** — `(user, job)` plus `cycle_at`, rerank score, qualification label, and skill buckets computed against the profile as it existed at that moment. Rows accumulate: a dirty rematch after a profile edit inserts a fresh row per job and retains superseded ones, and "the current match" is a derived concept (latest row per job — the read-side rule in `docs/UI.md`, API layer). The name `matches` misreads as a unique user↔job relationship and has already caused confusion (a 2026-08-19 debug session traced duplicate match-feed cards to exactly this misunderstanding).

**Decision (owner, 2026-08-19):** rename to `match_results`. ("`match_evaluations`" was rejected — "evaluations" already means the eval suite in this repo.) Purely mechanical (Alembic rename + model/query/docs sweep), but it touches most of the pipeline, so do it as its own change, ideally after DEF-35 to avoid churning the same files twice.

## 14. Generations must decouple from match rows (DEF-35, planned)

**Decision (owner, 2026-08-19):** a generated resume is a first-class entity owned by `(user, job)`, not a child of one match evaluation. Today `generations.match_id` is `NOT NULL ON DELETE CASCADE`, so deleting a superseded match row would destroy paid-for LLM output. Rationale: resumes are expensive and must outlive stale/deleted match results; they attach to the job they target; and they may later be reused or user-edited.

Also decided: the match result stops being a hard generation input. Skill buckets are cheap (`app/match/skills.py`, pure local set logic, no LLM), so **generate-resume uses the provided match's buckets when available and recomputes them itself when not** — the `missing_skills` bucket must always be present either way (it is the anti-fabrication input, hard rule 1).

Implementation plan lives in Linear **DEF-35**: `generations` gains `user_id` (FK users, CASCADE — preserves the §10 deletion path) and `job_id` (FK jobs, SET NULL); `match_id` becomes nullable SET-NULL provenance. One open point is flagged in the issue for owner confirmation: keying "skip if a generation already exists" on `(user, job)` instead of match id, which stops rematch cycles from auto-re-spending quota on already-generated jobs.

## 15. Qualification-label eval set (future work)

DEF-30 replaced the binary gate verdict with an ordinal qualification label. `docs/EVALUATION.md` §7 now describes a label/ranking eval (confusion matrix vs. human judgment, adjacent-tier vs. inversion errors). There is no labeled set yet — the current `jobmatch evals run` suites (extraction, skill linking, retrieval, fabrication) do not cover the screen prompt. Add a versioned screen-label fixture set before treating prompt edits as gated by that suite. Until then, re-run the existing four suites on screen-prompt changes (operational discipline) and rely on `rank_label_disagreement` events plus the owner's live review.

## 16. Qualification report — logistics axes surfaced separately (future work)

**Decision (owner, 2026-08-19):** the screen's qualification label measures qualification fit only — skills, experience, domain, seniority. Logistics (location, relocation, work authorization, work arrangement, timezone, comp, start date) are separate axes and must not move the label or drive `screen_reason`. Trigger: a live screen labeled a strong-overlap match `unqualified` with the reason "the candidate is not currently based in the San Francisco Bay Area or New York City as required by the role." The gate prompt (`app/screen/llm.py`) and the screen-job doc now say this explicitly; `docs/EVALUATION.md` §7 counts logistics-lowered labels as rubric violations.

Logistics mismatches still carry real information the user should see — the prefilter only catches what `user_filters` constrains, so an in-role, wrong-city posting legitimately reaches the screen. **Future work:** a comprehensive per-match "qualification report" that presents the fit judgment and the logistics axes side by side (e.g. qualification label + a location/arrangement/auth/comp checklist), instead of collapsing everything into one label and one sentence. Undesigned; no schema or UI commitment yet. Until it exists, logistics mismatches simply don't appear in the screen output.
