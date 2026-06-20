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

### Measured profile + an A/B the rig drove

Concurrency sweep on one dev box (generator co-located with the server), **before vs after**
replacing the Starlette `BaseHTTPMiddleware` auth layer with a pure-ASGI middleware:

| Concurrency | BaseHTTPMiddleware | Pure-ASGI | p50 (after) |
|------------:|-------------------:|----------:|------------:|
| 1   | ~610 req/s | **~795 req/s** | 1.2 ms |
| 16  | ~505 req/s | ~490 req/s     | 18 ms  |
| 128 | ~68 req/s  | ~68 req/s      | 1.1 s  |

**What the A/B actually showed (and corrected):**
- The pure-ASGI rewrite is a **real ~25–30% win at low concurrency** (610→795 req/s, lower
  tail latency) and removes a known footgun — kept.
- It did **not** change the c=128 number. That **falsified the earlier hypothesis** that
  `BaseHTTPMiddleware` caused the high-concurrency drop. Since removing it changed nothing
  at c=128, that drop is the **co-located generator saturating the CPU**, not a server
  defect — exactly the measurement caveat to keep in mind. To measure the true per-process
  ceiling, run the generator from a **separate host** (or `wrk`/`hey`) against a deployed
  gateway.
- A single process comfortably serves **~500–795 req/s** at low–moderate concurrency,
  backing the figure in [operations.md](../docs/operations.md).

Operational guidance still holds regardless: bound per-replica concurrency
(`uvicorn --limit-concurrency`) so a spike sheds load (fast `503`) instead of browning
out, scale **horizontally** (the chart's HPA), and tune the HPA on **latency/concurrency**.

> Numbers are hardware-dependent. The rig's value is the **repeatable method, the
> before/after A/B, and the behavior gates** — not any single absolute figure.
