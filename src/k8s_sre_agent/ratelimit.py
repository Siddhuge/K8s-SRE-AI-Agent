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


# Atomic token bucket in Redis: read tokens+timestamp, refill by elapsed*rate (capped at
# burst), consume if available, write back with a TTL — all server-side in one round trip
# so N replicas share ONE global limit (no read-modify-write race). Same math as _Bucket.
_LUA_TOKEN_BUCKET = """
local data = redis.call('HMGET', KEYS[1], 'tokens', 'updated')
local tokens = tonumber(data[1])
local updated = tonumber(data[2])
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])
if tokens == nil then
  tokens = burst
  updated = now
end
tokens = math.min(burst, tokens + (now - updated) * rate)
updated = now
local allowed = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
end
redis.call('HMSET', KEYS[1], 'tokens', tokens, 'updated', updated)
redis.call('EXPIRE', KEYS[1], ttl)
return allowed
"""


class RedisTokenBucketLimiter:
    """Distributed equivalent of TokenBucketLimiter — a STRICT global limit across all
    gateway replicas. `redis_client` is injected (so this module never hard-imports redis);
    `build_limiter` constructs it from settings."""

    def __init__(self, redis_client, rate: float, burst: int, *, namespace: str = "ratelimit") -> None:
        self.rate = rate
        self.burst = burst
        self._ns = namespace
        self._script = redis_client.register_script(_LUA_TOKEN_BUCKET)

    def allow(self, principal: str, cost: float = 1.0, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now  # wall clock: replicas share a time base
        # Expire idle buckets after they would have fully refilled (bounds memory).
        ttl = 3600 if self.rate <= 0 else max(1, int(self.burst / self.rate) + 1)
        result = self._script(keys=[f"{self._ns}:{principal}"],
                              args=[self.rate, self.burst, now, cost, ttl])
        return bool(int(result))


def build_limiter(settings):
    """In-memory limiter by default; a Redis-backed global limiter if RATELIMIT_REDIS_URL
    is set (per-replica vs strict-global — see docs/operations.md)."""
    url = getattr(settings, "ratelimit_redis_url", "") or ""
    rate = float(getattr(settings, "ratelimit_rate", 5.0))
    burst = int(getattr(settings, "ratelimit_burst", 30))
    if not url:
        return TokenBucketLimiter(rate=rate, burst=burst)
    import redis  # lazy: only needed when a Redis URL is configured

    return RedisTokenBucketLimiter(redis.Redis.from_url(url), rate=rate, burst=burst)
