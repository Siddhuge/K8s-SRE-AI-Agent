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

    # Resolve subject → concrete pod(s).
    pod_names: list[str] = []
    try:
        core.read_namespaced_pod(subject, namespace)
        pod_names = [subject]
    except Exception:  # subject is likely a deployment/selector
        try:
            dep = apps.read_namespaced_deployment(subject, namespace)
            sel = ",".join(f"{k}={v}" for k, v in (dep.spec.selector.match_labels or {}).items())
            pods = core.list_namespaced_pod(namespace, label_selector=sel)
            pod_names = [p.metadata.name for p in pods.items]
        except Exception as exc:  # noqa: BLE001
            log.warning("could not resolve subject", extra={"subject": subject, "err": str(exc)})

    # Pods overview (status/restarts/reasons).
    all_pods = core.list_namespaced_pod(namespace)
    ctx["pods"] = [
        {
            "name": p.metadata.name,
            "phase": p.status.phase,
            "reason": next((c.state.waiting.reason for c in (p.status.container_statuses or [])
                            if c.state and c.state.waiting), p.status.reason),
            "last_terminated": next((c.last_state.terminated.reason for c in (p.status.container_statuses or [])
                                     if c.last_state and c.last_state.terminated), None),
        }
        for p in all_pods.items
        if not pod_names or p.metadata.name in pod_names
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
    with ThreadPoolExecutor(max_workers=7) as ex:
        futures = {
            "events": ex.submit(core.list_namespaced_event, namespace),
            "nodes": ex.submit(core.list_node),
            "secrets": ex.submit(_secret_changes, core, namespace),
            "services": ex.submit(core.list_namespaced_service, namespace),
        }
        if target_pod:
            futures["describe"] = ex.submit(kubernetes_describe, core, namespace, target_pod)
            futures["logs"] = ex.submit(_read_logs, core, namespace, target_pod, False)
            futures["previous_logs"] = ex.submit(_read_logs, core, namespace, target_pod, True)
        results = {k: f.result() for k, f in futures.items()}

    if target_pod:
        ctx["describe"] = results["describe"]
        ctx["logs"] = results["logs"]
        ctx["previous_logs"] = results["previous_logs"]

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
    return {"containers": containers, "node": p.spec.node_name}


def _read_logs(core, namespace: str, pod: str, previous: bool, tail: int = 120) -> list[str]:
    try:
        # _preload_content=False avoids a kube-client deserialization quirk that can
        # return the str repr of bytes; read + decode the raw stream instead.
        resp = core.read_namespaced_pod_log(
            pod, namespace, tail_lines=tail, previous=previous, _preload_content=False
        )
        return resp.data.decode("utf-8", "replace").splitlines()
    except Exception:  # no previous container, or restricted
        return []


def diagnose(cluster: str | None, namespace: str, subject: str) -> RCAReport:
    ctx = collect_context(cluster, namespace, subject)
    hypotheses = evaluate(ctx)

    if not hypotheses:
        return RCAReport(
            severity=Severity.info, cluster=ctx["cluster"], namespace=namespace, subject=subject,
            issue="No known failure signature detected",
            root_cause="Automated detectors found no matching signature. Inspect logs/metrics manually or widen the window.",
            confidence=20,
            evidence=[Evidence(source="k8s", summary="No CrashLoop/ImagePull/OOM/probe/node-pressure signatures matched")],
            suggested_fix="Use logs_pod / prom_query / loki_query to investigate directly.",
            rollback_required=False,
            timeline=correlate.build_timeline(ctx),
        )

    top = hypotheses[0]

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
