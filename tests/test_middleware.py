"""In-process tests of the gateway ASGI app: auth, public-path bypass, and the
rate-limiter end-to-end through the real middleware stack (httpx ASGITransport, no
network, no real OIDC). This covers what the load test could not — driving the
per-principal rate limiter past its burst to a 429."""
from __future__ import annotations

import asyncio

import httpx

from k8s_sre_agent.auth import AuthError, Principal
from k8s_sre_agent.config import Settings


def _app(monkeypatch, *, burst=3, rate=0.0):
    """Build the real gateway app with a fake MCP inner app + mocked token verify."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    import k8s_sre_agent.middleware as mw

    async def _inner_ok(_req):
        return JSONResponse({"ok": True})

    class FakeMCP:
        def streamable_http_app(self):
            return Starlette(routes=[Route("/{path:path}", _inner_ok, methods=["GET", "POST"])])

    def fake_verify(token, settings):
        if token == "good":
            return Principal(subject="alice", groups=["sre-readonly"])
        raise AuthError("bad token")

    monkeypatch.setattr(mw, "verify_bearer_token", fake_verify)
    settings = Settings(oidc_issuer="https://issuer", ratelimit_rate=rate, ratelimit_burst=burst, _env_file=None)
    return mw.build_asgi_app(FakeMCP(), settings, readiness=lambda: (True, "ok"))


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")


def test_public_paths_bypass_auth(monkeypatch):
    app = _app(monkeypatch)

    async def run():
        async with _client(app) as c:
            assert (await c.get("/healthz")).status_code == 200
            r = await c.get("/readyz")
            assert r.status_code == 200 and r.json()["status"] == "ready"
            assert (await c.get("/metrics")).status_code == 200

    asyncio.run(run())


def test_missing_and_bad_tokens_are_401(monkeypatch):
    app = _app(monkeypatch)

    async def run():
        async with _client(app) as c:
            assert (await c.post("/mcp")).status_code == 401                       # no header
            r = await c.post("/mcp", headers={"Authorization": "Bearer nope"})
            assert r.status_code == 401                                            # verify raises -> 401

    asyncio.run(run())


def test_valid_token_passes_then_rate_limits(monkeypatch):
    # burst=3, rate=0 → 4th authed request from the same principal gets 429.
    app = _app(monkeypatch, burst=3, rate=0.0)

    async def run():
        async with _client(app) as c:
            h = {"Authorization": "Bearer good"}
            codes = [(await c.post("/mcp", headers=h)).status_code for _ in range(5)]
            assert codes[:3] == [200, 200, 200], codes
            assert codes[3] == 429 and codes[4] == 429, codes
            r = await c.post("/mcp", headers=h)
            assert r.headers.get("Retry-After") == "1"

    asyncio.run(run())
