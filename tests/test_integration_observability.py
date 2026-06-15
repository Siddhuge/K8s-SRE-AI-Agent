"""Live integration tests for the metric/log tools against in-cluster Prometheus + Loki.

Requires the kind demo plus Prometheus/Loki port-forwarded to localhost:9090 / :3100
(see the observability setup). SKIPS automatically when the backends aren't reachable.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("CLUSTERS_CONFIG", "config/clusters.yaml")
os.environ.setdefault("DEFAULT_CLUSTER", "kind-sre-demo")

C = "kind-sre-demo"
NS = "payments"


def _reachable(url: str) -> bool:
    import httpx
    try:
        return httpx.get(url, timeout=2.0).status_code < 500
    except Exception:
        return False


prom_up = _reachable("http://localhost:9090/-/ready")
loki_up = _reachable("http://localhost:3100/ready")


def _tools():
    captured = {}

    class M:
        def tool(self):
            def d(fn):
                captured[fn.__name__] = fn
                return fn
            return d

    from k8s_sre_agent.tools import logs, metrics
    metrics.register(M())
    logs.register(M())
    return captured


@pytest.mark.skipif(not prom_up, reason="Prometheus not reachable on :9090")
def test_prom_instant_query():
    out = _tools()["prom_query"]("up", cluster=C)
    assert out["result"], "expected 'up' series from Prometheus"


@pytest.mark.skipif(not prom_up, reason="Prometheus not reachable on :9090")
def test_metric_restarts_shows_crashloopers():
    out = _tools()["metric_restarts"](namespace=NS, cluster=C)
    assert out["result"], "expected restart series"
    # the crashlooping pods (api/dnsfail/oom/tlsfail) must show a positive restart rate
    rates = {
        s["metric"].get("pod", ""): float((s.get("values") or [["", "0"]])[-1][1])
        for s in out["result"]
    }
    assert any(v > 0 for k, v in rates.items()), f"expected a crashlooper with restarts>0, got {rates}"


@pytest.mark.skipif(not prom_up, reason="Prometheus not reachable on :9090")
def test_metric_disk_robust_to_mountpoint():
    """Regression: disk usage must resolve even when there's no '/' mountpoint."""
    out = _tools()["metric_disk"](cluster=C)
    assert out["result"], "metric_disk returned no series (mountpoint hard-coding regressed?)"


@pytest.mark.skipif(not loki_up, reason="Loki not reachable on :3100")
def test_loki_query_returns_pod_logs():
    out = _tools()["loki_query"](f'{{namespace="{NS}"}} |= "FATAL"', cluster=C, since="1h")
    assert out["result"], "expected FATAL log lines from Loki"
    text = " ".join(v[1] for st in out["result"] for v in st.get("values", []))
    assert "FATAL" in text
