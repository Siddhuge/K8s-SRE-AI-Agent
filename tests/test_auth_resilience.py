import pytest

from k8s_sre_agent.auth import AuthError, authorize_claims
from k8s_sre_agent.config import Settings
from k8s_sre_agent.resilience import ToolError, tool_guard


def _settings(groups=""):
    return Settings(oidc_required_groups=groups, _env_file=None)


def test_authorize_requires_group():
    s = _settings("sre-readonly,platform-oncall")
    with pytest.raises(AuthError):
        authorize_claims({"sub": "u1", "groups": ["random"]}, s)
    p = authorize_claims({"sub": "u1", "groups": ["sre-readonly"]}, s)
    assert p.subject == "u1"


def test_authorize_no_required_groups_allows_anyone():
    p = authorize_claims({"sub": "svc", "roles": []}, _settings(""))
    assert p.subject == "svc"


def test_tool_guard_returns_structured_error():
    @tool_guard
    def boom(cluster=None):
        raise RuntimeError("kaboom: secret-host:5432")

    out = boom(cluster="aks-prod")
    assert out["error"] == "error"
    assert "kaboom" not in out["message"]  # internal detail not leaked to the model


def test_tool_guard_classifies_known_kinds():
    @tool_guard
    def forbidden(cluster=None):
        raise ToolError("namespace not allowed", kind="forbidden")

    assert forbidden(cluster="x")["error"] == "forbidden"


def test_tool_guard_passes_success_through():
    @tool_guard
    def ok(cluster=None):
        return [{"name": "pod-1"}]

    assert ok(cluster="x") == [{"name": "pod-1"}]


def test_tool_guard_classifies_httpx_status_errors():
    """HTTP-backend errors (GitLab/GitHub/Prom/Loki/ArgoCD) carry the code on
    .response.status_code — must map to specific kinds, not generic upstream_error."""
    import httpx

    def _raise(code):
        @tool_guard
        def call(cluster=None):
            req = httpx.Request("GET", "http://x")
            resp = httpx.Response(code, request=req)
            raise httpx.HTTPStatusError("err", request=req, response=resp)
        return call(cluster="c")

    assert _raise(401)["error"] == "unauthenticated"
    assert _raise(403)["error"] == "forbidden"      # e.g. GitLab token missing read_api scope
    assert _raise(404)["error"] == "not_found"
    assert _raise(503)["error"] == "upstream_error"
