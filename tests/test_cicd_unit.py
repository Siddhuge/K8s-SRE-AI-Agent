"""Unit tests for the CI/CD tools — the empty-token auth fix (no live API)."""
from k8s_sre_agent.config import Settings
from k8s_sre_agent.tools.cicd import _gh_headers, _gl_headers


def test_github_omits_auth_header_when_no_token():
    h = _gh_headers(Settings(github_token="", _env_file=None))
    assert "Authorization" not in h  # empty 'Bearer ' would 401 — must be omitted
    assert h["Accept"] == "application/vnd.github+json"


def test_github_includes_auth_header_when_token_present():
    h = _gh_headers(Settings(github_token="ghp_abc", _env_file=None))
    assert h["Authorization"] == "Bearer ghp_abc"


def test_gitlab_omits_token_header_when_empty():
    assert "PRIVATE-TOKEN" not in _gl_headers(Settings(gitlab_token="", _env_file=None))
    assert _gl_headers(Settings(gitlab_token="glpat-x", _env_file=None))["PRIVATE-TOKEN"] == "glpat-x"
