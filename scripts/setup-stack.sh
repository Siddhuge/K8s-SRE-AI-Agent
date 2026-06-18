#!/usr/bin/env bash
# Stand up the FULL validated demo environment on a local kind cluster:
# kind + all failure scenarios + Prometheus/Loki/Grafana + Istio + ArgoCD, then
# print the port-forwards and a ready-to-use cluster registry. Idempotent-ish:
# re-running skips installs that already exist.
#
# Usage:  ./scripts/setup-stack.sh        (full stack)
#         FAST=1 ./scripts/setup-stack.sh (skip Istio + ArgoCD — k8s + observability only)
set -euo pipefail
CTX=kind-sre-demo
kctl() { kubectl --context "$CTX" "$@"; }
have() { command -v "$1" >/dev/null 2>&1; }

echo "==> kind cluster"
kind get clusters 2>/dev/null | grep -qx sre-demo || kind create cluster --name sre-demo --wait 90s

echo "==> namespaces + secret"
kctl create namespace payments --dry-run=client -o yaml | kctl apply -f - >/dev/null
kctl -n payments create secret generic db-credentials \
  --from-literal=host=db.payments.svc --from-literal=password=s3cret \
  --dry-run=client -o yaml | kctl apply -f - >/dev/null

echo "==> failure scenarios (4 waves)"
kctl apply -f tests/fixtures/crashloop-deploy.yaml >/dev/null
for f in scenarios scenarios2 scenarios3 scenarios4; do kctl apply -f "tests/fixtures/$f.yaml" >/dev/null; done

echo "==> Prometheus + Loki + Grafana"
if have helm; then
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
  helm repo add grafana https://grafana.github.io/helm-charts >/dev/null 2>&1 || true
  helm repo update >/dev/null 2>&1
  kctl create namespace monitoring --dry-run=client -o yaml | kctl apply -f - >/dev/null
  helm --kube-context "$CTX" status prometheus -n monitoring >/dev/null 2>&1 || \
    helm --kube-context "$CTX" install prometheus prometheus-community/prometheus -n monitoring \
      --set alertmanager.enabled=false --set prometheus-pushgateway.enabled=false \
      --set server.persistentVolume.enabled=false --wait --timeout 240s
  helm --kube-context "$CTX" status loki -n monitoring >/dev/null 2>&1 || \
    helm --kube-context "$CTX" install loki grafana/loki-stack -n monitoring \
      --set loki.persistence.enabled=false --set grafana.enabled=false --set prometheus.enabled=false \
      --wait --timeout 240s
  helm --kube-context "$CTX" status grafana -n monitoring >/dev/null 2>&1 || \
    helm --kube-context "$CTX" install grafana grafana/grafana -n monitoring \
      --set persistence.enabled=false --set adminPassword=admin --wait --timeout 240s
fi

if [ "${FAST:-0}" != "1" ] && have helm; then
  echo "==> Istio + mesh scenarios"
  helm repo add istio https://istio-release.storage.googleapis.com/charts >/dev/null 2>&1 || true
  helm repo update istio >/dev/null 2>&1
  kctl create namespace istio-system --dry-run=client -o yaml | kctl apply -f - >/dev/null
  helm --kube-context "$CTX" status istio-base -n istio-system >/dev/null 2>&1 || \
    helm --kube-context "$CTX" install istio-base istio/base -n istio-system --set defaultRevision=default --wait --timeout 120s
  helm --kube-context "$CTX" status istiod -n istio-system >/dev/null 2>&1 || \
    helm --kube-context "$CTX" install istiod istio/istiod -n istio-system --wait --timeout 180s
  for f in istio-scenario istio-mtls-sidecar istio-gateway; do kctl apply -f "tests/fixtures/$f.yaml" >/dev/null; done

  echo "==> ArgoCD + apps"
  kctl create namespace argocd --dry-run=client -o yaml | kctl apply -f - >/dev/null
  kctl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml >/dev/null
  kctl -n argocd patch configmap argocd-cmd-params-cm --type merge -p '{"data":{"server.insecure":"true"}}' >/dev/null
  kctl -n argocd rollout restart deploy/argocd-server >/dev/null
  kctl -n argocd wait --for=condition=available deploy/argocd-server deploy/argocd-repo-server --timeout=240s >/dev/null
  kctl create namespace guestbook --dry-run=client -o yaml | kctl apply -f - >/dev/null
  kctl apply -f tests/fixtures/argocd-apps.yaml >/dev/null
fi

cat <<'NOTE'

==> Ready. Start port-forwards (each in its own shell), then point config/clusters.yaml at them:
    kubectl --context kind-sre-demo -n monitoring port-forward svc/prometheus-server 9090:80
    kubectl --context kind-sre-demo -n monitoring port-forward svc/loki 3100:3100
    kubectl --context kind-sre-demo -n monitoring port-forward svc/grafana 3000:80
    kubectl --context kind-sre-demo -n argocd      port-forward svc/argocd-server 8083:80

Run an RCA:  make demo           (or: PYTHONPATH=src CLUSTERS_CONFIG=config/clusters.yaml \
             python3 -c "from k8s_sre_agent.rca.engine import diagnose; print(diagnose('kind-sre-demo','payments','api').to_markdown())")
Tear down:   ./scripts/teardown.sh
NOTE
