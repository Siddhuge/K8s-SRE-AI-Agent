"""Detector unit tests — verify the explainable rules fire on the right signatures.

These run with no cluster: they feed synthetic context bundles straight into the
detectors, which is exactly the contract the engine relies on.
"""
from k8s_sre_agent.rca.detectors import evaluate


def test_crashloop_db_auth_failure_credits_secret_rotation():
    """A rotated secret is a plausible cause ONLY for an AUTH failure, not a timeout."""
    ctx = {
        "describe": {"containers": [{"waiting_reason": "CrashLoopBackOff"}]},
        "pods": [{"reason": "CrashLoopBackOff"}],
        "previous_logs": ["FATAL: password authentication failed for user \"payments\""],
        "logs": [],
        "events": [],
        "recent_change": {"kind": "secret", "name": "db-credentials", "at": "2026-06-13T10:00:00+00:00"},
    }
    top = evaluate(ctx)[0]
    assert top.issue == "CrashLoopBackOff"
    assert "credential" in top.root_cause.lower()
    assert top.confidence >= 90  # auth failure + secret change → high
    assert top.rollback_required is False


def test_crashloop_timeout_is_reachability_not_secret():
    """A connection timeout must NOT be attributed to a secret rotation (the model
    caught this false positive live — db.payments.svc unreachable is not a creds bug)."""
    ctx = {
        "describe": {"containers": [{"waiting_reason": "CrashLoopBackOff"}]},
        "pods": [{"reason": "CrashLoopBackOff"}],
        "previous_logs": ["FATAL: could not connect to server: Connection timed out (host=db.payments.svc port=5432)"],
        "logs": [], "events": [],
        "recent_change": {"kind": "secret", "name": "db-credentials", "at": "2026-06-13T10:00:00+00:00"},
        "services": [],  # no db Service → missing dependency
    }
    top = evaluate(ctx)[0]
    assert "missing dependency" in top.issue.lower()
    assert "does not exist" in top.root_cause.lower()
    # the secret must NOT be the cited cause for a timeout
    assert all("secret" not in e.summary.lower() for e in top.evidence)


def test_image_pull_unauthorized():
    ctx = {
        "describe": {"containers": [{"waiting_reason": "ImagePullBackOff"}]},
        "pods": [],
        "events": [{"reason": "Failed", "message": "Error: ImagePullBackOff: pull access denied, unauthorized"}],
        "logs": [], "previous_logs": [],
    }
    top = evaluate(ctx)[0]
    assert top.issue == "ImagePullBackOff"
    assert "auth" in top.root_cause.lower()


def test_oom_killed_with_rising_memory():
    ctx = {
        "describe": {"containers": [{"last_terminated_reason": "OOMKilled"}]},
        "pods": [{"last_terminated": "OOMKilled"}],
        "events": [], "logs": [], "previous_logs": [],
        "memory_trend_rising": True,
    }
    top = evaluate(ctx)[0]
    assert top.issue == "OOMKilled"
    assert top.confidence >= 95


def test_pending_insufficient_resources():
    ctx = {
        "describe": {"containers": []},
        "pods": [{"phase": "Pending"}],
        "events": [{"reason": "FailedScheduling", "message": "0/5 nodes are available: 5 Insufficient cpu"}],
        "logs": [], "previous_logs": [],
    }
    top = evaluate(ctx)[0]
    assert top.issue == "Pending Pods"
    assert "cpu" in top.root_cause.lower()


def test_dns_error_beats_db_connection():
    """`dial tcp ... no such host` is DNS, not a downed database — DNS must win."""
    ctx = {
        "describe": {"containers": [{"waiting_reason": "CrashLoopBackOff"}]},
        "pods": [{"reason": "CrashLoopBackOff"}],
        "previous_logs": ["FATAL: dial tcp: lookup payments-db.invalid on 10.96.0.10:53: no such host"],
        "logs": [], "events": [],
    }
    top = evaluate(ctx)[0]
    assert top.issue == "DNS Resolution Failure"
    assert top.confidence >= 85


def test_tls_error_in_previous_container_logs():
    """Crashlooping pods carry the error in the previous container — detectors must read it."""
    ctx = {
        "describe": {"containers": [{"waiting_reason": "CrashLoopBackOff"}]},
        "pods": [{"reason": "CrashLoopBackOff"}],
        "logs": [],  # current container empty
        "previous_logs": ["tls: failed to verify certificate: x509: certificate has expired"],
        "events": [],
    }
    issues = {h.issue for h in evaluate(ctx)}
    assert "TLS Certificate Issue" in issues


def test_liveness_probe_durable_fallback_after_events_expire():
    """Once Unhealthy events age out, a restarting pod with a liveness probe and no
    app-level error in its logs should still be identified as a liveness probe failure."""
    ctx = {
        "describe": {"containers": [{"liveness": True, "readiness": False}]},
        "pods": [{"reason": "CrashLoopBackOff"}],
        "events": [],  # Unhealthy events expired
        "logs": ["10.0.0.1 - - [GET / HTTP/1.1] 200"],  # normal access log, no error
        "previous_logs": [],
    }
    top = evaluate(ctx)[0]
    assert top.issue == "Liveness Probe Failure"
    assert 55 <= top.confidence < 75  # an inference, not event-backed


def test_no_durable_probe_fallback_when_app_errors():
    """If the app logged a fatal error, it's a real crash — not a probe false-kill."""
    ctx = {
        "describe": {"containers": [{"liveness": True}]},
        "pods": [{"reason": "CrashLoopBackOff"}],
        "events": [],
        "logs": ["panic: runtime error: nil pointer dereference"],
        "previous_logs": [],
    }
    issues = {h.issue for h in evaluate(ctx)}
    assert "Liveness Probe Failure" not in issues


def test_istio_sidecar_oom_outranks_generic_oom():
    """A proxy OOM should be diagnosed as a sidecar problem, not a generic app OOM."""
    ctx = {
        "describe": {"containers": [
            {"name": "app", "ready": True},
            {"name": "istio-proxy", "ready": False, "waiting_reason": "CrashLoopBackOff",
             "last_terminated_reason": "OOMKilled"},
        ]},
        "pods": [{"last_terminated": "OOMKilled"}],
        "events": [], "logs": [], "previous_logs": [],
    }
    top = evaluate(ctx)[0]
    assert top.issue == "Istio Sidecar Not Ready"
    assert "proxy" in top.root_cause.lower() and top.confidence >= 90


def test_istio_sidecar_ready_no_finding():
    ctx = {
        "describe": {"containers": [
            {"name": "app", "ready": True},
            {"name": "istio-proxy", "ready": True},
        ]},
        "pods": [{"phase": "Running"}], "events": [], "logs": [], "previous_logs": [],
    }
    assert all(h.issue != "Istio Sidecar Not Ready" for h in evaluate(ctx))


def test_crashloop_durable_signal_when_sampled_mid_restart():
    """A pod caught in its brief running window (no CrashLoopBackOff waiting reason yet)
    is still crashlooping if it has repeated error-exits — the DB detector must fire."""
    ctx = {
        "describe": {"containers": [
            {"restart_count": 50, "last_terminated_reason": "Error", "ready": False},
        ]},
        "pods": [{"reason": None}],   # no instantaneous waiting reason
        "previous_logs": ["FATAL: could not connect to server: Connection timed out"],
        "logs": [], "events": [],
    }
    top = evaluate(ctx)[0]
    assert top.issue == "CrashLoopBackOff"
    assert "database" in top.root_cause.lower()


def test_init_container_failure_beats_pending():
    """A pod stuck in Init (failing init container) is an init problem, not Pending."""
    ctx = {
        "describe": {"init_containers": [
            {"name": "init-migrate", "waiting_reason": "CrashLoopBackOff", "last_terminated_reason": "Error", "last_exit_code": 1},
        ], "containers": []},
        "pods": [{"phase": "Pending", "init_waiting": "CrashLoopBackOff"}],
        "init_logs": ["ERROR: migration lock held by another process"],
        "failing_init_container": "init-migrate",
        "events": [], "logs": [], "previous_logs": [],
    }
    top = evaluate(ctx)[0]
    assert top.issue == "Init Container Failure"
    assert "init-migrate" in top.root_cause
    assert top.confidence >= 85
    assert all(h.issue != "Pending Pods" for h in evaluate(ctx))  # pending suppressed


def test_job_failure_detected():
    ctx = {
        "job": {"name": "migrate", "failed": 2,
                "conditions": [{"type": "Failed", "reason": "BackoffLimitExceeded", "message": "Job has reached the specified backoff limit"}]},
        "describe": {"containers": [], "init_containers": []},
        "pods": [{"reason": "Error", "last_terminated": "Error"}],
        "logs": ['FATAL: relation "orders" already exists'], "previous_logs": [], "events": [],
    }
    top = evaluate(ctx)[0]
    assert top.issue == "Job Failed"
    assert "migrate" in top.root_cause
    assert any("orders" in e.summary for e in top.evidence)


def test_ephemeral_storage_eviction():
    ctx = {
        "describe": {"containers": [], "init_containers": []},
        "pods": [{"phase": "Failed", "reason": "Evicted",
                  "message": 'Usage of EmptyDir volume "scratch" exceeds the limit "10Mi".'}],
        "events": [], "logs": [], "previous_logs": [],
    }
    top = evaluate(ctx)[0]
    assert top.issue == "Pod Evicted: Ephemeral Storage"
    assert top.confidence >= 85


def test_hpa_cannot_scale():
    ctx = {
        "describe": {"containers": [], "init_containers": []}, "pods": [], "events": [],
        "logs": [], "previous_logs": [],
        "hpas": [{"name": "scaleme", "target": "scaleme",
                  "conditions": [{"type": "ScalingActive", "status": "False",
                                  "reason": "FailedGetResourceMetric", "message": "no metrics"}]}],
    }
    top = evaluate(ctx)[0]
    assert top.issue == "HPA Cannot Scale"
    assert "FailedGetResourceMetric" in top.root_cause


def test_pdb_blocking():
    ctx = {
        "describe": {"containers": [], "init_containers": []}, "pods": [], "events": [],
        "logs": [], "previous_logs": [],
        "pdbs": [{"name": "locked", "disruptions_allowed": 0, "current_healthy": 1, "desired_healthy": 1}],
    }
    top = evaluate(ctx)[0]
    assert top.issue == "PodDisruptionBudget Blocking"
    assert "0 voluntary disruptions" in top.root_cause


def test_no_signature_returns_empty():
    ctx = {"describe": {"containers": []}, "pods": [{"phase": "Running"}], "events": [], "logs": [], "previous_logs": []}
    assert evaluate(ctx) == []
