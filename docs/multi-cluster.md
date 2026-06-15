# Multi-Cluster Support

One agent instance reaches many clusters — multiple AKS, multiple EKS, and on-prem /
OpenShift — with per-cluster auth and hard tenant boundaries.

## 1. Cluster registration

Clusters are declared in a registry file ([config/clusters.example.yaml](../config/clusters.example.yaml)),
typically rendered from a ConfigMap and reconciled by GitOps. Each entry carries:

```yaml
- name: aks-prod              # how callers/tools reference it
  tenant: payments            # tenant-isolation key (drives RAG RLS + namespace guard)
  provider: aks               # aks | eks | openshift | kubernetes
  region: westeurope
  auth: { mode: azure_workload, server: ..., clientId: ..., tenantId: ... }
  observability: { prometheus: ..., loki: ..., grafana: ... }   # per-cluster backends
  gitops: { argocd: ..., project: payments }
  allowedNamespaces: ["payments", "payments-staging", "monitoring"]
```

`list_clusters` exposes the fleet to Claude so it can pick the right target. Adding a
cluster is a registry edit + (for cloud) a federated-identity trust — no image rebuild.

## 2. Authentication per provider

| Provider | `auth.mode` | How a token is obtained |
|----------|-------------|--------------------------|
| AKS | `azure_workload` | Entra **Workload Identity**: the pod's federated SA token is exchanged for an AKS AAD-server token. No static secret. |
| EKS | `aws_eks` | **IRSA**: assume the cluster's read-only IAM role, mint an STS-presigned `k8s-aws-v1` token (15-min TTL, cached). |
| On-prem / OpenShift | `kubeconfig` / `oidc_exec` | A mounted read-only kubeconfig context, or an OIDC exec credential plugin. |
| Same cluster as the agent | `in_cluster` | The mounted projected ServiceAccount token. |

All branches live in [auth.build_kube_config](../src/k8s_sre_agent/auth.py). The agent
caches one `ApiClient` per cluster (`clusters.ClusterManager`) and never persists a
long-lived cluster credential.

## 3. Context switching

Tools take an optional `cluster` argument; omitting it uses `defaultCluster`. The
`ClusterManager` resolves the name → builds/caches clients → routes the call. Switching
clusters is just passing a different name — there is no global "current context" mutation,
so concurrent requests to different clusters are safe.

```
k8s_get_pods(namespace="payments", cluster="aks-prod")
k8s_get_pods(namespace="checkout", cluster="eks-prod")   # different creds, different backends
```

Observability/GitOps calls follow the same routing: `metric_cpu(..., cluster="eks-prod")`
queries *that cluster's* Prometheus (`observability.prometheus`), falling back to the
global default only if unset.

## 4. Tenant isolation

Two enforcement points, independent of network reachability:

1. **`allowedNamespaces` guard** — every namespaced read calls
   `ClusterManager.guard_namespace`, which raises `TenantIsolationError` if the namespace
   is outside the cluster's allow-list. A `payments` caller passing `namespace="checkout"`
   is rejected at the application layer before any API call.
2. **Per-namespace RoleBindings** (optional, stricter) — instead of one cluster-wide
   binding, bind the read-only ClusterRole only inside a tenant's namespaces
   ([rolebinding-tenant-scoped.yaml](../deploy/rbac/rolebinding-tenant-scoped.yaml)). Then
   even a guard bypass can't read another tenant — the API server denies it.

The same `tenant` key scopes RAG retrieval (Postgres RLS), so knowledge never leaks across
tenants either. For full multi-tenant SaaS, run **one agent instance per tenant** (separate
SA, separate vault, separate registry slice) for blast-radius isolation; the registry model
supports both shared-fleet and per-tenant deployments.

## 5. Scaling the fleet

* **Credentials**: federated identity (Workload Identity / IRSA) scales without secret
  sprawl — each cluster trusts the agent's identity, nothing is copied around.
* **CA bundles**: mounted per cluster at `/etc/k8s-sre-agent/ca/<name>.crt`.
* **Throughput**: the HTTP gateway runs ≥2 replicas; clients are cached per cluster and
  reused across requests. Read-only `list`/`watch` load on each cluster is modest.
