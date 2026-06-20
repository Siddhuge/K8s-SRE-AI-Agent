"""Chat backend: an agentic tool-use loop over the agent's read-only tools.

With ANTHROPIC_API_KEY set, Claude orchestrates the tools to answer in natural language
(the real chatbot). Without a key it degrades to a small intent fallback so the dashboard
still does something useful offline. Either way it is read-only — recommend, never execute.
"""
from __future__ import annotations

import json
import os

from .agent_tools import collect_tools, to_anthropic_schema

_SYSTEM = (
    "You are a read-only Kubernetes SRE assistant embedded in an operations dashboard. "
    "Use the provided tools to investigate clusters and produce root-cause analyses. You "
    "can ONLY read — recommend fixes and rollbacks, never execute them. Be concise and "
    "cite the evidence (pod states, events, logs) behind any conclusion. If a namespace "
    "or cluster isn't specified, ask or use the default."
)
_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
_MAX_TOOL_ROUNDS = 8


def chat_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def run_chat(message: str, history: list[dict] | None = None) -> dict:
    """Return {reply, trace, llm}. `trace` is the tool calls made (shown in the UI)."""
    if not chat_available():
        return _fallback(message)
    try:
        import anthropic
    except ImportError:
        return {"reply": "Install the chat extra: `pip install anthropic`.", "trace": [], "llm": False}

    tools = collect_tools()
    schemas = [to_anthropic_schema(n, f) for n, f in tools.items()]
    client = anthropic.Anthropic()
    messages = list(history or []) + [{"role": "user", "content": message}]
    trace: list[dict] = []

    for _ in range(_MAX_TOOL_ROUNDS):
        resp = client.messages.create(
            model=_MODEL, max_tokens=1500, system=_SYSTEM, tools=schemas, messages=messages
        )
        if resp.stop_reason != "tool_use":
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            return {"reply": text.strip(), "trace": trace, "llm": True}

        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if getattr(block, "type", "") != "tool_use":
                continue
            fn = tools.get(block.name)
            try:
                output = fn(**block.input) if fn else {"error": f"unknown tool {block.name}"}
            except Exception as exc:  # noqa: BLE001 — surface tool errors to the model, don't crash
                output = {"error": f"{type(exc).__name__}: {exc}"}
            trace.append({"tool": block.name, "input": block.input})
            results.append({
                "type": "tool_result", "tool_use_id": block.id,
                "content": json.dumps(output, default=str)[:20000],
            })
        messages.append({"role": "user", "content": results})

    return {"reply": "Stopped after the maximum number of tool steps.", "trace": trace, "llm": True}


def _sse(event_type: str, **data) -> str:
    return f"data: {json.dumps({'type': event_type, **data})}\n\n"


def stream_chat(message: str, history: list[dict] | None = None):
    """Server-Sent-Events generator: streams `delta` (text), `tool` (a tool call), and a
    final `done` event. Sync generator — Starlette runs it in a threadpool."""
    if not chat_available():
        out = _fallback(message)
        yield _sse("delta", text=out["reply"])
        yield _sse("done", trace=out["trace"], llm=False)
        return
    try:
        import anthropic
    except ImportError:
        yield _sse("delta", text="Install the chat extra: `pip install anthropic`.")
        yield _sse("done", trace=[], llm=False)
        return

    tools = collect_tools()
    schemas = [to_anthropic_schema(n, f) for n, f in tools.items()]
    client = anthropic.Anthropic()
    messages = list(history or []) + [{"role": "user", "content": message}]
    trace: list[dict] = []

    for _ in range(_MAX_TOOL_ROUNDS):
        with client.messages.stream(
            model=_MODEL, max_tokens=1500, system=_SYSTEM, tools=schemas, messages=messages
        ) as stream:
            for text in stream.text_stream:          # live token-by-token output
                yield _sse("delta", text=text)
            final = stream.get_final_message()

        if final.stop_reason != "tool_use":
            yield _sse("done", trace=trace, llm=True)
            return

        messages.append({"role": "assistant", "content": final.content})
        results = []
        for block in final.content:
            if getattr(block, "type", "") != "tool_use":
                continue
            yield _sse("tool", tool=block.name)       # show the live "🔧 calling X" chip
            fn = tools.get(block.name)
            try:
                output = fn(**block.input) if fn else {"error": f"unknown tool {block.name}"}
            except Exception as exc:  # noqa: BLE001
                output = {"error": f"{type(exc).__name__}: {exc}"}
            trace.append({"tool": block.name, "input": block.input})
            results.append({
                "type": "tool_result", "tool_use_id": block.id,
                "content": json.dumps(output, default=str)[:20000],
            })
        messages.append({"role": "user", "content": results})

    yield _sse("done", trace=trace, llm=True)


def _fallback(message: str) -> dict:
    """No API key: handle a couple of intents directly so the dashboard isn't dead."""
    tools = collect_tools()
    low = message.lower()
    try:
        if "cluster" in low and "list" in low:
            from k8s_sre_agent.clusters import manager
            return {"reply": json.dumps(manager().list_clusters(), indent=2), "trace": [], "llm": False}
        if ("diagnose" in low or "rca" in low) and "/" in message:
            # crude parse: "diagnose <ns>/<subject> [in <cluster>]"
            frag = message.split("diagnose", 1)[-1].replace("rca", "").strip().split()[0]
            ns, _, subject = frag.partition("/")
            cluster = message.split(" in ", 1)[1].strip() if " in " in message else None
            report = tools["rca_diagnose"](cluster=cluster, namespace=ns, subject=subject)
            text = report.get("markdown") or json.dumps(report, indent=2, default=str)
            return {"reply": text, "trace": [{"tool": "rca_diagnose", "input": {"namespace": ns, "subject": subject}}], "llm": False}
    except Exception as exc:  # noqa: BLE001
        return {"reply": f"Couldn't run that: {exc}", "trace": [], "llm": False}

    return {
        "reply": (
            "Chat needs `ANTHROPIC_API_KEY` for full natural-language investigation. "
            "Offline I can still: **list clusters** (say 'list clusters'), or run an RCA "
            "(say 'diagnose <namespace>/<workload> in <cluster>')."
        ),
        "trace": [], "llm": False,
    }
