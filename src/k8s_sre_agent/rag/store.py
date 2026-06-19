"""pgvector store: schema + upsert + similarity search.

Schema (one row per chunk):

    documents(id, source, title, service, tenant, severity, url, updated_at)
    chunks(id, document_id, ordinal, content, embedding vector(N),
           tenant, service, tsv tsvector)   -- tenant denormalized for RLS filtering

Security model:
  * `tenant` is denormalized onto chunks and every query is filtered by the caller's
    allowed tenants → a payments-tenant query never retrieves checkout runbooks.
  * Postgres Row-Level Security (RLS) policies enforce this at the DB layer too, so a
    bug in the app layer can't leak across tenants. See docs/rag.md.
  * Hybrid retrieval: vector similarity (semantic) + tsvector (lexical) so exact
    error strings ("ImagePullBackOff") match even when embeddings drift.
"""
from __future__ import annotations

from ..config import get_settings

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
  id          BIGSERIAL PRIMARY KEY,
  source      TEXT NOT NULL,            -- runbook | sop | confluence | postmortem | adr
  title       TEXT NOT NULL,
  service     TEXT,
  tenant      TEXT NOT NULL DEFAULT 'default',
  severity    TEXT,
  url         TEXT,
  updated_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chunks (
  id          BIGSERIAL PRIMARY KEY,
  document_id BIGINT REFERENCES documents(id) ON DELETE CASCADE,
  ordinal     INT NOT NULL,
  content     TEXT NOT NULL,
  embedding   vector(%(dim)s) NOT NULL,
  tenant      TEXT NOT NULL DEFAULT 'default',
  service     TEXT,
  tsv         tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
);

CREATE INDEX IF NOT EXISTS chunks_embedding_idx
  ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS chunks_tenant_idx ON chunks (tenant);

-- Row-Level Security: queries must set app.tenants; chunks outside it are invisible.
-- FORCE is required — without it the table OWNER (the role that created these tables)
-- bypasses RLS entirely, so the tenant isolation would be silently ineffective.
ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS chunks_tenant_isolation ON chunks;
CREATE POLICY chunks_tenant_isolation ON chunks
  USING (tenant = ANY (string_to_array(current_setting('app.tenants', true), ',')));
"""

HYBRID_SEARCH = """
WITH vec AS (
  SELECT c.id, c.document_id, c.content,
         1 - (c.embedding <=> %(qvec)s::vector) AS vscore
  FROM chunks c
  WHERE (%(service)s::text IS NULL OR c.service = %(service)s::text)
  ORDER BY c.embedding <=> %(qvec)s::vector
  LIMIT 40
),
lex AS (
  SELECT c.id, ts_rank(c.tsv, plainto_tsquery('english', %(qtext)s)) AS lscore
  FROM chunks c
  WHERE c.tsv @@ plainto_tsquery('english', %(qtext)s)
)
SELECT d.title, d.source, d.url, d.severity, vec.content,
       (0.7 * vec.vscore + 0.3 * COALESCE(lex.lscore, 0)) AS score
FROM vec
JOIN documents d ON d.id = vec.document_id
LEFT JOIN lex ON lex.id = vec.id
ORDER BY score DESC
LIMIT %(top_k)s;
"""


def _connect():
    import psycopg

    return psycopg.connect(get_settings().pgvector_dsn, autocommit=True)


def init_schema() -> None:
    with _connect() as conn:
        conn.execute(SCHEMA % {"dim": get_settings().embedding_dim})


def upsert_chunks(document: dict, chunks: list[str], vectors: list[list[float]]) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO documents (source, title, service, tenant, severity, url) "
            "VALUES (%(source)s,%(title)s,%(service)s,%(tenant)s,%(severity)s,%(url)s) RETURNING id",
            document,
        )
        doc_id = cur.fetchone()[0]
        for ordinal, (content, vec) in enumerate(zip(chunks, vectors, strict=True)):
            cur.execute(
                "INSERT INTO chunks (document_id, ordinal, content, embedding, tenant, service) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (doc_id, ordinal, content, vec, document["tenant"], document.get("service")),
            )


def search(qvec: list[float], qtext: str, tenants: list[str], service: str | None, top_k: int) -> list[dict]:
    with _connect() as conn, conn.cursor() as cur:
        # Scope this session to the caller's tenants (drives the RLS policy).
        # Postgres SET can't take a bind param; set_config() can (session-level).
        cur.execute("SELECT set_config('app.tenants', %s, false)", (",".join(tenants),))
        cur.execute(HYBRID_SEARCH, {"qvec": qvec, "qtext": qtext, "service": service, "top_k": top_k})
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
