# Production Readiness

An honest status of what's been **verified** vs what still needs **your staging
environment** to sign off. Nothing below is marked done unless it was actually run.

## ✅ Closed and verified (runs + tested)

| Gap (from the earlier assessment) | What was done | Evidence |
|-----------------------------------|---------------|----------|
| Inbound auth not wired | OIDC/Entra bearer validation now enforced as ASGI middleware on every non-public path ([middleware.py](../src/k8s_sre_agent/middleware.py)); authorization policy is a pure, unit-tested function | `test_auth_resilience.py` |
| No `/healthz` | `/healthz` (process) + `/readyz` (default-cluster reachability) + `/metrics` routes added; probes fixed in Helm | `helm lint` clean |
| Tools crash on backend errors | `@tool_guard` wraps every tool → structured `{"error": kind}` results, no internal-detail leakage, metered | `test_auth_resilience.py` |
| Secret-change correlation was a stub | Real rotation timestamps from `managedFields` (read-only, no audit-log dependency) | live kind test + `test_integration_kind.py` |
| No rate limiting | Per-principal token bucket + 429 handling | `test_ratelimit.py` |
| No agent self-observability | JSON audit logs + Prometheus `/metrics` (tool calls, latency, auth, rate-limits) | `observability.py` |
| No fail-fast config validation | `Settings.validate_for(transport)` refuses to start on insecure/broken config | `server.py` |
| k8s read tools unverified | `logs_pod` + `rca_diagnose` run against a live kind cluster; the bytes/str client bug fixed | `test_integration_kind.py` (4 live tests) |
| No CI | GitHub Actions: ruff, mypy, unit tests, **kind integration job**, Docker build + Trivy scan, helm lint | [.github/workflows/ci.yaml](../.github/workflows/ci.yaml) |
| Helm not deployable | `/readyz` probe, PDB, HPA, ServiceMonitor, hardened securityContext | `deploy/helm` |

**Test status:** 17 tests green (13 unit + 4 live integration). `ruff` clean. `helm lint` clean.

## ✅ Now validated live (against real backends on a kind cluster)

Since the original assessment, these were exercised end-to-end against real systems —
each pass found and fixed genuine bugs (the kind only live testing surfaces):

| Backend | Tools validated | Bugs found & fixed |
|---------|-----------------|--------------------|
| **Kubernetes** | all read tools + `rca_diagnose` across 9 failure classes | `logs_pod` bytes/`b'...'`; namespace-wide event leakage; false change-attribution; mid-restart flakiness; liveness fallback after event expiry |
| **Prometheus** | `prom_query`, `metric_cpu/memory/restarts/disk` | range queries 400'd (`now-1h` → real unix start/end); `metric_disk` hard-coded `mountpoint="/"` |
| **Loki** | `loki_query` | (clean) |
| **Istio** | `istio_get_*` + `istio_mesh_analyze` (subset, mTLS, gateway) + sidecar detector | (new tooling, validated) |
| **ArgoCD** | `argocd_app/sync_status/history/rollback_info` | added `conditions` (ComparisonError) so failures explain themselves |
| **GitHub Actions** | `github_actions_runs`, `recent_deployments`, `compare_deployments` | empty-token sent `Bearer ` → 401; now omits auth for public repos |

## ⚠️ Still requires YOUR staging environment to validate

Written defensively and unit-tested, but not yet run against the real system:

| Item | Why it needs staging | Risk if skipped |
|------|----------------------|-----------------|
| **GitLab CI tool** | No GitLab instance was available to test against (GitHub was) | `gitlab_pipelines` may have field-mapping bugs until run |
| **Jira / ServiceNow / Slack / Teams** | Need real tenants/tokens | incident read/post unproven |
| **AKS Workload Identity / EKS IRSA token paths** | Need a real AKS/EKS cluster + federated identity to exercise | auth to those clusters unproven |
| **Full MCP → Claude loop** | Needs the agent registered with Claude + an API key; the *reasoning* layer (tool planning, hypothesis arbitration) has only been reasoned about, not run | tiered routing / caching behavior unvalidated |
| **RAG retrieval quality** | Needs a real pgvector instance + your ingested runbooks; retrieval recall must be measured on your corpus | runbook matches unproven |
| **Load / soak / failover** | No load test yet | capacity + HPA thresholds unproven |

## Recommended path to "trusted in prod"

1. Deploy to staging via the Helm chart with `rbac.scope: namespaced` for one tenant.
2. Point each integration at the real backend and run a smoke RCA per failure class
   (use the scenarios in [incident-scenarios.md](incident-scenarios.md)) — fix any
   field-mapping bugs that surface (the `logs_pod` fix is the template).
3. Exercise the AKS/EKS token paths against a real cluster.
4. Register with Claude, run the full loop on shadow/replayed incidents, and confirm
   confidence scores + tiered routing behave.
5. Measure RAG recall on a labeled incident→runbook set.
6. Load-test the gateway; tune HPA/PDB/rate-limit.
7. Pen-test the auth boundary and the read-only RBAC (attempt a write — must 403).

## Bottom line

The **core plus the Kubernetes, Prometheus, Loki, Istio, ArgoCD and GitHub integrations
are production-ready and proven against real backends** — read-only security model,
resilient tools, real change-correlation, enforced auth, health/metrics, rate limiting,
CI that stands up all of those stacks, and a deployable hardened chart. Live testing
found and fixed ~10 genuine bugs that fixture tests never would. What **still needs a
staging pass** is narrower now: GitLab, the incident systems (Jira/ServiceNow/Slack/
Teams), the cloud-auth token paths (AKS/EKS), the full MCP→Claude reasoning loop, RAG
recall on your corpus, and load/failover — integration validation, not redesign.
