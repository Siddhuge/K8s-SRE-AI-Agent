"""Unit tests for the change-correlation layer that feeds the RCA engine. This is the
'what changed right before it broke?' logic — under-tested before, and a regression here
silently degrades diagnosis quality (the eval harness gates detectors, not this)."""
from datetime import datetime

from k8s_sre_agent.rca import correlate

T = "2026-06-13T10:00:00+00:00"


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def test_parse_handles_z_suffix_and_bad_input():
    assert correlate._parse("2026-06-13T10:00:00Z") == _dt(T)
    assert correlate._parse(None) is None
    assert correlate._parse("not-a-date") is None


def test_first_failure_time_picks_earliest_warning_only():
    events = [
        {"type": "Warning", "timestamp": "2026-06-13T10:05:00+00:00"},
        {"type": "Warning", "timestamp": "2026-06-13T10:01:00+00:00"},
        {"type": "Normal", "timestamp": "2026-06-13T09:00:00+00:00"},  # ignored: not a Warning
    ]
    assert correlate.first_failure_time(events) == _dt("2026-06-13T10:01:00+00:00")


def test_first_failure_time_none_when_no_warnings():
    assert correlate.first_failure_time([{"type": "Normal", "timestamp": T}]) is None


def test_recent_change_closest_preceding_wins_and_excludes_future():
    ctx = {
        "first_failure_time": _dt(T),
        "secret_changes": [{"name": "db", "at": "2026-06-13T09:55:00+00:00"}],   # 5m before
        "deployments": [
            {"sha": "old", "created_at": "2026-06-13T09:50:00+00:00"},           # 10m before
            {"sha": "future", "created_at": "2026-06-13T11:00:00+00:00"},        # AFTER failure → excluded
        ],
    }
    change = correlate.find_recent_change(ctx)
    assert change["kind"] == "secret" and change["name"] == "db"   # 09:55 is closest preceding


def test_recent_change_sets_rollback_target_to_prior_revision():
    ctx = {
        "first_failure_time": _dt(T),
        "deployments": [
            {"sha": "v1", "created_at": "2026-06-13T09:00:00+00:00"},
            {"sha": "v2", "created_at": "2026-06-13T09:50:00+00:00"},
        ],
    }
    change = correlate.find_recent_change(ctx)
    assert change["kind"] == "deploy" and change["revision"] == "v2"
    assert change["rollback_target"] == "v1"   # roll back to the change before the offending one
    assert "t" not in change                   # internal sort key stripped


def test_recent_change_none_when_no_candidates():
    assert correlate.find_recent_change({"first_failure_time": _dt(T)}) is None


def test_build_timeline_is_time_ordered_with_change_marker():
    ctx = {
        "events": [{"reason": "BackOff", "message": "back-off restarting", "timestamp": T}],
        "recent_change": {"kind": "secret", "name": "db-credentials", "at": "2026-06-13T09:55:00+00:00"},
    }
    tl = correlate.build_timeline(ctx)
    assert len(tl) == 2
    assert "CHANGE [secret] db-credentials" in tl[0]   # change (09:55) precedes the event (10:00)
    assert "BackOff" in tl[1]
