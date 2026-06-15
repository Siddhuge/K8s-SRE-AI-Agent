"""ArgoCD / GitOps tools — read-only.

The deployment-history correlation is the most valuable signal in RCA: "the failure
started right after sync to revision abc123 / release v1.3". These tools surface app
health, sync status, the revision history, and whether a rollback target exists.
"""
from __future__ import annotations

from ..clusters import manager
from ..config import get_settings
from ._http import get_json


def _argocd(cluster: str | None) -> tuple[str, dict]:
    settings = get_settings()
    cfg = manager().resolve(cluster)
    base = cfg.gitops.argocd or settings.argocd_server
    headers = {"Authorization": f"Bearer {settings.argocd_token}"} if settings.argocd_token else {}
    return base.rstrip("/"), headers


def register(mcp) -> None:
    @mcp.tool()
    def argocd_app(app: str, cluster: str | None = None) -> dict:
        """ArgoCD application health + sync state + current target revision."""
        base, headers = _argocd(cluster)
        data = get_json(f"{base}/api/v1/applications/{app}", headers=headers)
        status = data.get("status", {})
        return {
            "name": app,
            "health": status.get("health", {}).get("status"),
            "sync": status.get("sync", {}).get("status"),
            "revision": status.get("sync", {}).get("revision"),
            "target": data.get("spec", {}).get("source", {}).get("targetRevision"),
            "operationState": (status.get("operationState") or {}).get("phase"),
            "operationMessage": (status.get("operationState") or {}).get("message"),
            # conditions carry the actual failure reason (ComparisonError, SyncError,
            # OutOfSync, …) — the "why" behind an unhealthy/Unknown app.
            "conditions": [
                {"type": c.get("type"), "message": (c.get("message") or "")[:300]}
                for c in status.get("conditions", []) or []
            ],
            "resources": [
                {"kind": r.get("kind"), "name": r.get("name"), "health": (r.get("health") or {}).get("status"),
                 "status": r.get("status")}
                for r in status.get("resources", [])
                if (r.get("health") or {}).get("status") not in (None, "Healthy")
            ],
        }

    @mcp.tool()
    def argocd_sync_status(app: str, cluster: str | None = None) -> dict:
        """Whether the live state matches Git (Synced/OutOfSync) and which resources drift."""
        base, headers = _argocd(cluster)
        data = get_json(f"{base}/api/v1/applications/{app}", headers=headers)
        sync = data.get("status", {}).get("sync", {})
        return {
            "status": sync.get("status"),
            "revision": sync.get("revision"),
            "out_of_sync": [
                f"{r.get('kind')}/{r.get('name')}"
                for r in data.get("status", {}).get("resources", [])
                if r.get("status") == "OutOfSync"
            ],
        }

    @mcp.tool()
    def argocd_history(app: str, cluster: str | None = None, limit: int = 10) -> list[dict]:
        """Deployment history: recent synced revisions with timestamp + author.

        Compare the failure start time against these to find the offending release."""
        base, headers = _argocd(cluster)
        data = get_json(f"{base}/api/v1/applications/{app}", headers=headers)
        history = data.get("status", {}).get("history", [])[-limit:]
        return [
            {
                "id": h.get("id"),
                "revision": h.get("revision"),
                "deployed_at": h.get("deployedAt"),
                "source": h.get("source", {}).get("targetRevision"),
            }
            for h in reversed(history)
        ]

    @mcp.tool()
    def argocd_rollback_info(app: str, cluster: str | None = None) -> dict:
        """Identify the previous known-good revision to roll back TO (the agent reports
        it; it does NOT perform the rollback — that's a gated, mutating action)."""
        base, headers = _argocd(cluster)
        data = get_json(f"{base}/api/v1/applications/{app}", headers=headers)
        history = data.get("status", {}).get("history", [])
        current = data.get("status", {}).get("sync", {}).get("revision")
        previous = history[-2] if len(history) >= 2 else None
        return {
            "current_revision": current,
            "rollback_candidate": (previous or {}).get("revision"),
            "rollback_deployed_at": (previous or {}).get("deployedAt"),
            "note": "Rollback is a mutating action; execute via your change pipeline, not this agent.",
        }
