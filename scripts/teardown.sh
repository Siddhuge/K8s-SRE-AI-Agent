#!/usr/bin/env bash
# Tear down the demo environment (the whole kind cluster) and any port-forwards.
set -euo pipefail
pkill -f "port-forward.*kind-sre-demo" 2>/dev/null || true
kind delete cluster --name sre-demo
echo "deleted kind cluster sre-demo"
