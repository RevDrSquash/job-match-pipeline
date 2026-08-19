# UI Design

> **Status: partially implemented.** Surfaces and constraints agreed. Framework is decided (server-rendered Next.js App Router — see Hosting), and a local single-user cut is implemented (see "Local UI milestone" at the end). No visual design or wireframes for v1 yet; the purpose of this document is still to make sure v1 doesn't foreclose features we know we want.

## Principles

**Capture feedback from day one.** `pipeline_events` is the training set for a fine-tuned matching encoder and the main defensible asset. It only materializes if the UI records user actions. Labels not captured at launch cannot be backfilled — they are permanently lost. This is the single most irreversible UI decision.

**Make screen reasoning visible.** Qualification labels are billed as COGS and surfaced as product — a badge and a reason on every card. Without that, a user sees 50 matches produce 12 applications and concludes the system is broken.

**The digest may be the primary interface.** The pipeline is asynchronous; matches arrive on a cycle. Without notification the user has no reason to return. Design the web app as the place you act on a digest, not as somewhere people idly browse.

**Never nudge toward fabrication.** The resume-expansion agent is the one feature that cuts against the system's core constraint. See its section below.

## Free tier: public search + "Do I qualify?"

**Decided.** Unauthenticated metadata search over the corpus, with a **"Do I qualify?"** button on each result that starts the signup flow and lands the user on that job's match analysis.

Search alone would only demonstrate the commodity part — generic job search is solved and crowded. The button is what carries the value proposition: it converts on a *specific* job the user already self-selected as interesting, which is a stronger moment than an abstract resume-first pitch.

**Search exposes metadata only** — title, company, location, comp, posted date. No JD body text, and **no link out to the original posting**. The purpose of search is to demonstrate corpus breadth and prompt signup, not to be a usable job board. Exposing full text would make us a scrapeable mirror of the corpus we spent effort assembling; linking out would let visitors bypass signup entirely.

Note this is friction, not prevention — title plus company is enough to find any posting via a search engine. It's a speed bump that keeps us from being the convenient path, not a lock.

The outbound link is **gated, not absent**: it appears post-signup on the application handoff screen, where the user needs it to actually apply.

Search is SQL over ATS metadata. It triggers no extraction and is nearly free. Public endpoints still need rate limiting and bot protection from day one.

**Side benefit:** no personal information is collected before signup, so consent sits in the normal signup flow rather than on the landing page.

### The conversion flow is the risk

Clicking the button puts signup mid-task with a specific job in flight:

```
click "Do I qualify?" → OAuth → upload resume → parse → match analysis
```

That is three steps between intent and payoff, and it is where users will drop. Design requirements:

* **Stash the job ID through the OAuth round-trip.** The user must land on *that job's* analysis, not a generic dashboard.
* **Keep the target job visible throughout** — "analyzing your fit for Senior Backend Engineer at Acme" — so the motivation survives the resume upload step.
* The first post-signup experience is a cold profile. Parse latency is directly in the conversion path; optimize it as such.
* If parsing fails, do not dead-end. Manual entry must be reachable without losing the job context.

## Surfaces

### 1. Landing and public search

* Value proposition
* **Public job search** — SQL over ATS metadata, no auth, no extraction. **Metadata only** (title, company, location, comp, posted date). No full JD text and **no outbound link to the original posting.**
* **"Do I qualify?"** on each result → signup flow → that job's match analysis
* Google OAuth signin (for returning users)
* Pricing

Rate limiting and bot protection are required, not optional, on every unauthenticated endpoint.

### 2. Profile / resume ingestion

**PoC (DEF-19):** there is no UI yet. The same pipeline is a CLI:

```
jobmatch profile ingest <resume-file>   # PDF, markdown, or text
jobmatch profile show [--user-id UUID]
jobmatch profile edit <user-id> ...     # bumps profile_version, sets rematch_needed
```

See the README for flags and env vars. `profile show` is the review/correction surface until this UI exists.

**v1:** upload or paste resume → parse into structured work history → extract and ESCO-link skills → build synthesized profile document + embedding.

Users must be able to review and correct the parse. Extraction errors here propagate to every match and every generated resume, and the user is the only one who can catch them.

**Required states:** parsing in progress, parse review/correction, parse failure with manual entry fallback.

**Profile versioning:** edits bump `profile_version`, which invalidates the cached context block and sets `rematch_needed`. Re-matching is *not* triggered by the edit — a scheduled job picks up dirty profiles on its own cadence.

The UI should communicate that a re-scan is queued ("we'll re-scan your matches shortly") rather than appearing to do nothing.

#### Profile re-match is scheduled, not triggered

A profile edit invalidating the cached context block means the user's matches need recomputing against the whole corpus. Firing that on the version bump is wrong for two reasons: it makes a UI action a cost event, and under lazy extraction a full-corpus re-scan can trigger a burst of `extract-job` tasks.

**The mechanism already exists.** `match-batch` is a scheduled job that queries the database. A profile edit therefore needs to trigger nothing — it sets a dirty flag, and the next scheduled run picks it up. No debounce logic, no coalescing, no new machinery.

This also handles what debouncing would not: a user editing across several separate sessions over an hour. Each edit looks "final" and would fire its own re-match under a trigger model. A dirty flag collapses all of them regardless of timing.

**Use a separate cadence from new-job matching:**

| Trigger | Cadence | Scope |
| -- | -- | -- |
| New postings ingested | ~5 min | Incremental — jobs since last cycle |
| Dirty profiles | Hourly, or on demand | Full corpus scan for that user |

Same handler, different schedule, different rate limits — the profile path carries the extraction burst and should be throttled accordingly. See `match-batch` in Tasks and Handlers.

### 3. Resume expansion agent (later, but design for it now)

The premise: people omit things that belong on a resume because they don't realize they count. The backend for this largely exists — ESCO-linked skills across the corpus make "roles matching your profile frequently require X" a query we can already run.

**The tension:** an agent that suggests skills to add is, mechanically, prompting the user to inflate their resume. This is the one feature that cuts against the no-fabrication principle that the rest of the system is built around.

**Constraints, non-negotiable:**

* **Interrogative, never suggestive.** "Have you worked with Terraform? If so, describe what you did." — never "add Terraform to your resume."
* The user supplies the substance. The agent supplies the prompt.
* Anything added this way is flagged **user-asserted** in the profile, so the verification layer knows the provenance and can treat it accordingly.
* Never pre-fill a claim for the user to accept. Acceptance is not authorship.

### 4. Filter configuration

`user_filters` drives the prefilter, and over-tight filters are named in Evaluation Strategy as the top source of invisible false negatives — a user who filters themselves into nothing sees an empty feed and blames the product.

* Editable: comp floor, locations, work arrangement, seniority band, title families
* **Live match-count estimate** ("~40 matches/week at these settings") so the user can see when they've over-constrained
* Sensible defaults derived from the parsed profile, not empty fields

### 5. Match feed

The primary logged-in surface.

One ordered list, best matches first. Per match: title, company, location, comp, match score, **qualification label**, **matched skills**, **missing skills**, and the screen's stated reason. Unscreened matches (below the score floor, or still in flight) appear below screened ones.

**Actions — each writes to** `pipeline_events`**:**

| Action | Why it matters |
| -- | -- |
| Viewed | Baseline exposure signal |
| Skipped / not interested (+ reason) | Negative labels; reasons feed filter tuning |
| Generate resume | Positive intent, consumes quota |
| Marked applied | Strongest label available pre-outcome |
| Outcome (see below) | The label that would actually train a good encoder |

#### Outcome labels — low friction, no tracker

A full application-tracker interface is explicitly **not** in scope. Instead, applied jobs get inline buttons in the match list.

**Three states, not two:**

* **Got interview**
* **Rejected**
* **No response yet** (default)

We store the application date, so "no response" can be reinterpreted as an implicit rejection later without deciding a threshold now.

The third matters more than it looks. Ghosting is the overwhelmingly common outcome, and with only "rejected" as the negative button, silent rejections pile up as unlabeled applications — the largest category becomes the one with no signal.

**Put the same buttons in the email digest as one-click links.** The user who will never open the web app to report an outcome will click a link in an email about a job they applied to. This is the cheapest available lift on the most valuable label.

### 6. Qualification label on the card

The screen stage is advisory, not a hide. Low labels (`unqualified`, `minimally_qualified`) still appear in the ranked list with their reason. Doubles as a correction surface: a user disagreeing with a low label is a high-value signal, and should be actionable ("Actually, I qualify" → `disagree_with_gate`).

### 7. Generated resume + application handoff

Where the no-auto-submit boundary lives, and where the best label is captured.

* View generated resume with verification status
* Download (PDF / docx)
* Copy paste-ready context block (for use with the user's own browser tooling)
* Link out to the original posting (**authenticated users only** — public search deliberately withholds this)
* **Mark as applied**
* Match report: matched / adjacent / missing skills, gate reasoning

If verification flagged something, say so plainly. The user is the last line of defense against fabrication; hiding a flag defeats the whole verification design.

### 8. Quota and billing

The entire cost model rests on monthly generation caps.

* Remaining quota, reset date
* Tier, upgrade path, overage pricing
* Subscription management, cancellation
* Warn before quota exhaustion, not after

### 9. Account, consent, and data rights

Required by PIPEDA / BC PIPA, and cheap now versus painful later.

* Consent at profile upload — plain language, at the point of collection, stating where data goes and who processes it. Not buried in ToS.
* **Separate, revocable consent** for retaining interaction data to improve the model (grounds `pipeline_events` retention independently of service consent)
* Data export (right of access)
* Delete account — must cascade to embeddings, cached context blocks, matches, generations, and `pipeline_events`

### 10. Notifications

* Email digest of new matches — likely the primary re-engagement mechanism
* Configurable cadence (daily / weekly / off)
* Deep links straight to a match, so acting on the digest is one tap
* **One-click outcome buttons** (got interview / rejected) on previously-applied jobs — highest-yield placement for the most valuable label

### 11. Empty and cold-start states

Day-one users hit a thin corpus (there is no backfilled 1M-posting archive; see Posting Sources). "Still scanning for matches — check back tomorrow" must be a designed state with an explanation, not a blank list.

Also needed: quota exhausted, no matches at current filters (with a link to loosen them), extraction/parse in progress, verification failed.

## Admin dashboard

Business metrics — indexed jobs, users, cost, revenue — plus the operational ones that actually catch problems:

* **Per-stage funnel counts** — ingested → prefiltered → extracted → reranked → gated → generated → applied. Where candidates die is where the bugs are.
* **Extraction coverage %** of corpus (drives the cost projection)
* **Qualification-label distribution**, trended
* **Rank/label disagreement log** — the tuning signal named in Architecture
* Queue depths and task failure rates per handler
* Extraction and parse failure rates
* **LLM spend per user, with alerting.** Runaway spend is silent until the invoice arrives.
* Verification failure rate — a rise here is a fabrication-risk signal and should page someone

## Deferred

* Native mobile
* Full application tracker (outcome capture stays as inline buttons)
* Interview prep
* Employer-side anything
* Cover letter generation (same fabrication constraints would apply)
* Auto-submission — permanently out of scope, not merely deferred

## Hosting

**Decided: separate Cloud Run service, server-rendered Next.js (App Router).** Not a static bundle on a CDN. The deciding argument is SEO — public job search is a plausible organic acquisition channel, and server-rendered pages index far better than a client-rendered SPA.

Both services sit behind one load balancer on a single domain (`/api/*` → API, `/*` → frontend), which keeps session cookies first-party and avoids CORS entirely. Cloud CDN still fronts the frontend, so static assets are edge-cached — this is a rendering-model choice, not a decision against caching.

`min-instances: 1` on the frontend only: a cold start on the landing page sits directly in the "Do I qualify?" conversion funnel. See Infrastructure.

**SEO caveat worth resolving:** our public pages are deliberately thin (no JD body, no source link). Thin near-duplicate pages at corpus scale can be treated as low quality rather than indexed. Probably means indexing search and category pages rather than one page per posting — decide deliberately if organic traffic is actually part of the acquisition plan.

## Open questions

* SEO indexing strategy given deliberately thin public pages — per-posting, or search/category pages only?
* Does the paste-ready block target a specific browser extension, or is it format-agnostic clipboard content?
* Resume output templating — do users choose a template, or do we impose one?
* Dirty-profile re-match cadence — hourly, or user-visible "re-scan now" with a rate limit?
* Do we expect friction complaints from withholding the source link on public search? Users can find a posting by searching title + company anyway, so the gate is soft — it costs a little goodwill for a little conversion.

## Local UI milestone (PoC)

**Decided:** Next.js App Router in `frontend/` as a separate service. The browser talks to a user-facing API mounted at `/api/*` on the existing FastAPI app — distinct from internal `/handlers/*` pipeline workers. Locally, Next.js rewrites proxy `/api/*` to FastAPI on `:8080`, mirroring the single-domain production shape (`/api/*` → API, `/*` → frontend).

**Single-user model:** no auth. `GET /api/users` lists users; the frontend auto-selects when there is exactly one CLI-ingested profile and offers a picker otherwise. All endpoints take an explicit `user_id`.

### API layer

Read endpoints (thin queries over existing models):

| Endpoint | Purpose |
| -- | -- |
| `GET /api/users` | id, tier, quota |
| `GET /api/profile?user_id=` | profile + filters + resolved `skills` labels (same base shape as `jobmatch profile show`) |
| `GET /api/matches?user_id=` | single ranked list: match cards with job metadata, skill buckets as `{id, label}`, `qualification_label` / `screen_reason`, latest UI state. Ordered by label tier then `rerank_score`. One card per job: only the latest match row per job is returned (a dirty rematch after a profile edit inserts new rows and retains superseded ones) |
| `GET /api/generations/{id}` | resume, claim map, verification status, job link for handoff |
| `GET /api/admin/metrics` | funnel counts, extraction coverage %, label distribution, LLM spend |

Write endpoints:

| Endpoint | Purpose |
| -- | -- |
| `PATCH /api/profile` | same service path as `jobmatch profile edit`; response includes re-scan messaging |
| `POST /api/matches/{id}/events` | feedback actions (vocabulary below) |
| `POST /api/matches/{id}/generate` | enqueue `generate-resume` (consumes quota); no-op when a generation already exists; `quota_exhausted` when empty |

Resume upload stays CLI-only for this milestone.

Skill buckets in match and generation payloads, and the profile `skills` field, are objects `{id, label}` where `id` is the canonical ESCO concept URI (or PoC `esco:slug`) and `label` is resolved from the `skills` taxonomy table (`canonical_label`). Unknown ids echo the id as the label.

### UI feedback event vocabulary

Every action writes to `pipeline_events` with `stage="ui"`.

| Action | Semantics | `details` |
| -- | -- | -- |
| `viewed` | Baseline exposure | — (deduped: at most one row per user + job) |
| `skipped` | Negative label / correction | `reason_code` (enum) + optional `reason_text` |
| `generate_requested` | Positive intent before generation completes | — |
| `marked_applied` | Strong pre-outcome label | `applied_at` (ISO-8601; server defaults to now) |
| `outcome` | Post-application result | `outcome`: `interview` \| `rejected` (no-response is the absence of a row) |

`reason_code` values: `not_interested`, `wrong_location`, `wrong_comp`, `wrong_seniority`, `disagree_with_gate` (correction of a low qualification label), `other`.

### Explicitly out of scope (this cut)

OAuth/signup, public search, "Do I qualify?", quota/billing UI, email digest, resume upload UI, PDF/docx export, rate limiting (no unauthenticated public endpoints yet).
