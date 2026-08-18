# Design docs

**These docs are the source of truth for all design decisions.** If an implementation diverges from a doc, update the doc in the same change.

| Document | Contents |
| -- | -- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Design principles, target scale, data flow, matching approach, verification design, spend control |
| [INFRASTRUCTURE.md](INFRASTRUCTURE.md) | GCP platform choice (Cloud Tasks + Cloud Run + Cloud SQL), Terraform strategy, local development stack |
| [TASKS_AND_HANDLERS.md](TASKS_AND_HANDLERS.md) | The seven pipeline handlers, queue configuration, data model sketch |
| [COST_MODEL.md](COST_MODEL.md) | Per-job and per-user cost estimates, pricing structure, cost reduction levers |
| [EVALUATION.md](EVALUATION.md) | Per-stage eval strategy; the four non-negotiable evals; label bootstrapping |
| [PRIVACY_AND_COMPLIANCE.md](PRIVACY_AND_COMPLIANCE.md) | PIPEDA / BC PIPA analysis, model selection and data residency, deletion design |
| [POSTING_SOURCES.md](POSTING_SOURCES.md) | OpenPostings reference, license blocker, volume reconciliation, company registry plan |
| [UI.md](UI.md) | Surfaces, free-tier public search, feedback capture, admin dashboard, hosting |
| [OPEN_ISSUES.md](OPEN_ISSUES.md) | Inconsistencies and deferred decisions found during doc review — not blocking the local proof of concept |
| [POC_RESULTS.md](POC_RESULTS.md) | Local proof-of-concept measurement report (DEF-25) |
