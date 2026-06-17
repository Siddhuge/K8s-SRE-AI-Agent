"""Unit tests for the inbound OIDC verify flow (verify_bearer_token).

The signature/JWKS step needs a real IdP, so PyJWKClient + jwt.decode are mocked;
this validates the flow around them: issuer required, claims → Principal, group
enforcement, and that a decode failure propagates (the middleware turns it into 401)."""
from __future__ import annotations

import types

import jwt
import pytest

from k8s_sre_agent.auth import AuthError, verify_bearer_token
from k8s_sre_agent.config import Settings


def _mock_jwks(monkeypatch, claims=None, raise_decode=False):
    class FakeJWKClient:
        def __init__(self, url):
            pass

        def get_signing_key_from_jwt(self, token):
            return types.SimpleNamespace(key="signing-key")

    monkeypatch.setattr(jwt, "PyJWKClient", FakeJWKClient)

    def fake_decode(token, key, **kw):
        if raise_decode:
            raise jwt.InvalidTokenError("bad signature")
        return claims or {}

    monkeypatch.setattr(jwt, "decode", fake_decode)


def test_requires_issuer():
    with pytest.raises(AuthError):
        verify_bearer_token("tok", Settings(oidc_issuer="", _env_file=None))


def test_valid_claims_with_required_group(monkeypatch):
    _mock_jwks(monkeypatch, claims={"sub": "alice", "groups": ["sre-readonly"], "tid": "t1"})
    s = Settings(oidc_issuer="https://issuer", oidc_audience="api://x",
                 oidc_required_groups="sre-readonly,platform-oncall", _env_file=None)
    p = verify_bearer_token("tok", s)
    assert p.subject == "alice" and p.tenant == "t1"


def test_rejects_missing_required_group(monkeypatch):
    _mock_jwks(monkeypatch, claims={"sub": "bob", "groups": ["other"]})
    s = Settings(oidc_issuer="https://issuer", oidc_required_groups="sre-readonly", _env_file=None)
    with pytest.raises(AuthError):
        verify_bearer_token("tok", s)


def test_decode_failure_propagates(monkeypatch):
    _mock_jwks(monkeypatch, raise_decode=True)
    s = Settings(oidc_issuer="https://issuer", _env_file=None)
    with pytest.raises(jwt.InvalidTokenError):  # middleware catches this → 401
        verify_bearer_token("tok", s)
