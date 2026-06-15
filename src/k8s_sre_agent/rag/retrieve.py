"""Retrieval + the `kb_search` MCP tool.

Retrieval is tenant-scoped: the caller's allowed tenants are derived from the target
cluster (or the authenticated principal in HTTP mode) and passed to the store, which
enforces them via Row-Level Security. This prevents cross-tenant knowledge leakage.
"""
from __future__ import annotations

from ..clusters import manager
from . import embed, store


def search(query: str, *, tenants: list[str] | None = None, service: str | None = None, top_k: int = 4) -> list[dict]:
    qvec = embed.embed_query(query)
    return store.search(qvec, query, tenants or ["default"], service, top_k)


def register(mcp) -> None:
    @mcp.tool()
    def kb_search(query: str, cluster: str | None = None, service: str = "", top_k: int = 4) -> list[dict]:
        """Search internal SRE knowledge — runbooks, SOPs, Confluence, postmortems,
        architecture docs — for guidance relevant to the current incident.

        Results are scoped to the target cluster's tenant. Use this to ground a fix in
        your org's documented procedures rather than generic advice."""
        cfg = manager().resolve(cluster)
        hits = search(query, tenants=[cfg.tenant], service=service or None, top_k=top_k)
        return [
            {"title": h["title"], "source": h["source"], "url": h.get("url"),
             "severity": h.get("severity"), "excerpt": h["content"][:600], "score": round(h["score"], 3)}
            for h in hits
        ]
