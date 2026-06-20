"""ASGI middleware + operational endpoints for the streamable-HTTP transport.

Wraps the FastMCP Starlette app with, in order:
  1. health/metrics routes (unauthenticated: /healthz, /readyz, /metrics),
  2. OIDC/Entra bearer auth (every other path) — this CLOSES the gap where
     `verify_bearer_token` existed but was never invoked,
  3. per-principal token-bucket rate limiting,
  4. audit logging of the decision.

The pure logic (token verification, rate limiting, metrics) lives in unit-tested
modules; this file is the thin wiring. stdio transport doesn't use any of this —
there the OS user + their kubeconfig is the boundary.
"""
from __future__ import annotations

import logging

from .auth import AuthError, verify_bearer_token
from .config import Settings
from .observability import metrics_payload, record_auth, record_ratelimited
from .ratelimit import build_limiter

log = logging.getLogger("k8s_sre_agent.http")

_PUBLIC_PATHS = {"/healthz", "/readyz", "/metrics"}


def build_asgi_app(mcp, settings: Settings, *, readiness):
    """Return a Starlette app: health/metrics routes + auth + rate-limit around MCP."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, PlainTextResponse, Response
    from starlette.routing import Route

    limiter = build_limiter(settings)  # in-memory, or Redis-backed if RATELIMIT_REDIS_URL set

    async def healthz(_req: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def readyz(_req: Request) -> Response:
        ok, detail = readiness()
        return JSONResponse({"status": "ready" if ok else "not_ready", "detail": detail},
                            status_code=200 if ok else 503)

    async def metrics(_req: Request) -> Response:
        body, ctype = metrics_payload()
        return PlainTextResponse(body, media_type=ctype)

    inner = mcp.streamable_http_app()  # FastMCP's Starlette ASGI app
    base = Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/readyz", readyz),
            Route("/metrics", metrics),
        ]
    )
    base.mount("/", inner)

    # PURE-ASGI middleware (not Starlette BaseHTTPMiddleware, which serializes through an
    # anyio stream bridge and collapses throughput under concurrency — see loadtest/).
    # This is a thin async wrapper: inspect scope, reject early, or pass through.
    async def _send_json(scope, receive, send, status: int, body: dict, headers=None):
        await JSONResponse(body, status_code=status, headers=headers)(scope, receive, send)

    async def gateway(scope, receive, send):
        if scope["type"] != "http" or scope.get("path", "") in _PUBLIC_PATHS:
            await base(scope, receive, send)
            return

        auth = b""
        for key, value in scope.get("headers") or ():
            if key.lower() == b"authorization":
                auth = value
                break
        if auth[:7].lower() != b"bearer ":
            record_auth("missing_token")
            await _send_json(scope, receive, send, 401, {"error": "missing bearer token"})
            return
        token = auth[7:].decode("latin-1").strip()
        try:
            principal = verify_bearer_token(token, settings)
        except AuthError as exc:
            record_auth("rejected")
            await _send_json(scope, receive, send, 401, {"error": str(exc)})
            return
        except Exception:
            # Malformed token, JWKS fetch failure, signature error, etc. — reject as 401
            # rather than leaking a 500 / internal error to the caller.
            record_auth("error")
            await _send_json(scope, receive, send, 401, {"error": "token validation failed"})
            return

        record_auth("ok", principal.subject)

        if not limiter.allow(principal.subject):
            record_ratelimited(principal.subject)
            await _send_json(scope, receive, send, 429, {"error": "rate limit exceeded"},
                             headers={"Retry-After": "1"})
            return
        await base(scope, receive, send)

    return gateway


def settings_rate(settings: Settings) -> float:
    return float(getattr(settings, "ratelimit_rate", 5.0))


def settings_burst(settings: Settings) -> int:
    return int(getattr(settings, "ratelimit_burst", 30))
