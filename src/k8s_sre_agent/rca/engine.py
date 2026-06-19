"""RCA orchestration.

`rca_diagnose` is the headline tool. Given a subject (cluster/namespace/pod or
deployment), it:

  1. **Collects context deterministically** — pods, describe, events, recent logs
     (incl. previous-container logs), restart/memory metrics, deployment history and
     recent CI deploys — without the model having to issue a dozen tool calls itself.
  2. **Correlates** — finds the change that precedes the first failure event and
     builds a timeline.
  3. **Runs the rule-based detectors** to produce weighted, explainable hypotheses.
  4. **Optionally consults RAG** for a matching runbook.
  5. Returns a structured RCAReport. Claude turns it into the final narrative,
     arbitrates between alternatives, and (if asked) posts the summary.

The collection step is bounded and read-only, so the agent's first action on an
incident is cheap and high-signal instead of a long exploratory tool loop.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from kubernetes.client.rest import ApiException

from ..clusters import manager
from ..config import get_settings
from . import correlate
from .detectors import evaluate
from .models import Evidence, RCAReport, Severity

log = logging.getLogger("k8s_sre_agent.rca")

_SEVERITY_BY_ISSUE = {
    "NodeNotReady": Severity.critical,
    "Node MemoryPressure": Severity.high,
    "Node DiskPressure": Severity.high,
    "CrashLoopBackOff": Severity.high,
    "Init Container Failure": Severity.high,
    "Job Failed": Severity.medium,
    "Pod Evicted: Ephemeral Storage": Severity.high,
    "HPA Cannot Scale": Severity.medium,
    "PodDisruptionBudget Blocking": Severity.medium,
    "OOMKilled": Severity.high,
    "ImagePullBackOff": Severity.medium,
    "ErrImagePull": Severity.medium,
    "Pending Pods": Severity.medium,
}


def _last_change_time(obj) -> str:
    """Best-effort 'when did this object last change' from read-only metadata.

    Uses the most recent `managedFields[].time` (set by the API server on every
    apply/update) and falls back to creationTimestamp. This gives a real rotation
    timestamp without any audit-log integration — purely from the object we can read.
    """
    times = [mf.time for mf in (obj.metadata.managed_fields or []) if mf.time]
    latest = max(times) if times else obj.metadata.creation_timestamp
    return latest.isoformat() if latest else ""


def _secret_changes(core, namespace: str) -> list[dict]:
    """Real secret rotation times via managedFields, for change-correlation."""
    out = []
    for s in core.list_namespaced_secret(namespace).items:
        when = _last_change_time(s)
        if when:
            out.append({"name": s.metadata.name, "at": when})
    return out


def collect_context(cluster: str | None, namespace: str, subject: str) -> dict:
    """Read-only context bundle for `subject` (a pod name, or a deployment name)."""
    mgr = manager()
    mgr.guard_namespace(cluster, namespace)
    clients = mgr.clients(cluster)
    core, apps = clients.core_v1, clients.apps_v1

    ctx: dict = {"cluster": mgr.resolve(cluster).name, "namespace": namespace, "subject": subject}

    # Resolve subject → concrete pod(s): a pod, a Deployment, a StatefulSet, or a Job.
    pod_names: list[str] = []
    selector_labels: dict = {}

    def _pods_for(labels: dict) -> list[str]:
        sel = ",".join(f"{k}={v}" for k, v in labels.items())
        return [p.metadata.name for p in core.list_namespaced_pod(namespace, label_selector=sel).items]

    def _try(fn) -> bool:
        try:
            fn()
            return True
        except Exception:
            return False

    def _pod():
        nonlocal pod_names
        core.read_namespaced_pod(subject, namespace)
        pod_names = [subject]

    def _deploy():
        nonlocal pod_names, selector_labels
        selector_labels = apps.read_namespaced_deployment(subject, namespace).spec.selector.match_labels or {}
        pod_names = _pods_for(selector_labels)

    def _sts():
        nonlocal pod_names, selector_labels
        selector_labels = apps.read_namespaced_stateful_set(subject, namespace).spec.selector.match_labels or {}
        pod_names = _pods_for(selector_labels)

    def _job():
        nonlocal pod_names
        job = mgr.clients(cluster).batch_v1.read_namespaced_job(subject, namespace)
        ctx["job"] = {
            "name": subject, "failed": job.status.failed or 0, "succeeded": job.status.succeeded or 0,
            "conditions": [{"type": c.type, "reason": c.reason, "message": c.message}
                           for c in (job.status.conditions or [])],
        }
        pod_names = [p.metadata.name for p in
                     core.list_namespaced_pod(namespace, label_selector=f"job-name={subject}").items]

    resolved = _try(_pod) or _try(_deploy) or _try(_sts) or _try(_job)
    if not resolved:
        log.warning("could not resolve subject", extra={"subject": subject})
    ctx["resolved"] = resolved
    ctx["selector_labels"] = selector_labels

    # Pods overview (status/restarts/reasons). If the subject resolved, scope strictly
    # to its pods; if it did NOT resolve, do NOT fall back to all namespace pods — that
    # cross-contaminates the RCA with unrelated workloads (a real bug found live).
    all_pods = core.list_namespaced_pod(namespace)
    ctx["pods"] = [
        {
            "name": p.metadata.name,
            "phase": p.status.phase,
            "reason": next((c.state.waiting.reason for c in (p.status.container_statuses or [])
                            if c.state and c.state.waiting), p.status.reason),
            "message": p.status.message,   # e.g. eviction reason ("...exceeds the limit...")
            "last_terminated": next((c.last_state.terminated.reason for c in (p.status.container_statuses or [])
                                     if c.last_state and c.last_state.terminated), None),
            # init container failure signals (the engine used to ignore these entirely)
            "init_waiting": next((c.state.waiting.reason for c in (p.status.init_container_statuses or [])
                                  if c.state and c.state.waiting), None),
            "init_terminated": next((c.last_state.terminated.reason for c in (p.status.init_container_statuses or [])
                                     if c.last_state and c.last_state.terminated and c.last_state.terminated.exit_code), None),
        }
        for p in all_pods.items
        if p.metadata.name in pod_names
    ]

    target_pod = pod_names[0] if pod_names else None
    pod_start_time = None
    if target_pod:
        # Reuse the already-fetched list (no extra GET) for the pod start time.
        tp = next((p for p in all_pods.items if p.metadata.name == target_pod), None)
        pod_start_time = tp.status.start_time if tp else None

    # Fan out the independent reads concurrently. The kubernetes client is blocking
    # but releases the GIL during I/O, so on a remote cluster (50-150ms/round-trip)
    # this collapses ~6 serial calls into roughly one round-trip of wall time.
    clients = mgr.clients(cluster)
    with ThreadPoolExecutor(max_workers=9) as ex:
        futures = {
            "events": ex.submit(core.list_namespaced_event, namespace),
            "nodes": ex.submit(core.list_node),
            "secrets": ex.submit(_secret_changes, core, namespace),
            "services": ex.submit(core.list_namespaced_service, namespace),
            "hpas": ex.submit(clients.autoscaling_v2.list_namespaced_horizontal_pod_autoscaler, namespace),
            "pdbs": ex.submit(clients.policy_v1.list_namespaced_pod_disruption_budget, namespace),
        }
        if target_pod:
            futures["describe"] = ex.submit(kubernetes_describe, core, namespace, target_pod)
            futures["logs"] = ex.submit(_read_logs, core, namespace, target_pod, False)
            futures["previous_logs"] = ex.submit(_read_logs, core, namespace, target_pod, True)
        # Resolve each read independently. A least-privilege identity may be DENIED a
        # specific read (e.g. Azure RBAC "Reader" grants no cluster-scoped nodes and no
        # secrets) — degrade that one signal instead of aborting the whole diagnosis.
        _empty = SimpleNamespace(items=[])
        _defaults: dict = {
            "events": _empty, "nodes": _empty, "services": _empty,
            "hpas": _empty, "pdbs": _empty, "secrets": [],
            "describe": {}, "logs": "", "previous_logs": "",
        }
        results = {}
        degraded: list[str] = []
        for k, f in futures.items():
            try:
                results[k] = f.result()
            except ApiException as e:
                log.warning("rca: read %r unavailable (HTTP %s) — continuing without it", k, e.status)
                results[k] = _defaults[k]
                degraded.append(k)
            except Exception as e:  # noqa: BLE001 — one failed read must never kill the RCA
                log.warning("rca: read %r failed (%s) — continuing without it", k, e)
                results[k] = _defaults[k]
                degraded.append(k)
    ctx["degraded_reads"] = degraded

    # HPAs targeting the subject + PDBs selecting the subject's pods (scoped, not all).
    ctx["hpas"] = [
        {
            "name": h.metadata.name,
            "target": h.spec.scale_target_ref.name,
            "conditions": [{"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
                           for c in (h.status.conditions or [])],
        }
        for h in results["hpas"].items
        if h.spec.scale_target_ref.name == subject
    ]
    ctx["pdbs"] = [
        {
            "name": p.metadata.name,
            "disruptions_allowed": p.status.disruptions_allowed,
            "current_healthy": p.status.current_healthy,
            "desired_healthy": p.status.desired_healthy,
        }
        for p in results["pdbs"].items
        if selector_labels and (p.spec.selector.match_labels or {}).items() <= selector_labels.items()
    ]

    if target_pod:
        ctx["describe"] = results["describe"]
        ctx["logs"] = results["logs"]
        ctx["previous_logs"] = results["previous_logs"]
        # If an init container is failing, the real error is in ITS logs (not the app
        # container, which never starts). Read the failing init container's logs.
        failing_init = next(
            (c for c in ctx["describe"].get("init_containers", [])
             if c.get("waiting_reason") in ("CrashLoopBackOff", "Error") or c.get("last_terminated_reason")),
            None,
        )
        if failing_init:
            ctx["init_logs"] = _read_logs(core, namespace, target_pod, previous=False,
                                          container=failing_init["name"])
            ctx["failing_init_container"] = failing_init["name"]

    # Service names (lets detectors check whether a dependency's Service exists).
    ctx["services"] = [s.metadata.name for s in results["services"].items]

    # Events: fetched namespace-wide, then SCOPE to the subject's own objects (pod(s),
    # their ReplicaSet, the Deployment). Without this, a busy namespace's unrelated
    # events leak into the evidence and can mis-drive a detector.
    allowed = set(pod_names) | {subject}
    events_raw = results["events"]

    def _relevant(name: str) -> bool:
        # pod name match, deployment/subject match, or ReplicaSet (prefix of a pod name)
        return name in allowed or any(p == name or p.startswith(name + "-") for p in pod_names)

    ctx["events"] = [
        {
            "type": e.type, "reason": e.reason,
            "message": e.message or "",
            "object": f"{e.involved_object.kind}/{e.involved_object.name}",
            "timestamp": (e.last_timestamp or e.event_time).isoformat() if (e.last_timestamp or e.event_time) else "",
            "age": "",
        }
        for e in events_raw.items
        if _relevant(e.involved_object.name or "")
    ]

    # Failure anchor for change-correlation. A pod that has restarted and is not ready
    # has been failing since it started — so its true failure start is the pod start
    # time, which is more reliable than the oldest *retained* event (events expire ~1h,
    # which otherwise makes a later change look causal when it isn't).
    event_failure = correlate.first_failure_time(ctx["events"])
    crashing_since_start = any(
        (p.get("reason") in ("CrashLoopBackOff", "Error") or p.get("last_terminated"))
        for p in ctx.get("pods", [])
    )
    if crashing_since_start and pod_start_time:
        ctx["first_failure_time"] = (
            min(event_failure, pod_start_time) if event_failure else pod_start_time
        )
    else:
        ctx["first_failure_time"] = event_failure

    # Nodes (for node-pressure detectors) — fetched in the parallel fan-out above.
    ctx["nodes"] = [
        {
            "name": n.metadata.name,
            "ready": next((c.status for c in (n.status.conditions or []) if c.type == "Ready"), None),
            "disk_pressure": next((c.status for c in (n.status.conditions or []) if c.type == "DiskPressure"), None),
            "memory_pressure": next((c.status for c in (n.status.conditions or []) if c.type == "MemoryPressure"), None),
            "pid_pressure": next((c.status for c in (n.status.conditions or []) if c.type == "PIDPressure"), None),
        }
        for n in results["nodes"].items
    ]

    # Change correlation inputs: real secret rotation times from managedFields
    # (also fetched in the parallel fan-out). ArgoCD syncs / CI deploys are added by
    # the model from the gitops/cicd tools (or an audit-log integration) before
    # re-running correlation.
    ctx["secret_changes"] = results["secrets"]
    ctx["recent_change"] = correlate.find_recent_change(ctx)
    return ctx


def kubernetes_describe(core, namespace: str, pod: str) -> dict:
    p = core.read_namespaced_pod(pod, namespace)
    statuses = {c.name: c for c in (p.status.container_statuses or [])}
    containers = []
    for c in p.spec.containers:
        st = statuses.get(c.name)
        term = st.last_state.terminated if st and st.last_state else None
        containers.append({
            "name": c.name, "image": c.image,
            "waiting_reason": (st.state.waiting.reason if st and st.state and st.state.waiting else None),
            "last_terminated_reason": (term.reason if term else None),
            "last_exit_code": (term.exit_code if term else None),
            "liveness": bool(c.liveness_probe),    # durable signal (events expire ~1h)
            "readiness": bool(c.readiness_probe),
            "restart_count": (st.restart_count if st else 0),
            "ready": (st.ready if st else None),   # container readiness (sidecar checks)
        })
    init_statuses = {c.name: c for c in (p.status.init_container_statuses or [])}
    init_containers = []
    for c in (p.spec.init_containers or []):
        st = init_statuses.get(c.name)
        term = st.last_state.terminated if st and st.last_state else None
        init_containers.append({
            "name": c.name, "image": c.image,
            "waiting_reason": (st.state.waiting.reason if st and st.state and st.state.waiting else None),
            "last_terminated_reason": (term.reason if term else None),
            "last_exit_code": (term.exit_code if term else None),
        })
    return {"containers": containers, "init_containers": init_containers, "node": p.spec.node_name}


def _read_logs(core, namespace: str, pod: str, previous: bool, tail: int = 120, container: str = "") -> list[str]:
    try:
        # _preload_content=False avoids a kube-client deserialization quirk that can
        # return the str repr of bytes; read + decode the raw stream instead.
        resp = core.read_namespaced_pod_log(
            pod, namespace, container=(container or None), tail_lines=tail,
            previous=previous, _preload_content=False,
        )
        return resp.data.decode("utf-8", "replace").splitlines()
    except Exception:  # no previous container, or restricted
        return []


def _degraded_evidence(ctx: dict) -> list[Evidence]:
    """Note any reads that were denied/unavailable, so the RCA is honest about its inputs."""
    d = ctx.get("degraded_reads") or []
    if not d:
        return []
    return [Evidence(
        source="k8s", weight=0.0,
        summary=(f"Partial inputs — reads unavailable for: {', '.join(d)} "
                 "(least-privilege identity). Analysis proceeded without them."),
    )]


def diagnose(cluster: str | None, namespace: str, subject: str) -> RCAReport:
    ctx = collect_context(cluster, namespace, subject)
    hypotheses = evaluate(ctx)

    if not hypotheses:
        return RCAReport(
            severity=Severity.info, cluster=ctx["cluster"], namespace=namespace, subject=subject,
            issue="No known failure signature detected",
            root_cause="Automated detectors found no matching signature. Inspect logs/metrics manually or widen the window.",
            confidence=20,
            evidence=[Evidence(source="k8s", summary="No CrashLoop/ImagePull/OOM/probe/node-pressure signatures matched")]
            + _degraded_evidence(ctx),
            suggested_fix="Use logs_pod / prom_query / loki_query to investigate directly.",
            rollback_required=False,
            timeline=correlate.build_timeline(ctx),
        )

    top = hypotheses[0]
    top.evidence += _degraded_evidence(ctx)

    # Optional RAG runbook match.
    if get_settings().rag_enabled:
        try:
            from ..rag import retrieve as rag
            hit = rag.search(f"{top.issue} {top.root_cause}", top_k=1)
            if hit:
                top.runbook = hit[0]["title"]
                top.evidence.append(Evidence(source="kb", summary=f"Matched runbook: {hit[0]['title']}", weight=0.05))
                top.confidence = min(97, top.confidence + 3)
        except Exception as exc:  # noqa: BLE001
            log.info("RAG lookup skipped", extra={"err": str(exc)})

    severity = _SEVERITY_BY_ISSUE.get(top.issue, Severity.medium)
    return RCAReport(
        severity=severity,
        cluster=ctx["cluster"], namespace=namespace, subject=subject,
        issue=top.issue, root_cause=top.root_cause, confidence=top.confidence,
        evidence=top.evidence, suggested_fix=top.suggested_fix,
        rollback_required=top.rollback_required, rollback_target=top.rollback_target,
        timeline=correlate.build_timeline(ctx),
        incident_summary=(
            f"[{severity.value}] {top.issue} on {subject} ({ctx['cluster']}/{namespace}). "
            f"Root cause ({top.confidence}% confidence): {top.root_cause} Suggested fix: {top.suggested_fix}"
        ),
        alternative_hypotheses=hypotheses[1:4],
    )


def register(mcp) -> None:
    @mcp.tool()
    def rca_diagnose(namespace: str, subject: str, cluster: str | None = None) -> dict:
        """Run an automated root-cause analysis for a pod or deployment.

        Collects events, logs (including the previous crashed container), restart/
        memory metrics, deployment history and change-correlation, runs explainable
        detectors, and returns a confidence-scored RCA report with severity, evidence,
        a suggested fix, whether a rollback is required, and a timeline.

        This is the recommended FIRST call for any incident — it does the heavy
        read-only context-gathering for you. Use the individual tools afterward to
        drill into anything the report flags."""
        report = diagnose(cluster, namespace, subject)
        return {"markdown": report.to_markdown(), **report.model_dump(mode="json")}
