# Design docs

**These docs are the source of truth for all design decisions** — they record the owner's decisions and the reasoning behind them, and they outrank the code, not the owner. If an implementation diverges from a doc, update the doc in the same change. If the owner directs a change that contradicts a doc, that is a design change: update the doc and the code together (see `AGENTS.md`, "Read the docs first").

| Document | Contents |
| -- | -- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Design principles, target scale, data flow, matching approach, verification design, spend control |
| [INFRASTRUCTURE.md](INFRASTRUCTURE.md) | GCP platform choice (Cloud Tasks + Cloud Run + Cloud SQL), Terraform strategy, local development stack |
| [TASKS_AND_HANDLERS.md](TASKS_AND_HANDLERS.md) | The seven pipeline handlers, queue configuration, data model sketch |
| [COST_MODEL.md](COST_MODEL.md) | Per-job and per-user cost estimates, pricing structure, cost reduction levers |
| [EVALUATION.md](EVALUATION.md) | Per-stage eval strategy; the four non-negotiable evals; label bootstrapping |
| [PRIVACY_AND_COMPLIANCE.md](PRIVACY_AND_COMPLIANCE.md) | PIPEDA / BC PIPA analysis, model selection and data residency, deletion design |
| [POSTING_SOURCES.md](POSTING_SOURCES.md) | OpenPostings reference, license blocker, volume reconciliation, company registry plan |
| [UI.md](UI.md) | Surfaces, free-tier public search, feedback capture, admin dashboard, hosting, local UI milestone (Next.js + `/api/*` layer) |
| [OPEN_ISSUES.md](OPEN_ISSUES.md) | Inconsistencies and deferred decisions found during doc review — not blocking the local proof of concept |
| [POC_RESULTS.md](POC_RESULTS.md) | Local proof-of-concept measurement report (DEF-25) |
