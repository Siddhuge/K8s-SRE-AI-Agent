"""Live ArgoCD integration tests. Require ArgoCD port-forwarded to localhost:8083
(insecure) with a `guestbook` (Healthy/Synced) and `broken-app` (ComparisonError)
Application, and ARGOCD_TOKEN exported. SKIP automatically otherwise."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("CLUSTERS_CONFIG", "config/clusters.yaml")
os.environ.setdefault("DEFAULT_CLUSTER", "kind-sre-demo")

C = "kind-sre-demo"


def _reachable() -> bool:
    import httpx
    try:
        return httpx.get("http://localhost:8083/healthz", timeout=2.0).status_code < 500
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _reachable() or not os.environ.get("ARGOCD_TOKEN"),
    reason="ArgoCD not reachable on :8083 or ARGOCD_TOKEN unset",
)


def _tools():
    captured = {}

    class M:
        def tool(self):
            def d(fn):
                captured[fn.__name__] = fn
                return fn
            return d

    from k8s_sre_agent.tools import gitops
    gitops.register(M())
    return captured


def test_healthy_app_synced_with_history():
    t = _tools()
    app = t["argocd_app"](app="guestbook", cluster=C)
    assert app["health"] == "Healthy"
    assert app["sync"] == "Synced"
    assert app["revision"]
    assert t["argocd_history"](app="guestbook", cluster=C), "expected at least one sync in history"


def test_broken_app_surfaces_comparison_error():
    app = _tools()["argocd_app"](app="broken-app", cluster=C)
    assert app["sync"] in ("Unknown", "OutOfSync")
    assert any(c["type"] == "ComparisonError" for c in app["conditions"]), \
        "expected a ComparisonError condition explaining the failure"
