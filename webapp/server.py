"""v2 dashboard server (Starlette): cluster overview + chat (SSE streaming) + browser SSO.

Run:  PYTHONPATH=src:. python3 -m webapp.server          # http://127.0.0.1:8081
Read-only: every API path goes through the agent's read tools. SSO is OFF unless
DASHBOARD_OIDC_CLIENT_ID is set (see webapp/auth.py).
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import auth, chat
from .agent_tools import tool_catalog

_STATIC = Path(__file__).parent / "static"
_REACH_TIMEOUT = float(os.environ.get("DASHBOARD_REACH_TIMEOUT", "3.0"))
_AUTH = auth.load_config()
_PUBLIC_PATHS = {"/healthz", "/auth/login", "/auth/callback", "/auth/logout"}


# ---------------- read API ----------------

def _reachable(cluster_name: str) -> tuple[bool, str]:
    from k8s_sre_agent.clusters import manager

    def probe():
        manager().clients(cluster_name).core_v1.get_api_resources()
        return True

    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            ex.submit(probe).result(timeout=_REACH_TIMEOUT)
        return True, "reachable"
    except FuturesTimeout:
        return False, "timeout"
    except Exception as exc:  # noqa: BLE001
        return False, type(exc).__name__


async def api_clusters(_req: Request) -> JSONResponse:
    from k8s_sre_agent.clusters import manager

    try:
        clusters = manager().list_clusters()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"clusters": [], "error": f"{type(exc).__name__}: {exc}"})
    out = [{**c, "reachable": (r := _reachable(c["name"]))[0], "status_detail": r[1]} for c in clusters]
    return JSONResponse({"clusters": out, "chat_enabled": chat.chat_available()})


async def api_tools(_req: Request) -> JSONResponse:
    return JSONResponse({"tools": tool_catalog()})


async def api_me(req: Request) -> JSONResponse:
    user = req.scope.get("state", {}).get("user")
    return JSONResponse({"auth_enabled": _AUTH.enabled, "user": user})


async def api_chat(req: Request) -> JSONResponse:
    body = await req.json()
    message = (body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "empty message"}, status_code=400)
    return JSONResponse(chat.run_chat(message, body.get("history") or []))


async def api_chat_stream(req: Request) -> StreamingResponse:
    body = await req.json()
    message = (body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "empty message"}, status_code=400)
    return StreamingResponse(
        chat.stream_chat(message, body.get("history") or []),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def healthz(_req: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


# ---------------- SSO routes ----------------

async def auth_login(_req: Request):
    if not _AUTH.enabled:
        return RedirectResponse("/")
    url, tx = auth.begin_login(_AUTH)
    resp = RedirectResponse(url)
    resp.set_cookie(auth.TX_COOKIE, tx, max_age=600, httponly=True, samesite="lax",
                    secure=_AUTH.base_url.startswith("https"))
    return resp


async def auth_callback(req: Request):
    try:
        session = auth.complete_login(
            _AUTH, req.query_params["code"], req.query_params.get("state", ""),
            req.cookies.get(auth.TX_COOKIE),
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"login failed: {type(exc).__name__}: {exc}"}, status_code=403)
    resp = RedirectResponse("/")
    resp.set_cookie(auth.SESSION_COOKIE, auth.sign(session, _AUTH.secret_key),
                    max_age=auth.SESSION_TTL, httponly=True, samesite="lax",
                    secure=_AUTH.base_url.startswith("https"))
    resp.delete_cookie(auth.TX_COOKIE)
    return resp


async def auth_logout(_req: Request):
    resp = RedirectResponse("/")
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


# ---------------- SSO middleware (pure ASGI) ----------------

def _cookies(scope) -> dict[str, str]:
    for key, value in scope.get("headers") or ():
        if key == b"cookie":
            return {c.split("=", 1)[0].strip(): c.split("=", 1)[1]
                    for c in value.decode("latin-1").split(";") if "=" in c}
    return {}


def session_auth(app, cfg):
    async def middleware(scope, receive, send):
        if scope["type"] != "http" or not cfg.enabled:
            await app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in _PUBLIC_PATHS:
            await app(scope, receive, send)
            return
        user = auth.unsign(_cookies(scope).get(auth.SESSION_COOKIE), cfg.secret_key)
        if user:
            scope.setdefault("state", {})["user"] = user
            await app(scope, receive, send)
            return
        if path.startswith("/api/"):
            await JSONResponse({"error": "authentication required"}, status_code=401)(scope, receive, send)
        else:
            await RedirectResponse("/auth/login")(scope, receive, send)
    return middleware


routes = [
    Route("/api/clusters", api_clusters),
    Route("/api/tools", api_tools),
    Route("/api/me", api_me),
    Route("/api/chat", api_chat, methods=["POST"]),
    Route("/api/chat/stream", api_chat_stream, methods=["POST"]),
    Route("/auth/login", auth_login),
    Route("/auth/callback", auth_callback),
    Route("/auth/logout", auth_logout),
    Route("/healthz", healthz),
    Mount("/", app=StaticFiles(directory=str(_STATIC), html=True), name="static"),
]

app = session_auth(Starlette(routes=routes), _AUTH)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=os.environ.get("DASHBOARD_HOST", "127.0.0.1"),
                port=int(os.environ.get("DASHBOARD_PORT", "8081")), log_config=None)


if __name__ == "__main__":
    main()
