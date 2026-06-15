"""Metrics tools — Prometheus instant + range queries, plus typed convenience
wrappers (CPU/memory/disk/network/restarts) that emit ready-made PromQL so the
agent doesn't have to remember kube-state-metrics / cAdvisor metric names."""
from __future__ import annotations

import time

from ..clusters import manager
from ..config import get_settings
from ._http import get_json

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _prom(cluster: str | None) -> str:
    cfg = manager().resolve(cluster)
    return cfg.observability.prometheus or get_settings().prometheus_url


def parse_duration(rng: str) -> int:
    """'30m' -> 1800 seconds. Defaults the unit to minutes if unspecified."""
    rng = rng.strip()
    unit = _UNITS.get(rng[-1], 60)
    num = rng[:-1] if rng[-1] in _UNITS else rng
    return int(num) * unit


def _query(cluster: str | None, promql: str, rng: str | None = None) -> dict:
    base = _prom(cluster).rstrip("/")
    if rng:
        # Prometheus query_range needs real unix timestamps for start/end + a step
        # (NOT relative strings like "now-1h" — that's Grafana/Loki syntax and 400s).
        end = int(time.time())
        start = end - parse_duration(rng)
        step = max(15, parse_duration(rng) // 200)  # keep the series to ~200 points
        data = get_json(
            f"{base}/api/v1/query_range",
            params={"query": promql, "start": start, "end": end, "step": f"{step}s"},
        )
    else:
        data = get_json(f"{base}/api/v1/query", params={"query": promql})
    return {"query": promql, "result": data.get("data", {}).get("result", [])}


def register(mcp) -> None:
    @mcp.tool()
    def prom_query(promql: str, cluster: str | None = None, range: str = "") -> dict:
        """Run an arbitrary PromQL query. Pass `range` (e.g. '30m') for a range query,
        otherwise it's an instant query."""
        return _query(cluster, promql, range or None)

    @mcp.tool()
    def metric_cpu(namespace: str, pod: str = "", cluster: str | None = None, range: str = "30m") -> dict:
        """CPU usage (cores) vs limit for a namespace or specific pod. Reveals
        throttling and runaway loops."""
        manager().guard_namespace(cluster, namespace)
        sel = f'namespace="{namespace}"' + (f',pod="{pod}"' if pod else "")
        return _query(cluster, f"sum by (pod) (rate(container_cpu_usage_seconds_total{{{sel}}}[5m]))", range)

    @mcp.tool()
    def metric_memory(namespace: str, pod: str = "", cluster: str | None = None, range: str = "30m") -> dict:
        """Working-set memory vs limit. Pair with OOMKilled events to confirm the pod
        hit its memory limit."""
        manager().guard_namespace(cluster, namespace)
        sel = f'namespace="{namespace}"' + (f',pod="{pod}"' if pod else "")
        return _query(cluster, f"sum by (pod) (container_memory_working_set_bytes{{{sel}}})", range)

    @mcp.tool()
    def metric_disk(node: str = "", cluster: str | None = None, range: str = "30m") -> dict:
        """Worst real-filesystem usage percentage per node — root cause for
        DiskPressure / evictions. Reports the most-full ext4/xfs filesystem per node
        rather than hard-coding mountpoint="/", because node-exporter labels the root
        differently across distros (/, /host/root, /rootfs, /var, ...)."""
        sel = f',instance=~"{node}.*"' if node else ""
        return _query(
            cluster,
            f'max by (instance) (100 - (node_filesystem_avail_bytes{{fstype=~"ext4|xfs"{sel}}} '
            f'/ node_filesystem_size_bytes{{fstype=~"ext4|xfs"{sel}}} * 100))',
            range,
        )

    @mcp.tool()
    def metric_network(namespace: str, pod: str = "", cluster: str | None = None, range: str = "30m") -> dict:
        """Pod network receive/transmit bytes and dropped packets."""
        manager().guard_namespace(cluster, namespace)
        sel = f'namespace="{namespace}"' + (f',pod="{pod}"' if pod else "")
        return _query(cluster, f"sum by (pod) (rate(container_network_receive_bytes_total{{{sel}}}[5m]))", range)

    @mcp.tool()
    def metric_restarts(namespace: str, cluster: str | None = None, range: str = "1h") -> dict:
        """Container restart rate per pod from kube-state-metrics — confirms a restart
        loop and shows when it started (correlate with deploys/secret rotations)."""
        manager().guard_namespace(cluster, namespace)
        return _query(
            cluster,
            f'sum by (pod) (increase(kube_pod_container_status_restarts_total{{namespace="{namespace}"}}[15m]))',
            range,
        )
