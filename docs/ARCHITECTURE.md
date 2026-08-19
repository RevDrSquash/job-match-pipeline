# Architecture

## Design principles

**Separate work that scales with jobs from work that scales with users.**
The central constraint. Ingest is O(jobs) — paid once per posting regardless of user count. Matching is O(users). Extraction is deferred so it scales with users early and converges on O(jobs). Nothing in the system may be O(jobs × users) at LLM cost.

**Cheap filters before expensive ones.** A funnel: SQL metadata filter → skill-set overlap → vector recall → reranker → LLM qualification screen → generation. Each stage is roughly an order of magnitude more expensive than the last and sees roughly an order of magnitude fewer items.

**Pay for a posting only when someone might want it.** LLM extraction runs on first prefilter hit, not at ingest, and caches permanently. Metadata prefilters run on ATS-provided fields, so nothing is blocked by deferring it. This keeps early-stage cost proportional to users rather than to the whole corpus, and converges on O(jobs) only once the user base actually covers the corpus.

**Everything replayable.** Matching decisions are queries, not baked-in results. A user editing their profile, a new user signing up, or an improvement to the matching logic all re-run the same code path.

**Never fabricate.** Verification is a first-class pipeline stage, not a post-hoc check.

## Target scale

* Steady state ingest: ~10k distinct new postings/day observed (~0.12/sec), likely ~25k under systematic coverage. See Posting Sources for how this was reconciled against a conflicting 250k/day figure.
* Fetch volume is much higher than ingest volume (re-seeing known postings), but costs only HTTP requests — URL-hash dedup keeps it out of the LLM path.
* Corpus: accumulated over months from steady-state ingest, not backfilled. There is no pre-existing 1M-posting corpus to import; the reference implementation runs a rolling freshness window (24–168h, configurable). Target ~1M as an accumulated steady state.
* Candidates surviving metadata filters: ~1% per user (~100/user/day)
* Resumes generated: tens per user per month, capped by tier

## Data flow

```
                    ┌─────────────────────────┐
                    │  Cloud Scheduler (cron) │
                    └───────────┬─────────────┘
                                │
                    ┌───────────▼─────────────┐
                    │  fetch-link-list        │  1 task
                    │  pull posting index     │
                    └───────────┬─────────────┘
                                │ fan out, 1 task per posting
                    ┌───────────▼─────────────┐
                    │  ingest-job             │  ~10-25k tasks/day
                    │  • fetch JD             │
                    │  • store raw + ATS      │  ◄── O(jobs)
                    │    metadata             │      NO LLM CALLS
                    │  • upsert (dedup on     │
                    │    URL hash)            │
                    └───────────┬─────────────┘
                                │
                         ┌──────▼──────┐
                         │  Postgres   │
                         │  + pgvector │◄──────────────┐
                         └──────┬──────┘               │
                                │                      │
                    ┌───────────▼─────────────┐        │
                    │  match-batch            │        │
                    │  SQL join on ATS        │  ◄── O(users)
                    │  metadata (no           │        │
                    │  extraction needed)     │        │
                    └───────────┬─────────────┘        │
                                │                      │
                     ┌──────────┴──────────┐           │
                     │ not yet extracted?  │           │
                     ▼                     ▼           │
        ┌─────────────────────┐   (already extracted)  │
        │  extract-job        │            │           │
        │  • LLM extraction   │  ◄── lazy: │           │
        │  • ESCO linking     │   first    │           │
        │  • embed synth doc  │   hit only ├───────────┘
        │  • cache forever    │            │
        └──────────┬──────────┘            │
                   │ (next cycle)          │
                   └───────────┬───────────┘
                               │
                    ┌──────────▼──────────────┐
                    │  rerank → top-N/user    │
                    └───────────┬─────────────┘
                                │ 1 task per (user, job) survivor
                    ┌───────────▼─────────────┐
                    │  screen-job             │
                    │  • hard-req overlap     │
                    │    (recorded, not a     │
                    │     drop)               │
                    │  • cheap LLM screen     │
                    │  → label + reason       │
                    └───────────┬─────────────┘
                                │ if clearly_qualified AND quota
                    ┌───────────▼─────────────┐
                    │  generate-resume        │
                    │  • cached profile block │
                    │  • skill mapping        │
                    │    (matched/adjacent/   │
                    │     missing)            │
                    └───────────┬─────────────┘
                                │
                    ┌───────────▼─────────────┐
                    │  verify-resume          │
                    │  • deterministic checks │
                    │  • grounding check      │
                    │    (JD-blind)           │
                    │  • coverage check       │
                    └───────────┬─────────────┘
                                │
                    ┌───────────▼─────────────┐
                    │  Delivered to user      │
                    │  resume + match report  │
                    │  + paste-ready block    │
                    └─────────────────────────┘
```

## The matching approach

Off-the-shelf embedding similarity between a resume and a job description performs poorly. Embedding models are trained for short-query → passage retrieval; this is long-document → long-document and asymmetric. Research on person-job fit shows fine-tuned contrastive encoders beating BM25 and generic dense retrieval by 20–30% absolute, which indicates how much the naive approach leaves on the table.

Our approach compensates structurally:

### 1. Symmetric representation

Both sides are transformed into the same shape before comparison. The user's work history is synthesized into a job-description-shaped document; postings are reduced to a compact structured record. This mirrors what current person-job-fit systems do (concatenating title, skills, and description on one side; experience, education, and skills on the other).

### 2. Canonical skill linking

Skills on both sides are extracted as spans and linked to a shared taxonomy (ESCO, ~13.9k skills, or O*NET). This solves:

* **Surface variants** — "AWS" / "Amazon Web Services" resolve to one entity
* **Implicit skills** — "worked closely across teams" links to a teamwork competency with no string overlap
* **Set operations** — once canonicalized, skill overlap is an intersection: cheap, deterministic, SQL-filterable, and directly explainable to the user

Note from the literature: for *skill* extraction specifically, entity linking outperforms sentence-level embedding — extract the span, then link it. For *occupation/title* extraction, contextual sentence linking does better. Use both accordingly.

### 3. Compact reranking documents

Rerankers chunk documents over ~500 tokens, and a raw JD is 1,500–3,000. We rerank a synthesized document built from the structured extraction — title, seniority, canonical skills, hard requirements, comp — which fits in one chunk, drops boilerplate that dilutes signal, and cuts rerank cost.

Use top-N rather than a score threshold: cross-encoder scores are not reliably calibrated across different queries, so thresholds behave inconsistently between users. Top-N is also what the pricing model needs.

### 4. Labels from day one

Every `(user, job, stage, score, action)` tuple is logged — shown, skipped, screened, generated, applied. This is the training set for a fine-tuned person-job-fit encoder later, and it is the actual defensible asset. A tuned small model on proprietary interaction data plausibly beats frontier models used as generic rerankers.

## Verification design

Two separate LLM calls, deliberately:

* **Grounding check** — generated resume vs. user work history **only**. The JD is deliberately withheld. A verifier that can see the target is biased toward approving; an invented "5 years Kubernetes" reads as *correct* rather than *unsupported* when the model knows the JD asked for it.
* **Coverage check** — generated resume vs. JD. Did anything relevant get dropped?

Use a different model family than the generator. Self-verification within a family is weak at catching its own confabulations.

Most of the high-risk surface is checkable deterministically. The generator emits claim → source-span IDs, and we verify structurally:

* Employers, titles, date ranges: exact set membership against source
* **All numbers** (years, team size, percentages, dollar figures): must exist in source — regex-checkable, and where fabrication does the most interview damage
* Canonical skills in output ⊆ user's linked skill set

The LLM grounding check then only handles semantic drift ("led" vs. "contributed to").

## Spend control

Three independent caps:

* **Candidates per user per day** (~500 post-filter) — bounds rerank cost against a badly configured profile
* **`SCREEN_SCORE_FLOOR`** — skip the LLM screen on low-rerank matches; they still appear in the list, unscreened
* **Resume generations per month** (tens) — bounds the dominant cost

Screening is cheap enough to be generous, but the floor is the spend bound on top of Top-N / daily cap. Differentiation is on resume volume, which is also what users perceive as the product.

Screen labels are billed as COGS, not to the user, and surfaced as a product feature (qualification badge + reasoning on every card) rather than hidden. Without that visibility, users see 50 matches → 12 applications and assume it's broken.

## Application submission

**Out of scope for v1, deliberately.**

The line that matters is whether a human reviews and submits each application — not whether the automation runs on our servers or in the user's browser. An extension that auto-fills and clicks submit is automated submission with extra steps and will be treated that way under job-board and ATS terms of service.

We produce a paste-ready context block; the user submits. This also:

* Preserves a human check on fabrication — the last line of defense, which disappears entirely if submission is automated
* Produces higher-quality feedback labels than an auto-fire would
* Avoids the most brittle possible component (every ATS form differs and changes)

Related assumption to validate: the belief that ATS platforms auto-reject on keyword score is largely folklore. Greenhouse, Lever, and Workday are primarily databases recruiters *search*. Keyword alignment matters for **searchability**, not for surviving a robot gatekeeper — which argues for natural terminology alignment over density optimization, and changes how the feature is marketed.

## Deferred

* **UI — entirely unplanned as of this document.** No decisions made on web app, extension, email digest, or delivery format.
* Fine-tuned matching encoder (needs accumulated labels)
* Automated application submission (see above)
* Configurable hard-requirement gating policy — currently we do not drop on a single missing hard requirement (`HARD_REQ_MISSING_DROP_THRESHOLD` unset). The env knob exists; it should become user- or tier-configurable once we have data on false-negative rates
