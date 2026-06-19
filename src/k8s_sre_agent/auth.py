"""Authentication helpers.

Two distinct auth surfaces exist:

1. **Inbound** (remote HTTP transport): callers present an OIDC / Entra ID bearer
   token. `verify_bearer_token` validates issuer, audience, expiry and group
   membership before any tool runs. In stdio mode the OS user is the boundary.

2. **Outbound to clusters**: per-cluster credentials are minted from the cluster's
   `auth.mode` — Workload Identity (AKS), IRSA (EKS) or a kubeconfig context. No
   long-lived cluster secrets are stored by the agent; federated tokens are short-lived.

This module keeps the cloud SDKs optional so the core install stays light.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .config import ClusterConfig, Settings


@dataclass
class Principal:
    """The authenticated caller (remote HTTP mode)."""

    subject: str
    groups: list[str]
    tenant: str | None = None


class AuthError(Exception):
    pass


def verify_bearer_token(token: str, settings: Settings) -> Principal:
    """Validate an inbound OIDC / Entra ID access token.

    Uses the issuer's JWKS to verify the RS256 signature, then checks audience,
    expiry and the required-groups claim. (PyJWT + JWKS client; imported lazily so
    the dependency is only needed for the HTTP transport.)
    """
    try:
        import jwt
        from jwt import PyJWKClient
    except ImportError as exc:  # pragma: no cover - optional dep
        raise AuthError("pyjwt is required for HTTP-transport auth") from exc

    if not settings.oidc_issuer:
        raise AuthError("OIDC_ISSUER not configured; refusing unauthenticated HTTP access")

    jwks = PyJWKClient(f"{settings.oidc_issuer.rstrip('/')}/discovery/v2.0/keys")
    signing_key = jwks.get_signing_key_from_jwt(token).key
    claims = jwt.decode(
        token,
        signing_key,
        algorithms=["RS256"],
        audience=settings.oidc_audience or None,
        issuer=settings.oidc_issuer,
        options={"require": ["exp", "iss", "aud"]},
    )
    return authorize_claims(claims, settings)


def authorize_claims(claims: dict, settings: Settings) -> Principal:
    """Pure authorization step (unit-testable): enforce required-groups on validated
    claims and build the Principal. Separated from JWKS/signature verification so the
    authorization policy can be tested without a live IdP."""
    groups = claims.get("groups", []) or claims.get("roles", [])
    required = {g.strip() for g in settings.oidc_required_groups.split(",") if g.strip()}
    if required and not (required & set(groups)):
        raise AuthError(f"caller lacks required group(s): {sorted(required)}")
    return Principal(subject=claims.get("sub", "unknown"), groups=groups, tenant=claims.get("tid"))


# ── Outbound: build a kubernetes.client.Configuration for a target cluster ──────

def build_kube_config(cluster: ClusterConfig):
    """Return a configured kubernetes.client.ApiClient for `cluster`.

    Each branch produces a short-lived bearer token where possible so the agent
    holds no durable cluster credential.
    """
    from kubernetes import client, config as kconfig

    mode = cluster.auth.mode

    if mode == "in_cluster":
        kconfig.load_incluster_config()
        return client.ApiClient()

    if mode == "kubeconfig":
        kconfig.load_kube_config(
            config_file=cluster.auth.kubeconfig or None,
            context=cluster.auth.context or None,
        )
        return client.ApiClient()

    cfg = client.Configuration()
    cfg.host = cluster.auth.server

    if mode == "azure_workload":
        token = _azure_workload_token(cluster)
    elif mode == "aws_eks":
        token = _eks_token(cluster)
    elif mode == "oidc_exec":
        # The exec plugin is configured in kubeconfig; defer to it.
        kconfig.load_kube_config(context=cluster.auth.context or None)
        return client.ApiClient()
    else:  # pragma: no cover
        raise AuthError(f"unsupported auth mode {mode!r}")

    # CA bundle is mounted per-cluster at a well-known path in production.
    cfg.ssl_ca_cert = f"/etc/k8s-sre-agent/ca/{cluster.name}.crt"
    api_client = client.ApiClient(cfg)
    # Set the bearer header DIRECTLY. The Configuration.api_key/api_key_prefix path is
    # keyed by the OpenAPI security-scheme name ("BearerToken"), not "authorization",
    # so api_key={"authorization": ...} silently sends NO auth header → 401. Verified
    # live against AKS Workload Identity: the same token via set_default_header works.
    api_client.set_default_header("Authorization", f"Bearer {token}")
    return api_client


def _azure_workload_token(cluster: ClusterConfig) -> str:
    """Federated Entra Workload Identity token scoped to the AKS AAD server app."""
    from azure.identity import WorkloadIdentityCredential

    cred = WorkloadIdentityCredential(
        client_id=cluster.auth.client_id,
        tenant_id=cluster.auth.tenant_id,
    )
    # 6dae42f8-... is the well-known AKS AAD server application scope.
    token = cred.get_token("6dae42f8-4368-4678-94ff-3960e28e3630/.default")
    return token.token


_eks_token_cache: dict[str, tuple[float, str]] = {}


def _eks_token(cluster: ClusterConfig) -> str:
    """STS-presigned EKS token (IRSA). Cached ~14 min (tokens live 15)."""
    import base64

    import boto3
    from botocore.signers import RequestSigner

    name = cluster.auth.cluster_name
    cached = _eks_token_cache.get(name)
    if cached and cached[0] > time.time():
        return cached[1]

    session = boto3.Session()
    sts = session.client("sts")
    signer = RequestSigner(
        sts.meta.service_model.service_id, session.region_name, "sts", "v4",
        session.get_credentials(), session.events,
    )
    url = signer.generate_presigned_url(
        {
            "method": "GET",
            "url": (
                f"https://sts.{session.region_name}.amazonaws.com/"
                "?Action=GetCallerIdentity&Version=2011-06-15"
            ),
            "body": {},
            "headers": {"x-k8s-aws-id": name},
            "context": {},
        },
        region_name=session.region_name,
        expires_in=900,
        operation_name="",
    )
    token = "k8s-aws-v1." + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    _eks_token_cache[name] = (time.time() + 14 * 60, token)
    return token
