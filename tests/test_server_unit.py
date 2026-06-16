"""Unit tests for server assembly — no cluster needed (build doesn't connect)."""
import os

os.environ.setdefault("CLUSTERS_CONFIG", "config/clusters.example.yaml")


def test_build_server_registers_all_tools():
    import asyncio

    from k8s_sre_agent.server import build_server

    mcp = build_server()
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    # core surface present
    for expected in ["rca_diagnose", "k8s_get_pods", "logs_pod", "metric_cpu",
                     "argocd_app", "istio_mesh_analyze", "gitlab_pipelines", "list_clusters"]:
        assert expected in names, f"missing tool {expected}"
    assert len(names) >= 38


def test_guarded_tools_keep_real_schemas():
    """GuardedMCP + functools.wraps must preserve params for FastMCP schema gen."""
    import asyncio

    from k8s_sre_agent.server import build_server

    tools = {t.name: t for t in asyncio.run(build_server().list_tools())}
    schema = tools["rca_diagnose"].inputSchema
    assert set(schema["properties"]) >= {"namespace", "subject", "cluster"}
    assert "kwargs" not in schema["properties"]
