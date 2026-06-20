#!/usr/bin/env python3
"""Gateway throughput / latency load generator.

Two ways to use it:

  # 1. Self-contained: boot a real uvicorn gateway locally and hammer /healthz over TCP
  #    (validates the server stack + the ~400-600 req/s claim in docs/operations.md)
  PYTHONPATH=src python3 loadtest/run_load.py -c 64 -d 10

  # 2. Against a deployed gateway in staging (real auth path with a token)
  python3 loadtest/run_load.py --url https://sre-agent.staging --path /mcp/ \
      --token "$JWT" -c 100 -d 30

Reports RPS, latency p50/p90/p95/p99/max, and the HTTP status breakdown. Real sockets,
real concurrency — not an in-process approximation.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import time
from collections import Counter

import httpx


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_ready(url: str, timeout: float = 20.0) -> bool:
    async with httpx.AsyncClient(timeout=2.0) as c:
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            with contextlib.suppress(Exception):
                if (await c.get(url)).status_code == 200:
                    return True
            await asyncio.sleep(0.25)
    return False


async def _worker(client, url, headers, deadline, n_target, counter, lats, statuses):
    while time.perf_counter() < deadline and counter[0] < n_target:
        counter[0] += 1
        t0 = time.perf_counter()
        try:
            r = await client.get(url, headers=headers)
            statuses[r.status_code] += 1
        except Exception:
            statuses[0] += 1  # 0 == connection/transport error
        lats.append((time.perf_counter() - t0) * 1000.0)


async def run(url: str, headers: dict, concurrency: int, duration: float, requests: int) -> dict:
    lats: list[float] = []
    statuses: Counter = Counter()
    counter = [0]
    n_target = requests if requests else 10**12
    deadline = time.perf_counter() + (duration if not requests else 10**6)
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=10.0, limits=httpx.Limits(max_connections=concurrency + 8)) as client:
        await asyncio.gather(*[
            _worker(client, url, headers, deadline, n_target, counter, lats, statuses)
            for _ in range(concurrency)
        ])
    wall = time.perf_counter() - started
    total = len(lats)
    return {
        "total": total, "wall": wall, "rps": total / wall if wall else 0.0,
        "p50": _percentile(lats, 50), "p90": _percentile(lats, 90),
        "p95": _percentile(lats, 95), "p99": _percentile(lats, 99),
        "max": max(lats) if lats else 0.0, "statuses": dict(statuses),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Gateway load test")
    ap.add_argument("--url", help="target base URL; if omitted, boot a local gateway")
    ap.add_argument("--path", default="/healthz", help="path to hit (default /healthz)")
    ap.add_argument("--token", help="bearer token for protected paths")
    ap.add_argument("-c", "--concurrency", type=int, default=50)
    ap.add_argument("-d", "--duration", type=float, default=10.0, help="seconds (ignored if -n)")
    ap.add_argument("-n", "--requests", type=int, default=0, help="total requests (overrides -d)")
    args = ap.parse_args()

    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    proc = None
    try:
        if args.url:
            base = args.url.rstrip("/")
        else:
            port = _free_port()
            base = f"http://127.0.0.1:{port}"
            env = {**os.environ, "OIDC_ISSUER": "https://loadtest.invalid/", "OIDC_AUDIENCE": "api://loadtest"}
            env.setdefault("PYTHONPATH", "src")
            print(f"booting local gateway on :{port} (hitting {args.path}) …")
            proc = subprocess.Popen(
                [sys.executable, "-m", "k8s_sre_agent.server", "http", "--host", "127.0.0.1", "--port", str(port)],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if not asyncio.run(_wait_ready(base + "/healthz")):
                print("gateway did not become ready", file=sys.stderr)
                return 2

        url = base + args.path
        mode = f"n={args.requests}" if args.requests else f"d={args.duration}s"
        print(f"load: {url}  c={args.concurrency} {mode}\n")
        rep = asyncio.run(run(url, headers, args.concurrency, args.duration, args.requests))

        print("=== Gateway load test ===")
        print(f"  requests     : {rep['total']}  in {rep['wall']:.2f}s")
        print(f"  throughput   : {rep['rps']:.0f} req/s")
        print(f"  latency (ms) : p50={rep['p50']:.1f}  p90={rep['p90']:.1f}  "
              f"p95={rep['p95']:.1f}  p99={rep['p99']:.1f}  max={rep['max']:.1f}")
        print(f"  statuses     : {rep['statuses']}  (0 == transport error)")
        return 0
    finally:
        if proc:
            proc.terminate()
            with contextlib.suppress(Exception):
                proc.wait(timeout=5)


if __name__ == "__main__":
    sys.exit(main())
