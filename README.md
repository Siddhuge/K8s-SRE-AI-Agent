# K8s SRE Agent — AI-Powered Kubernetes Troubleshooting via MCP

An enterprise-grade, **read-only-by-default** Site Reliability Engineering agent that
diagnoses Kubernetes failures and produces accurate Root Cause Analysis (RCA) with
remediation recommendations. It is exposed to **Claude** through the
**Model Context Protocol (MCP)**.

The agent behaves like a Senior SRE with working knowledge of Kubernetes, AKS, EKS,
OpenShift, Helm, ArgoCD/GitOps, GitLab CI & GitHub Actions, Istio, Prometheus, Grafana,
Loki, ELK, Azure, AWS and Terraform.

```
┌──────────┐   MCP (stdio / streamable-HTTP)   ┌────────────────────────────────┐
│  Claude  │ ───────────────────────────────▶  │        k8s-sre-agent MCP        │
│ (Opus /  │ ◀───────────────────────────────  │  ┌──────────────────────────┐  │
│  Sonnet) │       tool calls + results        │  │ Tools (get/list/watch)   │  │
└──────────┘                                    │  │  k8s · logs · metrics    │  │
                                                │  │  gitops · cicd · incident│  │
                                                │  ├──────────────────────────┤  │
                                                │  │ RCA Engine (correlation) │  │
                                                │  ├──────────────────────────┤  │
                                                │  │ RAG (runbooks/postmortem)│  │
                                                │  └──────────────────────────┘  │
                                                └───────────┬────────────────────┘
                                                            │ read-only ServiceAccount
                                       ┌────────────────────┼────────────────────┐
                                   AKS-prod              EKS-prod            on-prem-dc1
```

## Why this design

* **Security first.** The agent's Kubernetes ServiceAccount is bound to a ClusterRole
  with **only `get`, `list`, `watch`**. No `delete / apply / patch / exec / scale`. It
  *recommends* fixes; a human (or a separately-gated change agent) executes them. See
  [docs/security-rbac.md](docs/security-rbac.md).
* **Multi-cluster.** A cluster registry lets a single agent instance reach many AKS/EKS/
  on-prem clusters with per-cluster auth and tenant isolation. See
  [docs/multi-cluster.md](docs/multi-cluster.md).
* **Grounded.** The RCA engine collects context deterministically (events, logs,
  metrics, deploy history) and correlates it before Claude reasons over it — so the
  model isn't guessing. RAG injects your own runbooks/postmortems. See
  [docs/rca-engine.md](docs/rca-engine.md) and [docs/rag.md](docs/rag.md).
* **Cost-aware.** Three reference architectures (POC / Production / Enterprise) and an
  Opus-vs-Sonnet cost model. See [docs/cost-optimization.md](docs/cost-optimization.md).

## Quick start (local, against your current kubeconfig)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .

cp .env.example .env          # fill in Prometheus/Loki/ArgoCD endpoints as needed
cp config/clusters.example.yaml config/clusters.yaml

# Run as an MCP stdio server (what Claude Desktop / Claude Code launch):
k8s-sre-agent stdio

# Or as a remote streamable-HTTP MCP server (for the gateway deployment):
k8s-sre-agent http --host 0.0.0.0 --port 8080
```

Register with Claude Code:

```bash
claude mcp add k8s-sre -- k8s-sre-agent stdio
```

`.mcp.json` equivalent:

```json
{
  "mcpServers": {
    "k8s-sre": { "command": "k8s-sre-agent", "args": ["stdio"] }
  }
}
```

## Tool surface (all read-only)

| Domain     | Tools |
|------------|-------|
| Kubernetes | `k8s_get_pods`, `k8s_describe_pod`, `k8s_get_events`, `k8s_get_deployments`, `k8s_get_daemonsets`, `k8s_get_nodes`, `k8s_get_services`, `k8s_get_ingress`, `k8s_get_configmaps`, `k8s_get_secrets_metadata` |
| Logs       | `logs_pod`, `logs_container`, `logs_node`, `loki_query`, `grafana_panel` |
| Metrics    | `prom_query`, `metric_cpu`, `metric_memory`, `metric_disk`, `metric_network`, `metric_restarts` |
| Istio      | `istio_get_virtualservices`, `istio_get_destinationrules`, `istio_get_gateways`, `istio_get_peerauthentications`, `istio_mesh_analyze` (flags dangling-subset routes **and** mTLS mode conflicts → 503). Sidecar-not-ready (incl. proxy OOM) is detected by `rca_diagnose`. |
| GitOps     | `argocd_app` (health/sync + `conditions` explaining failures like ComparisonError), `argocd_history`, `argocd_sync_status`, `argocd_rollback_info` |
| CI/CD      | `gitlab_pipelines`, `github_actions_runs`, `recent_deployments`, `compare_deployments` (GitHub tools work unauthenticated on public repos; set `GITHUB_TOKEN` for private/higher limits) |
| Incidents  | `jira_search`, `servicenow_search`, `slack_post`, `teams_post` |
| RCA        | `rca_diagnose` (orchestrates the above into a confidence-scored report) |
| RAG        | `kb_search` (runbooks / SOPs / postmortems) |

> `slack_post` / `teams_post` are the only **outward-facing** tools. They are
> disabled unless `ALLOW_NOTIFICATIONS=true` and require explicit channel allow-listing.

## Repository layout

```
src/k8s_sre_agent/
  server.py        MCP entrypoint (FastMCP); registers every tool
  config.py        Settings + secrets resolution
  auth.py          OIDC / Entra ID / Workload Identity
  clusters.py      Multi-cluster registry + context switching + tenant guard
  tools/           One module per integration domain
  rca/             Deterministic context collection + correlation engine
  rag/             pgvector-backed retrieval over your knowledge base
deploy/
  helm/            Production Helm chart
  rbac/            Read-only ClusterRole + bindings (the security boundary)
docs/              Architecture, security, RCA, RAG, multi-cluster, cost
```

See [docs/architecture.md](docs/architecture.md) for the full design.
