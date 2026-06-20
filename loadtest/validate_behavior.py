#!/usr/bin/env python3
"""Validate gateway BEHAVIOR under concurrent load (not just throughput):

  1. the auth boundary holds — no token → 401, every time, under a concurrent burst;
  2. the per-principal token-bucket rate limiter trips — a burst from one principal is
     allowed up to `burst` then 429'd;
  3. limits are per-principal — a second principal is unaffected by the first's burst.

Runs the REAL middleware (`build_asgi_app`) in-process against a trivial inner app, with
`verify_bearer_token` patched so we don't need a live OIDC issuer. Deterministic, fast,
and CI-friendly. Exits non-zero on any violation.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from collections import Counter
from types import SimpleNamespace

import httpx

logging.disable(logging.CRITICAL)  # quiet the per-request audit logs during the burst

from k8s_sre_agent import middleware
from k8s_sre_agent.config import Settings

BURST = 20
RATE = 5.0


def _build_app():
    """Real auth+ratelimit middleware wrapped around a trivial 200-returning inner app."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def ok(_req):
        return JSONResponse({"ok": True})

    fake_mcp = SimpleNamespace(
        streamable_http_app=lambda: Starlette(routes=[Route("/{path:path}", ok, methods=["GET", "POST"])])
    )
    settings = Settings(
        oidc_issuer="https://loadtest.invalid/", oidc_audience="api://loadtest",
        ratelimit_rate=RATE, ratelimit_burst=BURST,
    )
    # Patch the token verification seam: accept any token, principal = the token string.
    middleware.verify_bearer_token = lambda token, _s: SimpleNamespace(subject=token, groups=[])
    return middleware.build_asgi_app(fake_mcp, settings, readiness=lambda: (True, "ok"))


async def _fire(client, n, headers):
    async def one():
        r = await client.post("/mcp/", headers=headers)
        return r.status_code
    return Counter(await asyncio.gather(*[one() for _ in range(n)]))


async def _run() -> list[str]:
    app = _build_app()
    failures: list[str] = []
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        # 1. Auth boundary: 64 concurrent requests, no token → all 401.
        no_auth = await _fire(client, 64, headers={})
        if set(no_auth) != {401}:
            failures.append(f"auth boundary: expected all 401, got {dict(no_auth)}")

        # 2. Rate limit: principal 'p1' bursts BURST+30 requests → ~BURST allowed, rest 429.
        p1 = await _fire(client, BURST + 30, headers={"Authorization": "Bearer p1"})
        allowed = sum(v for k, v in p1.items() if k != 429)
        limited = p1.get(429, 0)
        if limited == 0:
            failures.append(f"rate limiter never tripped: {dict(p1)}")
        if not (BURST - 2 <= allowed <= BURST + 2):
            failures.append(f"allowed {allowed} not ~= burst {BURST}: {dict(p1)}")

        # 3. Per-principal isolation: a fresh principal 'p2' is unaffected by p1's burst.
        p2 = await _fire(client, 1, headers={"Authorization": "Bearer p2"})
        if 429 in p2:
            failures.append(f"per-principal isolation broken: p2 got 429 {dict(p2)}")

        print("  auth-boundary (no token x64) :", dict(no_auth))
        print(f"  p1 burst ({BURST + 30} reqs)        :", dict(p1), f"→ allowed≈{allowed}, 429={limited}")
        print("  p2 single (isolation)        :", dict(p2))
    return failures


def main() -> int:
    print("=== Gateway behavior under load ===")
    failures = asyncio.run(_run())
    print("\n--- Gates ---")
    if failures:
        for f in failures:
            print(f"  [XX] {f}")
        return 1
    print("  [OK] auth boundary holds (401)")
    print("  [OK] rate limiter trips at burst (429)")
    print("  [OK] limits are per-principal")
    return 0


if __name__ == "__main__":
    sys.exit(main())
