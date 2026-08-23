# Tasks and Handlers

## Conventions

* Every handler is an idempotent HTTP POST endpoint.
* Handlers are dispatched by Cloud Tasks with an OIDC token; none are publicly reachable.
* **Return 2xx on permanent failure** (after logging). Only 5xx for retryable errors — a poison message that returns 5xx will retry to `maxAttempts` and burn LLM spend on every attempt.
* **LLM failure classification** (shared plumbing in `app/llm/`): LangChain/SDK transport errors, 408/429/5xx are retryable (5xx). 401/403/404 are operator config errors — bad key or model name — that affect every task and bill no tokens, so they also stay retryable rather than dropping work as permanent. Any other request-level 4xx is a poison payload → permanent (2xx, `llm_permanent_failure` event). A billed-but-malformed completion (schema parse failure, empty structured output) gets **one in-process retry**, then goes permanent — temperature is 0, so queue-level retries would pay full price for the same bad output. Chat models are constructed with `max_retries=0` so LangChain does not multiply spend or defeat this queue accounting.
* **Within-handler vs cross-handler orchestration.** `TaskQueue` is still the cross-handler orchestrator (at-least-once delivery, idempotency, the 2xx/5xx contract, verify's regenerate-once enqueue of `generate-resume`). LangGraph runs *inside* a handler. The first graph is `verify-resume` (`app/verify/graph.py`: deterministic → JD-blind grounding → coverage → pass / regenerate / needs_review). Future multi-step LLM workflows belong in the same pattern, not as a replacement for the queue.
* **Follow-on enqueues dispatch only after the handler's transaction commits.** Handlers wrap the queue in `BufferedTaskQueue` and flush after `session.commit()`. The local queue delivers from a background thread immediately, so an enqueue mid-transaction can race the commit: the child handler sees `not_found`, returns a permanent 2xx, and the stage is silently lost. If the transaction fails nothing is dispatched — redelivery of the parent task redoes the work idempotently.
* Deterministic Cloud Tasks names (hash of the natural key) are the target redelivery-dedup design; neither queue implementation sets a task name yet (`docs/OPEN_ISSUES.md` §3). Handler idempotency is the current guard.
* Every handler writes a row to `pipeline_events` regardless of outcome.

## Queues

One Cloud Tasks queue per job type, each rate-limited independently. These are the primary cost and backpressure controls.

| Queue | Rate limit driver |
| -- | -- |
| `ingest-job` | Source job-board API rate limit |
| `extract-job` | Cheap-model LLM rate limit; bursty on new-user onboarding |
| `screen-job` | Cheap-model LLM rate limit |
| `generate-resume` | Frontier-model rate limit; low volume |
| `verify-resume` | Follows generate |

Set `max-concurrent-dispatches` in line with the Cloud SQL connection budget, not just the API limits.

---

## Handlers

### `fetch-link-list`

**Trigger:** Cloud Scheduler
**Volume:** a few per day

* Pull posting index/links + metadata from source API
* Filter out URL hashes already present
* Enqueue one `ingest-job` per new posting

**Fan-out note:** Cloud Tasks has no batch-create — one API call per task. Fire concurrently (async client), not serially. For large fan-outs use two levels: enqueue N chunk tasks, each enqueuing its own leaf tasks.

---

### `ingest-job`

**Trigger:** `fetch-link-list`
**Volume:** ~10k–25k/day distinct new postings
**Scales with:** jobs
**Cost: no LLM calls.** See `extract-job` below.

1. Fetch JD content. Several ATS providers return full content inline on the list endpoint (e.g. Greenhouse `?content=true`), skipping this fetch entirely.
2. Normalize layout, strip boilerplate
3. Store **raw JD** plus the structured fields the ATS already provides — title, location, department, employment type, sometimes comp. Also store `raw_jd_html`: a sanitized HTML copy of the ATS description, **display-only**. Sanitization runs once in `ingest_posting` (`nh3`; structural tags only). `raw_jd` stays plain text and is the only input to prompts, the heuristic extractor, embeddings, and the length gate.
4. Upsert on `url_hash` (dedup; at-least-once delivery makes duplicates certain). The conflict update refreshes ATS metadata, raw JD, and `raw_jd_html` but **never `ingested_at`** — a redelivered or re-fetched posting must not look new to the incremental `match-batch` predicate (`ingested_at > last_cycle`), or every re-seen posting would re-drive the paid extract → screen → generate funnel.

**No LLM extraction here.** Deliberate — see the note below.

**Permanent-failure cases** (log, return 2xx): dead link, non-job page, unparseable content, posting already expired.

---

### `extract-job`

**Trigger:** `match-batch`, on first prefilter hit for a job
**Volume:** only jobs that pass at least one user's metadata prefilter
**Scales with:** users early, plateaus at O(jobs) as coverage saturates

1. **LLM structured extraction** → seniority, hard requirements, nice-to-haves, comp range, work arrangement, raw skill spans
2. **Link skill spans to canonical taxonomy** (ESCO/O*NET) → `skill_id[]`
3. Build synthesized compact document
4. Embed the synthesized document
5. Write back to the job row; **cached permanently**

**PoC implementation** (`POST /handlers/extract-job` with `{job_id}`):

* Extraction model: `gemini-3.5-flash-lite` (current GA budget tier; postings are not personal information — no residency/ZDR constraint). Prompt calls out hard vs nice-to-have explicitly — that split drives the deterministic gate (Evaluation eval 1). `skill_spans` items must each be a single skill (prompt change 2026-08-18; eval suite not yet in place — re-run when `docs/EVALUATION.md` Operational discipline applies).
* Skill spans go through `app.skills.SkillLinker` only (no string matching at the handler). Compound spans (commas, slashes, semicolons, "and" / "or") are split in the shared linker before exact/alias/similarity linking so a packed list still yields one id per fragment.
* **A loaded skills taxonomy is a hard prerequisite.** If the `skills` table is empty, extract-job refuses with a retryable config error (503, `skills_taxonomy_missing` event) — checked *before* the LLM call, so nothing is spent. Extraction against an empty table would cache a permanently skill-less record (`extracted_at` never resets), silently breaking hard-requirement overlap and the matched/adjacent/missing buckets. Run `python -m scripts.load_esco` first; `match-batch` re-enqueues the job on a later cycle once the load is done.
* Synthesized doc is title + seniority + canonical skill labels + hard requirements + comp, clipped to one ~500-token rerank chunk (`ARCHITECTURE.md` §3).
* Document embedding is 768-d. Default `EMBEDDING_PROVIDER=hashing` (offline); `gemini` uses `gemini-embedding-001` truncated to 768 and is the setting for retrieval-quality evals. Job and profile docs must share one provider — see `OPEN_ISSUES.md` §6.
* Every call logs real `prompt_tokens` / `completion_tokens` / estimated `cost_usd` (Cost Model measurement caution; needed for `OPEN_ISSUES.md` §1). JD text is never logged. Successful `extracted` events also store `skill_spans`, `linked_skill_ids`, and `unlinked_spans` in `pipeline_events.details` (job postings are not personal information) so a thin skill set is diagnosable without a live DB dump.
* Permanent failures (missing/invalid `job_id`, unknown job, empty/unparseable JD, permanent LLM/embed failure per the conventions classification): log + `pipeline_events` + 2xx. Retryable LLM/embed errors: `pipeline_events` + 5xx.
* Idempotency: skip when `extracted_at IS NOT NULL`; write-back is `UPDATE … WHERE extracted_at IS NULL`.

#### Why extraction is lazy

An earlier design extracted every posting at ingest. Moving it behind the prefilter is a large early-stage cost reduction:

* Metadata prefilters run on ATS-provided fields, so they don't need extraction to work
* The first user to match a job pays; every user after gets it free
* At small user counts only a few percent of the corpus is ever extracted — roughly $25/mo rather than $470/mo (see Cost Model)
* Cost scales with users early, then converges to O(jobs) as the user base covers more of the corpus

**Trade-offs accepted:**

* Extraction lands in the match path, adding seconds of latency on first hit
* Canonical skills can't be used in the *prefilter* for not-yet-extracted jobs — only in reranking, after extraction. Prefilters therefore rely on ATS metadata plus title/text matching.

**Idempotency:** guard on `extracted_at IS NULL`. Multiple users matching the same job in one cycle must not trigger multiple extractions — `match-batch` enqueues `extract-job` once per distinct `job_id` in-process. Deterministic Cloud Tasks names on `job_id` are the target cloud-path dedup (`docs/OPEN_ISSUES.md` §3), not current behavior.

---

### `match-batch`

**Triggers:** two, on separate schedules
**Volume:** 1 task per cycle (not per job, not per user-job pair)
**Scales with:** users

| Trigger | Cadence | Scope |
| -- | -- | -- |
| New postings ingested | ~5 min | Incremental — jobs since last cycle, all active users |
| Dirty profiles | Hourly (or on demand) | Full corpus scan, only profiles flagged dirty |

Both run the same matching SQL; only the job-side predicate differs (`ingested_at > last_cycle` vs. no date bound).

#### Why the profile path is scheduled, not triggered

A profile edit invalidates the cached context block and requires re-matching against the whole corpus. Firing that on the edit itself is wrong: it turns a UI action into a cost event, and under lazy extraction a full-corpus scan can trigger a burst of `extract-job` tasks.

Instead, a profile edit sets a **dirty flag** (`user_profiles.rematch_needed`) and returns. The scheduled run picks it up. No debounce logic, no coalescing — and it handles the case debouncing would not, where a user edits across several separate sessions and each edit looks final.

**Rate-limit the profile path separately.** It carries the extraction burst; new-job matching does not. Cap dirty profiles processed per run so a wave of profile edits (or a batch of new signups) can't spike extraction spend.

The percolator pattern, batched. A single SQL join between candidate jobs and user filter rows:

1. Join `jobs` against `user_filters` on **ATS-provided metadata** — location, work arrangement, comp floor, title family, and seniority band. Location / arrangement / comp / title work without extraction. Seniority is NULL until `extract-job`, so unextracted jobs still pass (the predicate only bites on the post-extraction recall cycle).
2. For prefilter survivors with `extracted_at IS NULL`, enqueue `extract-job` and defer them to the next cycle
3. For extracted jobs: canonical skill-set overlap as a scored feature
4. Vector similarity between user profile embedding and job embedding
5. Rerank surviving candidates (compact synthesized docs, one chunk each)
6. Take top-N per user, subject to the daily candidate cap
7. Enqueue one `screen-job` per survivor
8. Clear `rematch_needed` for profiles processed on the dirty path

Step 2 means a newly-matched job takes two cycles (~10 min) to reach screening the first time. Acceptable for job postings; every subsequent user matching that job gets it in one cycle.

**Why batched rather than per-job:** per-job push means 10k tasks each loading all user filters. A 5-minute batch gets effectively the same latency with one query.

**Why the pull path exists regardless:** the same SQL must run on demand for (a) profile edits, (b) new user backfill over the full corpus, (c) any change to matching logic. Push-only bakes decisions in at ingest time and makes A/B testing the matcher impossible.

**PoC implementation** (`POST /handlers/match-batch` with `{mode: incremental|dirty}`):

* No Cloud Scheduler. `jobmatch match run --mode incremental|dirty` POSTs to the handler (`LOCAL_QUEUE_BASE_URL`). Optional payload `user_ids` scopes a cycle to specific profiles (debug / tests).
* Same SQL for both modes (`app/match/sql.py`): ATS metadata join on location (substring), work arrangement, comp floor, title-family token, and seniority band, plus pgvector cosine in the same statement. Seniority only applies once `jobs.seniority` is set (extracted jobs); a NULL job-side seniority still passes. Incremental adds `ingested_at > last_cycle OR extracted_at > last_cycle` so a job extracted after cycle N is recalled in cycle N+1. Dirty drops the date bound. Incremental mode's "all active users" means users with a `user_filters` row (profile ingest always writes one).
* Last-cycle watermark is `max(pipeline_events.ts)` for `stage=match-batch` / `action=completed`. Override with `since` in the payload.
* Unextracted prefilter survivors enqueue `extract-job` once per distinct `job_id` in the handler (TaskQueue has no named-task dedup) and are not matched this cycle.
* Skill overlap is a Jaccard feature blended into the local rerank score (0.7 cosine + 0.3 Jaccard). Matched / adjacent / missing buckets are written onto `matches` (adjacency is a small label-sibling table — including SQL ↔ PostgreSQL/MySQL/SQLite — until ESCO hierarchy is loaded).
* Reranker is behind `app.match.Reranker`: `RERANK_PROVIDER=local` (default, embedding cosine) or `hosted` (Cohere-compatible HTTP API, cosine fallback on failure).
* Top-N per user (`MATCH_TOP_N`, default 100) is also clipped by the remaining daily candidate cap (`DAILY_CANDIDATE_CAP`, default 500).
* Dirty mode selects `user_profiles.rematch_needed` up to `DIRTY_PROFILE_CAP` (default 25), processes the full corpus for those users, then clears the flag.
* Each survivor writes a `matches` row. `screen-job` is enqueued most-promising-first (rerank order) when `rerank_score` meets `SCREEN_SCORE_FLOOR` (unset = screen every Top-N survivor). Below-floor matches stay in the list with a NULL label and a `below_screen_floor` event. Cycle-level and per-pair `pipeline_events` are always written. Profile/resume text is never logged.
* Match rows accumulate: a later cycle (notably a dirty rematch) writes a fresh row per `(user, job)` and does **not** delete or invalidate earlier ones — generations and events hang off them. Superseded rows are hidden at read time: the user-facing `GET /api/matches` returns only the latest match row per job (see `docs/UI.md`, API layer).
* Meaningful vector recall needs `EMBEDDING_PROVIDER=gemini` (same provider as profile ingest). The hashing default is plumbing-only (`docs/OPEN_ISSUES.md` §6).

---

### `screen-job`

**Trigger:** `match-batch` (most-promising-first; skipped below `SCREEN_SCORE_FLOOR`)
**Volume:** ~100/user/day, bounded by Top-N, daily candidate cap, and the score floor

**Stage 1 — deterministic overlap (no LLM).** Both sides are canonicalized, so hard-requirement overlap is a set operation. The missing count is recorded on the event. It does **not** hard-reject — only the metadata prefilter drops candidates.

**Stage 2 — cheap LLM screen.** Condensed JD + condensed profile, small model, structured output:

```json
{ "label": "unqualified|minimally_qualified|overqualified|potentially_qualified|clearly_qualified", "reason": "...", "confidence": 0.0 }
```

The label is a ranking signal, not a verdict. `confidence` is logged, not persisted. Ranking in the match feed is label tier first (clearly_qualified highest; NULL / unscreened last), then `rerank_score` within a tier.

**The label measures qualification fit only** — skills, experience, domain, seniority. Logistics (location, relocation, work authorization, work arrangement, timezone, comp, start date) are separate axes: the prompt instructs the model that they must not move the label or drive the reason. Location and comp preferences are the prefilter's job (`user_filters`); logistics mismatches that survive it belong in the planned qualification report, not the label (`docs/OPEN_ISSUES.md` §16).

**Placement matters:** this is a *separate* call, not a judgment embedded in resume generation. Aborting inside generation means the ~8k input tokens are already paid — you save only output, roughly 45%. A separate cheap screen call saves the full generation cost when we choose not to auto-generate. The pre-measurement estimate here was ~$0.005/call; **use the measured mean in [`docs/POC_RESULTS.md`](POC_RESULTS.md)** (this figure is what `docs/OPEN_ISSUES.md` §1 is about).

On `clearly_qualified` **and** remaining quota → enqueue `generate-resume`. Other labels stay on the ranked list for the user to triage. The user can also trigger generation from the UI (`POST /api/matches/{id}/generate`); that path consumes quota too.

**PoC implementation** (`POST /handlers/screen-job` with `{match_id}` from `match-batch`):

* **Stage 1** is set-math on canonical IDs: `jobs.skill_ids` vs `user_profiles.skill_ids` (`app.screen.hard_requirement_overlap`). Extract does not yet write a separate hard-requirement id list, so the PoC uses the job's linked skill set. Missing count is logged and returned; it never auto-drops.
* **Stage 2** sends `jobs.synthesized_doc` + `user_profiles.synthesized_doc` to `GATE_MODEL` (default `gemini-3.5-flash-lite`). Structured output `{label, reason, confidence}` with an explicit rubric per label. Condensed profile is personal information — prompt/completion text is never logged; retryable errors are stripped of upstream args. Missing condensed docs write `missing_docs` and leave the label NULL (no fabricated label).
* `qualification_label` / `screen_reason` are written with `UPDATE … WHERE qualification_label IS NULL`. Redelivery of an already-screened match is a no-op (`skipped_screened`) and does not decrement quota again.
* Successful screens write `pipeline_events.action = screened` with the label in `details`. `clearly_qualified` + `users.quota_remaining > 0` atomically decrements quota and enqueues `generate-resume` with `{user_id, job_id, match_id}`. `clearly_qualified` with no remaining quota is `quota_exhausted` — the label still lands on the row.
* Rank/label disagreement is logged both ways as `rank_label_disagreement`: `rerank_score >= RERANK_HIGH_SCORE_THRESHOLD` (default 0.7) with `unqualified` / `minimally_qualified`, or `rerank_score <= RERANK_LOW_SCORE_THRESHOLD` (default 0.3) with `clearly_qualified`. That is the feedback-loop signal (`EVALUATION.md` operational discipline).
* Every screen LLM call logs real `prompt_tokens` / `completion_tokens` / estimated `cost_usd` (needed for `OPEN_ISSUES.md` §1). Permanent failures (missing/invalid `match_id`, unknown match): 2xx. Retryable LLM errors: `pipeline_events` + 5xx. A permanent screen LLM failure returns 2xx (`llm_permanent_failure`) and leaves `qualification_label` NULL — no fabricated label, and the match stays screenable if re-driven.

---

### `generate-resume`

**Trigger:** `screen-job` on `clearly_qualified` (auto, quota-gated), or `POST /api/matches/{id}/generate` (manual, same quota)
**Volume:** tens per user per month (tier-capped)

Input assembled as **three explicit skill buckets**:

| Bucket | Meaning | Instruction |
| -- | -- | -- |
| **Matched** | User has it, JD wants it | Surface prominently, use JD's phrasing |
| **Adjacent** | User has taxonomy sibling/parent (JD: Terraform, user: CloudFormation) | Frame the bridge honestly |
| **Missing** | JD wants it, user lacks it | **Do not invent under any circumstances** |

The missing bucket does the most work. Without an explicit list, a model asked to "optimize this resume for this job" will manufacture experience. Naming the gaps gives it something concrete not to do.

**No find-replace on skill terms.** The taxonomy maps canonical entities, but surface forms carry meaning the mapping discards — "Python scripting" → "Python development" changes the claim; "Deployed to Kubernetes" and "Managed Kubernetes clusters" link to the same node with very different seniority signals. Pass terminology as *context* ("canonical skill X — JD says 'AWS', resume says 'Amazon Web Services'") and let the model choose. The form that satisfies both without substitution: **"AWS (Amazon Web Services)"**.

**Prompt caching:** the user's work history block is identical across every resume they generate; only the JD varies. Cache it. This is worth more than the abort optimization and compounds with it.

**Output:** resume + claim → source-span ID mapping (required by verification).

**PoC implementation** (`POST /handlers/generate-resume` with `{user_id, job_id, match_id}` from `screen-job` or the generate API, plus `attempt` / `violations` on a single regenerate):

* Generation model: `GENERATION_MODEL` (default `gemini-3.1-pro-preview`). Resume text is personal information — ZDR/no-training vendor terms apply (`docs/PRIVACY_AND_COMPLIANCE.md`); paperwork is deferred. Prompt/completion text is never logged.
* Input is assembled as the three match buckets (`matched_skills` / `adjacent_skills` / `missing_skills`) plus terminology context (canonical label, JD surface form, resume surface form). No find-replace on skill terms.
* Job context prefers `jobs.raw_jd` and falls back to `jobs.synthesized_doc`. Compact synth docs are for rerank (`ARCHITECTURE.md` §3); generation is low-volume and needs JD surface forms for the "use the JD's phrasing" instruction. Token counts and estimated cost are logged on every call.
* The work-history block is a stable prefix keyed on `user_id` + `profile_version`. Gemini explicit `cachedContents` is attempted; short prefixes and unsupported models fall back to implicit prefix caching (identical first part, JD-only suffix).
* Structured output is `resume_doc` plus a `claim_source_map` (`claims[]` with `span_ids` from profile ingest, plus employers / titles / date ranges / `claimed_skill_ids`). Stored on `generations`.
* Idempotency: redelivery of the same `attempt` is a no-op (`skipped_existing`). `attempt` is at most 2 (verify-resume regenerates once).
* On success enqueues `verify-resume` with `{generation_id, match_id, attempt}`. Permanent failures (missing/invalid `match_id`, unknown match/profile, permanent LLM failure — no generation row written): 2xx. Retryable LLM errors: `pipeline_events` + 5xx. Token counts and estimated cost are logged on every call.

---

### `verify-resume`

**Trigger:** `generate-resume`

**Stage 1 — deterministic checks (no LLM).** Using the claim → source-span map:

* Employers, titles, date ranges: exact set membership against source
* **All numbers** — years, team size, percentages, dollar figures — must exist in source. Regex-checkable, and where fabrication does the most damage in an interview.
* Canonical skills in output ⊆ user's linked skill set

**Stage 2 — grounding check (LLM, JD-blind).** Resume vs. work history only. The JD is deliberately withheld: a verifier that can see the target is biased toward approving, because an invented "5 years Kubernetes" reads as *correct* rather than *unsupported*.

**Stage 3 — coverage check (LLM, JD-aware).** Did anything relevant get dropped or under-weighted in generation?

Stages 2 and 3 must be separate calls — one call cannot do both honestly. Use a **different model family than the generator**; self-verification within a family is weak at catching its own confabulations.

Failure → regenerate once with the specific violations named, then flag for human review rather than looping.

**PoC implementation** (`POST /handlers/verify-resume` with `{generation_id, match_id, attempt}` from `generate-resume`):

* **Stage 1** is set membership / regex, no LLM: claimed employers, titles, and date ranges must be in the source work-history sets; every number token in `resume_doc` must appear in the source (span IDs and list markers stripped first); canonical skills in the output (claim map + `SkillLinker.scan_text`) must be ⊆ `user_profiles.skill_ids`.
* **Stage 2** (JD-blind grounding) and **Stage 3** (JD-aware coverage) are separate calls on `VERIFY_MODEL` (default `claude-sonnet-4-5` via `VERIFY_API_KEY` / `ANTHROPIC_API_KEY`). Different family than the Gemini generator. The grounding prompt receives resume + work history only. The three stages and the pass / regenerate / `needs_review` decision run as a LangGraph `StateGraph` in `app/verify/graph.py`.
* Stages 2 and 3 still run when stage 1 fails so `pipeline_events` records all three signals. Each stage writes a row (`stage1_pass|fail`, `stage2_pass|fail`, `stage3_pass|fail`).
* Failure on attempt 1 writes `verify_status=failed` / `verify_failures[]` and enqueues `generate-resume` with the named violations and `attempt=2`. Failure on attempt 2 writes `needs_review` and stops — no loops.
* Redelivery of an already-verified generation is a no-op (`skipped_verified`). Permanent failures: 2xx. Retryable LLM errors: `pipeline_events` + 5xx. A **permanent verify LLM failure fails safe to `needs_review`** — an unverifiable resume is never delivered as passed and never silently dropped. Token counts and estimated cost are logged per call; resume text is never logged.

---

## Profile ingestion (PoC CLI, not a handler)

Profile parse is 1× per user and is not on the job-ingest path, so it is a CLI rather than a `/handlers/*` endpoint until the UI issue lands. `jobmatch profile ingest` writes `users`, `user_profiles`, and default `user_filters`; `jobmatch profile edit` bumps `profile_version` and sets `rematch_needed` (the scheduled `match-batch` dirty path picks it up — the edit does not enqueue work).

Each `work_history` entry carries per-entry `source: parsed | user_asserted` and every bullet has a stable `span_id` (`wh:{role}:b:{bullet}` after deterministic role sort) for `verify-resume`.

---

## Data model sketch

```
jobs
  id, url_hash (unique), url, source, ats_provider, company_id
  ingested_at, posted_at, expires_at
  -- from ATS metadata, available at ingest, drives the prefilter:
  title, location, work_arrangement, department, employment_type, comp_min, comp_max
  raw_jd
  raw_jd_html              -- sanitized at ingest; UI display only; NULL when source was plain text
  -- from extract-job, NULL until first prefilter hit:
  extracted_at
  seniority, hard_requirements[], nice_to_haves[]
  skill_ids[]              -- canonical
  synthesized_doc, embedding vector(768)

companies
  id, name, ats_provider, board_token, country, discovered_via

skills
  id                       -- opaque taxonomy id (ESCO concept URI in the PoC)
  canonical_label, alt_labels[], description
  embedding vector(768)    -- span-similarity fallback for the linker
  embedding_model          -- which model produced embedding (nullable)

users
  id, tier, quota_remaining, quota_reset_at

user_profiles
  user_id, work_history (structured JSONB; each entry includes source: parsed | user_asserted)
  skill_ids[]              -- canonical
  synthesized_doc, embedding vector(768)
  profile_version          -- cache key for context block
  rematch_needed           -- dirty flag; set on edit, cleared by match-batch

user_filters
  user_id, title_families[], locations[], comp_floor,
  seniority_band, work_arrangement[]

matches
  id, user_id, job_id, cycle_at
  rerank_score, qualification_label, screen_reason
  matched_skills[], adjacent_skills[], missing_skills[]

generations
  id, match_id, resume_doc, claim_source_map
  verify_status, verify_failures[]

pipeline_events                        -- the training set
  id, user_id (nullable, no FK — strippable for anonymization), job_id,
  stage, score, action, ts,
  details jsonb                        -- token/cost/latency; never resume/JD text
```

`pipeline_events` is not incidental logging. Every `(user, job, stage, score, action)` tuple — shown, skipped, screened, generated, applied — is the dataset for a fine-tuned person-job-fit encoder later, and the main defensible asset. Populate it from the first day of the local proof of concept.

## Feedback loop to build immediately

When the screen label disagrees with the rerank score — high rerank + low label, or low rerank + `clearly_qualified` — log `rank_label_disagreement` explicitly. It is the highest-value signal for tuning the metadata filters and rerank stage.
