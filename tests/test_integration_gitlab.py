"""Live GitLab integration test for `gitlab_pipelines`.

Gated on env so it never needs a baked-in secret: set GITLAB_TOKEN (a token with
`read_api`/`api` scope), optionally GITLAB_URL (default https://gitlab.com) and
GITLAB_TEST_PROJECT (a project path with CI pipelines). SKIPS otherwise.

Validated manually on 2026-06-15 against Siddhuge/gitlab-devsecops-poc: returns real
pipelines with correct field mapping (id/status/ref/sha/created_at/web_url) and the
status filter works. (A `write_repository`-only token correctly yields a structured
`forbidden` error — see test_auth_resilience.)
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("GITLAB_TOKEN") or not os.environ.get("GITLAB_TEST_PROJECT"),
    reason="GITLAB_TOKEN / GITLAB_TEST_PROJECT not set",
)


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


def test_gitlab_pipelines_live():
    proj = os.environ["GITLAB_TEST_PROJECT"]
    out = _tools()["gitlab_pipelines"](project=proj, limit=5)
    assert isinstance(out, list), f"expected pipeline list, got {out}"
    if out:  # project may legitimately have no pipelines
        p = out[0]
        assert {"id", "status", "ref", "sha", "created_at", "web_url"} <= p.keys()
