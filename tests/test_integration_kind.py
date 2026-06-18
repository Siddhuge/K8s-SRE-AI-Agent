"""Integration tests against a live kind cluster.

Run the demo setup first (see README / the live-demo steps): a `payments/api`
deployment that crashloops on a DB connection error. These tests SKIP automatically
if the cluster isn't reachable, so they're safe in CI without a cluster — and CI
spins one up explicitly (see .github/workflows/ci.yaml).
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("CLUSTERS_CONFIG", "config/clusters.yaml")
os.environ.setdefault("DEFAULT_CLUSTER", "kind-sre-demo")

CLUSTER = "kind-sre-demo"
NS = "payments"


def _manager():
    from k8s_sre_agent.clusters import manager
    return manager()


def _reachable() -> bool:
    try:
        _manager().clients(CLUSTER).core_v1.get_api_resources()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="kind cluster kind-sre-demo not reachable")


def _api_pod() -> str:
    pods = _manager().clients(CLUSTER).core_v1.list_namespaced_pod(NS, label_selector="app=api")
    assert pods.items, "expected the demo payments/api pod to exist"
    return pods.items[0].metadata.name


def test_logs_pod_returns_clean_lines():
    """Regression test for the kube-client bytes/str quirk we fixed live."""
    captured = {}

    class FakeMCP:
        def tool(self):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    from k8s_sre_agent.tools import logs
    logs.register(FakeMCP())
    out = captured["logs_pod"](namespace=NS, pod=_api_pod(), cluster=CLUSTER, previous=True, grep="FATAL")
    assert out["lines"], "expected the crash log line"
    line = out["lines"][0]
    assert not line.startswith("b'"), "log line must be decoded, not a bytes repr"
    assert "connect" in line.lower()


def test_rca_diagnose_identifies_crashloop():
    from k8s_sre_agent.rca.engine import diagnose
    report = diagnose(CLUSTER, NS, "api")
    assert report.issue.startswith("CrashLoopBackOff")  # may be "(missing dependency)"
    assert report.confidence >= 70
    assert "database" in report.root_cause.lower()


def test_secret_change_has_real_timestamp():
    from k8s_sre_agent.rca.engine import _secret_changes
    core = _manager().clients(CLUSTER).core_v1
    changes = _secret_changes(core, NS)
    # If the demo secret exists it must carry a real ISO timestamp (not a stub "").
    for c in changes:
        assert c["at"], f"secret {c['name']} should have a real change timestamp"


def test_tenant_guard_blocks_disallowed_namespace():
    from k8s_sre_agent.clusters import TenantIsolationError
    with pytest.raises(TenantIsolationError):
        _manager().guard_namespace(CLUSTER, "kube-node-lease")  # not in allowedNamespaces


# Each failure-scenario deployment (tests/fixtures/scenarios.yaml) → expected issue.
SCENARIOS = {
    "imagepull": {"ImagePullBackOff", "ErrImagePull"},
    "oom": {"OOMKilled", "CrashLoopBackOff"},
    "configerror": {"CreateContainerConfigError", "CreateContainerError"},
    "pending": {"Pending Pods", "Pending Pods (PVC)"},
    # second wave (tests/fixtures/scenarios2.yaml)
    "probefail": {"Liveness Probe Failure"},
    "dnsfail": {"DNS Resolution Failure"},
    "tlsfail": {"TLS Certificate Issue"},
    "pvcpending": {"Pending Pods (PVC)"},
    # third wave (tests/fixtures/scenarios3.yaml)
    "initfail": {"Init Container Failure"},
    "readinessfail": {"Readiness Probe Failure"},
    # fourth wave (tests/fixtures/scenarios4.yaml)
    "ephemeral": {"Pod Evicted: Ephemeral Storage", "Pod Evicted"},
    "pgsql": {"Pending Pods (PVC)"},          # StatefulSet subject resolution
    "scaleme": {"HPA Cannot Scale"},
    "locked": {"PodDisruptionBudget Blocking"},
}


def _deployed(subject: str) -> bool:
    pods = _manager().clients(CLUSTER).core_v1.list_namespaced_pod(NS, label_selector=f"app={subject}")
    return bool(pods.items)


@pytest.mark.parametrize("subject,expected", SCENARIOS.items())
def test_scenario_classified_correctly(subject, expected):
    if not _deployed(subject):
        pytest.skip(f"scenario {subject} not deployed (apply tests/fixtures/scenarios.yaml)")
    from k8s_sre_agent.rca.engine import diagnose
    report = diagnose(CLUSTER, NS, subject)
    assert report.issue in expected, f"{subject}: got {report.issue!r}, expected one of {expected}"
    # 60 floor: the durable liveness-probe fallback (used once Unhealthy events expire)
    # is an honest inference at ~62; event-backed detectors score higher.
    assert report.confidence >= 60


def test_job_failure_live():
    """The 'migrate' Job (scenarios3) — a Job subject must resolve and be diagnosed."""
    from k8s_sre_agent.clusters import manager
    try:
        manager().clients(CLUSTER).batch_v1.read_namespaced_job("migrate", NS)
    except Exception:
        pytest.skip("migrate Job not deployed")
    from k8s_sre_agent.rca.engine import diagnose
    r = diagnose(CLUSTER, NS, "migrate")
    assert r.issue == "Job Failed"
    assert r.confidence >= 80


def test_events_are_scoped_to_subject():
    """Regression: events from OTHER pods must not pollute a subject's evidence."""
    if not _deployed("imagepull"):
        pytest.skip("imagepull scenario not deployed")
    from k8s_sre_agent.rca.engine import collect_context
    ctx = collect_context(CLUSTER, NS, "imagepull")
    for e in ctx["events"]:
        name = e["object"].split("/", 1)[-1]
        assert name == "imagepull" or name.startswith("imagepull-"), \
            f"event for unrelated object leaked into imagepull context: {e['object']}"
