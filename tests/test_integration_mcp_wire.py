"""End-to-end test over the REAL MCP stdio wire protocol.

Spawns `k8s_sre_agent.server stdio` as a subprocess, performs the MCP handshake,
lists tools, and calls a tool over the wire (not a direct function call). Codifies
the manual proof that the FastMCP server + tool registration + protocol work together.
SKIPS when the kind cluster isn't reachable.
"""
from __future__ import annotations

import os

import pytest

CLUSTER = "kind-sre-demo"


def _cluster_up() -> bool:
    try:
        import mcp  # noqa: F401
        from k8s_sre_agent.clusters import manager
        os.environ.setdefault("CLUSTERS_CONFIG", "config/clusters.yaml")
        manager().clients(CLUSTER).core_v1.get_api_resources()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _cluster_up(), reason="mcp/kind cluster not available")


async def _drive():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command="python3", args=["-m", "k8s_sre_agent.server", "stdio"],
        env={**os.environ, "PYTHONPATH": "src", "CLUSTERS_CONFIG": "config/clusters.yaml"},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert "rca_diagnose" in names and len(names) >= 30
            # call a tool over the wire
            res = await session.call_tool("list_clusters", {})
            assert CLUSTER in res.content[0].text
            return names


def test_mcp_server_serves_tools_over_the_wire():
    import asyncio
    names = asyncio.run(_drive())
    assert "k8s_get_pods" in names
