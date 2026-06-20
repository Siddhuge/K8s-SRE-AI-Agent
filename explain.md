# K8s SRE Agent — What It Is and Why It Matters

A one-page-ish explainer for engineering leadership. Plain language first, technical
detail where it counts.

---

## The problem

When something breaks in Kubernetes — a service crash-loops, a pod won't schedule, a
database connection drops — an on-call engineer spends the first 15–45 minutes doing the
same mechanical detective work every time: pulling pod status, reading logs, checking
recent deploys, correlating metrics, cross-referencing past incidents. It's repetitive,
it's slow, and it happens at 3am when people make mistakes.

**This agent does that first-responder investigation automatically** and hands the
engineer a confidence-scored root-cause analysis with a recommended fix — in seconds
instead of half an hour.

## What it is

An **AI-powered Kubernetes troubleshooting agent** that plugs into Claude (Anthropic's
AI) using the **Model Context Protocol (MCP)** — an open standard for giving an AI safe,
structured access to tools. An engineer asks, in plain English, *"payments API is
crash-looping in the prod cluster, what's wrong?"* and the agent:

1. **Gathers evidence** — pods, events, logs, deployments, metrics, mesh config, GitOps
   history — across one or many clusters.
2. **Diagnoses** — runs ~18 deterministic "detectors" (CrashLoop, ImagePull, OOM,
   Pending, DNS, TLS, node pressure, storage, Istio sidecar, etc.), correlates *what
   changed right before it broke*, and produces a ranked, confidence-scored root cause.
3. **Recommends** — a specific fix or rollback target, with the evidence that supports it.

The AI's role is the *reasoning and judgment* on top of that evidence — and notably it can
**overrule the automated detectors when they're wrong**. In live testing it did exactly
that: the rule-based engine was 94% sure a rotated secret caused a crash; the model read
the actual logs, saw a connection *timeout* to a database that didn't exist, and correctly
called it a missing dependency instead.

## The single most important design decision: it is **read-only**

The agent can **look but never touch**. It has `get`/`list`/`watch` access only — **no
ability to delete, modify, restart, scale, or run commands** in your clusters. It
*recommends* fixes; a human executes them. This is enforced at three layers:

- Kubernetes RBAC (the API server itself refuses any write),
- the cluster's cloud authorization (we proved on a real Azure cluster that write attempts
  return "Forbidden"),
- and **secret values are never readable** — the agent can see that a secret named
  `db-credentials` exists and changed, but never its contents (also proven against real
  cloud authorization).

The worst case is therefore *"it gives a wrong recommendation,"* never *"it broke
production."* That's what makes it safe to pilot.

## How it's secured and operated (the enterprise checklist)

| Concern | How it's handled |
|---|---|
| **Who can use it** | Every request authenticated via corporate SSO (OIDC / Entra ID), restricted to allowed groups |
| **Multi-cloud** | Works on Azure (AKS), AWS (EKS), and on-prem using each platform's secure identity federation — no long-lived keys |
| **Multi-tenant** | Teams are isolated to their own namespaces; one team can't see another's workloads |
| **Abuse protection** | Per-user rate limiting (optionally global across replicas via Redis) |
| **Outbound safety** | The only thing it can send externally (Slack/Teams alerts) is off by default and channel-allow-listed |
| **Observability** | Exposes its own health, metrics, and audit logs — every action is recorded |
| **Supply chain** | Pinned dependencies + automated CVE scanning that fails the build on a vulnerable package |

## How we know it works (not just "it compiles")

Most of the value here is that it was **validated against real systems**, not just
unit-tested with fake data. We stood up real backends and drove real failures through it:

- A real **Azure AKS cluster** (provisioned with the included Terraform) — proved the
  secure cloud login, the read-only boundary, and an end-to-end root-cause analysis.
- Real **Prometheus, Loki, Grafana, Istio, ArgoCD, GitHub, GitLab**, and a real **Slack**
  workspace.
- **~12 genuine bugs were found and fixed** this way — including two that *no amount of
  unit testing could have caught*, because they only appear against a real cloud
  identity. That's the point of testing against reality.

We also built an **evaluation harness** that scores the diagnosis accuracy on a labeled
set of incidents (currently 100% on the seed set, with zero "confidently wrong" answers
and zero false alarms on healthy workloads), and a **load-test rig** that measured the
gateway at ~500–800 requests/second per instance. Both run in CI, so quality can't
silently regress.

## Cost

It uses Claude in a **tiered** way — cheaper, faster models for routine triage, escalating
to the most capable model only for genuinely ambiguous cases — plus prompt caching to
avoid re-paying for repeated context. The design supports POC, production, and enterprise
cost tiers; the expensive model is the exception, not the default.

## Where it stands today

**Production-ready as a foundation, ready for a controlled pilot.** The engine, the
security model, the integrations, and the operational tooling are all built and
**proven against real systems**. The honest remaining work is a **staging burn-in** that
needs our own environment — wiring up our Jira/ServiceNow/Teams, a load test at our real
traffic, a security pen-test, and measuring diagnosis accuracy on *our* historical
incidents.

**Recommended next step:** deploy it in **advisory mode** (it suggests, humans act) for a
single team's namespace, and measure how much on-call time it saves on real incidents.
That's a low-risk way to prove the value before widening it out.

---

### One-sentence version for the skip-level

> *It's a read-only AI assistant that does the first 30 minutes of Kubernetes incident
> triage automatically — safely, because it can look but never touch — and it's been
> proven against real cloud infrastructure, not just demos.*
