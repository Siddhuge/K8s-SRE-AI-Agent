"""Per-principal token-bucket rate limiting.

Protects both the agent and the downstream clusters/LLM from a runaway caller or a
buggy automation. Thread-safe, in-memory (per replica). For a multi-replica gateway
that needs a global limit, back this with Redis using the same token-bucket math; the
interface stays identical.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    tokens: float
    updated: float


@dataclass
class TokenBucketLimiter:
    """`rate` tokens/sec replenishment, up to `burst` capacity, keyed per principal."""

    rate: float = 1.0
    burst: int = 20
    _buckets: dict[str, _Bucket] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def allow(self, principal: str, cost: float = 1.0, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            b = self._buckets.get(principal)
            if b is None:
                b = _Bucket(tokens=float(self.burst), updated=now)
                self._buckets[principal] = b
            # Replenish.
            b.tokens = min(self.burst, b.tokens + (now - b.updated) * self.rate)
            b.updated = now
            if b.tokens >= cost:
                b.tokens -= cost
                return True
            return False
