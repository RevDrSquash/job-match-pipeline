# Tasks and Handlers

## Conventions

* Every handler is an idempotent HTTP POST endpoint.
* Handlers are dispatched by Cloud Tasks with an OIDC token; none are publicly reachable.
* **Return 2xx on permanent failure** (after logging). Only 5xx for retryable errors — a poison message that returns 5xx will retry to `maxAttempts` and burn LLM spend on every attempt.
* Task names are deterministic (hash of the natural key) so redelivery dedups.
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
3. Store **raw JD** plus the structured fields the ATS already provides — title, location, department, employment type, sometimes comp
4. Upsert on `url_hash` (dedup; at-least-once delivery makes duplicates certain)

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

#### Why extraction is lazy

An earlier design extracted every posting at ingest. Moving it behind the prefilter is a large early-stage cost reduction:

* Metadata prefilters run on ATS-provided fields, so they don't need extraction to work
* The first user to match a job pays; every user after gets it free
* At small user counts only a few percent of the corpus is ever extracted — roughly $25/mo rather than $470/mo (see Cost Model)
* Cost scales with users early, then converges to O(jobs) as the user base covers more of the corpus

**Trade-offs accepted:**

* Extraction lands in the match path, adding seconds of latency on first hit
* Canonical skills can't be used in the *prefilter* for not-yet-extracted jobs — only in reranking, after extraction. Prefilters therefore rely on ATS metadata plus title/text matching.

**Idempotency:** guard on `extracted_at IS NULL`. Multiple users matching the same job in one cycle must not trigger multiple extractions — dedup by deterministic task name on `job_id`.

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

1. Join `jobs` against `user_filters` on **ATS-provided metadata** — location, work arrangement, comp floor, title family. Works without extraction.
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

---

### `screen-job`

**Trigger:** `match-batch`
**Volume:** ~100/user/day

**Stage 1 — deterministic gate (no LLM).** Both sides are canonicalized, so hard-requirement overlap is a set operation. Cheaper than the cheap gate and fully explainable.

> Current policy: do **not** auto-drop on a single missing hard requirement. The threshold should become configurable once we have false-negative data.

**Stage 2 — cheap LLM gate.** Condensed JD + condensed profile, small model, structured output:

```json
{ "verdict": "pass|reject", "reason": "...", "confidence": 0.0 }
```

**Placement matters:** this is a *separate* call, not a judgment embedded in resume generation. Aborting inside generation means the ~8k input tokens are already paid — you save only output, roughly 45%. A separate ~$0.005 gate call saves the full generation cost on reject.

On pass **and** user has remaining quota → enqueue `generate-resume`.

---

### `generate-resume`

**Trigger:** `screen-job`
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

---

## Data model sketch

```
jobs
  id, url_hash (unique), url, source, ats_provider, company_id
  ingested_at, posted_at, expires_at
  -- from ATS metadata, available at ingest, drives the prefilter:
  title, location, work_arrangement, department, employment_type, comp_min, comp_max
  raw_jd
  -- from extract-job, NULL until first prefilter hit:
  extracted_at
  seniority, hard_requirements[], nice_to_haves[]
  skill_ids[]              -- canonical
  synthesized_doc, embedding vector(768)

companies
  id, name, ats_provider, board_token, country, discovered_via

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
  rerank_score, gate_verdict, gate_reason
  matched_skills[], adjacent_skills[], missing_skills[]

generations
  id, match_id, resume_doc, claim_source_map
  verify_status, verify_failures[]

pipeline_events                        -- the training set
  id, user_id (nullable, no FK — strippable for anonymization), job_id,
  stage, score, action, ts
```

`pipeline_events` is not incidental logging. Every `(user, job, stage, score, action)` tuple — shown, skipped, gate-rejected, generated, applied — is the dataset for a fine-tuned person-job-fit encoder later, and the main defensible asset. Populate it from the first day of the local proof of concept.

## Feedback loop to build immediately

When the LLM gate rejects something the reranker scored highly, log the disagreement explicitly. It is the highest-value signal for tuning the metadata filters and rerank stage, and driving the gate's rejection rate down directly cuts COGS.
