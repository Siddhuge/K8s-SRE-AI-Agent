"""Multi-cluster manager: context switching + tenant isolation guard.

Tools receive an optional `cluster` argument. The manager:
  * resolves it against the registry (or the default),
  * lazily builds + caches a kubernetes ApiClient per cluster,
  * enforces the per-cluster `allowedNamespaces` allow-list (tenant isolation).

Every namespace-scoped read passes through `guard_namespace` so a caller cannot
read across tenant boundaries even if they pass an arbitrary namespace.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from .auth import build_kube_config
from .config import ClusterConfig, get_registry


class TenantIsolationError(PermissionError):
    pass


@dataclass
class ClusterClients:
    cluster: ClusterConfig
    api_client: object  # kubernetes.client.ApiClient

    @property
    def core_v1(self):
        from kubernetes import client

        return client.CoreV1Api(self.api_client)

    @property
    def apps_v1(self):
        from kubernetes import client

        return client.AppsV1Api(self.api_client)

    @property
    def networking_v1(self):
        from kubernetes import client

        return client.NetworkingV1Api(self.api_client)

    @property
    def custom(self):
        from kubernetes import client

        return client.CustomObjectsApi(self.api_client)


class ClusterManager:
    def __init__(self) -> None:
        self._registry = get_registry()
        self._cache: dict[str, ClusterClients] = {}
        self._lock = threading.Lock()

    def resolve(self, name: str | None) -> ClusterConfig:
        return self._registry.get(name)

    def clients(self, name: str | None) -> ClusterClients:
        cfg = self.resolve(name)
        with self._lock:
            if cfg.name not in self._cache:
                self._cache[cfg.name] = ClusterClients(cfg, build_kube_config(cfg))
            return self._cache[cfg.name]

    def guard_namespace(self, name: str | None, namespace: str) -> ClusterConfig:
        """Raise if `namespace` is outside the cluster's allow-list."""
        cfg = self.resolve(name)
        if not cfg.namespace_allowed(namespace):
            raise TenantIsolationError(
                f"namespace {namespace!r} is not in the allow-list for cluster "
                f"{cfg.name!r} (tenant={cfg.tenant})"
            )
        return cfg

    def list_clusters(self) -> list[dict]:
        return [
            {
                "name": c.name,
                "tenant": c.tenant,
                "provider": c.provider,
                "region": c.region,
                "namespaces": c.allowed_namespaces,
            }
            for c in self._registry.clusters
        ]


# Single shared instance, built on first import.
_manager: ClusterManager | None = None


def manager() -> ClusterManager:
    global _manager
    if _manager is None:
        _manager = ClusterManager()
    return _manager
