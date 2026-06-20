# RCA Evaluation Harness

Measures whether the agent's **diagnoses are trustworthy** — not just that the code runs.
It scores the deterministic detector engine (`evaluate(ctx)`), the explainable layer the
LLM reasons over, against a labeled dataset.

```bash
make eval                          # or: PYTHONPATH=src python3 evals/run_eval.py
```

Exit code is non-zero if any gate fails, and `tests/test_evals.py` runs it in CI — so a
regression that makes the engine confidently wrong **blocks the build**.

## Why detectors, not the LLM

This harness deliberately does **not** call the model: that keeps it free, deterministic,
and CI-gateable. The detectors produce the issue class, the confidence score, and the
evidence — i.e. everything the LLM arbitrates over. If the detectors are accurate and
well-calibrated, the model has good material to reason with; if they regress, this catches
it. A model-in-the-loop eval (accuracy of the *final* arbitrated answer) is a separate,
cost-incurring harness and would live alongside this one.

## What it measures

| Metric | Why it matters |
|--------|----------------|
| **Top-1 accuracy** | Does the headline diagnosis match ground truth? (gate: ≥ 90%) |
| **Recall@3** | Is the right cause at least *among* the hypotheses (for ambiguous cases)? |
| **Confidence calibration** | Does an 88% really mean ~88% right? Buckets predictions by confidence and reports the hit rate per bucket. |
| **Confident-but-wrong** | Wrong top-1 at ≥ 80% confidence — the most trust-eroding failure. (gate: 0) |
| **False positives** | A finding on a healthy pod — paging on noise. (gate: 0) |

## The dataset

`cases/*.json` — each file is an array of cases. A case is a synthetic context bundle
(`ctx`, exactly the shape `collect_context` produces) plus a ground-truth `expected`
label. The seed set is harvested from the detector scenarios validated live on kind/AKS,
so the labels are real, not invented.

### Case schema

```jsonc
{
  "name": "crashloop-db-auth",
  "description": "...",
  "ctx": { /* pods, describe, logs, previous_logs, events, services, hpas, ... */ },
  "expected": {
    "issue": "CrashLoopBackOff",        // null = expect NO finding (negative case)
    "match": "top1",                    // or "top3" (label need only be ranked top-3)
    "min_confidence": 90,               // optional bounds
    "max_confidence": 74,
    "rollback_required": false,         // optional
    "root_cause_contains": ["credential"],   // all substrings must appear
    "evidence_contains": ["orders"],         // appear in some evidence line
    "evidence_excludes": ["secret"],         // must NOT appear (false-attribution guard)
    "forbid_issue": "Pending Pods"           // this class must not appear at all
  }
}
```

## Adding cases

Drop a new object into a `cases/*.json` array (or add a new file). The best cases come
from **real incidents**: capture the `ctx` the agent collected, label the true cause, and
add it — especially cases the engine got *wrong*, so the suite guards against regressions.
Grow this set and the accuracy/calibration numbers become a real, trend-able quality bar.
