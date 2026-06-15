"""Tool resilience: turn upstream failures into structured, model-readable errors.

A diagnostic agent must never crash a whole investigation because one backend is
misconfigured, down, or returns an unexpected shape. `@tool_guard` wraps a tool so
that any exception becomes a structured ``{"error": ...}`` result the model can read
and route around ("Prometheus is unreachable — continuing with logs"), and records
the outcome for metrics/audit.

It classifies common failure modes (auth, not-found, timeout, tenant isolation) so
the model gets an actionable hint rather than a raw stack trace, and so we never leak
internal detail (tracebacks, hosts) into the model context.
"""
from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger("k8s_sre_agent.tool")


class ToolError(Exception):
    """Raised by tools for an expected, user-facing failure (kind drives the hint)."""

    def __init__(self, message: str, *, kind: str = "error"):
        super().__init__(message)
        self.kind = kind


def _classify(exc: Exception) -> tuple[str, str]:
    """Map an exception to (kind, safe_message). Never echoes internal detail."""
    name = type(exc).__name__
    text = str(exc)

    # Tenant isolation is a security decision — surface it explicitly.
    if name == "TenantIsolationError":
        return "forbidden", text
    if isinstance(exc, ToolError):
        return exc.kind, text

    # kubernetes.client.ApiException carries an HTTP status.
    status = getattr(exc, "status", None)
    if status == 401:
        return "unauthenticated", "credentials rejected by the upstream API"
    if status == 403:
        return "forbidden", "read access denied by RBAC for this resource"
    if status == 404:
        return "not_found", "resource not found"
    if status and 500 <= int(status) < 600:
        return "upstream_error", f"upstream returned {status}"

    # httpx / network classes.
    if "Timeout" in name:
        return "timeout", "upstream timed out"
    if "ConnectError" in name or "ConnectionError" in name or "TransportError" in name:
        return "unreachable", "upstream unreachable (check endpoint/network policy)"
    if "HTTPStatusError" in name:
        # httpx carries the status on .response.status_code (not .status like the
        # kube client) — classify HTTP-backend errors (GitLab/GitHub/Prom/Loki/ArgoCD)
        # specifically so the model gets an actionable hint, not a generic failure.
        code = getattr(getattr(exc, "response", None), "status_code", None)
        if code == 401:
            return "unauthenticated", "upstream rejected the credentials (401)"
        if code == 403:
            return "forbidden", "access denied by the upstream (403) — check the token's scope/permissions"
        if code == 404:
            return "not_found", "resource not found (404)"
        if code and 500 <= int(code) < 600:
            return "upstream_error", f"upstream returned {code}"
        return "upstream_error", f"upstream returned HTTP {code}"

    return "error", "unexpected tool failure"


class GuardedMCP:
    """Proxy over a FastMCP instance that applies `tool_guard` to every registered
    tool automatically. `functools.wraps` keeps the original signature, so FastMCP
    still derives the correct JSON schema from each tool's parameters."""

    def __init__(self, mcp: Any):
        self._mcp = mcp

    def tool(self, *args: Any, **kwargs: Any):
        inner = self._mcp.tool(*args, **kwargs)

        def deco(fn: Callable[..., Any]):
            return inner(tool_guard(fn))

        return deco

    def __getattr__(self, name: str) -> Any:  # passthrough custom_route, settings, run, ...
        return getattr(self._mcp, name)


def tool_guard(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator: never raise out of a tool; return a structured error + record timing."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        from . import observability  # late import to avoid cycles

        start = time.monotonic()
        cluster = kwargs.get("cluster") or "default"
        try:
            result = fn(*args, **kwargs)
            observability.record_tool(fn.__name__, cluster, "ok", time.monotonic() - start)
            return result
        except Exception as exc:  # noqa: BLE001 — deliberate boundary
            kind, safe = _classify(exc)
            observability.record_tool(fn.__name__, cluster, kind, time.monotonic() - start)
            log.warning(
                "tool failed",
                extra={"tool": fn.__name__, "cluster": cluster, "kind": kind, "detail": str(exc)},
            )
            return {"error": kind, "message": safe, "tool": fn.__name__}

    return wrapper
