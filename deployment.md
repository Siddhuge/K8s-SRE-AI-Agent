# Deployment Guide — K8s SRE Agent

How to deploy the agent, from a 5-minute laptop setup to a hardened in-cluster gateway.
Commands are copy-pasteable; substitute the `<PLACEHOLDERS>`.

---

## 0. Pick your deployment shape

| Shape | Who it's for | Transport | Effort |
|---|---|---|---|
| **A. Local (stdio)** | One engineer, via Claude Code on their laptop | stdio | ~5 min |
| **B. In-cluster gateway (Helm)** | A shared/team service, multiple users over HTTP | streamable-HTTP | ~30–60 min |

Both run the **same agent** and the **same read-only security model**. Start with A to try
it; do B for a real team deployment.

---

## 1. Prerequisites

- Python 3.11+ (the container image is built on 3.11)
- `kubectl` with access to the target cluster(s)
- For shape B: `helm` 3.x, a container registry (e.g. ACR/ECR/GHCR), and `docker`
- For cloud identity (AKS/EKS): `az` or `aws` CLI
- The agent only needs **read** (`get`/`list`/`watch`) RBAC — it never writes

Clone and install (for local use / building):

```bash
git clone <repo> && cd K8s-SRE-Agent
pip install -e ".[dev,azure,aws]" -c constraints.txt   # add rag,redis extras if needed
```

---

## 2. Option A — Local (stdio) with Claude Code

The fastest way to use the agent yourself. It runs as a subprocess of Claude Code and
talks to your cluster using **your existing kubeconfig**.

1. **Point it at your cluster.** Copy the example registry and edit it:

   ```bash
   cp config/clusters.example.yaml config/clusters.yaml
   ```

   Minimal entry using your kubeconfig context:

   ```yaml
   defaultCluster: dev
   clusters:
     - name: dev
       tenant: my-team
       provider: onprem
       auth:
         mode: kubeconfig
         context: <your-kubectl-context>     # or omit for current-context
       observability:                         # optional, enables metric/log tools
         prometheus: http://localhost:9090
         loki: http://localhost:3100
       allowedNamespaces: ["default", "payments"]   # tenant isolation
   ```

2. **Register it with Claude Code** (writes `.mcp.json`, or use the CLI):

   ```bash
   claude mcp add k8s-sre -- python -m k8s_sre_agent.server stdio
   ```

   Equivalent `.mcp.json`:

   ```json
   {
     "mcpServers": {
       "k8s-sre": {
         "command": "python",
         "args": ["-m", "k8s_sre_agent.server", "stdio"],
         "env": { "CLUSTERS_CONFIG": "config/clusters.yaml", "PYTHONPATH": "src" }
       }
     }
   }
   ```

3. **Use it.** In Claude Code, ask: *"payments API is crash-looping in dev — give me an
   RCA."* Claude will call the agent's tools and return a confidence-scored diagnosis.

> In stdio mode the security boundary is the OS user + their kubeconfig. There's no
> inbound auth because there's no network listener — it's a local subprocess.

---

## 3. Option B — In-cluster gateway (Helm)

A long-running HTTP service multiple engineers (or automation) can call, with SSO,
rate limiting, metrics, and a hardened pod.

### 3.1 Build and push the image

```bash
ACR=<registry-host>            # e.g. myacr.azurecr.io or ghcr.io/org
docker build -t "$ACR/k8s-sre-agent:1.0.0" .
docker push  "$ACR/k8s-sre-agent:1.0.0"
```

### 3.2 Install the read-only RBAC (the security boundary)

```bash
kubectl apply -f deploy/rbac/                          # read-only ClusterRole + binding
# Recommended: lock it so it can never be widened (requires Kyverno)
kubectl apply -f deploy/policy/kyverno-readonly-lock.yaml
```

### 3.3 Choose how the agent authenticates to the cluster(s)

| `auth.mode` | Use when | Needs |
|---|---|---|
| `in_cluster` | Agent reads the **same** cluster it runs in | Just the mounted ServiceAccount token (RBAC from 3.2) |
| `azure_workload` | Reading **AKS** clusters | Workload Identity (see §4) + CA bundle (§3.6) |
| `aws_eks` | Reading **EKS** clusters | IRSA (see §4) + CA bundle (§3.6) |
| `kubeconfig` | A mounted kubeconfig | A projected kubeconfig file |

Put your cluster registry in a ConfigMap-backed file. Example for the in-cluster case:

```yaml
# clusters.yaml
defaultCluster: prod
clusters:
  - name: prod
    tenant: payments
    provider: aks
    auth: { mode: in_cluster }
    observability:
      prometheus: http://prometheus.monitoring:9090
      loki: http://loki.monitoring:3100
    allowedNamespaces: ["payments", "checkout"]
```

```bash
kubectl create namespace sre-system
kubectl -n sre-system create configmap k8s-sre-agent-clusters --from-file=clusters.yaml=./clusters.yaml
```

### 3.4 Create the inbound-auth + integration secret

The HTTP gateway **refuses to start without an OIDC issuer** (fail-fast on insecure
config). Provide it (and any integration tokens) via a Secret — never bake them in:

```bash
kubectl -n sre-system create secret generic k8s-sre-agent-secrets \
  --from-literal=OIDC_ISSUER="https://login.microsoftonline.com/<tenant-id>/v2.0" \
  --from-literal=OIDC_AUDIENCE="api://k8s-sre-agent" \
  --from-literal=OIDC_REQUIRED_GROUPS="sre-readonly,platform-oncall"
  # optional integrations (only if used):
  # --from-literal=SLACK_BOT_TOKEN=xoxb-... --from-literal=SLACK_ALLOWED_CHANNELS="#sre-incidents"
  # --from-literal=GRAFANA_TOKEN=... --from-literal=JIRA_TOKEN=...
```

### 3.5 Install the chart

```bash
helm install k8s-sre-agent deploy/helm/k8s-sre-agent -n sre-system \
  --set image.repository="$ACR/k8s-sre-agent" --set image.tag=1.0.0 \
  --set existingSecret=k8s-sre-agent-secrets \
  --set clustersConfig=k8s-sre-agent-clusters \
  --set auth.oidcIssuer="https://login.microsoftonline.com/<tenant-id>/v2.0" \
  --set auth.oidcAudience="api://k8s-sre-agent" \
  --set auth.requiredGroups="sre-readonly,platform-oncall"
```

Useful extra `--set` flags:

- `replicaCount=2` (creates a PodDisruptionBudget for safe node drains)
- `autoscaling.enabled=true` (HPA)
- `metrics.serviceMonitor.enabled=true` (Prometheus Operator scrape)
- `networkPolicy.enabled=true` + `networkPolicy.allowedEgressPorts={443,9090,3100,3000}`
- `notifications.enabled=true` + `notifications.slackAllowedChannels="#sre-incidents"`
- `ratelimit` via the Secret: `RATELIMIT_REDIS_URL=redis://redis:6379/0` for a strict
  global limit across replicas (install the `redis` extra)

### 3.6 (Remote clusters only) mount the cluster CA bundles

For `azure_workload`/`aws_eks` the agent verifies TLS against
`/etc/k8s-sre-agent/ca/<cluster-name>.crt`. Provide them via a Secret:

```bash
kubectl -n sre-system create secret generic k8s-sre-agent-cluster-cas \
  --from-file=prod.crt=./prod-ca.crt --from-file=eks-prod.crt=./eks-ca.crt
helm upgrade k8s-sre-agent ... --set clusterCAs.enabled=true
```

### 3.7 Observability

```bash
kubectl apply -f deploy/observability/prometheus-rules.yaml   # alerts (Prometheus Operator)
# import deploy/observability/grafana-dashboard.json into Grafana
```

---

## 4. Cloud identity setup (no long-lived keys)

### Azure AKS — Workload Identity (Terraform included)

The repo ships Terraform that provisions an AKS cluster wired for this exactly:

```bash
cd terraform
az login && az account set --subscription <SUB_ID>
cp terraform.tfvars.example terraform.tfvars
terraform init && terraform apply
terraform output -raw clusters_yaml_snippet   # paste into your clusters.yaml
```

It creates a user-assigned managed identity + a **federated credential** trusting
`system:serviceaccount:sre-system:k8s-sre-agent`, and a read-only `AKS RBAC Reader` role.
Set the agent's SA annotation to the identity's client ID:

```bash
helm upgrade k8s-sre-agent ... \
  --set serviceAccount.annotations."azure\.workload\.identity/client-id"="$(terraform output -raw agent_client_id)"
```

> ⚠️ Azure RBAC role assignments take a few minutes to propagate — early calls may return
> 401 until they do. (This is documented behavior, not a bug.)

### AWS EKS — IRSA

Create an IAM role with a trust policy for the EKS OIDC provider scoped to
`system:serviceaccount:sre-system:k8s-sre-agent`, annotate the ServiceAccount with
`eks.amazonaws.com/role-arn`, and map a read-only Kubernetes RBAC group to that role.
Set `auth.mode: aws_eks` in the cluster entry.

---

## 5. Optional features

- **RAG over runbooks/postmortems** — needs a pgvector database. Set `rag.enabled=true`,
  point `PGVECTOR_DSN` at it (via a Secret), and choose `EMBEDDING_MODEL` (local
  `local:BAAI/bge-small-en-v1.5` needs no API key). **Connect as a non-superuser role** —
  row-level tenant isolation is bypassed by superusers.
- **Notifications (Slack/Teams)** — OFF by default. Enable with `ALLOW_NOTIFICATIONS=true`
  + an allow-listed channel/webhook. This is the only outward-facing capability.
- **Global rate limiting** — set `RATELIMIT_REDIS_URL`; otherwise the limit is per-replica.

---

## 6. Verify the deployment

```bash
kubectl -n sre-system get pods                                   # Running, 1/1
kubectl -n sre-system port-forward deploy/k8s-sre-agent 8080:8080 &
curl -s localhost:8080/healthz    # {"status":"ok"}   (process up)
curl -s localhost:8080/readyz     # {"status":"ready"} (can reach the default cluster)
curl -s localhost:8080/metrics | head             # Prometheus metrics

# The read-only guarantee — this MUST say "no":
kubectl auth can-i delete pods \
  --as=system:serviceaccount:sre-system:k8s-sre-agent
```

Then drive a real RCA through Claude (or your MCP client) against a known-broken workload
and confirm the diagnosis.

---

## 7. Day-2: upgrade, rollback, teardown

```bash
# upgrade (rolling, PDB-protected)
helm upgrade k8s-sre-agent deploy/helm/k8s-sre-agent -n sre-system --set image.tag=1.1.0
# rollback
helm rollback k8s-sre-agent -n sre-system
# uninstall
helm uninstall k8s-sre-agent -n sre-system
# (Azure) tear down the Terraform cluster to stop billing
cd terraform && terraform destroy
```

---

## 8. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Pod won't start, logs say *"refusing to start"* | HTTP mode needs `OIDC_ISSUER` — set it in the Secret |
| `/readyz` returns 503 | Default cluster unreachable — check the SA token / endpoint / NetworkPolicy egress |
| 401 on every call to a remote AKS cluster | Azure RBAC assignment not yet propagated (wait), or the cluster CA isn't mounted (§3.6) |
| Tool errors `outcome=forbidden` | Expected for writes/secret values; for an upstream tool, the token may lack scope (e.g. GitLab needs `read_api`) |
| `429` to a caller | Rate limit hit — tune `RATELIMIT_RATE`/`RATELIMIT_BURST`, or back it with Redis |
| Image won't import in-cluster | Ensure `PYTHONPATH=/app/src` (set in the image) and the build used `constraints.txt` |

More operational detail in [docs/operations.md](docs/operations.md); security model in
[docs/security-rbac.md](docs/security-rbac.md); multi-cluster auth in
[docs/multi-cluster.md](docs/multi-cluster.md).
