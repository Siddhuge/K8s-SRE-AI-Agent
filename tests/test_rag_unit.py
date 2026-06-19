"""Unit tests for the RAG pipeline pieces that need no database.

Chunking is pure. The local-embedding test is gated on fastembed being installed."""
import importlib.util

import pytest

from k8s_sre_agent.rag.embed import chunk_document


def test_chunk_document_splits_on_headings():
    text = "# Title\nintro\n## Symptoms\nfoo bar\n## Resolution\nbaz qux"
    chunks = chunk_document(text)
    assert len(chunks) >= 2
    assert any("Symptoms" in c for c in chunks)
    assert any("Resolution" in c for c in chunks)


def test_chunk_document_token_bounded():
    big = "## Big\n" + " ".join(f"w{i}" for i in range(2000))
    chunks = chunk_document(big, max_tokens=200)
    assert len(chunks) > 1
    assert all(len(c.split()) <= 200 for c in chunks)


@pytest.mark.skipif(importlib.util.find_spec("fastembed") is None, reason="fastembed not installed")
def test_local_embedding_dimension(monkeypatch):
    from k8s_sre_agent.config import get_settings
    import k8s_sre_agent.rag.embed as embed

    get_settings.cache_clear()
    monkeypatch.setenv("EMBEDDING_MODEL", "local:BAAI/bge-small-en-v1.5")
    vecs = embed.embed_texts(["hello world", "kubernetes crashloop"])
    assert len(vecs) == 2
    assert len(vecs[0]) == 384 and all(isinstance(x, float) for x in vecs[0])
    get_settings.cache_clear()
