# Root Cause Analysis Engine

The RCA engine turns a vague "X is broken" into a structured, confidence-scored report.
It is **deterministic where it can be** (collection + correlation + rule-based detection)
and leaves **judgment to Claude** (arbitration between hypotheses, the human narrative,
deciding whether to notify). This split keeps the reasoning auditable and the token cost low.

## Pipeline

```
rca_diagnose(cluster, namespace, subject)
        │
        ▼
1. COLLECT   ── pods, describe(target), events (Warnings),
   (read-only)   logs (current + previous container), nodes,
                 restart/memory signals, secret ages
        │
        ▼
2. CORRELATE ── first_failure_time(events)
                find_recent_change(syncs|deploys|secret rotations) ranked by
                proximity to the first failure
                build_timeline()
        │
        ▼
3. DETECT    ── run every rule-based detector; each emits a Hypothesis with
                weighted Evidence + a suggested fix + rollback flag
        │
        ▼
4. GROUND    ── (optional) RAG lookup for a matching runbook; small confidence bump
        │
        ▼
5. REPORT    ── RCAReport: severity, root cause, confidence %, evidence,
                suggested fix, rollback target, timeline, alternatives
```

`collect_context` is the high-value step: instead of Claude issuing a dozen tool calls
and paying for a dozen round-trips, one `rca_diagnose` call gathers bounded, summarized
context. ([src/k8s_sre_agent/rca/engine.py](../src/k8s_sre_agent/rca/engine.py))

## Detectors (explainable rules)

Each detector is a pure function `context → Hypothesis | None`. It owns one failure
signature, emits **weighted evidence** (the weights sum toward a confidence score), and
proposes a fix. ([src/k8s_sre_agent/rca/detectors.py](../src/k8s_sre_agent/rca/detectors.py))

| Detector | Signature it matches | Distinguishing signals |
|----------|----------------------|------------------------|
| `crashloop_db_connection` | CrashLoopBackOff | DB connection errors in *previous* logs; secret rotation / release just before T0 |
| `image_pull` | ImagePullBackOff / ErrImagePull | `not found` (bad tag) vs `unauthorized`/`denied` (pull secret) |
| `container_config_error` | CreateContainer(Config)Error | missing ConfigMap vs missing Secret reference |
| `oom_killed` | OOMKilled | last-terminated reason + rising memory trend |
| `probe_failure` | Liveness/Readiness probe failures | `Unhealthy` events; liveness → restart loop |
| `pending_unschedulable` | Pending / FailedScheduling | insufficient cpu/mem vs taint vs affinity vs PVC binding |
| `node_pressure` | NodeNotReady / Disk/Memory/PID pressure | node conditions |
| `tls_certificate` | TLS errors | x509/expired/verify-failed in logs |
| `dns_failure` | DNS errors | "no such host" / resolution failures in logs |

Detectors return ranked by confidence; the engine promotes the top one and carries the
next three as `alternative_hypotheses` so Claude can reason about competing causes.

## Confidence scoring

Confidence starts at the detector's base (signature certainty) and is adjusted by
corroborating evidence:

```
base signature match            e.g. CrashLoopBackOff present        +40
high-signal log evidence        DB timeout in previous-container log +40
change correlation              secret rotated / release just before +14
RAG runbook match               documented procedure exists           +3
                                                          capped at   95–97
```

A low score (no signature matched) returns an explicit "no known signature —
investigate directly" report rather than a confident guess. Honesty over false certainty.

## Example output

`rca_diagnose` returns both a structured object and a rendered markdown block:

```
Severity: High
Cluster / Namespace: aks-prod / payments
Subject: api
Issue: CrashLoopBackOff
Root Cause: Application cannot connect to its database (connection refused/timeout
            in crash logs). Secret 'db-credentials' rotated 3m before failures began.
Evidence:
- Pod in CrashLoopBackOff
- DB connection error in previous-container logs
- Secret db-credentials rotated 2026-06-13T10:00:00Z before crash
Confidence: 94%
Suggested Fix: Verify the new db-credentials value; restart the deployment.
Rollback Required: No
```

This matches the target output shape from the brief. The matching unit tests live in
[tests/test_rca.py](../tests/test_rca.py) (all green, no cluster required).

## Why not "just let the model figure it out"?

A pure-LLM approach is non-deterministic, expensive (huge exploratory context), and hard
to audit. By making collection + correlation + signature detection deterministic, we get:
* **reproducibility** — the same incident yields the same evidence,
* **auditability** — every verdict cites weighted evidence,
* **lower cost** — bounded context instead of a sprawling tool loop,
* **better model output** — Claude reasons over clean evidence instead of raw noise.

The model still does what it's best at: weighing ambiguous evidence, explaining clearly,
and recommending the right next action.
