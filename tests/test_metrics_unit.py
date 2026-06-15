"""Pure unit tests for the metrics helpers (no backend needed).

Locks in the range-query fix: Prometheus needs real unix start/end + step, not the
relative 'now-1h' string (which 400s) we shipped originally.
"""
from k8s_sre_agent.tools.metrics import parse_duration


def test_parse_duration_units():
    assert parse_duration("30m") == 1800
    assert parse_duration("1h") == 3600
    assert parse_duration("2d") == 172800
    assert parse_duration("45s") == 45
    assert parse_duration("15") == 900  # bare number defaults to minutes


def test_query_range_builds_unix_timestamps(monkeypatch):
    import k8s_sre_agent.tools.metrics as m

    captured = {}

    def fake_get_json(url, params=None, headers=None):
        captured["url"] = url
        captured["params"] = params
        return {"data": {"result": []}}

    monkeypatch.setattr(m, "get_json", fake_get_json)
    monkeypatch.setattr(m, "_prom", lambda c: "http://prom:9090")

    m._query("c", "up", rng="30m")
    assert captured["url"].endswith("/api/v1/query_range")
    p = captured["params"]
    # start/end must be integers (unix), not "now-..." strings
    assert isinstance(p["start"], int) and isinstance(p["end"], int)
    assert p["end"] - p["start"] == 1800
    assert p["step"].endswith("s")
