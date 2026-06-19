# Terraform — AKS cluster for Workload-Identity validation

Provisions exactly what the agent's `azure_workload` auth path needs, in one apply:

- an **AKS cluster** with **OIDC issuer** + **Workload Identity** enabled and **Entra +
  Azure RBAC** for Kubernetes,
- a **user-assigned managed identity** for the agent,
- a **federated identity credential** trusting `system:serviceaccount:sre-system:k8s-sre-agent`,
- a **read-only** Azure RBAC role assignment for that identity (`AKS RBAC Reader`),
- (optional) an **ACR** + AcrPull so the cluster can pull the agent image.

> 💸 This creates real, billable Azure resources (AKS + 1 node + ACR). Run
> `terraform destroy` when done.

## 1. Apply

```bash
az login                       # or set ARM_* env vars
az account set --subscription <SUB_ID>
cd terraform
cp terraform.tfvars.example terraform.tfvars   # edit if you like
terraform init
terraform apply
```

## 2. Wire up the agent

```bash
# admin kubeconfig (printed by the output)
eval "$(terraform output -raw get_credentials_cmd)"

# the cluster registry entry for the agent (paste into ../config/clusters.yaml)
terraform output -raw clusters_yaml_snippet

# build + push the agent image to the ACR, then deploy with Helm
ACR=$(terraform output -raw acr_login_server)
az acr login -n "${ACR%%.*}"
docker build -t "$ACR/k8s-sre-agent:dev" ..
docker push "$ACR/k8s-sre-agent:dev"

helm install k8s-sre-agent ../deploy/helm/k8s-sre-agent -n sre-system --create-namespace \
  --set image.repository="$ACR/k8s-sre-agent" --set image.tag=dev --set image.pullPolicy=Always \
  --set serviceAccount.annotations."azure\.workload\.identity/client-id"="$(terraform output -raw agent_client_id)" \
  --set existingSecret=k8s-sre-agent-secrets \
  --set autoscaling.enabled=false --set metrics.serviceMonitor.enabled=false --set networkPolicy.enabled=false
kubectl -n sre-system create secret generic k8s-sre-agent-secrets --from-literal=LOG_LEVEL=INFO
```

The Helm chart already labels the pod `azure.workload.identity/use: "true"` and annotates
the SA — so the webhook injects `AZURE_CLIENT_ID` / `AZURE_FEDERATED_TOKEN_FILE`, which
`auth._azure_workload_token` consumes.

## 3. Validate (what I'll interpret)

Run the one-shot Job below (uses the agent image + the workload identity). It mints the
federated Entra token and lists pods via the `azure_workload` path, then a
SubjectAccessReview confirms writes are denied. Share its logs.

```yaml
apiVersion: batch/v1
kind: Job
metadata: { name: wi-validate, namespace: sre-system }
spec:
  backoffLimit: 0
  template:
    metadata:
      labels: { azure.workload.identity/use: "true" }
    spec:
      serviceAccountName: k8s-sre-agent
      restartPolicy: Never
      containers:
        - name: validate
          image: REPLACE_WITH_ACR/k8s-sre-agent:dev
          env:
            - { name: PYTHONPATH, value: /app/src }
            - { name: CLUSTERS_CONFIG, value: /etc/agent/clusters.yaml }
          command: ["python3", "-c"]
          args:
            - |
              from k8s_sre_agent.clusters import manager
              c = manager().clients("aks-wi")
              pods = c.core_v1.list_namespaced_pod("default").items
              print("WORKLOAD-IDENTITY OK: listed", len(pods), "pods via azure_workload token")
          volumeMounts: [{ name: cfg, mountPath: /etc/agent }]
      volumes:
        - name: cfg
          configMap: { name: agent-clusters }
```

Create the `agent-clusters` ConfigMap from the `clusters_yaml_snippet` output first:
`kubectl -n sre-system create configmap agent-clusters --from-file=clusters.yaml=<file>`.

A successful run prints `WORKLOAD-IDENTITY OK: listed N pods …` — proving the federated
token mint + read-only Azure RBAC end to end. Then:
`kubectl auth can-i delete pods --as=<agent MI objectId>` → `no`.

## 4. Tear down

```bash
terraform destroy
```
