"""Embedding strategy.

We embed *chunks*, not whole documents. Chunking is structure-aware: runbooks and
postmortems are split on headings (## sections) so a chunk is a coherent unit
("Symptoms", "Resolution", "Root cause"), then sub-split to ~800 tokens with ~100
token overlap. Each chunk keeps its document metadata (title, source, service,
severity) so retrieval can filter by service/tenant.

The embedding model is configurable (EMBEDDING_MODEL). Default `voyage-3` (1024-dim)
— Anthropic's recommended embedding family. Swap to any model your gateway exposes
by changing EMBEDDING_MODEL / EMBEDDING_DIM and `embed_texts`.
"""
from __future__ import annotations

import re

from ..config import get_settings

_MAX_TOKENS = 800
_OVERLAP = 100


def chunk_document(text: str, *, max_tokens: int = _MAX_TOKENS) -> list[str]:
    """Heading-aware chunking with token-bounded sub-splits and overlap."""
    sections = re.split(r"(?m)^(?=#{1,3}\s)", text)  # split before markdown headings
    chunks: list[str] = []
    for section in sections:
        words = section.split()
        if len(words) <= max_tokens:
            if section.strip():
                chunks.append(section.strip())
            continue
        step = max_tokens - _OVERLAP
        for i in range(0, len(words), step):
            chunks.append(" ".join(words[i : i + max_tokens]))
    return chunks


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Return one embedding vector per input text.

    Implemented against Voyage (Anthropic's recommended embeddings). Replace the body
    to point at whichever embedding endpoint your platform standardizes on; the rest
    of the pipeline only depends on the returned vector dimension matching
    EMBEDDING_DIM.
    """
    settings = get_settings()
    try:
        import voyageai
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("voyageai not installed; `pip install voyageai` or swap embed_texts") from exc

    client = voyageai.Client()
    result = client.embed(texts, model=settings.embedding_model, input_type="document")
    return result.embeddings


def embed_query(text: str) -> list[float]:
    settings = get_settings()
    import voyageai

    client = voyageai.Client()
    return client.embed([text], model=settings.embedding_model, input_type="query").embeddings[0]
