"""Browser SSO for the dashboard — OIDC Authorization-Code flow with PKCE.

Flow: /auth/login → IdP → /auth/callback (exchange code, validate id_token, enforce
groups) → signed session cookie → app. Off by default: if DASHBOARD_OIDC_CLIENT_ID isn't
set, auth is DISABLED (local dev) and a warning is logged. The session cookie is HMAC-
signed with DASHBOARD_SECRET_KEY (stdlib only — no extra deps).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

log = logging.getLogger("webapp.auth")

SESSION_COOKIE = "sre_session"
TX_COOKIE = "sre_oidc_tx"          # short-lived: holds state/nonce/PKCE between login→callback
SESSION_TTL = 8 * 3600


@dataclass
class AuthConfig:
    issuer: str
    client_id: str
    client_secret: str
    base_url: str                  # public URL of the dashboard, for redirect_uri
    required_groups: list[str]
    secret_key: bytes

    @property
    def enabled(self) -> bool:
        return bool(self.issuer and self.client_id)

    @property
    def redirect_uri(self) -> str:
        return f"{self.base_url.rstrip('/')}/auth/callback"


def load_config() -> AuthConfig:
    issuer = os.environ.get("DASHBOARD_OIDC_ISSUER") or os.environ.get("OIDC_ISSUER", "")
    sk = os.environ.get("DASHBOARD_SECRET_KEY", "")
    if not sk:
        sk = secrets.token_hex(32)  # ephemeral: sessions won't survive a restart (dev only)
    cfg = AuthConfig(
        issuer=issuer.rstrip("/"),
        client_id=os.environ.get("DASHBOARD_OIDC_CLIENT_ID", ""),
        client_secret=os.environ.get("DASHBOARD_OIDC_CLIENT_SECRET", ""),
        base_url=os.environ.get("DASHBOARD_BASE_URL", "http://127.0.0.1:8081"),
        required_groups=[g.strip() for g in os.environ.get("DASHBOARD_OIDC_REQUIRED_GROUPS", "").split(",") if g.strip()],
        secret_key=sk.encode(),
    )
    if not cfg.enabled:
        log.warning("dashboard auth DISABLED (set DASHBOARD_OIDC_CLIENT_ID to enable SSO) — dev mode")
    return cfg


# --- signed cookies (HMAC-SHA256, stdlib) ---

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(payload: dict, secret: bytes) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def unsign(token: str | None, secret: bytes) -> dict | None:
    if not token or "." not in token:
        return None
    body, _, sig = token.partition(".")
    expected = _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


# --- OIDC discovery (cached) ---

_disco: dict[str, dict] = {}


def discover(issuer: str) -> dict:
    if issuer not in _disco:
        url = f"{issuer}/.well-known/openid-configuration"
        _disco[issuer] = httpx.get(url, timeout=10).raise_for_status().json()
    return _disco[issuer]


# --- login / callback ---

def begin_login(cfg: AuthConfig) -> tuple[str, str]:
    """Return (authorize_url, signed tx-cookie value carrying state/nonce/PKCE verifier)."""
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    params = {
        "response_type": "code", "client_id": cfg.client_id, "redirect_uri": cfg.redirect_uri,
        "scope": "openid profile email", "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256",
    }
    url = f"{discover(cfg.issuer)['authorization_endpoint']}?{urlencode(params)}"
    tx = sign({"state": state, "nonce": nonce, "verifier": verifier, "exp": time.time() + 600}, cfg.secret_key)
    return url, tx


def complete_login(cfg: AuthConfig, code: str, state: str, tx_cookie: str | None) -> dict:
    """Exchange the code, validate the id_token, enforce groups → session payload. Raises on failure."""
    tx = unsign(tx_cookie, cfg.secret_key)
    if not tx or not hmac.compare_digest(tx.get("state", ""), state):
        raise PermissionError("invalid or expired login state")

    meta = discover(cfg.issuer)
    token_resp = httpx.post(meta["token_endpoint"], data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": cfg.redirect_uri,
        "client_id": cfg.client_id, "client_secret": cfg.client_secret, "code_verifier": tx["verifier"],
    }, timeout=10).raise_for_status().json()

    claims = _validate_id_token(cfg, token_resp["id_token"], meta["jwks_uri"], tx["nonce"])
    groups = claims.get("groups") or claims.get("roles") or []
    if cfg.required_groups and not (set(cfg.required_groups) & set(groups)):
        raise PermissionError("user is not in a required group")

    return {
        "sub": claims.get("sub"),
        "name": claims.get("name") or claims.get("preferred_username") or claims.get("email") or claims.get("sub"),
        "groups": groups,
        "exp": time.time() + SESSION_TTL,
    }


def _validate_id_token(cfg: AuthConfig, id_token: str, jwks_uri: str, nonce: str) -> dict:
    import jwt
    from jwt import PyJWKClient

    key = PyJWKClient(jwks_uri).get_signing_key_from_jwt(id_token).key
    claims = jwt.decode(id_token, key, algorithms=["RS256"], audience=cfg.client_id, issuer=cfg.issuer)
    if nonce and claims.get("nonce") not in (nonce, None):
        raise PermissionError("id_token nonce mismatch")
    return claims
