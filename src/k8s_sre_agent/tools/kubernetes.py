"""Kubernetes read-only tools.

Every function maps to a `get`/`list` verb only. Output is summarized into compact
dicts (not raw API objects) to keep Claude's context small and token cost low —
full manifests run thousands of tokens each; the agent rarely needs all of it.

Secrets are deliberately exposed as **metadata only** (name, type, keys, age) —
never values — so the agent can reason about "the db-credentials secret changed"
without ever reading credential material.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..clusters import manager


def _age(ts) -> str:
    if not ts:
        return "?"
    delta = datetime.now(timezone.utc) - ts
    secs = int(delta.total_seconds())
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def register(mcp) -> None:
    @mcp.tool()
    def k8s_get_pods(namespace: str, cluster: str | None = None, label_selector: str = "") -> list[dict]:
        """List pods in a namespace with status, restarts, readiness and node.

        Use this first when triaging — it surfaces CrashLoopBackOff, ImagePullBackOff,
        Pending, OOMKilled and restart counts at a glance.
        """
        mgr = manager()
        mgr.guard_namespace(cluster, namespace)
        pods = mgr.clients(cluster).core_v1.list_namespaced_pod(
            namespace, label_selector=label_selector or None
        )
        out = []
        for p in pods.items:
            statuses = p.status.container_statuses or []
            restarts = sum(c.restart_count for c in statuses)
            waiting = next(
                (c.state.waiting.reason for c in statuses if c.state and c.state.waiting), None
            )
            last_term = next(
                (
                    c.last_state.terminated.reason
                    for c in statuses
                    if c.last_state and c.last_state.terminated
                ),
                None,
            )
            out.append(
                {
                    "name": p.metadata.name,
                    "phase": p.status.phase,
                    "reason": waiting or p.status.reason,
                    "last_terminated": last_term,  # e.g. OOMKilled
                    "restarts": restarts,
                    "ready": f"{sum(1 for c in statuses if c.ready)}/{len(statuses)}",
                    "node": p.spec.node_name,
                    "age": _age(p.metadata.creation_timestamp),
                }
            )
        return out

    @mcp.tool()
    def k8s_describe_pod(namespace: str, name: str, cluster: str | None = None) -> dict:
        """Detailed pod view: containers, images, probes, resource limits, conditions
        and per-container state (waiting/terminated reasons + exit codes)."""
        mgr = manager()
        mgr.guard_namespace(cluster, namespace)
        p = mgr.clients(cluster).core_v1.read_namespaced_pod(name, namespace)
        containers = []
        statuses = {c.name: c for c in (p.status.container_statuses or [])}
        for c in p.spec.containers:
            st = statuses.get(c.name)
            term = st.last_state.terminated if st and st.last_state else None
            containers.append(
                {
                    "name": c.name,
                    "image": c.image,
                    "requests": (c.resources.requests if c.resources else None),
                    "limits": (c.resources.limits if c.resources else None),
                    "liveness": bool(c.liveness_probe),
                    "readiness": bool(c.readiness_probe),
                    "restart_count": st.restart_count if st else 0,
                    "waiting_reason": (st.state.waiting.reason if st and st.state and st.state.waiting else None),
                    "last_exit_code": (term.exit_code if term else None),
                    "last_terminated_reason": (term.reason if term else None),
                }
            )
        return {
            "name": p.metadata.name,
            "phase": p.status.phase,
            "node": p.spec.node_name,
            "service_account": p.spec.service_account_name,
            "conditions": [
                {"type": c.type, "status": c.status, "reason": c.reason} for c in (p.status.conditions or [])
            ],
            "containers": containers,
            "volumes": [v.name for v in (p.spec.volumes or [])],
        }

    @mcp.tool()
    def k8s_get_events(namespace: str, cluster: str | None = None, involved_object: str = "") -> list[dict]:
        """Recent events in a namespace (Warnings first), optionally filtered to one
        object. Events carry the ground truth: FailedScheduling, BackOff, Unhealthy
        probe failures, FailedMount, image pull errors, etc."""
        mgr = manager()
        mgr.guard_namespace(cluster, namespace)
        field = f"involvedObject.name={involved_object}" if involved_object else None
        ev = mgr.clients(cluster).core_v1.list_namespaced_event(namespace, field_selector=field)
        items = sorted(
            ev.items,
            key=lambda e: (e.type != "Warning", e.last_timestamp or e.event_time or datetime.min.replace(tzinfo=timezone.utc)),
        )
        return [
            {
                "type": e.type,
                "reason": e.reason,
                "object": f"{e.involved_object.kind}/{e.involved_object.name}",
                "message": e.message,
                "count": e.count,
                "age": _age(e.last_timestamp or e.event_time),
            }
            for e in items[:50]
        ]

    @mcp.tool()
    def k8s_get_deployments(namespace: str, cluster: str | None = None) -> list[dict]:
        """Deployments with replica health and the current rollout condition."""
        mgr = manager()
        mgr.guard_namespace(cluster, namespace)
        deps = mgr.clients(cluster).apps_v1.list_namespaced_deployment(namespace)
        return [
            {
                "name": d.metadata.name,
                "ready": f"{d.status.ready_replicas or 0}/{d.spec.replicas}",
                "updated": d.status.updated_replicas or 0,
                "available": d.status.available_replicas or 0,
                "image": d.spec.template.spec.containers[0].image if d.spec.template.spec.containers else None,
                "generation": d.metadata.generation,
                "conditions": [
                    {"type": c.type, "status": c.status, "reason": c.reason}
                    for c in (d.status.conditions or [])
                ],
            }
            for d in deps.items
        ]

    @mcp.tool()
    def k8s_get_daemonsets(namespace: str, cluster: str | None = None) -> list[dict]:
        """DaemonSets with desired/ready/available counts (CNI, log shippers, etc.)."""
        mgr = manager()
        mgr.guard_namespace(cluster, namespace)
        ds = mgr.clients(cluster).apps_v1.list_namespaced_daemon_set(namespace)
        return [
            {
                "name": d.metadata.name,
                "desired": d.status.desired_number_scheduled,
                "ready": d.status.number_ready,
                "available": d.status.number_available,
                "misscheduled": d.status.number_misscheduled,
            }
            for d in ds.items
        ]

    @mcp.tool()
    def k8s_get_nodes(cluster: str | None = None) -> list[dict]:
        """Nodes with Ready status and pressure conditions (Disk/Memory/PID pressure,
        NetworkUnavailable). Cluster-scoped — no namespace guard applies."""
        nodes = manager().clients(cluster).core_v1.list_node()
        out = []
        for n in nodes.items:
            conds = {c.type: c.status for c in (n.status.conditions or [])}
            out.append(
                {
                    "name": n.metadata.name,
                    "ready": conds.get("Ready"),
                    "disk_pressure": conds.get("DiskPressure"),
                    "memory_pressure": conds.get("MemoryPressure"),
                    "pid_pressure": conds.get("PIDPressure"),
                    "network_unavailable": conds.get("NetworkUnavailable"),
                    "unschedulable": bool(n.spec.unschedulable),
                    "kubelet": n.status.node_info.kubelet_version if n.status.node_info else None,
                    "allocatable": n.status.allocatable,
                }
            )
        return out

    @mcp.tool()
    def k8s_get_services(namespace: str, cluster: str | None = None) -> list[dict]:
        """Services with type, clusterIP, ports, selector and (for LoadBalancers)
        external ingress status."""
        mgr = manager()
        mgr.guard_namespace(cluster, namespace)
        svcs = mgr.clients(cluster).core_v1.list_namespaced_service(namespace)
        return [
            {
                "name": s.metadata.name,
                "type": s.spec.type,
                "cluster_ip": s.spec.cluster_ip,
                "ports": [f"{p.port}/{p.protocol}->{p.target_port}" for p in (s.spec.ports or [])],
                "selector": s.spec.selector,
                "lb_ingress": [i.ip or i.hostname for i in (s.status.load_balancer.ingress or [])]
                if s.status.load_balancer else [],
            }
            for s in svcs.items
        ]

    @mcp.tool()
    def k8s_get_ingress(namespace: str, cluster: str | None = None) -> list[dict]:
        """Ingress objects: hosts, paths, backing services, TLS secrets and the
        controller-reported address."""
        mgr = manager()
        mgr.guard_namespace(cluster, namespace)
        ings = mgr.clients(cluster).networking_v1.list_namespaced_ingress(namespace)
        out = []
        for i in ings.items:
            rules = []
            for r in (i.spec.rules or []):
                for p in (r.http.paths if r.http else []):
                    rules.append({
                        "host": r.host,
                        "path": p.path,
                        "service": f"{p.backend.service.name}:{p.backend.service.port.number}"
                        if p.backend.service else None,
                    })
            out.append({
                "name": i.metadata.name,
                "class": i.spec.ingress_class_name,
                "rules": rules,
                "tls": [{"hosts": t.hosts, "secret": t.secret_name} for t in (i.spec.tls or [])],
                "address": [a.ip or a.hostname for a in (i.status.load_balancer.ingress or [])]
                if i.status.load_balancer else [],
            })
        return out

    @mcp.tool()
    def k8s_get_configmaps(namespace: str, cluster: str | None = None) -> list[dict]:
        """ConfigMaps with their keys and last-modified age (values truncated)."""
        mgr = manager()
        mgr.guard_namespace(cluster, namespace)
        cms = mgr.clients(cluster).core_v1.list_namespaced_config_map(namespace)
        return [
            {
                "name": cm.metadata.name,
                "keys": list((cm.data or {}).keys()),
                "age": _age(cm.metadata.creation_timestamp),
                "resource_version": cm.metadata.resource_version,
            }
            for cm in cms.items
        ]

    @mcp.tool()
    def k8s_get_secrets_metadata(namespace: str, cluster: str | None = None) -> list[dict]:
        """Secret METADATA ONLY — name, type, key names, age. Never returns values.

        Critical for RCA ("the db-credentials secret rotated 5m before the crash")
        without ever exposing credential material."""
        mgr = manager()
        mgr.guard_namespace(cluster, namespace)
        secrets = mgr.clients(cluster).core_v1.list_namespaced_secret(namespace)
        return [
            {
                "name": s.metadata.name,
                "type": s.type,
                "keys": list((s.data or {}).keys()),  # key names only, NOT values
                "age": _age(s.metadata.creation_timestamp),
                "resource_version": s.metadata.resource_version,
            }
            for s in secrets.items
        ]
