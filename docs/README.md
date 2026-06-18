# Documentation index

Start with the top-level [README](../README.md) for the overview and quick start.

| Doc | What's in it |
|-----|--------------|
| [architecture.md](architecture.md) | Topology, component responsibilities, MCP flow, deployment shapes, token discipline (+ mermaid diagram) |
| [security-rbac.md](security-rbac.md) | The read-only boundary, deny-by-omission RBAC, auth (OIDC + Workload Identity/IRSA), policy enforcement, security self-review |
| [rca-engine.md](rca-engine.md) | The collect → correlate → detect → score pipeline; the explainable detectors; confidence scoring |
| [incident-scenarios.md](incident-scenarios.md) | Every failure class the agent diagnoses (pod / node / app / platform / mesh / scaling / storage) and how |
| [multi-cluster.md](multi-cluster.md) | Cluster registry, per-provider auth, context switching, tenant isolation |
| [rag.md](rag.md) | Runbook/postmortem retrieval (pgvector, hybrid, tenant-scoped RLS) |
| [cost-optimization.md](cost-optimization.md) | POC/Production/Enterprise architectures; Opus vs Sonnet; token & monthly cost model |
| [operations.md](operations.md) | Operator runbook: deploy, configure, observe (dashboard/alerts), troubleshoot, scale |
| [production-readiness.md](production-readiness.md) | Honest verified-vs-needs-staging status |

Contributing & the detector workflow: [../CONTRIBUTING.md](../CONTRIBUTING.md).
Reproducible demo environment: `make stack-up` (see [../scripts/setup-stack.sh](../scripts/setup-stack.sh)).
