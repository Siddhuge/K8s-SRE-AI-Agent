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

    Default backend is Voyage (Anthropic's recommended embeddings). Set
    EMBEDDING_MODEL to a `local:<fastembed-model>` value (e.g. `local:BAAI/bge-small-en-v1.5`)
    to use a CPU-only ONNX model with no API key — handy for dev/CI and air-gapped
    installs. The rest of the pipeline only depends on the vector dimension matching
    EMBEDDING_DIM.
    """
    return _embed(texts, input_type="document")


def embed_query(text: str) -> list[float]:
    return _embed([text], input_type="query")[0]


_LOCAL_MODEL = None  # cached fastembed model


def _embed(texts: list[str], *, input_type: str) -> list[list[float]]:
    model = get_settings().embedding_model
    if model.startswith("local:"):
        return _local_embed(texts, model.split(":", 1)[1])
    try:
        import voyageai
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("voyageai not installed; set EMBEDDING_MODEL=local:<model> or pip install voyageai") from exc
    return voyageai.Client().embed(texts, model=model, input_type=input_type).embeddings


def _local_embed(texts: list[str], model_name: str) -> list[list[float]]:
    """CPU-only ONNX embeddings via fastembed (no torch, no API key)."""
    global _LOCAL_MODEL
    from fastembed import TextEmbedding

    if _LOCAL_MODEL is None:
        _LOCAL_MODEL = TextEmbedding(model_name=model_name or "BAAI/bge-small-en-v1.5")
    return [list(map(float, v)) for v in _LOCAL_MODEL.embed(texts)]
