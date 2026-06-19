"""A least-privilege identity (e.g. Azure RBAC 'Reader': no cluster nodes, no secrets)
gets 403 on some reads. The RCA must degrade gracefully, not crash — regression test for
the bug found live on AKS, where list_node / list_namespaced_secret 403 aborted diagnose."""
from types import SimpleNamespace

from kubernetes.client.rest import ApiException

from k8s_sre_agent.rca import engine


def _empty(*_a, **_k):
    return SimpleNamespace(items=[])


def _forbidden(*_a, **_k):
    raise ApiException(status=403, reason="Forbidden")


def _not_found(*_a, **_k):
    raise ApiException(status=404, reason="Not Found")


def _fake_clients():
    core = SimpleNamespace(
        read_namespaced_pod=_not_found,           # subject doesn't resolve as a pod
        list_namespaced_pod=_empty,
        list_namespaced_event=_empty,
        list_node=_forbidden,                     # denied (cluster-scoped) → degrade
        list_namespaced_secret=_forbidden,        # denied → degrade
        list_namespaced_service=_empty,
    )
    return SimpleNamespace(
        core_v1=core,
        apps_v1=SimpleNamespace(read_namespaced_deployment=_not_found,
                                read_namespaced_stateful_set=_not_found),
        batch_v1=SimpleNamespace(read_namespaced_job=_not_found),
        autoscaling_v2=SimpleNamespace(list_namespaced_horizontal_pod_autoscaler=_empty),
        policy_v1=SimpleNamespace(list_namespaced_pod_disruption_budget=_empty),
    )


def test_diagnose_degrades_when_reads_are_forbidden(monkeypatch):
    fake_mgr = SimpleNamespace(
        guard_namespace=lambda *a, **k: None,
        clients=lambda *a, **k: _fake_clients(),
        resolve=lambda *a, **k: SimpleNamespace(name="aks-wi"),
    )
    monkeypatch.setattr(engine, "manager", lambda: fake_mgr)

    # Must NOT raise despite the 403s on nodes + secrets.
    report = engine.diagnose("aks-wi", "payments", "api")

    # And it must be honest that inputs were reduced.
    note = next((e.summary for e in report.evidence if "reads unavailable" in e.summary), "")
    assert "nodes" in note and "secrets" in note
