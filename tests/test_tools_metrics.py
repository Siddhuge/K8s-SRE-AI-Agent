"""Unit tests for the Prometheus metric tools (mocked HTTP). Covers duration parsing, the
instant-vs-range query construction (incl. the regression that range queries use real unix
timestamps + a step, NOT 'now-1h' relative strings which Prometheus 400s), and the typed
PromQL wrappers — including metric_disk picking real ext4/xfs filesystems, not mountpoint=/."""
from types import SimpleNamespace

import k8s_sre_agent.tools.metrics as mt


def _tools(monkeypatch, get_json, *, prom="http://prom:9090"):
    captured: dict = {}

    class FakeMCP:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    cfg = SimpleNamespace(observability=SimpleNamespace(prometheus=prom))
    mgr = SimpleNamespace(
        guard_namespace=lambda *a, **k: None,
        resolve=lambda *a, **k: cfg,
    )
    monkeypatch.setattr(mt, "manager", lambda: mgr)
    monkeypatch.setattr(mt, "get_json", get_json)
    mt.register(FakeMCP())
    return captured


def _recorder(result=None):
    seen: dict = {}

    def get_json(url, params=None, headers=None):
        seen["url"] = url
        seen["params"] = params
        return {"data": {"result": result if result is not None else [{"metric": {}, "value": [1, "2"]}]}}

    return get_json, seen


def test_parse_duration_units_and_default_minutes():
    assert mt.parse_duration("45s") == 45
    assert mt.parse_duration("30m") == 1800
    assert mt.parse_duration("2h") == 7200
    assert mt.parse_duration("1d") == 86400
    assert mt.parse_duration("10") == 600  # no unit → minutes


def test_instant_query_hits_query_endpoint(monkeypatch):
    get_json, seen = _recorder()
    tools = _tools(monkeypatch, get_json)
    out = tools["prom_query"]("up")
    assert seen["url"].endswith("/api/v1/query")
    assert seen["params"] == {"query": "up"}
    assert out["result"] == [{"metric": {}, "value": [1, "2"]}]


def test_range_query_uses_unix_timestamps_and_step(monkeypatch):
    get_json, seen = _recorder()
    tools = _tools(monkeypatch, get_json)
    tools["prom_query"]("up", range="30m")
    p = seen["params"]
    assert seen["url"].endswith("/api/v1/query_range")
    # real unix ints, not relative "now-1h" strings (the bug Prometheus 400'd on)
    assert isinstance(p["start"], int) and isinstance(p["end"], int)
    assert p["end"] - p["start"] == 1800
    assert p["step"] == "15s"  # max(15, 1800 // 200)
    assert "now-" not in str(p["start"]) and "now-" not in str(p["end"])


def test_metric_cpu_selector_scopes_to_pod_when_given(monkeypatch):
    get_json, seen = _recorder()
    tools = _tools(monkeypatch, get_json)

    tools["metric_cpu"]("payments")
    assert 'namespace="payments"' in seen["params"]["query"] and "pod=" not in seen["params"]["query"]

    tools["metric_cpu"]("payments", pod="api-1")
    assert 'pod="api-1"' in seen["params"]["query"]
    assert "rate(container_cpu_usage_seconds_total" in seen["params"]["query"]


def test_metric_disk_uses_real_filesystems_not_root_mount(monkeypatch):
    get_json, seen = _recorder()
    tools = _tools(monkeypatch, get_json)
    tools["metric_disk"](node="node-1")
    q = seen["params"]["query"]
    assert 'fstype=~"ext4|xfs"' in q
    assert 'mountpoint="/"' not in q                 # the fix: no hard-coded root mount
    assert 'instance=~"node-1.*"' in q
    assert "node_filesystem_avail_bytes" in q and "node_filesystem_size_bytes" in q


def test_metric_restarts_uses_kube_state_metrics(monkeypatch):
    get_json, seen = _recorder()
    tools = _tools(monkeypatch, get_json)
    tools["metric_restarts"]("payments")
    q = seen["params"]["query"]
    assert "increase(kube_pod_container_status_restarts_total" in q and 'namespace="payments"' in q


def test_metric_memory_and_network_promql(monkeypatch):
    get_json, seen = _recorder()
    tools = _tools(monkeypatch, get_json)

    tools["metric_memory"]("payments", pod="api-1")
    assert "container_memory_working_set_bytes" in seen["params"]["query"]

    tools["metric_network"]("payments")
    assert "rate(container_network_receive_bytes_total" in seen["params"]["query"]


def test_falls_back_to_settings_prometheus_url_when_unset(monkeypatch):
    get_json, seen = _recorder()
    # cluster cfg has no prometheus → _prom falls back to settings.prometheus_url
    tools = _tools(monkeypatch, get_json, prom="")
    monkeypatch.setattr(mt, "get_settings", lambda: SimpleNamespace(prometheus_url="http://fallback:9090"))
    tools["prom_query"]("up")
    assert seen["url"].startswith("http://fallback:9090")
