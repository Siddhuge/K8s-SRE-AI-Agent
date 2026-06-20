"""Tests for the v2 dashboard backend (mocked cluster manager, no real cluster, no API
key). Covers tool collection + read-only exclusion, Anthropic schema building, the
/api/clusters reachability endpoint, /api/tools, and the keyless chat fallback."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # repo root → import webapp

from webapp import agent_tools, chat, server  # noqa: E402


def test_collect_tools_has_read_tools_and_excludes_post_tools():
    tools = agent_tools.collect_tools()
    assert "k8s_get_pods" in tools and "rca_diagnose" in tools and "prom_query" in tools
    # the dashboard is strictly read-only:
    assert "slack_post" not in tools and "teams_post" not in tools


def test_anthropic_schema_marks_required_and_types():
    schema = agent_tools.to_anthropic_schema("k8s_get_pods", agent_tools.collect_tools()["k8s_get_pods"])
    props = schema["input_schema"]["properties"]
    assert props["namespace"]["type"] == "string"
    assert "namespace" in schema["input_schema"]["required"]       # no default → required
    assert "cluster" not in schema["input_schema"]["required"]     # has default → optional
    assert schema["description"]


def _fake_manager(reachable=True):
    core = SimpleNamespace(get_api_resources=(lambda: True) if reachable else _boom)
    return SimpleNamespace(
        list_clusters=lambda: [{"name": "prod", "tenant": "payments", "provider": "aks",
                                "region": "eastus", "namespaces": ["payments"]}],
        clients=lambda name: SimpleNamespace(core_v1=core),
    )


def _boom():
    raise RuntimeError("unreachable")


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://dash")


def test_api_clusters_reports_reachability(monkeypatch):
    import k8s_sre_agent.clusters as clusters_mod
    monkeypatch.setattr(clusters_mod, "manager", lambda: _fake_manager(reachable=True))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    async def run():
        async with _client() as c:
            data = (await c.get("/api/clusters")).json()
            assert data["clusters"][0]["name"] == "prod"
            assert data["clusters"][0]["reachable"] is True
            assert data["chat_enabled"] is False
    asyncio.run(run())


def test_api_clusters_marks_unreachable(monkeypatch):
    import k8s_sre_agent.clusters as clusters_mod
    monkeypatch.setattr(clusters_mod, "manager", lambda: _fake_manager(reachable=False))

    async def run():
        async with _client() as c:
            data = (await c.get("/api/clusters")).json()
            assert data["clusters"][0]["reachable"] is False
    asyncio.run(run())


def test_api_tools_lists_catalog():
    async def run():
        async with _client() as c:
            tools = (await c.get("/api/tools")).json()["tools"]
            names = {t["name"] for t in tools}
            assert "rca_diagnose" in names and "slack_post" not in names
    asyncio.run(run())


def test_chat_fallback_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = chat.run_chat("hello there")
    assert out["llm"] is False and "ANTHROPIC_API_KEY" in out["reply"]


def test_chat_empty_message_is_400():
    async def run():
        async with _client() as c:
            assert (await c.post("/api/chat", json={"message": "  "})).status_code == 400
    asyncio.run(run())
