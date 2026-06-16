"""Assert the read-only security boundary is enforced AT THE API SERVER.

Uses SubjectAccessReview to ask the cluster what the agent's ServiceAccount is
allowed to do — the authoritative check (independent of the agent code). Requires
the read-only ClusterRole + binding deployed (helm install into sre-system). SKIPS
otherwise. This is the test that guards against an accidental privilege escalation.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("CLUSTERS_CONFIG", "config/clusters.yaml")
CLUSTER = "kind-sre-demo"
SA_NS, SA_NAME = "sre-system", "k8s-sre-agent"
SA_USER = f"system:serviceaccount:{SA_NS}:{SA_NAME}"


def _can_i(verb: str, resource: str, *, group: str = "", subresource: str = "", namespace: str = "payments") -> bool:
    from kubernetes import client
    from k8s_sre_agent.clusters import manager

    api = client.AuthorizationV1Api(manager().clients(CLUSTER).api_client)
    attrs = client.V1ResourceAttributes(
        namespace=namespace, verb=verb, group=group, resource=resource, subresource=subresource or None
    )
    review = client.V1SubjectAccessReview(
        spec=client.V1SubjectAccessReviewSpec(user=SA_USER, resource_attributes=attrs)
    )
    return bool(api.create_subject_access_review(review).status.allowed)


def _boundary_deployed() -> bool:
    try:
        from kubernetes import client
        from k8s_sre_agent.clusters import manager
        client.RbacAuthorizationV1Api(manager().clients(CLUSTER).api_client).read_cluster_role(
            "k8s-sre-agent-readonly"
        )
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _boundary_deployed(), reason="read-only ClusterRole not deployed")


def test_reads_are_allowed():
    assert _can_i("list", "pods")
    assert _can_i("get", "pods", subresource="log")
    assert _can_i("watch", "deployments", group="apps")


def test_writes_exec_and_secret_values_are_denied():
    # The core of the security model: none of these may ever be allowed.
    assert not _can_i("delete", "pods")
    assert not _can_i("create", "deployments", group="apps")
    assert not _can_i("patch", "deployments", group="apps")
    assert not _can_i("create", "pods", subresource="exec")
    assert not _can_i("get", "secrets")          # secret VALUES (core group) must be denied
    assert not _can_i("update", "configmaps")
    assert not _can_i("create", "namespaces")
