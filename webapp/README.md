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
| `GET /api/me` | Current user + whether SSO is enabled |
| `POST /api/chat` | `{message, history}` → full reply (non-streaming) |
| `POST /api/chat/stream` | Same, **streamed over SSE** — live token-by-token text + `tool` events |
| `/auth/login` · `/auth/callback` · `/auth/logout` | OIDC browser SSO |
| `/` , `/healthz` | Dashboard UI + health |

## Streaming chat (live "typing")

`POST /api/chat/stream` returns `text/event-stream`. The UI reads it with `fetch` +
`ReadableStream` and renders incrementally:

- `{"type":"delta","text":"…"}` — a chunk of the model's answer (appended live)
- `{"type":"tool","tool":"rca_diagnose"}` — emitted when the agent calls a tool (shown as a chip)
- `{"type":"done","trace":[…],"llm":true}` — end of turn

It works token-by-token with an API key; without one it streams the single fallback reply.

## SSO (browser login)

OFF by default (local dev). Enable the OIDC Authorization-Code flow (PKCE) by setting:

```bash
DASHBOARD_OIDC_ISSUER=https://login.microsoftonline.com/<tenant>/v2.0   # or any OIDC IdP
DASHBOARD_OIDC_CLIENT_ID=<dashboard-app-client-id>
DASHBOARD_OIDC_CLIENT_SECRET=<secret>
DASHBOARD_BASE_URL=https://sre-dashboard.example          # for the redirect URI
DASHBOARD_OIDC_REQUIRED_GROUPS=sre-readonly,platform-oncall   # optional group gate
DASHBOARD_SECRET_KEY=<random-32+-bytes>                   # signs the session cookie
```

Register `${DASHBOARD_BASE_URL}/auth/callback` as a redirect URI in the IdP. When enabled,
every path except `/healthz` and `/auth/*` requires a valid session: unauthenticated
browser requests are 302'd to the IdP; `/api/*` returns 401. The id_token is validated
against the IdP's JWKS (issuer + audience + signature), optional group membership is
enforced, and a short-lived **HMAC-signed, httponly** session cookie is set. State/PKCE are
carried in a separate signed cookie between login and callback.

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
- **Browser SSO** (OIDC) protects every path when configured (see below); off by default
  for local dev. Sessions are HMAC-signed httponly cookies, optionally group-gated.
  Terminate TLS at your ingress in production.

## Architecture

`agent_tools.py` captures the MCP tool functions and turns their signatures into Anthropic
tool schemas. `chat.py` runs the tool-use loop with both a non-streaming (`run_chat`) and an
SSE-streaming (`stream_chat`) path, plus the keyless fallback. `auth.py` is the OIDC SSO
(discovery, PKCE, id_token validation via JWKS, HMAC-signed sessions). `server.py` is a
small Starlette app wiring the JSON APIs, the SSO routes + middleware, and the static UI in
`static/`. Tests: `tests/test_webapp.py` + `tests/test_webapp_auth.py` (no IdP/API key
needed). No new dependencies — auth reuses `httpx` + `pyjwt`.
