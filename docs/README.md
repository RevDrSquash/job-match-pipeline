# Design docs

**The repo is canonical for design docs.** These were drafted as Linear project documents and mirrored here. Going forward, edit these copies — Linear documents are a snapshot for discussion, not a second source of truth. If the two diverge, the repo wins.

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
