"""Expose the agent's MCP tools as plain callables + Anthropic tool schemas.

Reuses the EXACT tool functions the MCP server registers (captured via a fake registrar),
so the dashboard chat runs the same read-only code path — no duplication. The outward-
facing post tools (slack/teams) are excluded: the dashboard is strictly read-only.
"""
from __future__ import annotations

import inspect
from collections.abc import Callable

# Tools the chatbot must never call — keeps the dashboard read-only.
_EXCLUDED = {"slack_post", "teams_post"}


def collect_tools() -> dict[str, Callable]:
    """{tool_name: function} for every read tool the MCP server exposes."""
    from k8s_sre_agent.rca import engine as rca_engine
    from k8s_sre_agent.tools import (
        cicd,
        gitops,
        incidents,
        istio,
        kubernetes,
        logs,
        metrics,
    )

    captured: dict[str, Callable] = {}

    class _Capture:
        def tool(self, *_a, **_k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    cap = _Capture()
    for module in (kubernetes, logs, metrics, gitops, cicd, incidents, istio, rca_engine):
        module.register(cap)
    return {name: fn for name, fn in captured.items() if name not in _EXCLUDED}


def _json_type(annotation: object) -> str:
    # Tool modules use `from __future__ import annotations`, so annotations are strings.
    text = str(annotation)
    if "int" in text:
        return "integer"
    if "bool" in text:
        return "boolean"
    if "float" in text:
        return "number"
    return "string"


def to_anthropic_schema(name: str, fn: Callable) -> dict:
    """Build an Anthropic tool definition from a tool function's signature + docstring."""
    props: dict[str, dict] = {}
    required: list[str] = []
    for pname, param in inspect.signature(fn).parameters.items():
        props[pname] = {"type": _json_type(param.annotation)}
        if param.default is inspect.Parameter.empty:
            required.append(pname)
    return {
        "name": name,
        "description": (inspect.getdoc(fn) or name).strip()[:1000],
        "input_schema": {"type": "object", "properties": props, "required": required},
    }


def tool_catalog() -> list[dict]:
    """Human-facing list (name + one-line description) for the dashboard."""
    out = []
    for name, fn in sorted(collect_tools().items()):
        doc = (inspect.getdoc(fn) or "").strip().splitlines()
        out.append({"name": name, "summary": doc[0] if doc else ""})
    return out
