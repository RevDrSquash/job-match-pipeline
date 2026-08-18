# Local proof-of-concept results

> Measured 2026-08-18T00:03:02.488805Z. Figures come from `pipeline_events.details` and `jobmatch evals run` against the versioned eval set. Resume text is never recorded here.

## Run setup

| Item | Value |
| -- | -- |
| Corpus size | 500 |
| Extracted jobs | 0 |
| Users | 1 |
| Title families | Software Engineering |
| Locations | (unconstrained) |
| Work arrangement | remote, hybrid, onsite |
| Comp floor | — |

- No LLM_API_KEY/GEMINI_API_KEY — seed + profile ingest (fallback) and offline evals only. Extraction/screening/generation need a key.
- EMBEDDING_PROVIDER='hashing'. DEF-25 measurement requires gemini end-to-end; hashing is plumbing-only.
- Test profile is the in-repo fixture (`tests/fixtures/sample_resume.md`), the same persona as evals/sets/v1. Real owner resumes are not committed (docs/PRIVACY_AND_COMPLIANCE.md).
- Profile user_id=1ba1a480-5b7e-40f3-a43f-e82c74602e1b quota=3
- Prefilter location sensitivity on 500 seed jobs (title family held constant): unconstrained=83 (16.6%), Remote=7 (1.4%), Vancouver=0 (0.0%).
- Skipped match-batch / queue path — no live LLM key.
- Evals set=v1 PASS → 20260818T000302Z.json

## Eval results (four non-negotiables)

**Set version:** `v1`  
**Overall:** PASS  
**Embedding provider:** `hashing`

| Suite | Result | n | Headline | Tokens in/out | Cost |
| -- | -- | -- | -- | -- | -- |
| extraction | PASS | 2 | fields title=1.00, seniority=1.00, comp=1.00; hard P/R=1.00/1.00 | 0/0 | $0.000000 |
| skill_linking | PASS | 3 | P/R=1.00/0.88; implicit R=0.00 | 0/0 | $0.000000 |
| retrieval | PASS | 8 | metadata=0.60 vector@5=0.60 rerank@5=0.60 | 186/0 | $0.000000 |
| fabrication | PASS | 5 | fabricated_claims=0 | 0/0 | $0.000000 |

Fabrication is a hard gate (target zero fabricated claims). This run: **0** fabricated claims across **0** pairs.

Warnings:
- extraction used the offline heuristic predictor (set LLM_API_KEY and omit --offline for the production extractor)
- EMBEDDING_PROVIDER=hashing is an offline stand-in and is not valid for matching-quality evals (docs/OPEN_ISSUES.md §6). Re-run with EMBEDDING_PROVIDER=gemini for a real recall@K number.
- fabrication used the offline grounded generator (set LLM_API_KEY and omit --offline to call generate-resume)

## Token counts and per-call cost

These are billed tokens from the live handler path (`QUEUE_IMPL=local`), not the eval-suite offline predictors. Cost uses the list-price rates in `app/config.py`.

| Stage | Calls | Mean prompt | Mean completion | Mean $/call | Range | Total $ |
| -- | -- | -- | -- | -- | -- | -- |
| extract-job | 0 | — | — | — | — | $0 |
| screen-job | 0 | — | — | — | — | $0 |
| generate-resume | 0 | — | — | — | — | $0 |
| verify-resume | 0 | — | — | — | — | $0 |

Gate cost is unmeasured in this snapshot (no `screen-job` LLM rows). `docs/OPEN_ISSUES.md` §1 stays open until a live gate distribution exists.

## Funnel survival

| Stage | Count / rate |
| -- | -- |
| Jobs ingested (seed) | 500 |
| Prefilter survivors (peak pairs / cycle) | 83 |
| Prefilter survival rate | 16.60% |
| Extracts enqueued | 0 |
| Jobs extracted | 0 |
| Matches written (peak / cycle) | 0 |
| Match / prefilter | 0.00% |
| Screened | 0 |
| Gate pass | 0 |
| Gate reject | 0 |
| Gate pass rate | — |
| Resumes generated | 0 |
| Verify passed | 0 |
| End-to-end of corpus | 0.00% |

The headline rate above is the current profile's metadata join (16.60% on this seed). The Cost Model's ~1% line is the **Remote-location** probe in the run notes, not the unconstrained title-only rate.

## Latency per stage

| Stage | n | Mean | p50 | p95 | Max |
| -- | -- | -- | -- | -- | -- |
| extract-job | 0 | — | — | — | — |
| screen-job | 0 | — | — | — | — |
| generate-resume | 0 | — | — | — | — |
| verify-resume | 0 | — | — | — | — |

## Reranker / gate disagreements

No `reranker_gate_disagreement` events (gate reject at `rerank_score >= RERANK_HIGH_SCORE_THRESHOLD`).

## Generated, verified resumes

No generations yet. The exit criterion is at least one resume produced through the local queue path (`QUEUE_IMPL=local`) and verified (`verify_status=passed`).

## Raw snapshot

Machine-readable copy of the measurement (no personal information):

```json
{
  "collected_at": "2026-08-18T00:03:02.488805Z",
  "corpus": {
    "jobs_total": 500,
    "extracted": 0,
    "extraction_coverage": 0.0,
    "users": 1,
    "matches": 0,
    "generations": 0,
    "verified": 0,
    "verify_passed": 0
  },
  "funnel": {
    "jobs_ingested": 500,
    "prefilter_pairs_peak": 83,
    "prefilter_survival_rate": 0.166,
    "extracts_enqueued": 0,
    "extract_events_done": 0,
    "jobs_extracted": 0,
    "matches_written_peak": 0,
    "match_survival_of_prefilter": 0.0,
    "screened": 0,
    "screen_events_done": 0,
    "gate_pass": 0,
    "gate_reject": 0,
    "gate_pass_rate": null,
    "generated": 0,
    "verify_passed": 0,
    "end_to_end_of_corpus": 0.0,
    "prefilter_sql_pairs": 83
  },
  "usage": {
    "extract-job": {
      "n": 0,
      "prompt_tokens_total": 0,
      "completion_tokens_total": 0,
      "cost_usd_total": 0,
      "prompt_tokens_mean": 0,
      "completion_tokens_mean": 0,
      "cost_usd_mean": 0.0,
      "cost_usd_min": 0.0,
      "cost_usd_max": 0.0,
      "latency_ms": {
        "n": 0,
        "mean": 0.0,
        "p50": 0.0,
        "p95": 0.0,
        "max": 0.0
      }
    },
    "screen-job": {
      "n": 0,
      "prompt_tokens_total": 0,
      "completion_tokens_total": 0,
      "cost_usd_total": 0,
      "prompt_tokens_mean": 0,
      "completion_tokens_mean": 0,
      "cost_usd_mean": 0.0,
      "cost_usd_min": 0.0,
      "cost_usd_max": 0.0,
      "latency_ms": {
        "n": 0,
        "mean": 0.0,
        "p50": 0.0,
        "p95": 0.0,
        "max": 0.0
      }
    },
    "generate-resume": {
      "n": 0,
      "prompt_tokens_total": 0,
      "completion_tokens_total": 0,
      "cost_usd_total": 0,
      "prompt_tokens_mean": 0,
      "completion_tokens_mean": 0,
      "cost_usd_mean": 0.0,
      "cost_usd_min": 0.0,
      "cost_usd_max": 0.0,
      "latency_ms": {
        "n": 0,
        "mean": 0.0,
        "p50": 0.0,
        "p95": 0.0,
        "max": 0.0
      }
    },
    "verify-resume": {
      "n": 0,
      "prompt_tokens_total": 0,
      "completion_tokens_total": 0,
      "cost_usd_total": 0,
      "prompt_tokens_mean": 0,
      "completion_tokens_mean": 0,
      "cost_usd_mean": 0.0,
      "cost_usd_min": 0.0,
      "cost_usd_max": 0.0,
      "latency_ms": {
        "n": 0,
        "mean": 0.0,
        "p50": 0.0,
        "p95": 0.0,
        "max": 0.0
      }
    }
  },
  "reranker_gate_disagreements": [],
  "delivered_resumes": [],
  "filters": [
    {
      "user_id": "1ba1a480-5b7e-40f3-a43f-e82c74602e1b",
      "title_families": [
        "Software Engineering"
      ],
      "locations": [],
      "comp_floor": null,
      "seniority_band": "mid,senior,staff",
      "work_arrangement": [
        "remote",
        "hybrid",
        "onsite"
      ]
    }
  ],
  "eval": {
    "set_version": "v1",
    "passed": true,
    "embedding_provider": "hashing",
    "suites": {
      "extraction": {
        "passed": true,
        "n": 2,
        "metrics": {
          "predictor": "HeuristicJobLLM",
          "field_accuracy": {
            "title": 1.0,
            "seniority": 1.0,
            "comp": 1.0,
            "location": 1.0,
            "work_arrangement": 1.0
          },
          "hard_requirements": {
            "precision": 1.0,
            "recall": 1.0,
            "f1": 1.0,
            "true_positives": 8,
            "false_positives": 0,
            "false_negatives": 0
          },
          "nice_to_haves": {
            "precision": 1.0,
            "recall": 1.0,
            "f1": 1.0,
            "true_positives": 6,
            "false_positives": 0,
            "false_negatives": 0
          }
        },
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost_usd": 0.0,
        "warnings": [
          "extraction used the offline heuristic predictor (set LLM_API_KEY and omit --offline for the production extractor)"
        ],
        "error": null
      },
      "skill_linking": {
        "passed": true,
        "n": 3,
        "metrics": {
          "overall": {
            "precision": 1.0,
            "recall": 0.875,
            "f1": 0.9333333333333333,
            "true_positives": 21,
            "false_positives": 0,
            "false_negatives": 3
          },
          "explicit": {
            "precision": 1.0,
            "recall": 1.0,
            "f1": 1.0,
            "true_positives": 21,
            "false_positives": 0,
            "false_negatives": 0
          },
          "implicit": {
            "precision": null,
            "recall": 0.0,
            "f1": null,
            "true_positives": 0,
            "false_positives": 0,
            "false_negatives": 3
          },
          "n_spans": 24,
          "n_documents": 3
        },
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost_usd": 0.0,
        "warnings": [],
        "error": null
      },
      "retrieval": {
        "passed": true,
        "n": 8,
        "metrics": {
          "k": 5,
          "embedding_provider": "hashing",
          "embedding_model": "hashing-embedder-v1",
          "reranker": "cosine",
          "n_corpus": 8,
          "n_relevant": 5,
          "n_metadata_survivors": 3,
          "metadata_dropped_relevant": 2,
          "metadata_dropped_relevant_ids": [
            "drop-comp-floor",
            "drop-title-sre"
          ],
          "metadata_recall": 0.6,
          "vector_recall_at_k": 0.6,
          "rerank_recall_at_k": 0.6,
          "recall_at_k_curve": {
            "1": {
              "vector": 0.2,
              "rerank": 0.2
            },
            "3": {
              "vector": 0.6,
              "rerank": 0.6
            },
            "5": {
              "vector": 0.6,
              "rerank": 0.6
            }
          }
        },
        "prompt_tokens": 186,
        "completion_tokens": 0,
        "cost_usd": 0.0,
        "warnings": [
          "EMBEDDING_PROVIDER=hashing is an offline stand-in and is not valid for matching-quality evals (docs/OPEN_ISSUES.md \u00a76). Re-run with EMBEDDING_PROVIDER=gemini for a real recall@K number."
        ],
        "error": null
      },
      "fabrication": {
        "passed": true,
        "n": 5,
        "metrics": {
          "fabricated_claims": 0,
          "pairs_with_fabrication": 0,
          "n_pairs": 5,
          "planted": false,
          "target": 0,
          "pairs": [
            {
              "id": "lacks-rust",
              "temptation": "missing_skill",
              "fabricated_claims": 0,
              "failure_codes": []
            },
            {
              "id": "adjacent-iac",
              "temptation": "adjacent_not_equivalent",
              "fabricated_claims": 0,
              "failure_codes": []
            },
            {
              "id": "year-scope-inflation",
              "temptation": "year_scope_inflation",
              "fabricated_claims": 0,
              "failure_codes": []
            },
            {
              "id": "seniority-inflation",
              "temptation": "seniority_inflation",
              "fabricated_claims": 0,
              "failure_codes": []
            },
            {
              "id": "employer-title-drift",
              "temptation": "employer_title_drift",
              "fabricated_claims": 0,
              "failure_codes": []
            }
          ],
          "source_char_count": 371
        },
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost_usd": 0.0,
        "warnings": [
          "fabrication used the offline grounded generator (set LLM_API_KEY and omit --offline to call generate-resume)"
        ],
        "error": null
      }
    }
  }
}
```
