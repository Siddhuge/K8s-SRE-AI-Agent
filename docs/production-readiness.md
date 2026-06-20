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

**Test status:** 67 tests green (unit + live integration; 38 live-backend tests run in the
CI integration job). `ruff` clean, `mypy --strict` clean, `helm lint` clean, Docker image
builds **and boots** (`build_server()` constructs the full MCP server in the distroless image).

## ✅ Now validated live (against real backends: kind, a real AKS cluster, real Slack/pgvector)

Since the original assessment, these were exercised end-to-end against real systems —
each pass found and fixed genuine bugs that only live testing surfaces:

| Backend | Tools validated | Bugs found & fixed |
|---------|-----------------|--------------------|
| **Kubernetes** | all read tools + `rca_diagnose` across 9+ failure classes | `logs_pod` bytes/`b'...'`; namespace-wide event leakage; false change-attribution; mid-restart flakiness; liveness fallback after event expiry; init-container/Job/eviction/HPA/PDB detectors |
| **Prometheus** | `prom_query`, `metric_cpu/memory/restarts/disk` | range queries 400'd (`now-1h` → real unix start/end); `metric_disk` hard-coded `mountpoint="/"` |
| **Loki / Grafana** | `loki_query`, `grafana_panel` | (clean) |
| **Istio** | `istio_get_*` + `istio_mesh_analyze` (subset, mTLS, gateway) + sidecar detector | (new tooling, validated) |
| **ArgoCD** | `argocd_app/sync_status/history/rollback_info` | added `conditions` (ComparisonError) so failures explain themselves |
| **GitHub Actions** | `github_actions_runs`, `recent_deployments`, `compare_deployments` | empty-token sent `Bearer ` → 401; now omits auth for public repos |
| **GitLab CI** | `gitlab_pipelines` | clean (the 403 seen was a PAT scope issue — needed `read_api` — not a tool bug) |
| **RAG (pgvector)** | hybrid vector+lexical retrieval, tenant RLS | `SET app.tenants` can't bind → `set_config`; NULL `service` AmbiguousParameter → `::text`; RLS silently bypassed by owners → `FORCE ROW LEVEL SECURITY` + least-priv role (proven with `sre_ro`) |
| **Full MCP → Claude loop** | agent registered with Claude Code; an incident driven end-to-end | the model correctly **overrode** the engine's 94% secret-rotation verdict, diagnosing a missing DB service — tool planning + hypothesis arbitration validated |
| **AKS Workload Identity** | `azure_workload` federated-token path + `rca_diagnose` on managed AKS | **bearer header sent via `api_key` → no `Authorization` header → 401**, fixed to `set_default_header` (also fixes EKS/IRSA); **one forbidden read aborted the whole RCA**, fixed to degrade per-read. Also confirmed: write → 403, secret values → 403 (read-only holds under Azure RBAC) |
| **Slack** | `slack_post` | clean — real `chat.postMessage` succeeded; outward-facing allow-list guard refuses non-listed channels before any API call |

## ⚠️ Still requires YOUR environment to validate

Written defensively and unit-tested, but not yet run against the real system:

| Item | Why it needs your environment | Risk if skipped |
|------|-------------------------------|-----------------|
| **Jira / ServiceNow / Teams** | Need real tenants/tokens/webhook (not available) | incident read / Teams post unproven (Slack is proven; these share the same guarded HTTP pattern) |
| **EKS IRSA token path** | Needs a real EKS cluster | optional — shares the exact `set_default_header` code path **already proven on AKS**, so low risk |
| **Load / soak / failover at scale** | Soak validated on kind at 3 replicas (3/3 Ready, stable); full capacity + HPA-threshold tuning needs your traffic profile | thresholds may need tuning |

## Recommended path to "trusted in prod"

1. Deploy to staging via the Helm chart with `rbac.scope: namespaced` for one tenant.
2. Point each remaining integration (Jira/ServiceNow/Teams) at the real backend and run
   a smoke RCA + post — fix any field-mapping bugs that surface (the `logs_pod` and
   `set_default_header` fixes are the template).
3. (Optional) Exercise the EKS IRSA token path against a real cluster — the shared code
   is already proven on AKS.
4. Measure RAG recall on a labeled incident→runbook set from your own corpus.
5. Load-test the gateway at your traffic profile; tune HPA/PDB/rate-limit.
6. Pen-test the auth boundary and the read-only RBAC (attempt a write — must 403;
   verified on AKS, re-confirm in your tenant).

## Bottom line

The **core and almost every integration are production-ready and proven against real
backends** — Kubernetes, Prometheus, Loki, Grafana, Istio, ArgoCD, GitHub, GitLab, the
RAG pipeline, the full MCP→Claude reasoning loop, **AKS Workload Identity cloud-auth**,
and **Slack** notifications. The read-only security model holds under real Azure RBAC
(writes and secret values → 403). Live testing found and fixed ~12 genuine bugs that
fixture tests never would — including two on the real AKS cluster (the bearer-auth header
and the RCA's resilience to denied reads) that no mock could have caught. What **still
needs your environment** is narrow: Jira/ServiceNow/Teams (no tenants available),
optional EKS (code proven via AKS), RAG recall on your corpus, and load tuning at your
traffic profile — integration validation, not redesign.
