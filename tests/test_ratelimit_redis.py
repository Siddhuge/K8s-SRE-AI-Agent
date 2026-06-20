"""The Redis-backed limiter must make the SAME allow/deny decisions as the in-memory one
(the docs promise 'same token-bucket math'). No real Redis here: a fake client mirrors the
Lua script's algorithm, so we test the limiter wiring + semantic parity deterministically.
(The Lua itself is validated against a real Redis in staging.)"""
from types import SimpleNamespace

from k8s_sre_agent.ratelimit import (
    RedisTokenBucketLimiter,
    TokenBucketLimiter,
    build_limiter,
)


class _FakeRedis:
    """Mirrors _LUA_TOKEN_BUCKET in Python so the limiter can be exercised without a server."""

    def __init__(self):
        self.store: dict[str, tuple[float, float]] = {}

    def register_script(self, _lua):
        def run(keys=None, args=None):
            key = keys[0]
            rate, burst, now, cost, _ttl = (float(a) for a in args)
            tokens, updated = self.store.get(key, (burst, now))
            tokens = min(burst, tokens + (now - updated) * rate)
            allowed = 0
            if tokens >= cost:
                tokens -= cost
                allowed = 1
            self.store[key] = (tokens, now)
            return allowed
        return run


def test_redis_limiter_matches_in_memory_decisions():
    rate, burst = 2.0, 5
    mem = TokenBucketLimiter(rate=rate, burst=burst)
    red = RedisTokenBucketLimiter(_FakeRedis(), rate=rate, burst=burst)

    t = 1000.0
    mem_seq, red_seq = [], []
    # burst of 8 instant requests, then trickle in over time (refill at `rate`/sec)
    for i in range(8):
        mem_seq.append(mem.allow("alice", now=t))
        red_seq.append(red.allow("alice", now=t))
    for step in (0.4, 0.4, 0.4, 1.0, 2.0):
        t += step
        mem_seq.append(mem.allow("alice", now=t))
        red_seq.append(red.allow("alice", now=t))

    assert red_seq == mem_seq                      # identical decisions
    assert mem_seq[:5] == [True] * 5               # burst allowed
    assert mem_seq[5:8] == [False] * 3             # then exhausted


def test_redis_limiter_is_per_principal():
    red = RedisTokenBucketLimiter(_FakeRedis(), rate=0.0, burst=2)
    assert [red.allow("p1", now=1.0) for _ in range(3)] == [True, True, False]
    assert red.allow("p2", now=1.0) is True        # different principal unaffected


def test_build_limiter_selects_backend_by_url(monkeypatch):
    in_mem = build_limiter(SimpleNamespace(ratelimit_rate=5.0, ratelimit_burst=30, ratelimit_redis_url=""))
    assert isinstance(in_mem, TokenBucketLimiter)

    # With a URL set it builds the Redis variant (lazy `import redis` + from_url). Patch
    # from_url so no real connection is made.
    import redis
    monkeypatch.setattr(redis.Redis, "from_url", classmethod(lambda cls, url: _FakeRedis()))
    red = build_limiter(SimpleNamespace(ratelimit_rate=2.0, ratelimit_burst=5,
                                        ratelimit_redis_url="redis://localhost:6379/0"))
    assert isinstance(red, RedisTokenBucketLimiter)
    assert red.allow("alice", now=1.0) is True
