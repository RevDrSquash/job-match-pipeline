# Cost Model

> All figures are order-of-magnitude estimates for architecture decisions, not quotes. LLM and rerank pricing moves constantly — verify against live pricing pages before committing to a subscription price. Rerank pricing in particular has shifted between per-search and per-token billing across model versions.

## Headline

**Infrastructure is a rounding error. Resume generation is essentially the entire bill.**

Everything upstream of generation — ingest, embedding, matching, reranking, screening — is cheap enough that it can be generous on every tier. Pricing should be structured around resume volume, which is also what users perceive as the product.

## Fixed / platform costs

| Item | Estimate | Notes |
| -- | -- | -- |
| Cloud Tasks | ~$0–1/mo | ~1M ops/mo territory; free tier covers most |
| Cloud Run | ~$10–30/mo | 10k ingests/day × ~2s × 0.5 vCPU |
| Cloud SQL (Postgres) | ~$50–100/mo | Small instance; 1M jobs + vectors ≈ 3GB |
| Secret Manager, Artifact Registry, Scheduler | <$5/mo |  |
| **Subtotal** | **\~$70–140/mo** | Flat, does not scale with users |

## Per-job costs

### Volume reconciliation

Two conflicting figures existed for daily posting volume. Resolved:

* **\~250k/day** — from the OpenPostings author's social post at ~7,500 companies. That implies ~33 *new* postings per company per day, which is implausible as an average. Read as *total active postings returned by a full sync* (~33 open roles per company), it's entirely reasonable for a set skewed toward Workday/Taleo enterprises. Treated as a sync result-set size, not distinct new postings.
* **\~10k/day** — observed directly by running OpenPostings locally. Consistent with a bottom-up estimate: ~100k companies × ~40% with open roles × ~8 open postings ÷ ~35-day average posting lifetime ≈ ~9k new/day.

**\~10k/day distinct new postings is the working number.** It is a floor: random sampling misses postings that appear and expire between samples, so systematic coverage should raise it — assume ~2–2.5×.

### Fetch volume ≠ extraction volume

Moving from random sampling to systematic coverage increases *fetches*, not *extractions*. URL-hash dedup means re-seeing a known posting costs an HTTP request, not an LLM call. Fetches are effectively free; sweeping 100k boards daily adds maybe $20/mo of Cloud Run.

Several ATS providers return full job content inline on the list endpoint (e.g. Greenhouse `?content=true`), removing the per-job detail fetch entirely.

### If extraction ran on every posting (rejected design)

| Scenario | New/day | Extraction (~$0.0006) | Embedding | **Monthly** |
| -- | -- | -- | -- | -- |
| Observed (random sampling) | 10k | $6/day | $0.30/day | **\~$190** |
| Full coverage (likely) | 25k | $15/day | $0.75/day | **\~$470** |
| If 250k were real | 250k | $150/day | $7.50/day | **\~$4,700** |

Even the worst case doesn't break the model, but ~$470/mo of fixed cost while serving ten users is bad early-stage economics.

### Actual design: lazy extraction

Extraction runs on first prefilter hit, not at ingest, and caches permanently. See `extract-job` in Tasks and Handlers.

| Users | ~Corpus extracted | **Monthly extraction cost** |
| -- | -- | -- |
| 10 | ~5% | **\~$25** |
| 100 | ~25% | **\~$120** |
| 1,000+ | approaching 100% | **\~$470 (ceiling)** |

Cost scales with users early, then converges on the O(jobs) ceiling as the user base covers more of the corpus. This is the right direction: the expense arrives alongside the revenue rather than ahead of it.

**No one-time backfill cost.** An earlier version budgeted ~$600 for extracting a 1M-posting backfill. That corpus does not exist — the reference implementation runs a rolling freshness window, so we accumulate our own corpus from steady-state ingest over months. See Posting Sources.

Extraction cost is paid **once per posting**, not once per posting per user — so a posting matched by 500 users costs the same as one matched by one.

**Measurement caution:** extraction prompt length is the main variable in every figure above. Measure real token counts on a few hundred postings before trusting the ceiling.

## Per-user costs

Assuming ~10k postings/day ingested, ~1% surviving metadata filters (~100 candidates/user/day).

**Measured on the 500-posting seed (DEF-25, same `match-batch` SQL):** title-family "Software Engineering", no comp floor. Location is the load-bearing knob:

| Location filter | Survivors | Rate |
| -- | -- | -- |
| Unconstrained (empty array) | 83 / 500 | **16.6%** |
| `Remote` (substring on ATS location) | 7 / 500 | **1.4%** |
| `Vancouver` (sample-resume city) | 0 / 500 | **0%** |

The ~1% working number is in the right ballpark **when the profile constrains location to Remote** (or a similarly common ATS token). A single-city filter against this US-heavy seed drops everyone. Title-only is an order of magnitude more generous than the cost model. See [`POC_RESULTS.md`](POC_RESULTS.md).

| Stage | Volume | Est. monthly cost/user |
| -- | -- | -- |
| Metadata filter (SQL) | ~10k/day | ~$0 |
| Vector recall | ~10k/day | ~$0 |
| Rerank (compact docs) | ~100/day | **\~$0.10–0.30** |
| Deterministic gate | ~100/day | ~$0 |
| Cheap LLM gate | ~100/day | **\~$0.50–1.50** |
| **Screening subtotal** |  | **\~$0.60–1.80** |

**Rerank chunking caveat:** rerankers bill per search, but documents over ~500 tokens split into chunks that count separately. A raw 3,000-token JD becomes ~6 chunks. Reranking the compact synthesized document instead of the raw JD is both a quality decision and roughly a 5× cost reduction here.

## Resume generation — the dominant cost

Per resume: ~8k input + ~1.5k output tokens on a model good enough not to embarrass the user.

| Component | Est. |
| -- | -- |
| Generation | ~$0.05 |
| Verification (2 LLM checks, long input / short output) | ~$0.015 |
| **Total per delivered resume** | **\~$0.065** |

**Verification adds \~25–35%, not 100%** — output tokens dominate pricing and the checks emit very little. Given the failure mode is a fabricated credential going out under the user's name, this is cheap insurance and a legitimate differentiator to charge for.

### Why the cap must be monthly, in the tens

| Resumes/mo | LLM cost/user/mo | Viability |
| -- | -- | -- |
| 300 (10/day) | ~$20 | Kills a $30 subscription |
| 100 | ~$6.50 | Thin |
| 50 | ~$3.25 | Healthy |
| 20 | ~$1.30 | Very healthy |

Real behavior favors the low end — people apply to a few dozen jobs a month, not 300. A cap of 30–50/month bounds exposure while almost never binding.

**Prompt caching** on the user's work-history block (identical across every generation, only the JD varies) should cut the input side substantially. Worth measuring early — it directly moves the only number that matters.

## Blended per-user economics

| Tier (resumes/mo) | Screening | Generation | **Total COGS** |
| -- | -- | -- | -- |
| 20 | ~$1.20 | ~$1.30 | **\~$2.50** |
| 50 | ~$1.20 | ~$3.25 | **\~$4.45** |
| 100 | ~$1.20 | ~$6.50 | **\~$7.70** |

Plus ~$70–140/mo platform and extraction cost amortized across the user base. Because extraction is lazy, that second term starts near zero and grows with adoption:

| Users | Platform + extraction | Per user |
| -- | -- | -- |
| 10 | ~$95–165/mo | ~$12 |
| 100 | ~$190–260/mo | ~$2.20 |
| 1,000 | ~$540–610/mo | ~$0.57 |

The ~$12/user at 10 users is dominated by the flat Cloud SQL bill, not by anything volume-driven. It falls away quickly.

## Pricing structure

Two independent spend caps:

* **Candidates per user per day** (~500 post-filter) — bounds rerank spend against a misconfigured profile
* **Resume generations per month** — bounds the dominant cost

**Recommended model: subscription with N included, overage priced per application.**

Per-application-only pricing aligns revenue to COGS almost perfectly, which is genuinely attractive. The argument against it is behavioral: users are disproportionately unemployed and cost-anxious, and metering creates hesitation at exactly the moment we want action. Every application becomes a purchase decision, which suppresses the usage that makes the product feel valuable.

The hybrid gives breakage in our favor (most users won't hit N), a cap against power users, and no per-click friction inside the allotment.

**Screen labels are COGS, not billed to the user.** This is only affordable because the screen is a cheap separate call — which is why its placement is an economic decision, not just an architectural one. Labels and reasons should be surfaced as product on the ranked list rather than hidden; otherwise users see 50 matches → 12 applications and assume it's broken. `SCREEN_SCORE_FLOOR` is the spend bound that keeps the ~100 screens/user/day assumption from running away.

## Cost reduction levers, in order of value

1. **Lazy extraction** — already adopted; ~20× reduction at early user counts
2. **Prompt caching** on the profile block — largest lever on the dominant cost
3. **ATS inline content** — skip per-job detail fetches where the list endpoint returns full content
4. **`SCREEN_SCORE_FLOOR`** — skip the LLM screen on low-rerank matches; they still appear, unscreened
5. **Compact rerank documents** — avoids the chunking multiplier
6. **Feedback loop tuning** — `rank_label_disagreement` (high rerank + low label, and the inverse) is what `pipeline_events` pays for; generation volume is bounded by the manual, quota-gated Generate button
7. **Fine-tuned small reranker** on accumulated labels — plausibly better *and* cheaper than frontier models used as generic rerankers

## Open cost questions

* Real token counts for extraction and generation prompts (biggest source of error in this model)
* Prompt caching effectiveness in practice
* Actual candidate survival rate — first cut on the 500-posting seed: **1.4% with a `Remote` location filter, 16.6% unconstrained, 0% on Vancouver**. The ~1% line item holds only under a Remote-like constraint; see DEF-25 / `POC_RESULTS.md`. Re-measure when the labeled owner profile and gemini embeddings land.
* Whether verification can be cut to one LLM call without losing the JD-blindness property (probably not — the separation is the point)
