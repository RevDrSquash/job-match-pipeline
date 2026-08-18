# Eval sets

Versioned label files for the four non-negotiable evals in
[`docs/EVALUATION.md`](../docs/EVALUATION.md). The runner is
`jobmatch evals run [--suite NAME]`. Producing the hand labels is human work;
this directory ships **sample** items so the harness runs against the local
stack without a labeling pass.

```
evals/sets/<version>/
  manifest.json          # set version recorded in every result
  extraction/
  skill_linking/
  retrieval/
  fabrication/
evals/results/           # timestamped JSON + .txt summaries (gitignored)
```

Results always include `set_version`, per-suite latency, token counts, and
estimated USD cost. Prompt or resume text is never written to the report.

## Run

```bash
# All four suites, sample labels, offline predictors (no API key)
jobmatch evals run --offline

# One suite
jobmatch evals run --suite extraction
jobmatch evals run --suite skill_linking
jobmatch evals run --suite retrieval
jobmatch evals run --suite fabrication

# Prove the fabrication hard gate (must exit 1)
jobmatch evals run --suite fabrication --plant-fabrication --offline
echo $?   # 1
```

Retrieval recall@K is only meaningful with `EMBEDDING_PROVIDER=gemini`. The
default hashing embedder is an offline stand-in (`docs/OPEN_ISSUES.md` §6).
The runner **warns loudly** under hashing and **refuses** the suite if you
pass `--require-gemini-embeddings`.

The skill-linking suite's similarity fallback likewise picks its span
embedder from `EMBEDDING_PROVIDER` (seed labels are embedded on the fly);
implicit-mention recall is always 0 without it. `--offline` keeps the
exact/alias-only linker and needs no API key.

Live extraction / generation (optional):

```bash
# Uses EXTRACTION_MODEL / GENERATION_MODEL when LLM_API_KEY is set
jobmatch evals run --suite extraction
jobmatch evals run --suite fabrication
```

## Labeling workflow

Copy `evals/sets/v1/` to `evals/sets/v2/` when the item list changes in a way
that would make scores incomparable, bump `manifest.json` `version`, and keep
v1 around. A quality change is meaningless if the set moved underneath it.

Do not put real user resumes or work history into these files beyond the
in-repo test fixture. Job postings are not personal information; profile
text is (`docs/PRIVACY_AND_COMPLIANCE.md`).

### 1. Extraction accuracy

**Who:** a human with the seed corpus open.

**What:** ~100 JDs, spread across ATS sources and seniority. Prefer postings
already in `jobs` (copy `raw_jd` into `extraction/jds/<id>.txt` or point
`jd_file` at a checkout-relative path).

**Schema** (`extraction/labels.json` → `items[]`):

| Field | Notes |
| --- | --- |
| `id` | Stable slug |
| `jd_file` | Path relative to the labels file |
| `title` | Role title only (no company / em-dash suffix) |
| `seniority` | `intern` `junior` `mid` `senior` `staff` `principal` `executive` |
| `comp_min` / `comp_max` | Annual cash integers, or omit if unstated |
| `location` | As stated; strip work-arrangement parentheticals |
| `work_arrangement` | `remote` `hybrid` `onsite` |
| `hard_requirements` | Must-haves. This split drives the deterministic gate. |
| `nice_to_haves` | Preferred / bonus / plus |

Watch the hard vs nice-to-have distinction. If the posting does not
distinguish, put concrete qualifications in hard and stretch items in nice.

### 2. Skill linking precision/recall

**Who:** same JDs as extraction, plus the test resume (and later ~20 resumes).

**What:** span the skills, then assign a canonical `skill_id` from the loaded
ESCO table (or the in-repo `esco:<slug>` seed if the table is empty).

**Schema** (`skill_linking/labels.json` → `items[]` → `spans[]`):

| Field | Notes |
| --- | --- |
| `text` | Surface form as it appears |
| `skill_id` | Canonical id, or `null` if the span must not link |
| `mention` | `explicit` (string overlap with a label/alias) or `implicit` |

Implicit examples: "worked closely across teams" → teamwork; "partner across
product and design" → teamwork with no alias overlap. **Do not average
implicit into explicit** — the runner reports them separately on purpose.

Skill ids must come from the shared linker taxonomy. Do not invent slugs.

`skill_linking/calibration_spans.json` is a calibration-only companion file
(sibling-dense cases and `skill_id: null` negatives) consumed by
`scripts/calibrate_link_threshold.py`, **not** by the eval runner — v1
`labels.json` stays frozen per the versioning rule above. Fold those spans
into `labels.json` when cutting v2.

### 3. Retrieval recall@K

**Who:** one (later a handful) of labeled profiles against an exhaustive
corpus. First cut: the ~500-posting seed. Few-thousand comes before scale-up.

**What:** for every job in the corpus, `relevant: true` if the human would
want it surfaced for that profile. Binary is enough for recall@K; graded
labels are the second-tier ranking eval.

**Schema** (`retrieval/labels.json`):

- `profile` — filters (`locations`, `comp_floor`, `work_arrangement`,
  `title_families`) plus `synthesized_doc` used as the query
- `corpus[]` — `id`, ATS-style metadata, `synthesized_doc`, `relevant`
- `k` — cutoff for the headline recall@K (curve also reports 1/3/5/10)

Mark jobs that *should* match but will die on an over-tight filter as
`relevant: true` anyway. Metadata-stage recall is the watch metric.

When swapping in the seed corpus, keep job ids stable (use `jobs.id` or
`url_hash`) so later sets can be diffed.

### 4. Fabrication — hard gate, target zero

**Who:** construct adversarial JDs against the test profile. Mutating real
seed postings is the intended method.

**Cover these temptations** (one pair each, then more):

1. `missing_skill` — JD demands a skill the user plainly lacks
2. `adjacent_not_equivalent` — Terraform vs CloudFormation (or similar siblings)
3. `year_scope_inflation` — years or team size above the source
4. `seniority_inflation` — "led the org" / Staff when the source is Senior
5. `employer_title_drift` — famous employer or inflated title

**Schema** (`fabrication/pairs.json`):

- `profile.work_history` — same span-id contract as profile ingest
- `profile.skill_ids` — canonical ids only
- `pairs[].temptation` — one of the five names above
- `pairs[].job.raw_jd` / `skill_ids` / `title`
- `pairs[].forbidden_claims` — phrases that must not appear in output

The suite calls `app.verify.deterministic.run_deterministic_checks` (the
same stage-1 gate as `verify-resume`). Any failure, or a hit on
`forbidden_claims`, counts as a fabricated claim. **Target zero. This
blocks deploys.**

`--plant-fabrication` appends a known-bad claim for each temptation so the
exit code can be wired in CI before a live generator exists.

When `generate-resume` is called live (API key set, no `--offline`), the
same checker scores model output. Do not log the resume.

## Adding a new set version

1. `cp -r evals/sets/v1 evals/sets/v2`
2. Edit items; set `"version": "v2"` in `manifest.json` and each labels file
3. `jobmatch evals run --set-version v2 --offline`
4. Commit the set. Do not edit v1 in place once it has been used for a
   reported baseline.
