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
from .ratelimit import TokenBucketLimiter

log = logging.getLogger("k8s_sre_agent.http")

_PUBLIC_PATHS = {"/healthz", "/readyz", "/metrics"}


def build_asgi_app(mcp, settings: Settings, *, readiness):
    """Return a Starlette app: health/metrics routes + auth + rate-limit around MCP."""
    from starlette.applications import Starlette
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse, PlainTextResponse, Response
    from starlette.routing import Route

    limiter = TokenBucketLimiter(rate=settings_rate(settings), burst=settings_burst(settings))

    async def healthz(_req: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def readyz(_req: Request) -> Response:
        ok, detail = readiness()
        return JSONResponse({"status": "ready" if ok else "not_ready", "detail": detail},
                            status_code=200 if ok else 503)

    async def metrics(_req: Request) -> Response:
        body, ctype = metrics_payload()
        return PlainTextResponse(body, media_type=ctype)

    class AuthRateLimitMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if request.url.path in _PUBLIC_PATHS:
                return await call_next(request)

            auth_header = request.headers.get("authorization", "")
            if not auth_header.lower().startswith("bearer "):
                record_auth("missing_token")
                return JSONResponse({"error": "missing bearer token"}, status_code=401)
            token = auth_header.split(" ", 1)[1]
            try:
                principal = verify_bearer_token(token, settings)
            except AuthError as exc:
                record_auth("rejected")
                return JSONResponse({"error": str(exc)}, status_code=401)
            except Exception:
                # Malformed token, JWKS fetch failure, signature error, etc. — reject
                # as 401 rather than leaking a 500 / internal error to the caller.
                record_auth("error")
                return JSONResponse({"error": "token validation failed"}, status_code=401)

            record_auth("ok", principal.subject)
            request.state.principal = principal

            if not limiter.allow(principal.subject):
                record_ratelimited(principal.subject)
                return JSONResponse({"error": "rate limit exceeded"}, status_code=429,
                                    headers={"Retry-After": "1"})
            return await call_next(request)

    inner = mcp.streamable_http_app()  # FastMCP's Starlette ASGI app
    app = Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/readyz", readyz),
            Route("/metrics", metrics),
        ]
    )
    app.mount("/", inner)
    app.add_middleware(AuthRateLimitMiddleware)
    return app


def settings_rate(settings: Settings) -> float:
    return float(getattr(settings, "ratelimit_rate", 5.0))


def settings_burst(settings: Settings) -> int:
    return int(getattr(settings, "ratelimit_burst", 30))
