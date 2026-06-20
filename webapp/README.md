# v2 Dashboard

A web UI for the K8s SRE Agent: a **live cluster overview** + a **chat box** that drives
the agent's read-only tools in natural language. It's a thin layer over the v1 engine — the
same read-only guarantee holds (the outward-facing Slack/Teams post tools are excluded from
the chat entirely).

```
┌──────────────────────────┬───────────────────────────────────────┐
│  Connected clusters       │  Ask the agent                         │
│  ● prod   aks  reachable   │  > diagnose payments/api in prod       │
│    payments, checkout      │  ⎈ CrashLoopBackOff (missing dep)…     │
│  ● dev    onprem  timeout  │     🔧 rca_diagnose  🔧 k8s_get_events │
│  [available tools ▾]       │  [ type a question…            ][Send] │
└──────────────────────────┴───────────────────────────────────────┘
```

## Run it

```bash
# read-only tools + cluster overview work with no API key:
PYTHONPATH=src:. python3 -m webapp.server         # → http://127.0.0.1:8081

# full natural-language chat (Claude orchestrates the tools):
pip install -e ".[web]"
ANTHROPIC_API_KEY=sk-... PYTHONPATH=src:. python3 -m webapp.server
```

It reads the same `CLUSTERS_CONFIG` registry as the agent, so whatever clusters the agent
is configured for show up here with live reachability.

## What it does

| Endpoint | Purpose |
|---|---|
| `GET /api/clusters` | Configured clusters + live per-cluster reachability (bounded probe) |
| `GET /api/tools` | The read-only tools the chat can use |
| `POST /api/chat` | `{message, history}` → Claude runs an agentic tool-use loop and replies |
| `/` , `/healthz` | Dashboard UI + health |

**With an API key**, the chat runs a real tool-use loop: Claude calls `rca_diagnose`,
`k8s_get_pods`, `logs_pod`, `prom_query`, etc. (the exact MCP tool functions, captured —
no duplication), and answers with the evidence. The tool calls are shown as chips under
each reply. **Without a key**, it degrades gracefully — it can still list clusters and run
an RCA from a simple command, and tells the user how to enable full chat.

## Security

- **Read-only.** Only `get`/`list`/`watch` tools are exposed; the Slack/Teams post tools
  are explicitly excluded. The chat cannot mutate anything.
- It inherits the agent's tenant `allowedNamespaces` guards — a question about a
  disallowed namespace is refused by the underlying tool.
- This dev server has **no inbound auth** of its own — put it behind your SSO/ingress (or
  the v1 gateway's auth) before exposing it beyond localhost.

## Architecture

`agent_tools.py` captures the MCP tool functions and turns their signatures into Anthropic
tool schemas. `chat.py` runs the tool-use loop (or the keyless fallback). `server.py` is a
small Starlette app serving the JSON APIs + the static UI in `static/`. Tests live in
`tests/test_webapp.py`.
