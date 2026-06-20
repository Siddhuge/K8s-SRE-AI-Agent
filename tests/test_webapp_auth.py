"""Tests for the dashboard SSO + the streaming chat endpoint. No real IdP: cover the
session cookie crypto, config gating, and the middleware's allow/redirect/401 behavior
with fabricated sessions; and that the SSE endpoint streams delta+done events."""
import asyncio
import sys
import time
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp import auth, server  # noqa: E402

SECRET = b"test-secret-key"


def test_session_sign_unsign_roundtrip_and_tamper():
    token = auth.sign({"sub": "alice", "exp": time.time() + 100}, SECRET)
    assert auth.unsign(token, SECRET)["sub"] == "alice"
    assert auth.unsign(token, b"wrong-secret") is None        # bad key → rejected
    assert auth.unsign(token + "x", SECRET) is None           # tampered sig → rejected


def test_session_expiry_rejected():
    expired = auth.sign({"sub": "alice", "exp": time.time() - 1}, SECRET)
    assert auth.unsign(expired, SECRET) is None


def test_config_disabled_without_client_id(monkeypatch):
    monkeypatch.delenv("DASHBOARD_OIDC_CLIENT_ID", raising=False)
    monkeypatch.setenv("OIDC_ISSUER", "https://issuer")
    assert auth.load_config().enabled is False


def _enabled_cfg():
    return auth.AuthConfig(issuer="https://issuer", client_id="dash", client_secret="s",
                           base_url="http://x", required_groups=[], secret_key=SECRET)


def _guarded_app(cfg):
    inner = Starlette(routes=[
        Route("/api/x", lambda r: JSONResponse({"ok": True})),
        Route("/", lambda r: JSONResponse({"page": True})),
        Route("/healthz", lambda r: JSONResponse({"status": "ok"})),
    ])
    return server.session_auth(inner, cfg)


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://d",
                             follow_redirects=False)


def test_middleware_blocks_and_allows():
    app = _guarded_app(_enabled_cfg())
    valid = auth.sign({"sub": "u", "name": "U", "exp": time.time() + 100}, SECRET)

    async def run():
        async with _client(app) as c:
            assert (await c.get("/healthz")).status_code == 200             # public
            assert (await c.get("/api/x")).status_code == 401               # API, no session
            r = await c.get("/")                                            # HTML, no session
            assert r.status_code == 307 and "/auth/login" in r.headers["location"]
            ok = await c.get("/api/x", headers={"Cookie": f"{auth.SESSION_COOKIE}={valid}"})
            assert ok.status_code == 200                                    # valid session
    asyncio.run(run())


def test_middleware_passthrough_when_disabled():
    app = _guarded_app(auth.AuthConfig("", "", "", "http://x", [], SECRET))  # disabled

    async def run():
        async with _client(app) as c:
            assert (await c.get("/api/x")).status_code == 200   # no auth required
    asyncio.run(run())


def test_begin_login_builds_authorize_url(monkeypatch):
    monkeypatch.setattr(auth, "discover", lambda issuer: {
        "authorization_endpoint": "https://issuer/authorize",
        "token_endpoint": "https://issuer/token", "jwks_uri": "https://issuer/jwks",
    })
    url, tx = auth.begin_login(_enabled_cfg())
    assert url.startswith("https://issuer/authorize?")
    assert "code_challenge=" in url and "client_id=dash" in url
    assert auth.unsign(tx, SECRET)["state"]                      # tx cookie carries state


def test_sse_stream_emits_delta_and_done_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),
                                     base_url="http://d") as c:
            r = await c.post("/api/chat/stream", json={"message": "hi"})
            assert r.headers["content-type"].startswith("text/event-stream")
            assert '"type": "delta"' in r.text and '"type": "done"' in r.text
    asyncio.run(run())
