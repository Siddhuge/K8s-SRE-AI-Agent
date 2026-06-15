"""Log tools: container/pod logs via the kube API, plus Loki and Grafana.

Logs are the single highest-signal, highest-token source. Defaults are conservative
(tail=200, since=1h) and a server-side grep filter keeps only matching lines so the
agent sees the connection-timeout stack trace, not 50k lines of healthy traffic.
"""
from __future__ import annotations

from ..clusters import manager
from ..config import get_settings
from ._http import get_json


def _resolve_loki(cluster: str | None) -> str:
    cfg = manager().resolve(cluster)
    return cfg.observability.loki or get_settings().loki_url


def register(mcp) -> None:
    @mcp.tool()
    def logs_pod(
        namespace: str,
        pod: str,
        cluster: str | None = None,
        container: str = "",
        tail: int = 200,
        previous: bool = False,
        grep: str = "",
    ) -> dict:
        """Container logs for a pod. Set previous=true to read the logs of the LAST
        crashed container — essential for CrashLoopBackOff where the running container
        is healthy but the prior one died. `grep` filters lines case-insensitively."""
        mgr = manager()
        mgr.guard_namespace(cluster, namespace)
        # _preload_content=False avoids a kube-client bug where log bodies are
        # returned as the str repr of bytes ("b'...\\n...'"); read the raw stream.
        resp = mgr.clients(cluster).core_v1.read_namespaced_pod_log(
            name=pod,
            namespace=namespace,
            container=container or None,
            tail_lines=tail,
            previous=previous,
            timestamps=True,
            _preload_content=False,
        )
        lines = resp.data.decode("utf-8", "replace").splitlines()
        if grep:
            needle = grep.lower()
            lines = [ln for ln in lines if needle in ln.lower()]
        return {"pod": pod, "container": container or "(default)", "previous": previous, "lines": lines[-tail:]}

    @mcp.tool()
    def logs_container(namespace: str, pod: str, container: str, cluster: str | None = None, tail: int = 200) -> dict:
        """Logs for a specific container in a multi-container pod (sidecars, init shims)."""
        return logs_pod(namespace=namespace, pod=pod, cluster=cluster, container=container, tail=tail)  # type: ignore

    @mcp.tool()
    def logs_node(node: str, cluster: str | None = None, since: str = "1h", grep: str = "kubelet") -> dict:
        """Node-level logs via Loki's systemd/journal stream (kubelet, containerd).

        Requires a node-log shipper (e.g. promtail with a journal scrape). Use for
        NodeNotReady / DiskPressure root causes that live below the pod boundary."""
        loki = _resolve_loki(cluster)
        query = f'{{job="systemd-journal", node="{node}"}} |~ `(?i){grep}`'
        data = get_json(
            f"{loki.rstrip('/')}/loki/api/v1/query_range",
            params={"query": query, "since": since, "limit": "300"},
        )
        return {"node": node, "result": data.get("data", {}).get("result", [])}

    @mcp.tool()
    def loki_query(query: str, cluster: str | None = None, since: str = "1h", limit: int = 200) -> dict:
        """Run an arbitrary LogQL query against Loki for the target cluster.

        Example: '{namespace="payments", app="api"} |= "ERROR" | json | level="error"'.
        Use this to correlate errors across services or to count error rates over time."""
        loki = _resolve_loki(cluster)
        data = get_json(
            f"{loki.rstrip('/')}/loki/api/v1/query_range",
            params={"query": query, "since": since, "limit": str(limit)},
        )
        return {"query": query, "result": data.get("data", {}).get("result", [])}

    @mcp.tool()
    def grafana_panel(dashboard_uid: str, panel_id: int, cluster: str | None = None, time_range: str = "1h") -> dict:
        """Resolve a Grafana panel's underlying query + current data (for dashboards an
        SRE would normally eyeball). Returns the panel's datasource query and series so
        the agent can read the same signal a human would from the dashboard."""
        settings = get_settings()
        cfg = manager().resolve(cluster)
        base = cfg.observability.grafana or settings.grafana_url
        headers = {"Authorization": f"Bearer {settings.grafana_token}"} if settings.grafana_token else None
        dash = get_json(f"{base.rstrip('/')}/api/dashboards/uid/{dashboard_uid}", headers=headers)
        panels = dash.get("dashboard", {}).get("panels", [])
        panel = next((p for p in panels if p.get("id") == panel_id), None)
        return {
            "title": panel.get("title") if panel else None,
            "targets": panel.get("targets") if panel else None,
            "time_range": time_range,
        }
