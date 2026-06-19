"""Live RAG pipeline test against pgvector. Gated on env so the suite stays green
without a database:

  PGVECTOR_DSN=postgresql://...           (write/owner role — ingest + recall)
  EMBEDDING_MODEL=local:BAAI/bge-small-en-v1.5
  PGVECTOR_RO_DSN=postgresql://...        (optional non-superuser role — RLS isolation)

Validated manually 2026-06-19: recall@1 5/5 on synthetic runbooks; RLS isolates by
tenant only for a non-superuser role (superusers/owners bypass RLS — FORCE RLS + a
least-privilege role are both required).
"""
from __future__ import annotations

import importlib.util
import os
import uuid

import pytest

_HAVE = (
    os.environ.get("PGVECTOR_DSN")
    and os.environ.get("EMBEDDING_MODEL", "").startswith("local:")
    and importlib.util.find_spec("fastembed") is not None
    and importlib.util.find_spec("psycopg") is not None
)
pytestmark = pytest.mark.skipif(not _HAVE, reason="PGVECTOR_DSN/local EMBEDDING_MODEL/fastembed not set")


def _ingest(tenant, title, text):
    from k8s_sre_agent.rag.ingest import ingest_text
    return ingest_text(text=text, source="runbook", title=title, tenant=tenant)


def test_ingest_and_recall():
    from k8s_sre_agent.rag import retrieve, store

    store.init_schema()
    tag = uuid.uuid4().hex[:8]
    _ingest("payments", f"db-unreachable-{tag}",
            "# DB unreachable\nThe payments api cannot connect to its database; connection timed out to the db service.")
    _ingest("payments", f"oom-{tag}",
            "# OOMKilled\nThe container exceeded its memory limit and was OOM killed by the kernel.")
    hits = retrieve.search("payments api cannot reach the database, connection timed out",
                           tenants=["payments"], top_k=3)
    assert hits, "expected retrieval hits"
    assert f"db-unreachable-{tag}" == hits[0]["title"]


@pytest.mark.skipif(not os.environ.get("PGVECTOR_RO_DSN"), reason="PGVECTOR_RO_DSN (non-superuser) not set")
def test_rls_tenant_isolation(monkeypatch):
    from k8s_sre_agent.config import get_settings
    from k8s_sre_agent.rag import retrieve

    # seed a payments doc with the write DSN
    _ingest("payments", f"rls-{uuid.uuid4().hex[:8]}",
            "# secret runbook\npayments-only tenant content for RLS isolation test.")
    # query as the least-privilege (non-superuser) role → RLS must apply
    get_settings.cache_clear()
    monkeypatch.setenv("PGVECTOR_DSN", os.environ["PGVECTOR_RO_DSN"])
    q = "payments tenant content"
    assert retrieve.search(q, tenants=["payments"], top_k=3), "payments tenant should see its docs"
    assert retrieve.search(q, tenants=["checkout"], top_k=3) == [], "cross-tenant query must return nothing"
    get_settings.cache_clear()
