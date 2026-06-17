"""Unit tests for the cloud token-minting paths (EKS IRSA / AKS Workload Identity).

The real federation needs a cloud cluster, but the token-construction LOGIC (EKS
STS-presigned 'k8s-aws-v1.' token + base64 + cache; AKS federated token) is validated
here with mocked boto3/azure SDKs injected via sys.modules — these are otherwise the
highest-risk never-executed code paths.
"""
from __future__ import annotations

import base64
import sys
import types

from k8s_sre_agent.config import ClusterAuth, ClusterConfig


def _eks_cluster():
    return ClusterConfig(name="eks-prod", auth=ClusterAuth(mode="aws_eks", clusterName="eks-prod"))


def _install_fake_boto3(monkeypatch, presigned_url="https://sts.eu-west-1.amazonaws.com/?X=1"):
    siglib = types.ModuleType("botocore.signers")

    class RequestSigner:
        def __init__(self, *a, **k):
            pass

        def generate_presigned_url(self, *a, **k):
            return presigned_url

    siglib.RequestSigner = RequestSigner
    botocore = types.ModuleType("botocore")
    botocore.signers = siglib

    boto3 = types.ModuleType("boto3")

    class Session:
        region_name = "eu-west-1"

        def client(self, name):
            sm = types.SimpleNamespace(meta=types.SimpleNamespace(
                service_model=types.SimpleNamespace(service_id="sts")))
            return sm

        def get_credentials(self):
            return object()

        @property
        def events(self):
            return object()

    boto3.Session = Session
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.signers", siglib)


def test_eks_token_format_and_cache(monkeypatch):
    import k8s_sre_agent.auth as auth

    auth._eks_token_cache.clear()
    _install_fake_boto3(monkeypatch, "https://sts.amazonaws.com/?Action=GetCallerIdentity")
    tok = auth._eks_token(_eks_cluster())
    assert tok.startswith("k8s-aws-v1.")
    decoded = base64.urlsafe_b64decode(tok[len("k8s-aws-v1."):] + "==").decode()
    assert "GetCallerIdentity" in decoded
    assert "=" not in tok  # base64 padding stripped

    # second call is served from cache (even if boto3 would now return something else)
    _install_fake_boto3(monkeypatch, "https://sts.amazonaws.com/?DIFFERENT")
    assert auth._eks_token(_eks_cluster()) == tok


def test_azure_workload_token(monkeypatch):
    import k8s_sre_agent.auth as auth

    identity = types.ModuleType("azure.identity")

    class WorkloadIdentityCredential:
        def __init__(self, **kw):
            pass

        def get_token(self, scope):
            return types.SimpleNamespace(token="aad-federated-token")

    identity.WorkloadIdentityCredential = WorkloadIdentityCredential
    azure = types.ModuleType("azure")
    azure.identity = identity
    monkeypatch.setitem(sys.modules, "azure", azure)
    monkeypatch.setitem(sys.modules, "azure.identity", identity)

    cfg = ClusterConfig(name="aks", auth=ClusterAuth(
        mode="azure_workload", clientId="cid", tenantId="tid", server="https://aks"))
    assert auth._azure_workload_token(cfg) == "aad-federated-token"
