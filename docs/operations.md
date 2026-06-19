# Operator Runbook

How to deploy, run, observe, and troubleshoot the K8s SRE Agent in production.

## Deploy

```bash
# 1. Read-only RBAC + ServiceAccount (the security boundary)
kubectl apply -f deploy/rbac/

# 2. (Recommended) lock the SA to read-only via policy — needs Kyverno
kubectl apply -f deploy/policy/kyverno-readonly-lock.yaml

# 3. The agent (gateway/HTTP mode), via Helm
helm install k8s-sre-agent deploy/helm/k8s-sre-agent -n sre-system --create-namespace \
  --set image.repository=ghcr.io/<org>/k8s-sre-agent --set image.tag=<ver> \
  --set existingSecret=k8s-sre-agent-secrets

# 4. Observability
kubectl apply -f deploy/observability/prometheus-rules.yaml      # alerts (Prometheus Operator)
# import deploy/observability/grafana-dashboard.json into Grafana
```

Secrets (`k8s-sre-agent-secrets`) come from your platform secret store (Key Vault /
Secrets Manager), projected as env vars — never baked into the image or git.

## Configure

* **Clusters**: `config/clusters.yaml` (mounted from a ConfigMap). One entry per cluster
  with `auth.mode` (`azure_workload` / `aws_eks` / `kubeconfig` / `in_cluster`),
  observability endpoints, and `allowedNamespaces` (tenant isolation). See
  [multi-cluster.md](multi-cluster.md).
* **Inbound auth** (HTTP): set `OIDC_ISSUER` / `OIDC_AUDIENCE` / `OIDC_REQUIRED_GROUPS`.
  The server refuses to start in HTTP mode without an issuer.
* **Notifications** are off by default. To enable: `ALLOW_NOTIFICATIONS=true` +
  `SLACK_ALLOWED_CHANNELS` and the relevant token.

## The read-only guarantee

The agent's ServiceAccount has only `get/list/watch` — no write/exec/scale, and Secret
*values* are unreadable (metadata API only). This is enforced by the API server and
**verifiable**: `kubectl auth can-i delete pods --as=system:serviceaccount:sre-system:k8s-sre-agent`
returns `no`. Tested in CI (`tests/test_integration_rbac.py`). See
[security-rbac.md](security-rbac.md).

## Observe

The agent exposes Prometheus metrics at `/metrics`. Scrape via the pod annotations
(`prometheus.io/scrape`) the Helm chart sets, or the bundled `ServiceMonitor`.

| Signal | Metric | Alert |
|--------|--------|-------|
| Tool error rate | `sre_agent_tool_calls_total{outcome!="ok"}` | `SreAgentHighToolErrorRate` (>25%) |
| Tool latency | `sre_agent_tool_duration_seconds` | (dashboard p95) |
| Auth failures | `sre_agent_auth_total{outcome=~"rejected\|error\|missing_token"}` | `SreAgentAuthFailureSpike` |
| Rate limiting | `sre_agent_ratelimited_total` | `SreAgentRateLimiting` |
| Liveness | `up{...k8s-sre-agent...}` | `SreAgentDown` |

Health: `/healthz` (process), `/readyz` (default-cluster reachability). JSON audit logs
go to stderr (tool, cluster, outcome, duration, principal).

## Troubleshooting

| Symptom | Likely cause / action |
|---------|----------------------|
| Pod not Ready (`/readyz` 503) | Default cluster unreachable — check the SA token / cluster endpoint / network policy egress to the API server |
| High tool error rate, `outcome=unreachable` | A backend (Prometheus/Loki/ArgoCD) endpoint in `clusters.yaml` is wrong or down — tools degrade gracefully, fix the endpoint |
| Tool errors `outcome=forbidden` | RBAC denied a read (expected for writes/secrets) or an upstream token lacks scope (e.g. GitLab needs `read_api`) |
| Many `auth_total{rejected}` | Misconfigured client, expired token, or probing — check `OIDC_*` and the caller |
| `429` to a caller | Token-bucket limit hit — a runaway client; tune `RATELIMIT_RATE`/`RATELIMIT_BURST` or fix the caller |
| RCA seems wrong | Detectors are explainable — read the `evidence`; the model arbitrates over it. File the case to refine a detector |

## Routine ops

* **Scale**: `replicaCount` + HPA (`autoscaling.enabled`). Reads are cheap; a single
  process does ~400–600 req/s, scale horizontally for more. Validated at 3 replicas
  (3/3 Ready, stable under soak). Set `replicaCount > 1` **at install** so the chart
  creates the PodDisruptionBudget (minAvailable 1) for safe node drains.
  > ⚠️ **Rate limiting is per-replica** (in-memory token bucket). With N replicas a
  > principal's effective limit is N× the configured value, since the Service
  > load-balances across them. For a strict global limit, back the limiter with Redis
  > (same token-bucket math — see `ratelimit.py`) or enforce limits at the ingress.
* **Rotate cluster creds**: federated (Workload Identity / IRSA) tokens auto-refresh;
  for `kubeconfig` mode, rotate the mounted file. No agent restart needed for federated.
* **Remote-cluster CA bundles**: for `azure_workload` (AKS) / `aws_eks` clusters the agent
  verifies TLS against `/etc/k8s-sre-agent/ca/<cluster>.crt`. Provide them via a Secret and
  `--set clusterCAs.enabled=true` (see `values.yaml`). On AKS, Azure RBAC role assignments
  take a few minutes to propagate — early calls may 401 until they do.
* **Upgrade**: bump image tag via Helm; the chart has a PDB + rolling update.
* **Cost**: tiered model routing (Haiku→Sonnet→Opus) + prompt caching — see
  [cost-optimization.md](cost-optimization.md).

## Using the agent in an incident

Register with Claude Code (`.mcp.json` / `claude mcp add`) and ask in plain English —
e.g. *"payments API is crashlooping in <cluster>, give me an RCA."* Claude calls
`rca_diagnose` and drills in with the read tools, returning a confidence-scored RCA. It
**recommends** fixes/rollbacks; it never executes them.
