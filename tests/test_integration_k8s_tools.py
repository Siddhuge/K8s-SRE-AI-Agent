"""Live coverage of every Kubernetes read tool against the kind cluster.

Codifies the manual read-tool sweep: each tool is invoked as a TOOL (not via the
engine) so the tool functions themselves are exercised. SKIPS if the cluster is down.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("CLUSTERS_CONFIG", "config/clusters.yaml")
C, NS = "kind-sre-demo", "payments"


def _reachable() -> bool:
    try:
        from k8s_sre_agent.clusters import manager
        manager().clients(C).core_v1.get_api_resources()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="kind cluster not reachable")


def _tools():
    captured = {}

    class M:
        def tool(self):
            def d(fn):
                captured[fn.__name__] = fn
                return fn
            return d

    from k8s_sre_agent.tools import kubernetes
    kubernetes.register(M())
    return captured


def test_all_k8s_read_tools_execute():
    t = _tools()
    pods = t["k8s_get_pods"](namespace=NS, cluster=C)
    assert isinstance(pods, list) and pods, "expected pods in payments"
    pod = pods[0]["name"]

    # every read tool returns without raising / without a structured error
    assert "containers" in t["k8s_describe_pod"](namespace=NS, name=pod, cluster=C)
    assert isinstance(t["k8s_get_events"](namespace=NS, cluster=C), list)
    assert isinstance(t["k8s_get_deployments"](namespace=NS, cluster=C), list)
    assert isinstance(t["k8s_get_daemonsets"](namespace=NS, cluster=C), list)
    assert isinstance(t["k8s_get_nodes"](cluster=C), list)
    assert isinstance(t["k8s_get_services"](namespace=NS, cluster=C), list)
    assert isinstance(t["k8s_get_ingress"](namespace=NS, cluster=C), list)
    assert isinstance(t["k8s_get_configmaps"](namespace=NS, cluster=C), list)

    secrets = t["k8s_get_secrets_metadata"](namespace=NS, cluster=C)
    assert isinstance(secrets, list)
    # security invariant: metadata only — no secret values leak
    assert "s3cret" not in str(secrets)
    assert all("keys" in s and "value" not in s for s in secrets)
