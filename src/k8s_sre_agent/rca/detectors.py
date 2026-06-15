"""Issue detectors.

A detector is a pure function: given the collected context bundle, it decides
whether its failure signature is present and emits a Hypothesis with weighted
evidence and a remediation. Detectors are deliberately rule-based and explainable —
they make the agent's reasoning auditable rather than a black box. Claude then
arbitrates between competing hypotheses and writes the human-facing narrative.

Each detector maps to one of the scenarios in docs/incident-scenarios.md.
"""
from __future__ import annotations

from collections.abc import Callable

from .models import Evidence, Hypothesis

# A context bundle is the dict assembled by engine.collect_context().
Detector = Callable[[dict], Hypothesis | None]

_REGISTRY: list[Detector] = []


def detector(fn: Detector) -> Detector:
    _REGISTRY.append(fn)
    return fn


def all_detectors() -> list[Detector]:
    return list(_REGISTRY)


def _waiting_reasons(ctx: dict) -> set[str]:
    reasons = set()
    for c in ctx.get("describe", {}).get("containers", []):
        if c.get("waiting_reason"):
            reasons.add(c["waiting_reason"])
    for p in ctx.get("pods", []):
        if p.get("reason"):
            reasons.add(p["reason"])
    return reasons


def _event_reasons(ctx: dict) -> list[dict]:
    return ctx.get("events", [])


def _recent_change(ctx: dict) -> dict | None:
    """Most recent deploy/sync/secret-rotation that precedes the failure window."""
    return ctx.get("recent_change")


def _is_crashlooping(ctx: dict) -> bool:
    """Durable crashloop signal. The instantaneous waiting reason is only
    'CrashLoopBackOff' during the back-off window — a pod sampled mid-restart shows no
    waiting reason yet is clearly crashlooping. So also treat repeated error-exits
    (restart_count >= 3 with a non-OOM 'Error' termination) as crashlooping."""
    if "CrashLoopBackOff" in _waiting_reasons(ctx):
        return True
    for c in ctx.get("describe", {}).get("containers", []):
        if (c.get("restart_count") or 0) >= 3 and c.get("last_terminated_reason") == "Error":
            return True
    return False


# ── Pod issues ────────────────────────────────────────────────────────────────

def _parse_db_host(logs: str) -> str:
    """Best-effort extraction of the DB host from a crash log
    ('host=db.payments.svc ...' or 'connecting to ... db.payments.svc:5432')."""
    import re

    m = re.search(r"host[=:]\s*([a-z0-9.\-]+)", logs)
    if m:
        return m.group(1).rstrip(".")
    m = re.search(r"([a-z0-9][a-z0-9.\-]*\.[a-z0-9.\-]+|[a-z0-9\-]+):\d{2,5}", logs)
    return m.group(1).rstrip(".") if m else ""


# A DB-connection failure splits into two very different root causes:
_DB_AUTH = ("password authentication failed", "authentication failed", "access denied for user",
            "permission denied", "auth failed", "invalid password", "role \"")
_DB_REACH = ("connection refused", "connection timed out", "could not connect", "econnrefused",
             "i/o timeout", "no route to host", "dial tcp", "timed out")


@detector
def crashloop_db_connection(ctx: dict) -> Hypothesis | None:
    if not _is_crashlooping(ctx):
        return None
    logs = _all_logs(ctx)
    h = Hypothesis(issue="CrashLoopBackOff", confidence=40, root_cause="Container repeatedly exits after start.")
    h.evidence.append(Evidence(source="k8s", summary="Pod in CrashLoopBackOff", weight=0.4))

    # DNS errors are owned by dns_failure (scores higher) — don't claim a DB issue.
    dns_specific = ("no such host", "name or service not known", "could not resolve",
                    "temporary failure in name resolution")
    if any(s in logs for s in dns_specific):
        return h

    is_auth = any(s in logs for s in _DB_AUTH)
    is_reach = any(s in logs for s in _DB_REACH)
    if not (is_auth or is_reach):
        return h  # generic crashloop, low confidence — let other detectors compete

    change = _recent_change(ctx)

    if is_auth:
        # Credentials rejected — THIS is when a rotated secret is a plausible cause.
        h.root_cause = "Database rejected the application's credentials (auth failure in crash logs)."
        h.confidence = 82
        h.suggested_fix = "Verify the DB username/password in the credentials secret; restart the deployment once corrected."
        h.evidence.append(Evidence(source="logs", summary="DB authentication failure in crash logs", weight=0.42))
        if change and change.get("kind") == "secret":
            h.confidence = min(95, h.confidence + 12)
            h.suggested_fix = f"Secret {change['name']!r} changed at {change['at']} before failures — verify the new credentials; restart."
            h.evidence.append(Evidence(source="k8s", summary=f"Secret {change['name']} changed {change['at']} before crash", weight=0.12, timestamp=change["at"]))
    else:
        # Reachability (timeout/refused) — a network/dependency problem, NOT credentials.
        # A rotated secret does NOT cause a timeout, so it is deliberately not credited here.
        db_host = _parse_db_host(logs)
        host_short = db_host.split(".")[0] if db_host else ""
        services = set(ctx.get("services", []))
        h.root_cause = (
            f"Application cannot reach its database{f' at {db_host}' if db_host else ''} "
            "(connection refused/timed out) — a reachability problem, not credentials."
        )
        h.confidence = 80
        h.suggested_fix = "Check the DB Service/endpoints exist and are reachable (host, port, NetworkPolicy); verify the configured DB host."
        h.evidence.append(Evidence(source="logs", summary="DB connection timeout/refused in crash logs", weight=0.4))
        if host_short and "services" in ctx and host_short not in services:
            h.issue = "CrashLoopBackOff (missing dependency)"
            h.root_cause = (
                f"The database the app targets does not exist: no Service '{host_short}' in the "
                f"namespace, and the crash log shows a connection timeout to {db_host or host_short}. "
                "The dependency was never deployed (the app has not been healthy)."
            )
            h.confidence = 88
            h.suggested_fix = f"Deploy the database (Service '{host_short}' + workload on its port), or point the app's DB host at the real endpoint; then restart."
            h.evidence.append(Evidence(source="k8s", summary=f"No Service '{host_short}' in namespace — DB dependency absent", weight=0.3))

    # A bad release/sync is a rollback candidate regardless of auth vs reachability.
    if change and change.get("kind") in ("deploy", "sync"):
        h.confidence = min(95, h.confidence + 8)
        h.rollback_required = True
        h.rollback_target = change.get("rollback_target", "previous revision")
        h.evidence.append(Evidence(source="gitops", summary=f"Failures started after release {change.get('revision','?')}", weight=0.08, timestamp=change["at"]))
    return h


@detector
def image_pull(ctx: dict) -> Hypothesis | None:
    reasons = _waiting_reasons(ctx)
    hit = reasons & {"ImagePullBackOff", "ErrImagePull"}
    if not hit:
        return None
    msg = " ".join(e["message"] for e in _event_reasons(ctx) if e.get("reason") in ("Failed", "BackOff")).lower()
    h = Hypothesis(
        issue=sorted(hit)[0],
        confidence=88,
        root_cause="Image cannot be pulled.",
        suggested_fix="Check the image tag exists and the registry pull secret is valid.",
    )
    h.evidence.append(Evidence(source="k8s", summary=f"Container waiting: {sorted(hit)[0]}", weight=0.5))
    if "not found" in msg or "manifest unknown" in msg:
        h.root_cause = "Image tag/digest does not exist in the registry (likely a typo or a never-pushed tag)."
        h.confidence = 92
    elif "unauthorized" in msg or "denied" in msg or "forbidden" in msg:
        h.root_cause = "Registry authentication failed — imagePullSecret missing, expired, or wrong registry."
        h.suggested_fix = "Repair the imagePullSecret / registry credentials referenced by the pod's serviceAccount."
        h.confidence = 92
    if msg:
        h.evidence.append(Evidence(source="events", summary=msg[:200], weight=0.4))
    return h


@detector
def container_config_error(ctx: dict) -> Hypothesis | None:
    reasons = _waiting_reasons(ctx)
    hit = reasons & {"CreateContainerConfigError", "CreateContainerError"}
    if not hit:
        return None
    # Prefer Warning events for the evidence snippet (the high-signal "not found"
    # message), not Normal lifecycle events like "successfully assigned to node".
    warnings = [e for e in _event_reasons(ctx) if e.get("type") == "Warning"]
    msg = " ".join(e["message"] for e in warnings).lower()
    evidence_msg = next((e["message"] for e in warnings), "")
    h = Hypothesis(issue=sorted(hit)[0], confidence=85, root_cause="Container cannot be created from its config.")
    h.evidence.append(Evidence(source="k8s", summary=f"{sorted(hit)[0]}", weight=0.5))
    if "configmap" in msg and "not found" in msg:
        h.root_cause = "Referenced ConfigMap does not exist (envFrom/volume mount points at a missing ConfigMap)."
        h.suggested_fix = "Create the missing ConfigMap or fix the reference name."
        h.confidence = 90
    elif "secret" in msg and "not found" in msg:
        h.root_cause = "Referenced Secret does not exist."
        h.suggested_fix = "Create the missing Secret or fix the reference name."
        h.confidence = 90
    if evidence_msg:
        h.evidence.append(Evidence(source="events", summary=evidence_msg[:200], weight=0.4))
    return h


@detector
def oom_killed(ctx: dict) -> Hypothesis | None:
    last = {c.get("last_terminated_reason") for c in ctx.get("describe", {}).get("containers", [])}
    last |= {p.get("last_terminated") for p in ctx.get("pods", [])}
    if "OOMKilled" not in last:
        return None
    h = Hypothesis(
        issue="OOMKilled",
        confidence=90,
        root_cause="Container exceeded its memory limit and was OOM-killed by the kernel.",
        suggested_fix="Raise the memory limit, fix the leak, or reduce workload concurrency. Confirm with the memory metric trend.",
    )
    h.evidence.append(Evidence(source="k8s", summary="Container last terminated with OOMKilled", weight=0.6))
    if ctx.get("memory_trend_rising"):
        h.confidence = 95
        h.evidence.append(Evidence(source="metrics", summary="Working-set memory climbed to the limit before restart", weight=0.35))
    return h


# App-level fatal signals — their ABSENCE (with a liveness probe + restarts) points
# at an external killer (the probe) rather than the app crashing on its own.
_APP_ERROR_SIGNALS = (
    "fatal", "panic", "traceback", "exception", "could not connect", "connection refused",
    "no such host", "x509", "segmentation fault", "unhandled", "exit status 1",
)


@detector
def probe_failure(ctx: dict) -> Hypothesis | None:
    unhealthy = [e for e in _event_reasons(ctx) if e.get("reason") == "Unhealthy"]
    if unhealthy:
        msgs = " ".join(e["message"] for e in unhealthy).lower()
        kind = "Liveness" if "liveness" in msgs else "Readiness" if "readiness" in msgs else "Probe"
        h = Hypothesis(
            issue=f"{kind} Probe Failure",
            confidence=78,
            root_cause=f"{kind} probe is failing — app not responding healthy on the probe endpoint/port.",
            suggested_fix=f"Check the {kind.lower()} probe path/port and the app's startup time; consider a startupProbe or higher initialDelaySeconds.",
        )
        h.evidence.append(Evidence(source="events", summary=unhealthy[0]["message"][:200], weight=0.6, timestamp=unhealthy[0].get("age", "")))
        if kind == "Liveness":
            h.evidence.append(Evidence(source="k8s", summary="Failing liveness probes cause restarts → possible restart loop", weight=0.18))
        return h

    # Durable fallback: Unhealthy events expire (~1h). A container that keeps
    # restarting with a liveness probe configured but NO application-level error in
    # its logs is the signature of an external killer — i.e. the liveness probe.
    containers = ctx.get("describe", {}).get("containers", [])
    has_liveness = any(c.get("liveness") for c in containers)
    restarting = any(
        p.get("reason") == "CrashLoopBackOff" or p.get("last_terminated") for p in ctx.get("pods", [])
    )
    app_error = any(s in _all_logs(ctx) for s in _APP_ERROR_SIGNALS)
    if has_liveness and restarting and not app_error:
        h = Hypothesis(
            issue="Liveness Probe Failure",
            confidence=62,
            root_cause=(
                "Container is restarting repeatedly with a liveness probe configured and "
                "no application-level error in its logs — the liveness probe is most likely "
                "failing (the transient Unhealthy events have already aged out)."
            ),
            suggested_fix=(
                "Verify the liveness probe path/port matches where the app actually listens, "
                "and review initialDelaySeconds/failureThreshold. Confirm against current "
                "probe config since kubelet's Unhealthy events expire ~1h."
            ),
        )
        h.evidence.append(Evidence(
            source="k8s",
            summary="Repeated restarts + liveness probe configured + no app error in logs (Unhealthy events expired)",
            weight=0.5,
        ))
        return h
    return None


@detector
def pending_unschedulable(ctx: dict) -> Hypothesis | None:
    pending = [p for p in ctx.get("pods", []) if p.get("phase") == "Pending"]
    sched = [e for e in _event_reasons(ctx) if e.get("reason") == "FailedScheduling"]
    if not pending and not sched:
        return None
    msg = (sched[0]["message"] if sched else "").lower()
    h = Hypothesis(issue="Pending Pods", confidence=80, root_cause="Scheduler cannot place the pod.")
    if sched:
        h.evidence.append(Evidence(source="events", summary=sched[0]["message"][:200], weight=0.6))
    if "insufficient cpu" in msg or "insufficient memory" in msg:
        h.root_cause = "No node has enough free CPU/memory for the pod's requests."
        h.suggested_fix = "Scale the node pool / cluster-autoscaler, lower pod requests, or evict lower-priority workloads."
    elif "taint" in msg or "node(s) had untolerated taint" in msg:
        h.root_cause = "Pod does not tolerate the taints on available nodes."
        h.suggested_fix = "Add the required toleration/nodeSelector, or untaint a node pool."
    elif "didn't match" in msg and "affinity" in msg or "node affinity" in msg:
        h.root_cause = "Node affinity/selector matches no available node."
        h.suggested_fix = "Relax nodeAffinity/nodeSelector or add a matching node pool."
    elif "persistentvolumeclaim" in msg or "pvc" in msg or "volume node affinity" in msg:
        h.root_cause = "PVC cannot be bound/attached on a schedulable node (storage class / zone mismatch)."
        h.suggested_fix = "Check the PVC StorageClass, volume zone, and CSI driver health."
        h.issue = "Pending Pods (PVC)"
    return h


# ── Node issues ─────────────────────────────────────────────────────────────

@detector
def node_pressure(ctx: dict) -> Hypothesis | None:
    bad = [n for n in ctx.get("nodes", []) if n.get("ready") != "True"
           or "True" in (n.get("disk_pressure"), n.get("memory_pressure"), n.get("pid_pressure"))]
    if not bad:
        return None
    n = bad[0]
    pressures = [k for k in ("disk_pressure", "memory_pressure", "pid_pressure") if n.get(k) == "True"]
    if n.get("ready") != "True":
        issue, rc = "NodeNotReady", f"Node {n['name']} is NotReady (kubelet/network/runtime problem)."
        fix = "Check kubelet + container runtime on the node; cordon/drain and replace if it stays NotReady."
    else:
        label = pressures[0].split("_")[0].title()
        issue = f"Node {label}Pressure"
        rc = f"Node {n['name']} reports {issue} — kubelet will evict pods."
        fix = {"Disk": "Free disk / increase volume / prune images & logs.",
               "Memory": "Reduce memory pressure: evict, add capacity, fix leaks.",
               "Pid": "Lower process count or raise PID limits."}.get(label, "Investigate node resource pressure.")
    h = Hypothesis(issue=issue, confidence=85, root_cause=rc, suggested_fix=fix)
    h.evidence.append(Evidence(source="k8s", summary=f"Node {n['name']}: ready={n.get('ready')} pressures={pressures or 'none'}", weight=0.7))
    return h


# ── Service mesh (Istio sidecar) ──────────────────────────────────────────────

@detector
def istio_sidecar_not_ready(ctx: dict) -> Hypothesis | None:
    """The istio-proxy sidecar is present but not Ready — the pod won't serve traffic
    even if the app container is healthy. Common causes: proxy can't reach istiod, a
    too-low proxy resource limit (OOM), or an injection/config error."""
    containers = ctx.get("describe", {}).get("containers", [])
    proxy = next((c for c in containers if c.get("name") == "istio-proxy"), None)
    if proxy is None or proxy.get("ready") is True:
        return None

    waiting = proxy.get("waiting_reason")
    last_term = proxy.get("last_terminated_reason")
    h = Hypothesis(
        issue="Istio Sidecar Not Ready",
        confidence=80,
        root_cause="The istio-proxy sidecar is not Ready — the pod cannot serve mesh traffic.",
        suggested_fix="Check the istio-proxy container logs and its connectivity to istiod; verify injection and proxy resource limits.",
    )
    h.evidence.append(Evidence(source="k8s", summary=f"istio-proxy not ready (waiting={waiting}, last_terminated={last_term})", weight=0.6))
    if last_term == "OOMKilled":
        h.confidence = 91  # more specific than a generic OOM
        h.root_cause = "The istio-proxy sidecar is OOMKilled — its memory limit is too low for the proxy."
        h.suggested_fix = "Raise the proxy memory limit (sidecar.istio.io/proxyMemoryLimit) or the mesh default; confirm with the proxy's memory trend."
    elif waiting in ("ImagePullBackOff", "ErrImagePull"):
        h.confidence = 88
        h.root_cause = "The istio-proxy image cannot be pulled — wrong proxy image/tag or registry access."
    return h


# ── Application / platform issues (TLS, DNS, ingress) ──────────────────────────

def _all_logs(ctx: dict) -> str:
    """Current + previous-container logs. Crashlooping pods carry the real error in
    the previous container, so detectors must read both."""
    return " ".join(ctx.get("logs", []) + ctx.get("previous_logs", [])).lower()


@detector
def tls_certificate(ctx: dict) -> Hypothesis | None:
    logs = _all_logs(ctx)
    signals = ("x509", "certificate has expired", "certificate verify failed", "tls handshake", "unable to verify the first certificate")
    if not any(s in logs for s in signals):
        return None
    h = Hypothesis(
        issue="TLS Certificate Issue",
        confidence=75,
        root_cause="TLS verification failing (expired/invalid cert or missing CA in trust store).",
        suggested_fix="Check cert-manager Certificate/Order status and the mounted CA bundle; renew or fix the trust chain.",
    )
    h.evidence.append(Evidence(source="logs", summary="x509/TLS verification error in logs", weight=0.7))
    return h


@detector
def dns_failure(ctx: dict) -> Hypothesis | None:
    logs = _all_logs(ctx)
    # "no such host" / "name or service not known" / "could not resolve" are
    # unambiguous DNS — score them high so they win over the generic DB-connection
    # detector when the error is `dial tcp ... no such host`.
    strong = ("no such host", "name or service not known", "could not resolve")
    weak = ("temporary failure in name resolution", "server misbehaving")
    if not any(s in logs for s in strong + weak):
        return None
    h = Hypothesis(
        issue="DNS Resolution Failure",
        confidence=85 if any(s in logs for s in strong) else 72,
        root_cause="In-cluster DNS resolution is failing (CoreDNS unhealthy, NetworkPolicy blocking :53, or bad Service/host name).",
        suggested_fix="Check CoreDNS pods/logs, NetworkPolicies allowing UDP/TCP 53, and that the target Service/FQDN exists.",
    )
    h.evidence.append(Evidence(source="logs", summary="DNS resolution error in logs", weight=0.7))
    return h


def evaluate(ctx: dict) -> list[Hypothesis]:
    """Run every detector and return non-null hypotheses, highest confidence first."""
    out = [h for d in all_detectors() if (h := d(ctx)) is not None]
    out.sort(key=lambda h: h.confidence, reverse=True)
    return out
