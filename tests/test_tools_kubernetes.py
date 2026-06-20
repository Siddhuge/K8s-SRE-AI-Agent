"""Unit tests for the Kubernetes read tools (mocked client — no cluster). These are the
agent's primary read surface; they were exercised only by skipped live tests, so they had
little regression protection in CI. Includes the security assertion that secret tooling
returns key NAMES only, never values."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import k8s_sre_agent.tools.kubernetes as kt


def _ns(**kw):
    return SimpleNamespace(**kw)


def _tools(monkeypatch, clients):
    captured: dict = {}

    class FakeMCP:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    mgr = SimpleNamespace(
        guard_namespace=lambda *a, **k: None,
        clients=lambda *a, **k: clients,
        resolve=lambda *a, **k: _ns(name="c1"),
    )
    monkeypatch.setattr(kt, "manager", lambda: mgr)
    kt.register(FakeMCP())
    return captured


def _list(items):
    return _ns(items=items)


def _age_ago(**delta):
    return datetime.now(timezone.utc) - timedelta(**delta)


def test_age_buckets():
    assert kt._age(None) == "?"
    assert kt._age(_age_ago(minutes=30)) == "30m"
    assert kt._age(_age_ago(hours=5)) == "5h"
    assert kt._age(_age_ago(days=3)) == "3d"


def test_get_pods_summarizes_status_restarts_readiness(monkeypatch):
    cs = _ns(
        restart_count=3, ready=True,
        state=_ns(waiting=_ns(reason="CrashLoopBackOff")),
        last_state=_ns(terminated=_ns(reason="Error")),
    )
    pod = _ns(
        metadata=_ns(name="api-1", creation_timestamp=_age_ago(hours=2)),
        status=_ns(phase="Running", reason=None, container_statuses=[cs]),
        spec=_ns(node_name="node-1"),
    )
    core = _ns(list_namespaced_pod=lambda ns, label_selector=None: _list([pod]))
    tools = _tools(monkeypatch, _ns(core_v1=core))

    out = tools["k8s_get_pods"]("payments")
    assert out == [{
        "name": "api-1", "phase": "Running", "reason": "CrashLoopBackOff",
        "last_terminated": "Error", "restarts": 3, "ready": "1/1",
        "node": "node-1", "age": "2h",
    }]


def test_describe_pod_pulls_container_state_and_probes(monkeypatch):
    st = _ns(restart_count=2, state=_ns(waiting=_ns(reason="CrashLoopBackOff")),
             last_state=_ns(terminated=_ns(exit_code=1, reason="Error")))
    st.name = "app"
    pod = _ns(
        metadata=_ns(name="api-1"),
        status=_ns(phase="Running", container_statuses=[st],
                   conditions=[_ns(type="Ready", status="False", reason="ContainersNotReady")]),
        spec=_ns(
            node_name="node-1", service_account_name="api-sa", volumes=[_ns(name="cfg")],
            containers=[_ns(name="app", image="api:1.2", resources=None,
                            liveness_probe=object(), readiness_probe=None)],
        ),
    )
    core = _ns(read_namespaced_pod=lambda name, ns: pod)
    tools = _tools(monkeypatch, _ns(core_v1=core))

    d = tools["k8s_describe_pod"]("payments", "api-1")
    assert d["service_account"] == "api-sa" and d["volumes"] == ["cfg"]
    c = d["containers"][0]
    assert c["image"] == "api:1.2" and c["liveness"] is True and c["readiness"] is False
    assert c["last_exit_code"] == 1 and c["waiting_reason"] == "CrashLoopBackOff"


def test_get_events_sorts_warnings_first(monkeypatch):
    base = datetime.now(timezone.utc)
    normal = _ns(type="Normal", reason="Pulled", message="ok", count=1, last_timestamp=base,
                 event_time=None, involved_object=_ns(kind="Pod", name="api-1"))
    warn = _ns(type="Warning", reason="BackOff", message="back-off", count=9,
               last_timestamp=base - timedelta(minutes=5), event_time=None,
               involved_object=_ns(kind="Pod", name="api-1"))
    core = _ns(list_namespaced_event=lambda ns, field_selector=None: _list([normal, warn]))
    tools = _tools(monkeypatch, _ns(core_v1=core))

    out = tools["k8s_get_events"]("payments")
    assert out[0]["type"] == "Warning" and out[0]["reason"] == "BackOff"   # warnings first


def test_get_nodes_surfaces_pressure_conditions(monkeypatch):
    node = _ns(
        metadata=_ns(name="node-1"),
        status=_ns(
            conditions=[_ns(type="Ready", status="True"), _ns(type="DiskPressure", status="True")],
            node_info=_ns(kubelet_version="v1.30.0"), allocatable={"cpu": "4"},
        ),
        spec=_ns(unschedulable=None),
    )
    core = _ns(list_node=lambda: _list([node]))
    tools = _tools(monkeypatch, _ns(core_v1=core))

    out = tools["k8s_get_nodes"]()
    assert out[0]["ready"] == "True" and out[0]["disk_pressure"] == "True"
    assert out[0]["kubelet"] == "v1.30.0"


def test_secrets_metadata_returns_key_names_never_values(monkeypatch):
    secret = _ns(
        metadata=_ns(name="db-credentials", creation_timestamp=_age_ago(minutes=5), resource_version="123"),
        type="Opaque",
        data={"password": "c3VwZXJzZWNyZXQ=", "host": "ZGIuc3Zj"},  # base64 VALUES
    )
    core = _ns(list_namespaced_secret=lambda ns: _list([secret]))
    tools = _tools(monkeypatch, _ns(core_v1=core))

    out = tools["k8s_get_secrets_metadata"]("payments")
    assert out[0]["keys"] == ["password", "host"]          # NAMES only
    # the security guarantee: no value material anywhere in the output
    blob = repr(out)
    assert "c3VwZXJzZWNyZXQ=" not in blob and "ZGIuc3Zj" not in blob


def test_get_deployments_and_services(monkeypatch):
    dep = _ns(
        metadata=_ns(name="api", generation=4),
        spec=_ns(replicas=3, template=_ns(spec=_ns(containers=[_ns(image="api:1.2")]))),
        status=_ns(ready_replicas=2, updated_replicas=3, available_replicas=2,
                   conditions=[_ns(type="Available", status="False", reason="MinimumReplicasUnavailable")]),
    )
    svc = _ns(
        metadata=_ns(name="api"),
        spec=_ns(type="ClusterIP", cluster_ip="10.0.0.1",
                 ports=[_ns(port=80, protocol="TCP", target_port=8080)], selector={"app": "api"}),
        status=_ns(load_balancer=None),
    )
    core = _ns(list_namespaced_service=lambda ns: _list([svc]))
    apps = _ns(list_namespaced_deployment=lambda ns: _list([dep]))
    tools = _tools(monkeypatch, _ns(core_v1=core, apps_v1=apps))

    deps = tools["k8s_get_deployments"]("payments")
    assert deps[0]["ready"] == "2/3" and deps[0]["image"] == "api:1.2"
    svcs = tools["k8s_get_services"]("payments")
    assert svcs[0]["ports"] == ["80/TCP->8080"] and svcs[0]["selector"] == {"app": "api"}


def test_get_configmaps_lists_keys_with_version(monkeypatch):
    cm = _ns(metadata=_ns(name="app-config", creation_timestamp=_age_ago(days=1), resource_version="9"),
             data={"LOG_LEVEL": "info", "FEATURE_X": "true"})
    core = _ns(list_namespaced_config_map=lambda ns: _list([cm]))
    tools = _tools(monkeypatch, _ns(core_v1=core))

    out = tools["k8s_get_configmaps"]("payments")
    assert out[0]["keys"] == ["LOG_LEVEL", "FEATURE_X"] and out[0]["resource_version"] == "9"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
