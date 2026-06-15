# Security & RBAC

The agent's guiding principle: **it can see everything it needs to diagnose, and
change nothing.** Diagnosis is read-only; remediation is a human/pipeline decision.

## 1. Kubernetes RBAC — least privilege, deny-by-omission

The ServiceAccount is bound to a ClusterRole with **only** `get`, `list`, `watch`.
([deploy/rbac/clusterrole-readonly.yaml](../deploy/rbac/clusterrole-readonly.yaml))

The agent is **never granted**, at provisioning time:

| Capability | Verb / resource | Why it's absent |
|------------|-----------------|-----------------|
| `kubectl delete` | `delete`, `deletecollection` | not in any rule |
| `kubectl apply` | `create`, `update`, `patch` | not in any rule |
| `kubectl patch` | `patch` | not in any rule |
| `kubectl exec` | `pods/exec` subresource | not listed |
| `kubectl scale` | `*/scale`, `patch` on Deployment | not listed |
| read secret values | `secrets` (core group) `get/list` | not granted — see below |

Because Kubernetes RBAC is **deny-by-default**, omitting a verb is a hard denial: the
API server returns `403 Forbidden` for any mutating or `exec` call, no matter what the
agent code or a manipulated model attempts. There is no "are you sure?" — the request
simply cannot be authorized.

### Secret values are unreadable by design

The role grants `secrets` **only via `metadata.k8s.io`** (partial-object metadata),
never via the core `""` group. So the agent reads a secret's name, type, key names and
age — enough to correlate *"db-credentials rotated 3 minutes before the crash"* — but
the API server never returns the base64 values. `k8s_get_secrets_metadata` enforces the
same at the application layer.

### Provisioning trust

> "The AI Agent must NOT *initially* have permissions for delete/apply/patch/exec/scale."

This is enforced two ways:
1. The shipped ClusterRole contains none of those verbs.
2. A cluster policy (Kyverno/Gatekeeper) rejects any RoleBinding/ClusterRoleBinding that
   would grant a write verb to the `k8s-sre-agent` ServiceAccount — so privilege can't be
   escalated later without a policy-level change reviewed by platform security. A separate,
   **explicitly gated change agent** (with its own SA, approval workflow, and audit) is the
   path to ever executing a fix — never this diagnostic agent.

## 2. Authentication

### Outbound — to clusters (no static cluster secrets)

| Mode | Mechanism | Secret lifetime |
|------|-----------|-----------------|
| `azure_workload` (AKS) | Entra **Workload Identity** federated token | minutes (federated, auto-refreshed) |
| `aws_eks` (EKS) | **IRSA** → STS-presigned token | 15 min, cached ~14 |
| `in_cluster` | mounted projected SA token | bound, auto-rotated by kubelet |
| `kubeconfig` / `oidc_exec` | exec credential plugin / on-prem OIDC | per plugin |

The agent holds **no durable kube credential**. ([src/k8s_sre_agent/auth.py](../src/k8s_sre_agent/auth.py))

### Inbound — to the agent (HTTP transport)

Callers present an **OIDC / Entra ID** bearer token. `verify_bearer_token` validates
issuer, audience, signature (JWKS, RS256), expiry, and a **required-groups** claim
(`sre-readonly`, `platform-oncall`). Requests without an allowed group are rejected
before any tool runs. In stdio mode the OS user is the boundary (their kubeconfig).

## 3. Authorization layers (defense in depth)

```
inbound OIDC group check  ──▶  tenant guard (allowedNamespaces)  ──▶  k8s RBAC (RO verbs)  ──▶  RAG RLS (tenant)
   (who can call)               (which namespaces, app layer)         (what the SA may read)     (which docs)
```

* **Tenant guard** (`clusters.guard_namespace`): every namespaced read is checked against
  the cluster's `allowedNamespaces`. A payments caller cannot read `checkout` even by
  passing the namespace explicitly.
* **k8s RBAC**: the read-only ClusterRole (or per-namespace RoleBindings for stricter
  tenant isolation — [rolebinding-tenant-scoped.yaml](../deploy/rbac/rolebinding-tenant-scoped.yaml)).
* **RAG RLS**: Postgres Row-Level Security scopes knowledge retrieval to the caller's tenant.

## 4. Outward-facing actions

Only `slack_post` / `teams_post` leave the read-only boundary. They are:
* **off by default** (`ALLOW_NOTIFICATIONS=false`),
* **channel allow-listed** (`SLACK_ALLOWED_CHANNELS`),
* and post **only the agent-generated RCA summary** — never raw secret/log material.

## 5. Runtime hardening

`runAsNonRoot`, `readOnlyRootFilesystem`, `allowPrivilegeEscalation: false`,
`drop ALL` capabilities, `seccompProfile: RuntimeDefault`, distroless image, and a
NetworkPolicy that allows egress only to DNS + the kube-apiserver + the configured
observability/GitOps ports. ([deploy/helm](../deploy/helm/k8s-sre-agent))

## 6. Auditability

* Every cluster read is a normal Kubernetes API call → captured in the cluster **audit log**
  under the `k8s-sre-agent` SA identity.
* The agent emits structured JSON logs (tool, cluster, namespace, principal) to stderr.
* RCA detectors are **rule-based and explainable** — each conclusion lists weighted
  evidence, so an auditor can see *why* the agent reached a verdict, not just the verdict.
