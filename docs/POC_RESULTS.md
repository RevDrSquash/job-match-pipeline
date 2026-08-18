# Local proof-of-concept results

> Measured 2026-08-18T05:46:48.017819Z. Figures come from `pipeline_events.details` and `jobmatch evals run` against the versioned eval set. Resume text is never recorded here.

## Run setup

| Item | Value |
| -- | -- |
| Corpus size | 500 |
| Extracted jobs | 4 |
| Users | 1 |
| Title families | Backend Engineering |
| Locations | (unconstrained) |
| Work arrangement | remote, hybrid, onsite |
| Comp floor | — |

## Eval results (four non-negotiables)

**Set version:** `v1`  
**Overall:** FAIL  
**Embedding provider:** `gemini`

| Suite | Result | n | Headline | Tokens in/out | Cost |
| -- | -- | -- | -- | -- | -- |
| extraction | PASS | 2 | fields title=1.00, seniority=0.50, comp=0.50; hard P/R=1.00/1.00 | 1058/383 | $0.001275 |
| skill_linking | PASS | 3 | P/R=1.00/0.88; implicit R=0.00 | 0/0 | $0.000000 |
| retrieval | PASS | 8 | metadata=0.60 vector@5=0.60 rerank@5=0.60 | 186/0 | $0.000028 |
| fabrication | FAIL (RetryableLLMError) | 0 | fabricated_claims=— | 0/0 | $0.000000 |

Fabrication is a hard gate (target zero fabricated claims). This run: **—** fabricated claims across **—** pairs.

## Token counts and per-call cost

These are billed tokens from the live handler path (`QUEUE_IMPL=local`), not the eval-suite offline predictors. Cost uses the list-price rates in `app/config.py`.

| Stage | Calls | Mean prompt | Mean completion | Mean $/call | Range | Total $ |
| -- | -- | -- | -- | -- | -- | -- |
| extract-job | 4 | 1482 | 206 | $0.000979 | $0.000942–$0.001045 | $0.003916 |
| screen-job | 8 | 442 | 53 | $0.000266 | $0.000245–$0.000288 | $0.002126 |
| generate-resume | 5 | 1871 | 690 | $0.009237 | $0.007149–$0.009976 | $0.046186 |
| verify-resume | 5 | 2399 | 152 | $0.009470 | $0.009042–$0.010521 | $0.047352 |

**Open issue §1 (gate cost):** measured mean **$0.000266/call** over 8 live gate calls (prompt≈442, completion≈53). This is closer to **Cost Model (~$0.0002–0.0005)** than to Tasks and Handlers. At 100 calls/day that is ~$0.80/user/mo, vs the Cost Model's $0.50–1.50 screening line and the Tasks-and-Handlers $0.005 × 3,000 = $15/user/mo implication.

## Funnel survival

| Stage | Count / rate |
| -- | -- |
| Jobs ingested (seed) | 500 |
| Prefilter survivors (peak pairs / cycle) | 4 |
| Prefilter survival rate | 0.80% |
| Extracts enqueued | 0 |
| Jobs extracted | 4 |
| Matches written (peak / cycle) | 0 |
| Match / prefilter | 0.00% |
| Screened | 8 |
| Gate pass | 5 |
| Gate reject | 3 |
| Gate pass rate | 62.50% |
| Resumes generated | 5 |
| Verify passed | 0 |
| End-to-end of corpus | 0.00% |

The headline rate above is the current profile's metadata join (0.80% on this seed). The Cost Model's ~1% line is the **Remote-location** probe in the run notes, not the unconstrained title-only rate.

## Latency per stage

| Stage | n | Mean | p50 | p95 | Max |
| -- | -- | -- | -- | -- | -- |
| extract-job | 4 | 26976 ms | 43502 ms | 47252 ms | 47252 ms |
| screen-job | 8 | 1083 ms | 1038 ms | 1462 ms | 1462 ms |
| generate-resume | 5 | 8951 ms | 8397 ms | 11082 ms | 11082 ms |
| verify-resume | 5 | 5806 ms | 5191 ms | 7376 ms | 7376 ms |

## Reranker / gate disagreements

No `reranker_gate_disagreement` events (gate reject at `rerank_score >= RERANK_HIGH_SCORE_THRESHOLD`).

## Generated, verified resumes

5 generation(s), 0 with `verify_status=passed`. Resume text is omitted (personal information).

| Company | Title | Rerank | Verify |
| -- | -- | -- | -- |
| Discord | Senior Software Engineer, Safety Backend | 0.558 | failed |
| Asana | Senior Backend Software Engineer | 0.556 | needs_review |
| Asana | Senior Backend Software Engineer | 0.556 | failed |
| Asana | Backend Software Engineer | 0.542 | failed |
| Asana | Backend Software Engineer | 0.542 | needs_review |

## Raw snapshot

Machine-readable copy of the measurement (no personal information):

```json
{
  "collected_at": "2026-08-18T05:46:48.017819Z",
  "corpus": {
    "jobs_total": 500,
    "extracted": 4,
    "extraction_coverage": 0.008,
    "users": 1,
    "matches": 8,
    "generations": 5,
    "verified": 5,
    "verify_passed": 0
  },
  "funnel": {
    "jobs_ingested": 500,
    "prefilter_pairs_peak": 4,
    "prefilter_survival_rate": 0.008,
    "extracts_enqueued": 0,
    "extract_events_done": 4,
    "jobs_extracted": 4,
    "matches_written_peak": 0,
    "match_survival_of_prefilter": 0.0,
    "screened": 8,
    "screen_events_done": 8,
    "gate_pass": 5,
    "gate_reject": 3,
    "gate_pass_rate": 0.625,
    "generated": 5,
    "verify_passed": 0,
    "end_to_end_of_corpus": 0.0,
    "prefilter_sql_pairs": 4
  },
  "usage": {
    "extract-job": {
      "n": 4,
      "prompt_tokens_total": 5927,
      "completion_tokens_total": 825,
      "cost_usd_total": 0.003916,
      "prompt_tokens_mean": 1482,
      "completion_tokens_mean": 206,
      "cost_usd_mean": 0.000979,
      "cost_usd_min": 0.000942,
      "cost_usd_max": 0.001045,
      "latency_ms": {
        "n": 4,
        "mean": 26975.5,
        "p50": 43502.4,
        "p95": 47251.8,
        "max": 47251.8
      }
    },
    "screen-job": {
      "n": 8,
      "prompt_tokens_total": 3536,
      "completion_tokens_total": 426,
      "cost_usd_total": 0.002126,
      "prompt_tokens_mean": 442,
      "completion_tokens_mean": 53,
      "cost_usd_mean": 0.000266,
      "cost_usd_min": 0.000245,
      "cost_usd_max": 0.000288,
      "latency_ms": {
        "n": 8,
        "mean": 1082.9,
        "p50": 1037.8,
        "p95": 1461.9,
        "max": 1461.9
      }
    },
    "generate-resume": {
      "n": 5,
      "prompt_tokens_total": 9357,
      "completion_tokens_total": 3449,
      "cost_usd_total": 0.046186,
      "prompt_tokens_mean": 1871,
      "completion_tokens_mean": 690,
      "cost_usd_mean": 0.009237,
      "cost_usd_min": 0.007149,
      "cost_usd_max": 0.009976,
      "latency_ms": {
        "n": 5,
        "mean": 8951.3,
        "p50": 8397.2,
        "p95": 11082.1,
        "max": 11082.1
      }
    },
    "verify-resume": {
      "n": 5,
      "prompt_tokens_total": 11994,
      "completion_tokens_total": 758,
      "cost_usd_total": 0.047352,
      "prompt_tokens_mean": 2399,
      "completion_tokens_mean": 152,
      "cost_usd_mean": 0.00947,
      "cost_usd_min": 0.009042,
      "cost_usd_max": 0.010521,
      "latency_ms": {
        "n": 5,
        "mean": 5806.2,
        "p50": 5191.1,
        "p95": 7376.3,
        "max": 7376.3
      }
    }
  },
  "reranker_gate_disagreements": [],
  "delivered_resumes": [
    {
      "generation_id": "f7b2ba5d-7a2e-41e1-82a3-5bbb562ce126",
      "verify_status": "failed",
      "job_title": "Senior Software Engineer, Safety Backend",
      "company": "Discord",
      "rerank_score": 0.5576552867889404,
      "gate_verdict": "pass"
    },
    {
      "generation_id": "de41de66-efb1-491b-bfc3-57a8602551a4",
      "verify_status": "needs_review",
      "job_title": "Senior Backend Software Engineer",
      "company": "Asana",
      "rerank_score": 0.555548062589881,
      "gate_verdict": "pass"
    },
    {
      "generation_id": "dd0fc9d0-bfbe-46dd-aaf4-caa625f03f9e",
      "verify_status": "failed",
      "job_title": "Senior Backend Software Engineer",
      "company": "Asana",
      "rerank_score": 0.555548062589881,
      "gate_verdict": "pass"
    },
    {
      "generation_id": "a17b99aa-b3ba-4bf5-ade7-d97562d34110",
      "verify_status": "failed",
      "job_title": "Backend Software Engineer",
      "company": "Asana",
      "rerank_score": 0.5420797628784111,
      "gate_verdict": "pass"
    },
    {
      "generation_id": "a9c0f150-be57-498b-9be4-2783341efa02",
      "verify_status": "needs_review",
      "job_title": "Backend Software Engineer",
      "company": "Asana",
      "rerank_score": 0.5420797628784111,
      "gate_verdict": "pass"
    }
  ],
  "filters": [
    {
      "user_id": "fdd20f39-e314-4443-800c-155aa66daaba",
      "title_families": [
        "Backend Engineering"
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
    "passed": false,
    "embedding_provider": "gemini",
    "suites": {
      "extraction": {
        "passed": true,
        "n": 2,
        "metrics": {
          "predictor": "GeminiJobLLM",
          "field_accuracy": {
            "title": 1.0,
            "seniority": 0.5,
            "comp": 0.5,
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
        "prompt_tokens": 1058,
        "completion_tokens": 383,
        "cost_usd": 0.001275,
        "warnings": [],
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
          "embedding_provider": "gemini",
          "embedding_model": "gemini-embedding-001",
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
        "cost_usd": 2.8e-05,
        "warnings": [],
        "error": null
      },
      "fabrication": {
        "passed": false,
        "n": 0,
        "metrics": {},
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost_usd": 0.0,
        "warnings": [],
        "error": "RetryableLLMError"
      }
    }
  }
}
```
