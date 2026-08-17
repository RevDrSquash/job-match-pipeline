# Privacy and Compliance

> **Status: preliminary research only.** This reflects a first pass, not legal advice. Nobody on this project is a lawyer. Get an hour with a Canadian privacy lawyer before launch — the questions in the open list below are worth that cost several times over.

## The core finding

**A resume is entirely personal information.** Not just the phone number and email — the employers, dates, titles, and skill history are all information about an identifiable individual, and the combination is identifying even with direct identifiers stripped.

Under PIPEDA, personal information means information about an identifiable individual, and across the Canadian private-sector statutes it is broadly defined that way with few exclusions.

### What this rules out

An early idea was to scan for identifiers (phone numbers, emails), tokenize them before sending to LLM providers, and substitute them back in the final output — on the theory that this keeps personal information inside our system.

**This does not reduce regulatory scope.** The work history being sent to the LLM *is* the personal information. Tokenizing direct identifiers reduces breach severity and is worth doing as defense in depth, but it is not a compliance strategy and should not be treated as one.

## Which law applies

Both, in different places:

| Regime | When it applies to us |
| -- | -- |
| **BC PIPA** | Collection, use, and disclosure occurring within BC. Organizations subject to a substantially similar provincial law are generally exempt from PIPEDA for in-province activity. |
| **PIPEDA** | Whenever personal data crosses provincial or national borders — e.g. a BC company sending data to a service provider elsewhere. |

Sending resumes to a US-hosted LLM API is squarely a cross-border transfer, so PIPEDA is in scope regardless of BC PIPA.

Additional regimes to assess based on where users actually are:

* **Quebec Law 25** — stricter, GDPR-inspired; applies if we take Quebec users
* **GDPR** — applies if we take EU users
* US state laws (CCPA etc.) — if we take US users

The user base for a job-search product is likely to be geographically messy from day one. Decide early whether to geo-restrict at signup or accept the compliance surface.

## What is actually required

None of this prohibits the architecture. Cross-border processing is permitted under a transfer-for-processing model. The obligations are compliance work, not redesign:

* **Transparency** — tell users where their data goes and who processes it
* **Meaningful consent** at profile upload, not buried in a ToS
* **Vendor terms** — zero-data-retention and no-training agreements with every LLM provider. Highest-value single item on this list.
* **Working deletion** — see below
* **Breach notification** process
* **Purpose limitation** — collect and use only for what a reasonable person would consider appropriate given the stated purpose

## Model selection and data residency

### Primary path: transparency + ZDR, best model for the job

Cross-border transfer is permitted under a transfer-for-processing model. Disclosure plus contractual protection is a sufficient and normal basis. We should **not** constrain ourselves to a weaker model on compliance grounds when the compliance work is achievable paperwork.

Requirements for this path:

* Plain-language disclosure at the point of profile upload — where the data goes and who processes it. Not buried in a ToS.
* **Zero-data-retention and no-training terms** with every LLM vendor handling user data. This is the load-bearing item, more so than the disclosure.
* A DPA covering the transfer
* Deletion that propagates to the vendor, including prompt caches

Note on accountability: disclosure alone does not discharge responsibility. We remain accountable for personal information in a processor's hands, which is why the ZDR/DPA terms matter more than the notice.

### Split model selection by stage

Volume and data sensitivity sit on opposite sides of this pipeline, which makes the split cheap:

| Stage | Personal information? | Volume | Model choice |
| -- | -- | -- | -- |
| `extract-job` | **No** | high | Cheapest adequate model; no residency constraint |
| Profile parsing | Yes | 1× per user | Best available, ZDR terms |
| `generate-resume` | Yes | tens/user/mo | Best available, ZDR terms |
| `verify-resume` | Yes | tens/user/mo | Best available, different family from generator |

Because the personal-information stages are low-volume, paying a premium there barely moves the cost model. The high-volume stage touches no personal information at all.

### Optimization to investigate: frontier models via Vertex

Vertex AI serves third-party models (Claude, others) in addition to Gemini. If a frontier model is available via Vertex in `northamerica-northeast1` (Montreal), we get frontier quality **and** in-country processing, and the cross-border question largely dissolves with no quality tradeoff.

**Verify rather than assume** — regional availability for third-party models on Vertex is narrower than for Gemini, and this changes over time. Bedrock is the equivalent path if we ever revisit AWS.

If that doesn't pan out, the primary path above stands on its own.

### Assumption to test before accepting any tradeoff

The perceived quality gap between frontier models is largest for open-ended writing. Our resume generation is heavily constrained — structured skill buckets in, grounded claims out, verified downstream. Constrained generation narrows model gaps considerably.

**Measure the actual delta on the eval set before paying any cost — compliance or financial — to close it.** See Evaluation Strategy.

## Deletion — design for it now

Deletion is the requirement most likely to be painful to retrofit. A user deletion request must cascade to:

* `user_profiles` — including the synthesized doc and **embedding**
* Cached context blocks (keyed on `profile_version`)
* `matches` and `generations` rows
* `pipeline_events` — the training-set table
* Any derived training data already extracted from `pipeline_events`
* Prompt caches held by the LLM vendor
* Backups (define a retention window and disclose it)

**The** `pipeline_events` **tension is real.** That table is the training set for a future fine-tuned matching encoder and the main defensible asset. Deletion requests conflict with keeping it forever. Two mitigations to evaluate:

1. **Anonymize rather than delete** where legally sufficient — strip user linkage, retain the (job features, outcome) pair. Whether this satisfies the obligation depends on whether the residual is still identifiable, which is exactly the kind of question to put to a lawyer.
2. **Separate consent** for using interaction data to improve the model, so retention is grounded in its own consent rather than the service consent.

Do not defer this. Retrofitting deletion into a system with derived embeddings and a training corpus is materially harder than building it in.

Note: job postings themselves are not personal information, so the `jobs` table and its extracted fields are out of scope for deletion. Only the user side cascades.

## Data handling baseline

* Encrypt at rest (Cloud SQL default) and in transit
* Secrets in Secret Manager, never in env files or images
* Retain `raw_jd` for audit; retain raw uploaded resumes only as long as needed to re-parse
* Access logging on anything touching `user_profiles`
* No personal information in application logs — including no resume text in error traces, which is an easy accidental leak

## Related: the submission boundary

Documented in Architecture, restated here because it is a legal decision as much as a product one. We do not automate application submission. A human reviews and submits each application. This is the line that matters under job-board and ATS terms of service — not whether the automation runs on our servers or in the user's browser.

## Open questions

* Confirm BC PIPA vs. PIPEDA split with counsel for our specific setup
* Which frontier models are available via Vertex in `northamerica-northeast1`, at what price and quality
* ZDR + DPA terms obtainable from each candidate vendor (blocking for the primary path)
* Measured quality delta between models on constrained resume generation — before accepting any tradeoff for it
* Whether anonymized `pipeline_events` retention satisfies deletion
* Geo-restrict signup, or accept GDPR / Law 25 / US state law surface?
* Do we need a formal privacy impact assessment?
* Consent flow design — needs to be resolved alongside UI, which is currently unplanned
