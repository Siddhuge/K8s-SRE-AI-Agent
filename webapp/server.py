"""v2 dashboard server (Starlette): cluster overview + chat API + static UI.

Run:  PYTHONPATH=src python3 -m webapp.server          # serves http://127.0.0.1:8081
  or  PYTHONPATH=src uvicorn webapp.server:app --port 8081

Read-only: every API path goes through the agent's read tools. No write capability.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .agent_tools import tool_catalog
from .chat import chat_available, run_chat

_STATIC = Path(__file__).parent / "static"
_REACH_TIMEOUT = float(os.environ.get("DASHBOARD_REACH_TIMEOUT", "3.0"))


def _reachable(cluster_name: str) -> tuple[bool, str]:
    """Cheap auth+reachability probe for one cluster, bounded by a timeout."""
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
    """Clusters the agent is configured for, each with live reachability."""
    from k8s_sre_agent.clusters import manager

    try:
        clusters = manager().list_clusters()
    except Exception as exc:  # noqa: BLE001 — no/invalid registry shouldn't 500 the UI
        return JSONResponse({"clusters": [], "error": f"{type(exc).__name__}: {exc}"})

    out = []
    for c in clusters:
        ok, detail = _reachable(c["name"])
        out.append({**c, "reachable": ok, "status_detail": detail})
    return JSONResponse({"clusters": out, "chat_enabled": chat_available()})


async def api_tools(_req: Request) -> JSONResponse:
    return JSONResponse({"tools": tool_catalog()})


async def api_chat(req: Request) -> JSONResponse:
    body = await req.json()
    message = (body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "empty message"}, status_code=400)
    history = body.get("history") or []
    return JSONResponse(run_chat(message, history))


async def healthz(_req: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


routes = [
    Route("/api/clusters", api_clusters),
    Route("/api/tools", api_tools),
    Route("/api/chat", api_chat, methods=["POST"]),
    Route("/healthz", healthz),
    Mount("/", app=StaticFiles(directory=str(_STATIC), html=True), name="static"),
]

app = Starlette(routes=routes)


def main() -> None:
    import uvicorn

    port = int(os.environ.get("DASHBOARD_PORT", "8081"))
    uvicorn.run(app, host=os.environ.get("DASHBOARD_HOST", "127.0.0.1"), port=port, log_config=None)


if __name__ == "__main__":
    main()
