"""Agent self-observability: structured audit logging + Prometheus metrics.

Metrics use prometheus_client when present and degrade to no-ops otherwise, so the
core install stays light and tests need no extra deps. Everything here is about the
agent observing *itself* — distinct from the tools that observe clusters.
"""
from __future__ import annotations

import logging

log = logging.getLogger("k8s_sre_agent.audit")

try:  # optional dependency
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

    _TOOL_CALLS = Counter(
        "sre_agent_tool_calls_total", "Tool invocations", ["tool", "cluster", "outcome"]
    )
    _TOOL_LATENCY = Histogram(
        "sre_agent_tool_duration_seconds", "Tool latency", ["tool"],
        buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
    )
    _AUTH = Counter("sre_agent_auth_total", "Auth decisions", ["outcome"])
    _RATELIMIT = Counter("sre_agent_ratelimited_total", "Rate-limited requests", ["principal"])
    _ENABLED = True
except Exception:  # pragma: no cover - prometheus_client absent
    _ENABLED = False
    CONTENT_TYPE_LATEST = "text/plain"


def record_tool(tool: str, cluster: str, outcome: str, seconds: float) -> None:
    """Record a tool invocation outcome + latency, and emit an audit log line."""
    if _ENABLED:
        _TOOL_CALLS.labels(tool=tool, cluster=cluster, outcome=outcome).inc()
        _TOOL_LATENCY.labels(tool=tool).observe(seconds)
    log.info(
        "tool_call",
        extra={"tool": tool, "cluster": cluster, "outcome": outcome, "duration_ms": round(seconds * 1000, 1)},
    )


def record_auth(outcome: str, subject: str = "") -> None:
    if _ENABLED:
        _AUTH.labels(outcome=outcome).inc()
    log.info("auth", extra={"outcome": outcome, "subject": subject})


def record_ratelimited(principal: str) -> None:
    if _ENABLED:
        _RATELIMIT.labels(principal=principal).inc()
    log.warning("rate_limited", extra={"principal": principal})


def metrics_payload() -> tuple[bytes, str]:
    """(body, content_type) for the /metrics endpoint."""
    if _ENABLED:
        return generate_latest(), CONTENT_TYPE_LATEST
    return b"# prometheus_client not installed\n", "text/plain"
