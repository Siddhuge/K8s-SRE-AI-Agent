# Cost Optimization

Three reference architectures, a Claude **Opus vs Sonnet** comparison for incident
analysis, and a token/cost model you can plug your own volumes into.

> Pricing used (per 1M tokens, as of this writing — verify against your account):
>
> | Model | Input | Output | Notes |
> |-------|------:|-------:|-------|
> | **Claude Opus 4.8** (`claude-opus-4-8`) | $5.00 | $25.00 | deepest reasoning; ambiguous/multi-cause RCA |
> | **Claude Sonnet 4.6** (`claude-sonnet-4-6`) | $3.00 | $15.00 | strong + fast; most triage |
> | **Claude Haiku 4.5** (`claude-haiku-4-5`) | $1.00 | $5.00 | classification/routing/summaries |
>
> **Prompt caching** (cache write ≈ 1.25× input, cache read ≈ 0.1× input) is the single
> biggest lever for an agentic, multi-turn workload like this — see §4.

## 1. The three architectures

### A. Low-cost POC

| Aspect | Choice |
|--------|--------|
| Transport | `stdio`, launched by Claude Code on an engineer's laptop |
| Clusters | 1, `in_cluster` or local kubeconfig |
| RAG | **off** |
| Observability | existing Prometheus/Loki (no new infra) |
| Model | **Sonnet 4.6** for everything |
| Infra cost | ~$0 (no hosted components) |
| Variable cost | per-investigation API spend only (§3) |

Goal: prove value on real incidents with zero standing infrastructure.

### B. Production

| Aspect | Choice |
|--------|--------|
| Transport | central **HTTP gateway**, 2 replicas behind OIDC ingress |
| Clusters | a handful (multi-cluster registry, Workload Identity / IRSA) |
| RAG | **on** — pgvector (1 small managed Postgres) |
| Model | **tiered**: Haiku/Sonnet triage → Opus on demand (§2) |
| Standing infra | ~2× (100m–1 CPU, 256–512Mi) pods + 1 small Postgres (~$50–150/mo) |
| Variable cost | API spend scales with incident volume |

### C. Enterprise

| Aspect | Choice |
|--------|--------|
| Transport | HA gateway (HPA), multi-region, per-tenant instances |
| Clusters | many AKS/EKS/on-prem; per-tenant SA + vault + registry slice |
| RAG | pgvector with RLS, HNSW, nightly re-ingest CronJobs; or OpenSearch if standardized |
| Model | tiered + **prompt caching** + **Batch API** for non-urgent bulk RCA (50% off) |
| Governance | Kyverno policy locking the SA to read-only; full audit pipeline; cost dashboards |
| Standing infra | gateway fleet + HA Postgres + ingestion jobs |

## 2. Opus vs Sonnet for incident analysis

| Dimension | Sonnet 4.6 | Opus 4.8 |
|-----------|-----------|----------|
| Cost / investigation (typical, §3) | ~**$0.25** | ~**$0.42** |
| Latency | faster | slower (more deliberation) |
| Best at | clear single-cause incidents, triage, summarization, routine RCA | ambiguous / multi-cause incidents, conflicting evidence, novel failure modes, cross-system correlation |
| When to use | the **default** for ~80% of incidents | escalate when Sonnet's confidence is low, evidence conflicts, or the blast radius is large |

**Recommended policy — tiered routing:**

```
incident ──▶ Haiku 4.5: classify severity + route        (cheap, fast)
          ──▶ Sonnet 4.6: rca_diagnose + standard RCA      (default)
          ──▶ Opus 4.8: escalate IF confidence < 70%       (deep reasoning)
                         OR evidence conflicts
                         OR severity ∈ {Critical, High} on a tier-1 service
```

Because the deterministic RCA engine does the heavy context-gathering and the detectors
pre-score the hypothesis, **Sonnet handles most incidents at ~60% of Opus's cost**, and you
spend Opus dollars only where the extra reasoning actually changes the outcome.

## 3. Token model for one RCA investigation

Assumptions: ~6 model turns (incident → `rca_diagnose` → 3–4 targeted drill-downs →
written RCA). Tool results are **summarized** (token discipline — see
[architecture.md](architecture.md) §5), not raw YAML/log dumps.

| Turn | New content into context | Model output |
|------|--------------------------|--------------|
| System prompt + ~30 tool schemas (cached prefix) | ~6,000 | — |
| 1. Incident + first reasoning | ~500 | ~0.6k (calls `rca_diagnose`) |
| 2. `rca_diagnose` report | ~4,000 | ~0.7k (calls `logs_pod previous=true`) |
| 3. previous-container logs (filtered) | ~3,000 | ~0.6k (calls `argocd_history`) |
| 4. deploy history | ~1,500 | ~0.5k (calls `compare_deployments`) |
| 5. commit/file diff | ~2,500 | ~0.9k (calls `kb_search`) |
| 6. runbook excerpt | ~1,200 | ~2.5k (final RCA write-up) |

* **Output total** ≈ **6k tokens**.
* **Input total** — each turn re-sends the conversation, so billed input depends heavily
  on caching:
  * **Without caching**: ~**85k** input tokens (the prefix + growing body re-billed each turn).
  * **With prompt caching** (static prefix + conversation prefix cached): ~**40k**
    effective input tokens.

Per-investigation cost:

| | Input | Output | **Total** |
|--|------:|-------:|----------:|
| **Sonnet 4.6**, cached | 40k × $3/1M = $0.120 | 6k × $15/1M = $0.090 | **≈ $0.21** |
| **Sonnet 4.6**, uncached | 85k × $3/1M = $0.255 | $0.090 | ≈ $0.35 |
| **Opus 4.8**, cached | 40k × $5/1M = $0.200 | 6k × $25/1M = $0.150 | **≈ $0.35** |
| **Opus 4.8**, uncached | 85k × $5/1M = $0.425 | $0.150 | ≈ $0.58 |

(The $0.25 / $0.42 figures in §2 average cached+uncached with a realistic cache hit rate.)

## 4. Cost levers (in order of impact)

1. **Prompt caching** — biggest win for this workload. The ~6k-token system+tool prefix is
   identical on every turn; cache it. With multi-turn sessions, caching roughly **halves**
   input cost. Keep the prefix byte-stable (no timestamps/UUIDs in the system prompt).
2. **Tiered model routing** — Haiku→Sonnet→Opus (§2). Don't pay Opus rates for "ImagePull,
   bad tag" incidents.
3. **Token-frugal tools** — summaries not raw objects; `tail`+`grep` on logs; secret
   **metadata** only. A naive "kubectl get -o yaml" approach can be 10–50× more tokens.
4. **Deterministic RCA engine** — one `rca_diagnose` call replaces a long exploratory tool
   loop, cutting both turns and tokens.
5. **Batch API** — for non-urgent bulk work (nightly fleet health RCA, postmortem
   drafting), the Batches API is **50% off**.
6. **Bounded context** — cap log tails and metric windows; the detectors only need the
   failure window, not hours of history.

## 5. Worked monthly estimates

Assume **cached** per-investigation costs from §3 and a tiered mix (≈80% Sonnet / 20% Opus,
blended ≈ $0.24/investigation).

| Scenario | Investigations / mo | API spend | + Standing infra | **≈ Monthly total** |
|----------|--------------------:|----------:|-----------------:|--------------------:|
| **POC** (Sonnet only, $0.21) | 200 | ~$42 | $0 | **~$42** |
| **Production** (tiered, $0.24) | 2,000 | ~$480 | ~$100 (pods + small PG) | **~$580** |
| **Enterprise** (tiered + caching + some Batch) | 20,000 | ~$3,800 | ~$1,200 (HA gateway + HA PG + ingest) | **~$5,000** |

These scale roughly linearly with incident volume; the standing infra is small relative to
API spend, so the **model-routing + caching** levers dominate the bill. Stand up a cost
dashboard (per-model token usage from `usage` fields) and alert on cost-per-investigation
drift — a sudden rise usually means caching broke (a silent prefix invalidator) or routing
sent too much to Opus.
