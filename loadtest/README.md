# Gateway Load Test Rig

Two complementary tools — one for **throughput/latency**, one for **behavior under load**.

```bash
make loadtest                                   # behavior gate (deterministic, CI-friendly)
PYTHONPATH=src python3 loadtest/run_load.py -c 16 -d 10   # throughput against a local gateway
```

## 1. `validate_behavior.py` — behavior under concurrency

Runs the **real** auth + rate-limit middleware in-process (with `verify_bearer_token`
patched, so no live OIDC issuer is needed) and asserts, under a concurrent burst:

1. **Auth boundary holds** — no token → `401`, every time.
2. **Rate limiter trips** — one principal bursting past `burst` gets `429`s; allowed ≈ burst.
3. **Limits are per-principal** — a second principal is unaffected by the first's burst.

Deterministic, fast, exits non-zero on violation. This is the part worth gating in CI.

## 2. `run_load.py` — throughput & latency

Real sockets, real concurrency. Boots a local uvicorn gateway and hammers `/healthz`
(public, no token), or targets a deployed gateway with `--url` + `--token`.

```bash
# local self-contained
PYTHONPATH=src python3 loadtest/run_load.py -c 16 -d 10
# deployed staging gateway, authenticated path
python3 loadtest/run_load.py --url https://sre-agent.staging --path /mcp/ --token "$JWT" -c 100 -d 30
```

### Measured profile (local, single process, generator co-located)

A concurrency sweep on one dev box, generator and server sharing cores:

| Concurrency | Throughput | p50 | p99 |
|------------:|-----------:|----:|----:|
| 1   | ~610 req/s | 1.5 ms | 2.4 ms |
| 16  | ~500 req/s | 15 ms  | 170 ms |
| 128 | ~70 req/s  | 1.2 s  | 5.1 s  |

**Reading it honestly:**
- A single process serves **~500–610 req/s** at low–moderate concurrency with low latency
  — this empirically backs the figure in [operations.md](../docs/operations.md).
- Throughput **collapses at very high concurrency** (c=128). Part of that is a measurement
  artifact — the async generator is co-located with the server and oversubscribes the CPU
  — so treat the absolute c=128 number with caution. To attribute it cleanly, run the
  generator from a **separate host** (or use `wrk`/`hey`) against a deployed gateway.
- Either way it shows a **per-process concurrency ceiling**. Operational guidance:
  - bound per-replica concurrency (`uvicorn --limit-concurrency`) so a connection spike
    sheds load (fast `503`) instead of browning out into multi-second tails;
  - scale **horizontally** (the chart's HPA) and front with a connection-limiting ingress;
  - tune the HPA on **latency/concurrency**, not just CPU.
- **Candidate optimization (unverified):** the auth+rate-limit layer uses Starlette
  `BaseHTTPMiddleware`, a known throughput bottleneck under load. Rewriting it as a pure
  ASGI middleware is the first thing to try if you need a higher single-process ceiling —
  re-run this rig before/after to confirm.

> Numbers are hardware-dependent. The rig's value is the **repeatable method + the
> behavior gates**, not any single absolute figure.
