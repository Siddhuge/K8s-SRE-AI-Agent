"""Unit tests for the log tools (mocked client + HTTP). Covers the kube-log bytes-decode
path (the `b'...'` bug live testing first caught), grep/tail filtering, and the Loki/
Grafana query construction + result extraction."""
from types import SimpleNamespace

import k8s_sre_agent.tools.logs as lt


def _tools(monkeypatch, *, clients=None, loki="http://loki:3100", grafana="http://graf:3000",
           get_json=None):
    captured: dict = {}

    class FakeMCP:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    cfg = SimpleNamespace(observability=SimpleNamespace(loki=loki, grafana=grafana))
    mgr = SimpleNamespace(
        guard_namespace=lambda *a, **k: None,
        clients=lambda *a, **k: clients,
        resolve=lambda *a, **k: cfg,
    )
    monkeypatch.setattr(lt, "manager", lambda: mgr)
    if get_json is not None:
        monkeypatch.setattr(lt, "get_json", get_json)
    lt.register(FakeMCP())
    return captured


def test_logs_pod_decodes_raw_bytes_and_greps(monkeypatch):
    # _preload_content=False → resp.data is raw bytes; must decode to text, not "b'...'".
    body = b"2026-06-13T10:00:00Z healthy request 200\n2026-06-13T10:00:05Z ERROR connection timeout\n"
    core = SimpleNamespace(read_namespaced_pod_log=lambda **kw: SimpleNamespace(data=body))
    tools = _tools(monkeypatch, clients=SimpleNamespace(core_v1=core))

    out = tools["logs_pod"]("payments", "api-1")
    assert not any(ln.startswith("b'") for ln in out["lines"])     # the bug: never the bytes-repr
    assert any("ERROR connection timeout" in ln for ln in out["lines"])

    grepped = tools["logs_pod"]("payments", "api-1", grep="error")  # case-insensitive
    assert len(grepped["lines"]) == 1 and "ERROR" in grepped["lines"][0]


def test_logs_pod_tail_limits_lines(monkeypatch):
    body = ("\n".join(f"line {i}" for i in range(500)) + "\n").encode()
    core = SimpleNamespace(read_namespaced_pod_log=lambda **kw: SimpleNamespace(data=body))
    tools = _tools(monkeypatch, clients=SimpleNamespace(core_v1=core))

    out = tools["logs_pod"]("payments", "api-1", tail=10)
    assert len(out["lines"]) == 10 and out["lines"][-1] == "line 499"


def test_loki_query_builds_request_and_extracts_result(monkeypatch):
    seen = {}

    def fake_get_json(url, params=None, headers=None):
        seen["url"] = url
        seen["params"] = params
        return {"data": {"result": [{"stream": {}, "values": [["1", "err"]]}]}}

    tools = _tools(monkeypatch, get_json=fake_get_json)
    out = tools["loki_query"]('{app="api"} |= "ERROR"', since="30m", limit=50)
    assert seen["url"].endswith("/loki/api/v1/query_range")
    assert seen["params"]["query"] == '{app="api"} |= "ERROR"' and seen["params"]["limit"] == "50"
    assert out["result"][0]["values"] == [["1", "err"]]


def test_logs_node_uses_systemd_journal_stream(monkeypatch):
    seen = {}

    def fake_get_json(url, params=None, headers=None):
        seen["params"] = params
        return {"data": {"result": []}}

    tools = _tools(monkeypatch, get_json=fake_get_json)
    tools["logs_node"]("node-1", grep="kubelet")
    assert 'job="systemd-journal"' in seen["params"]["query"] and 'node="node-1"' in seen["params"]["query"]


def test_grafana_panel_resolves_query_for_panel(monkeypatch):
    dash = {"dashboard": {"panels": [
        {"id": 1, "title": "CPU", "targets": [{"expr": "rate(cpu[5m])"}]},
        {"id": 2, "title": "Mem", "targets": [{"expr": "mem"}]},
    ]}}
    tools = _tools(monkeypatch, get_json=lambda *a, **k: dash)
    out = tools["grafana_panel"]("abc", 1)
    assert out["title"] == "CPU" and out["targets"][0]["expr"] == "rate(cpu[5m])"
