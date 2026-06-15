from k8s_sre_agent.ratelimit import TokenBucketLimiter


def test_burst_then_block():
    lim = TokenBucketLimiter(rate=1.0, burst=3)
    t = 1000.0
    assert lim.allow("alice", now=t)      # 3 -> 2
    assert lim.allow("alice", now=t)      # 2 -> 1
    assert lim.allow("alice", now=t)      # 1 -> 0
    assert not lim.allow("alice", now=t)  # blocked


def test_replenish_over_time():
    lim = TokenBucketLimiter(rate=2.0, burst=2)
    t = 0.0
    assert lim.allow("bob", now=t)
    assert lim.allow("bob", now=t)
    assert not lim.allow("bob", now=t)
    # 1 second later → 2 tokens replenished (capped at burst).
    assert lim.allow("bob", now=t + 1.0)


def test_principals_isolated():
    lim = TokenBucketLimiter(rate=0.0, burst=1)
    t = 0.0
    assert lim.allow("alice", now=t)
    assert not lim.allow("alice", now=t)
    assert lim.allow("bob", now=t)  # bob has his own bucket
