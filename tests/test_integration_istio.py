"""Live Istio integration tests. Require the mesh scenario (tests/fixtures/istio-scenario.yaml)
applied to the kind cluster. SKIP automatically when Istio/the mesh namespace is absent."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("CLUSTERS_CONFIG", "config/clusters.yaml")
os.environ.setdefault("DEFAULT_CLUSTER", "kind-sre-demo")

C = "kind-sre-demo"
NS = "mesh"


def _mesh_ready() -> bool:
    try:
        from k8s_sre_agent.clusters import manager
        manager().clients(C).custom.list_namespaced_custom_object(
            group="networking.istio.io", version="v1beta1", namespace=NS, plural="virtualservices"
        )
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _mesh_ready(), reason="Istio/mesh namespace not present")


def _tools():
    captured = {}

    class M:
        def tool(self):
            def d(fn):
                captured[fn.__name__] = fn
                return fn
            return d

    from k8s_sre_agent.tools import istio
    istio.register(M())
    return captured


def test_read_tools_return_mesh_objects():
    t = _tools()
    assert t["istio_get_virtualservices"](namespace=NS, cluster=C), "expected the reviews VirtualService"
    assert t["istio_get_destinationrules"](namespace=NS, cluster=C), "expected the reviews DestinationRule"
    assert t["istio_get_gateways"](namespace=NS, cluster=C), "expected the mesh-gw Gateway"


def test_mesh_analyze_catches_dangling_subset():
    res = _tools()["istio_mesh_analyze"](namespace=NS, cluster=C)
    assert res["healthy"] is False
    assert any(f["subset"] == "v2" and f["issue"] == "subset_not_defined" for f in res["findings"])


def _has(subject):
    from k8s_sre_agent.clusters import manager
    pods = manager().clients(C).core_v1.list_namespaced_pod(NS, label_selector=f"app={subject}")
    return bool(pods.items)


def test_mesh_analyze_catches_mtls_conflict():
    """Requires the mTLS scenario (istio-mtls-sidecar.yaml). Skips if not applied."""
    res = _tools()["istio_mesh_analyze"](namespace=NS, cluster=C)
    if res.get("peerauthentications", 0) == 0:
        pytest.skip("mTLS scenario (PeerAuthentication) not applied")
    assert any(f["issue"] == "mtls_mode_conflict" for f in res["findings"])


def test_rca_diagnose_sidecar_not_ready():
    """Requires the sidecar-OOM scenario. Skips if not deployed."""
    if not _has("sidecaroom"):
        pytest.skip("sidecaroom scenario not deployed")
    from k8s_sre_agent.rca.engine import diagnose
    r = diagnose(C, NS, "sidecaroom")
    assert r.issue == "Istio Sidecar Not Ready"
    assert r.confidence >= 80


def test_mesh_analyze_catches_gateway_binding_errors():
    """mesh-gw (unbound) and orphan-route (unknown gateway) → 404 findings."""
    res = _tools()["istio_mesh_analyze"](namespace=NS, cluster=C)
    issues = {f["issue"] for f in res["findings"]}
    assert "gateway_no_virtualservice" in issues, "expected the unbound mesh-gw finding"
    # orphan-route only present if istio-gateway.yaml was applied
    if any(f.get("virtualservice") == "orphan-route" for f in res["findings"]):
        assert "virtualservice_unknown_gateway" in issues
