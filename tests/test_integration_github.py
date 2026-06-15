"""Live GitHub API integration tests for the CI/CD tools.

Hit the public GitHub API unauthenticated against a stable public repo. SKIP when
the API is unreachable or rate-limited (unauth limit is 60/h) so the suite stays
green offline / in constrained CI.
"""
from __future__ import annotations

import pytest

REPO = "argoproj/argo-cd"


def _api_ok() -> bool:
    import httpx
    try:
        r = httpx.get("https://api.github.com/rate_limit", timeout=4.0)
        return r.status_code == 200 and r.json()["resources"]["core"]["remaining"] > 5
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _api_ok(), reason="GitHub API unreachable or rate-limited")


def _tools():
    captured = {}

    class M:
        def tool(self):
            def d(fn):
                captured[fn.__name__] = fn
                return fn
            return d

    from k8s_sre_agent.tools import cicd
    cicd.register(M())
    return captured


def test_github_actions_runs_live():
    runs = _tools()["github_actions_runs"](repo=REPO, limit=3)
    assert isinstance(runs, list) and runs, "expected recent workflow runs"
    r = runs[0]
    assert {"id", "status", "conclusion", "head_sha"} <= r.keys()


def test_compare_deployments_live_returns_real_diff():
    cmp = _tools()["compare_deployments"](repo=REPO, base_sha="v2.13.0", head_sha="v2.13.1")
    assert cmp["ahead_by"] == 7
    assert cmp["commits"] and cmp["changed_files"]
    assert "VERSION" in cmp["changed_files"]


def test_recent_deployments_live():
    deps = _tools()["recent_deployments"](repo=REPO, limit=3)
    assert isinstance(deps, list)  # may be empty for some repos; shape must be a list
