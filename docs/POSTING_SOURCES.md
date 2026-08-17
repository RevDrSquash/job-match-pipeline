# Posting Sources

> **Status: preliminary.** Based on reading the OpenPostings README and repo metadata, not the source.

## Reference implementation

**OpenPostings** — [https://github.com/Masterjx9/OpenPostings](https://github.com/Masterjx9/OpenPostings)
"The OpenSource ATS Aggregator." React Native client + Node/Express API + local SQLite + an MCP apply-agent server.

### What's genuinely valuable here

**The company registry, not the fetching code.** OpenPostings covers 100,000+ companies across 80+ ATS providers, assembled from search engine data (Google, DuckDuckGo) plus subdomain and directory search techniques.

That assembly is the hard part. Writing a Greenhouse fetcher is an afternoon; knowing which 100k companies have boards and on which ATS is months of work.

**The approach is the durable one.** OpenPostings goes ATS-direct — Greenhouse, Lever, Ashby, Workday, iCIMS, BambooHR, Jobvite, and ~75 others — rather than scraping job boards. This matters: JobFunnel, a well-known board scraper, was archived by its author, who noted it was built when boards exposed mostly static HTML, and that boards have since moved to aggressive anti-automation where browser-automation rewrites are too slow, fragile, and operationally complex. Per-company ATS endpoints don't have that problem.

## ⚠️ Blocker: no LICENSE file

The repository root file listing contains no LICENSE file. **Absent an explicit license, all rights are reserved by default and we cannot legally reuse the code.**

Before any code reuse:

* Re-check for a license (may be in `docs-site/`, `package.json`, or added since)
* If absent, contact the author — with 273 stars and 39 forks, a permissive license is likely intended and simply missing
* Until resolved, treat OpenPostings as a **reference for approach only**. Reading it to understand ATS endpoint patterns is fine; copying code is not.

Note that ATS endpoint patterns themselves are facts about public APIs, not copyrightable expression. We can build our own adapters informed by knowing which endpoints exist.

## Volume: ~10k/day distinct new postings

Running OpenPostings locally yields ~10k new postings/day. The author's "250,000+ jobs a day" figure (from a social post at ~7,500 companies) implies ~33 new postings per company per day, which is implausible as an average — it almost certainly describes the full sync result set, i.e. total active postings, not distinct new ones.

Bottom-up sanity check supports the lower number: ~100k companies × ~40% with open roles × ~8 open postings ÷ ~35-day average posting lifetime ≈ ~9k new/day.

Treat ~10k/day as a **floor**. Random sampling misses postings that appear and expire between samples, so systematic coverage should raise it — assume ~2–2.5×. See Cost Model.

Note: the freshness window is now configurable in OpenPostings from 24 hours up to 168 hours, so the hard 24-hour constraint is looser than the README implies.

## Building our own company registry

The registry ships as `jobs.db`, a 35MB SQLite file committed to the repo root — data, not a discovery script. The author bulk-adds periodically (visible in commit history: 7,500 → 37,000 → 100,664 companies). The Chrome extension's `POST /extension/seeded-source/upsert` adds sources to a *local* DB; it is not an upstream contribution path.

Given the license situation, we build our own. Methods, roughly by yield:

**Discovery**

* **Slug probing against public ATS APIs.** Greenhouse (`boards-api.greenhouse.io/v1/boards/{token}/jobs`), Lever (`api.lever.co/v0/postings/{org}`), and Ashby expose public JSON endpoints intended for consumption. Generate slug candidates from company names, probe, keep hits. Cheap, fast, high precision.
* **Common Crawl.** Query the index for links to `myworkdayjobs.com`, `greenhouse.io`, `jobs.lever.co` etc. from `.ca` domains. The systematic version of the author's "search engine data" method; best source for companies we don't already know about.
* **Certificate transparency logs ([crt.sh](http://crt.sh)).** Free subdomain enumeration — catches self-hosted `careers.company.ca` boards. Almost certainly one of the "subdomain searching techniques."
* **Search dorking** — `site:jobs.lever.co` + Canadian city names. Works, rate-limited, least systematic.

**Seed lists (Canadian focus — the existing registry is light here)**

* Corporations Canada / ISED open data; provincial registries (BC Registry) — free, exhaustive, noisy
* Regional tech directories: Communitech, MaRS, BetaKit, Vancouver Tech Journal
* YC and Techstars Canadian cohorts
* Crunchbase (paid, clean, well-structured)

Registry + probe is plausibly a weekend for a few thousand validated Canadian companies — likely better coverage of our target market than `jobs.db` has today.

## ⚠️ Architectural mismatch: no 1M corpus exists

Our docs originally assumed a ~1M-posting initial backfill. **OpenPostings' model does not produce one.**

Per its README: it pulls jobs posted in the last 24 hours (configurable to 168h) or with no posted date; older postings are deleted; and it pulls new job data **at random** from companies.

So the model is a rolling freshness window with random company sampling, not an accumulating corpus. Two consequences:

### 1. There is no backfill cost

There is no 1M-posting corpus to extract. Revised: we accumulate our own corpus over time by running steady-state ingest and *not* deleting after the freshness window. Reaching 1M postings takes months at ~10k/day.

This is arguably fine, possibly better — job postings go stale fast, and matching a user to a three-month-old posting wastes a generation. But it changes cold start (see below).

### 2. Random sampling is not good enough for us

OpenPostings samples companies at random because it's a browsing tool — a user scrolling a feed doesn't care which companies appear. We need **coverage**: a user's best match may sit at a company we never sampled, and that failure is invisible (it's a false negative, the category Evaluation Strategy flags as unrecoverable from production feedback).

We need systematic sweeps of the company registry, not random sampling. That raises fetch volume — but not extraction volume, since URL-hash dedup keeps re-seen postings out of the LLM path.

## Cold start without a backfill

A new user on day one has only whatever corpus we've accumulated. Options:

* Accept it — the product is "new jobs matched daily," and a thin first week is tolerable if framed correctly
* Prioritized sweep at signup — sweep companies matching the new user's filters first, giving them results faster
* Retain longer than the freshness window (30–60 days) so the corpus has depth, with staleness marked in the UI rather than deleted

Leaning toward the third: retention is cheap (1M postings ≈ 3GB with vectors), and a 30-day-old posting is still worth showing with a staleness flag.

Note the interaction with lazy extraction: a new user's first match cycle may trigger a burst of `extract-job` tasks across the accumulated corpus. Rate-limit that queue accordingly.

## What we'd build ourselves

| Piece | Reuse? | Notes |
| -- | -- | -- |
| Company registry (which company, which ATS) | **High value** — blocked on license | Rebuilding via subdomain/directory search is significant work |
| ATS endpoint adapters | Reference only | ~80 providers; we likely need only the top 5–10 initially |
| Fetch scheduling / sweep logic | No | Their random model doesn't fit our coverage requirement |
| Storage, dedup, lifecycle | No | Local SQLite ephemeral vs. our Cloud SQL accumulating |
| Apply agent / MCP server | **No — deliberately** | They automate submission; we do not (see Architecture) |

## Overlap note

OpenPostings' MCP apply-agent already drafts cover letters and applies via browser automation. Our differentiation is therefore **matching quality, resume tailoring, and the verification layer** — not aggregation. Aggregation is table stakes and already open-source.

## Cross-board deduplication

Not addressed by OpenPostings, and a real problem for us: the same role often appears on multiple ATS surfaces and aggregators. Duplicates show up as visibly broken matching ("why am I seeing this five times?") and waste generations.

Dedup key candidates: normalized (company, title, location) plus posting-body similarity. Needs design; the canonical skill extraction may help.

## Open questions

* License status — blocking for any code reuse
* Actual daily posting volume from a systematic sweep of the top ATS providers
* Sweep cadence per company — how often do we re-check a board?
* Retention window: freshness-window-only, 30d, 60d, indefinite?
* ToS review of the ATS endpoints themselves. We took submission-side ToS seriously; the ingest side deserves the same scrutiny.
* Cross-board dedup strategy
* Do we rebuild the company registry, or is there a licensable/public source?
