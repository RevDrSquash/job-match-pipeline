# Evaluation Strategy

> **Status: harness shipped (DEF-24).** The four non-negotiable suites run via
> `jobmatch evals run [--suite NAME]`. Label files in `evals/sets/v1/` are
> **samples** — producing the hand labels is human work. Thresholds below are
> still placeholders until those labels exist. See `evals/README.md`.

## Why per-stage, not end-to-end

The pipeline is a chain, and errors compound silently. A bad extraction produces a bad match, which produces a bad resume. Only the last one is visible, and by then the cause is three stages upstream.

End-to-end evals tell you something is wrong. Per-stage evals tell you what. Both matter, but per-stage is what makes the system debuggable.

## The four non-negotiables

Start here. These cover the errors that are either invisible in production or catastrophic when they happen.

### 1. Extraction accuracy

**Why first:** everything downstream inherits these errors, and it's the cheapest eval to build.

* **Set:** ~100 hand-labeled job descriptions, spread across sources and seniority levels
* **Measures:** field-level accuracy on title, seniority, comp range, location, work arrangement; precision/recall on hard requirements vs. nice-to-haves
* **Watch:** the hard/nice-to-have distinction specifically — it still feeds skill buckets and the recorded hard-req missing count, even though it no longer auto-drops

### 2. Skill linking precision/recall

Separate eval from extraction because it is a different failure mode — entity linking, not field parsing.

* **Set:** hand-labeled skill spans on the same ~100 JDs, plus ~20 resumes
* **Measures:** precision and recall against canonical taxonomy nodes
* **Recall matters more than precision.** A missed link becomes a false negative in matching, which is invisible. A spurious link mostly costs a little noise in ranking.
* **Watch:** implicit skills — phrasings with no string overlap with the taxonomy entry ("worked closely across teams" → a teamwork competency). Track these separately from explicit mentions; they will score much worse and averaging hides that.

### 3. Retrieval recall@K

**The one that cannot be recovered from production feedback.** A relevant job that never surfaces is never shown, never clicked, never complained about. It is invisible forever unless we measure it deliberately.

* **Set:** a fixed corpus (a few thousand postings) with exhaustive relevance labels for a handful of user profiles. Expensive to build, so keep the corpus small and reuse it.
* **Measures:** recall@K after metadata filter, after vector recall, after rerank — measured at each stage so we can see which one is dropping things
* **Watch:** metadata filters are the most likely culprit. An over-tight comp floor or seniority band silently removes good matches before anything semantic runs. This risk increased with lazy extraction, since prefilters now run on ATS metadata alone.

### 4. Fabrication — adversarial, hard gate, target zero

The catastrophic failure mode. Output goes out under the user's name; a fabricated credential is a real consequence for a real person.

* **Set:** deliberately adversarial (user, JD) pairs constructed to tempt the generator:
  * JD demands a skill the user plainly lacks
  * Adjacent-but-not-equivalent experience (JD: Terraform, user: CloudFormation)
  * Plausible year/scope inflation (user has 3y, JD asks for 5)
  * Seniority inflation (user contributed, JD wants "led")
  * Employer/title drift
* **Measure:** count of fabricated claims. **Target zero.**
* **This blocks deploys.** Not a metric to watch — a gate to pass.

## Second tier

Build once the four above are running.

### 5. Ranking quality

* Precision@N and NDCG on the reranked list
* Needs *graded* relevance labels (not binary), which is more labeling work — hence second tier
* This is where a fine-tuned encoder would show its value later, so the baseline is worth establishing before any tuning work

### 6. Verifier sensitivity

Inject known fabrications into otherwise-clean generated resumes and measure detection rate.

**Without this we do not know whether the safety net has holes** — which is arguably worse than having no net, because the verification stage creates confidence that may be unearned.

* Test both verification stages independently:
  * Deterministic checks (numbers, employers, titles, skill subset) — should be near-perfect on their categories; any miss is a bug, not a tuning issue
  * LLM grounding check — measures semantic drift detection
* Include the case the design specifically guards against: confirm the grounding check still catches fabrications **when the JD is withheld**, and measure how much worse it does when the JD is included. That delta is the justification for the JD-blind design and should be documented.

### 7. Qualification-label / ranking agreement

* Confusion matrix of LLM qualification label vs. human judgment (once a labeled set exists — see `OPEN_ISSUES.md`)
* Adjacent-tier mistakes (e.g. `potentially_qualified` vs `clearly_qualified`) are cheaper than inversions (`unqualified` vs `clearly_qualified`)
* The two error types that still cost differently:
  * **Over-promotion** — a `clearly_qualified` miss ranks the card too high
  * **Under-ranking** — a good job sinks below the fold
* Under-ranking is worse for the product and invisible to the user, so weight accordingly
* Include logistics-only cases in the labeled set (right skills, wrong city / arrangement / comp): the label must reflect qualification fit only, so a label lowered for a logistics mismatch is a rubric violation, not an adjacent-tier judgment call (`TASKS_AND_HANDLERS.md`, screen-job)

### 8. Resume quality

Hardest to automate, least dangerous failure. Human-rated on a rubric initially — relevance, clarity, evidence of tailoring, absence of filler. Revisit automation once there's enough rated data to calibrate a judge against.

### 9. Model comparison on constrained generation

Separate from raw quality: measure the actual delta between candidate models on *our* constrained generation task (structured skill buckets in, grounded claims out). The perceived gap between frontier models is largest for open-ended writing and narrows considerably under constraint. This number determines whether any compliance or cost tradeoff is worth paying. See Privacy and Compliance.

## Harness (PoC)

`jobmatch evals run` loads a versioned set from `evals/sets/<version>/`,
records that version on the report, and writes timestamped JSON + a text
summary under `evals/results/`. Cost and latency are first-class dimensions
on every suite (token counts and estimated USD when an LLM actually ran).

| Suite | What runs offline | Live path |
| --- | --- | --- |
| Extraction | Heuristic section/header parser (baseline) | `JobLLM` when `LLM_API_KEY` is set |
| Skill linking | Shared `SkillLinker` over the seed taxonomy (`seed:<slug>` ids) / canonical graph | same |
| Retrieval | In-set corpus + metadata predicate + embed + rerank | `EMBEDDING_PROVIDER=gemini` required for a quality number |
| Fabrication | Grounded copy-only generator + `run_deterministic_checks` | `GenerateLLM` when a key is set |

Retrieval **warns** under `EMBEDDING_PROVIDER=hashing` and **refuses** if
`--require-gemini-embeddings` is set (`docs/OPEN_ISSUES.md` §6).

Fabrication is a hard gate: any fabricated claim → suite fail → CLI exit 1.
`--plant-fabrication` injects a known-bad claim per temptation so CI can
assert that exit code before trusting a live generator. This blocks deploys.

Prompt changes re-run `jobmatch evals run` (operational discipline below).
The local proof-of-concept (`jobmatch poc run`) runs the same four suites
and records the measured baselines in [`POC_RESULTS.md`](POC_RESULTS.md).

## Bootstrapping labels

Start with our own resume against a few hundred real postings, hand-labeled. That is enough for evals 1–3 immediately, and for a first cut at 4.

`pipeline_events` grows the set from there — shown, skipped, screened, generated, applied. Note the privacy constraint: retention of this table for training purposes needs its own consent basis and must survive deletion requests (see Privacy and Compliance).

## Operational discipline

* **Every prompt change re-runs the full suite.** Prompt edits are code changes and regress silently otherwise.
* **Track cost and latency per stage as eval dimensions**, not just quality. Token counts per stage are the biggest source of error in the cost model.
* **Log the rank/label disagreement signal** — high `rerank_score` + low label, and the inverse. Highest-value signal for tuning both.
* Version the eval sets. A quality change is meaningless if the set moved underneath it.

## Open questions

* Graded relevance rubric — what does a 3 vs. a 4 actually mean?
* How large does the exhaustively-labeled corpus need to be for recall@K to be stable?
* Can an LLM judge substitute for human rating on resume quality, and how do we validate it against human ratings?
* Thresholds: what recall@K and extraction accuracy are good enough to proceed to full ingest coverage?
* Do we need per-domain eval sets (SWE vs. other fields), or does one generalize?
