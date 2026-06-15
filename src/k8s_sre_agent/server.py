"""MCP server entrypoint.

Registers every tool against a FastMCP server and exposes two transports:

  * `stdio` — launched directly by Claude Desktop / Claude Code (1 process per user).
  * `http`  — streamable-HTTP for the centralized gateway deployment, behind OIDC.

Run:  k8s-sre-agent stdio
      k8s-sre-agent http --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import argparse
import logging
import sys

try:  # python-json-logger >= 3 renamed the module
    from pythonjsonlogger.json import JsonFormatter
except ImportError:  # < 3
    from pythonjsonlogger.jsonlogger import JsonFormatter

from .config import get_settings

log = logging.getLogger("k8s_sre_agent")


def _configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stderr)  # stdout is the MCP channel in stdio mode
    handler.setFormatter(JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())


def build_server():
    """Create the FastMCP server and register all tool modules."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(
        "k8s-sre-agent",
        instructions=(
            "Read-only Kubernetes SRE agent. Use the tools to gather pods, events, logs, "
            "metrics, deployment history and incident context, then call rca_diagnose for a "
            "correlated, confidence-scored root cause analysis. You have NO mutating access: "
            "recommend fixes and rollbacks, never execute them."
        ),
    )

    # Each module exposes register(mcp). Wrap in GuardedMCP so every tool is
    # resilient (backend failures → structured errors) and metered, with no
    # per-function boilerplate.
    from .resilience import GuardedMCP
    from .tools import cicd, gitops, incidents, istio, kubernetes, logs, metrics
    from .rca import engine as rca_engine

    guarded = GuardedMCP(mcp)
    kubernetes.register(guarded)
    logs.register(guarded)
    metrics.register(guarded)
    gitops.register(guarded)
    cicd.register(guarded)
    incidents.register(guarded)
    istio.register(guarded)
    rca_engine.register(guarded)

    settings = get_settings()
    if settings.rag_enabled:
        from .rag import retrieve as rag_retrieve

        rag_retrieve.register(guarded)

    # A small read-only meta tool so Claude can discover the fleet.
    from .clusters import manager

    @guarded.tool()
    def list_clusters() -> list[dict]:
        """List clusters this agent can reach (name, tenant, provider, region, namespaces)."""
        return manager().list_clusters()

    return mcp


def readiness() -> tuple[bool, str]:
    """Readiness probe: registry parses and the default cluster's API answers a HEAD-ish
    list. Returns (ok, detail). Kept cheap — used by /readyz."""
    from .clusters import manager

    try:
        mgr = manager()
        cluster = mgr.resolve(None)
        mgr.clients(cluster.name).core_v1.get_api_resources()  # cheap auth+reachability check
        return True, f"default cluster {cluster.name} reachable"
    except Exception as exc:  # noqa: BLE001
        return False, f"default cluster not reachable: {type(exc).__name__}"


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="k8s-sre-agent")
    sub = parser.add_subparsers(dest="transport", required=True)
    sub.add_parser("stdio", help="run as an MCP stdio server")
    http = sub.add_parser("http", help="run as a streamable-HTTP MCP server")
    http.add_argument("--host", default="0.0.0.0")
    http.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    _configure_logging(settings.log_level)

    # Fail fast on misconfiguration rather than starting an insecure/broken server.
    problems = settings.validate_for(args.transport)
    if problems:
        for p in problems:
            log.error("config error", extra={"problem": p})
        raise SystemExit("refusing to start: " + "; ".join(problems))

    mcp = build_server()

    if args.transport == "stdio":
        log.info("starting MCP stdio transport")
        mcp.run(transport="stdio")
    else:
        import uvicorn

        from .middleware import build_asgi_app

        log.info("starting MCP streamable-http transport", extra={"host": args.host, "port": args.port})
        # OIDC bearer auth + rate limiting + /healthz /readyz /metrics are wired here
        # (see middleware.build_asgi_app). This is what actually enforces auth.
        app = build_asgi_app(mcp, settings, readiness=readiness)
        uvicorn.run(app, host=args.host, port=args.port, log_config=None)


if __name__ == "__main__":
    main()
