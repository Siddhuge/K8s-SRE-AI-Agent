# RAG — Grounding RCA in your own knowledge

Generic Kubernetes advice is cheap; *your org's* runbook for "payments DB failover" is
what actually resolves the incident. RAG injects internal knowledge — runbooks, SOPs,
Confluence pages, incident history, postmortems, architecture/ADR docs — into the agent's
reasoning, scoped to the right tenant.

Backend: **PostgreSQL + pgvector** (recommended for most orgs — one managed database,
SQL you already operate, RLS for tenant isolation, hybrid vector+lexical search).
OpenSearch is a drop-in alternative if you already run it at scale for logs and want a
single search substrate; the pipeline (chunk → embed → store → hybrid retrieve) is
identical, only the store adapter changes.

## 1. Document ingestion

```
Source connectors            Normalize            Chunk              Embed            Store
─────────────────            ─────────            ─────              ─────            ─────
Confluence (REST)      ┐                     heading-aware    →  voyage-3 (1024-d) → documents
Git postmortems (.md)  ├─▶ (metadata, text) ─▶ ~800 tok        per chunk            + chunks(vector,
Runbooks repo          │     title, source,    ~100 overlap                          tsv, tenant,
Jira closed incidents  ┘     service, tenant,                                        service)
ADR / arch docs              severity, url
```

* **Connectors** each yield `(metadata, text)`; they feed one common pipeline
  (`rag/ingest.py`). Adding a source = adding a connector, nothing downstream changes.
* **Idempotent**: re-ingesting a document replaces its chunks. Run on a CronJob so the
  KB tracks the wiki/postmortem repo.
* **Metadata carried per chunk**: `tenant`, `service`, `source`, `severity`, `url` — so
  retrieval can filter (and RLS can enforce isolation).

```bash
python -m k8s_sre_agent.rag.ingest --init --source runbook --tenant payments \
    --service api docs/runbooks/payments/*.md
```

## 2. Embedding strategy

* **Chunk, don't embed whole docs.** Structure-aware splitting on markdown headings keeps
  a chunk semantically coherent ("Symptoms" / "Resolution" / "Root cause"), then a
  token-bounded sub-split (~800 tokens, ~100 overlap) keeps each vector focused.
  ([rag/embed.py](../src/k8s_sre_agent/rag/embed.py))
* **Model**: `voyage-3` (1024-dim), Anthropic's recommended embedding family. Swap via
  `EMBEDDING_MODEL`/`EMBEDDING_DIM`; the pipeline only depends on the vector dimension
  matching the column.
* **Query vs document embeddings**: ingestion uses `input_type="document"`, retrieval uses
  `input_type="query"` — the asymmetric encoding the model is trained for.

## 3. Retrieval strategy

**Hybrid** = vector similarity (semantic) + `tsvector` lexical rank (exact strings), so a
query for `ImagePullBackOff` matches the chunk that literally contains that token even when
embeddings drift, while *"app can't reach the database"* still finds the
connection-failure runbook semantically.

```
score = 0.7 * cosine_similarity + 0.3 * ts_rank
```

The RCA engine auto-queries RAG with `"{issue} {root_cause}"` and attaches the top runbook
to the report. Operators can also call `kb_search` directly. ([rag/store.py](../src/k8s_sre_agent/rag/store.py))

## 4. Security model

Tenant isolation is enforced at **two layers**:

1. **Application**: the caller's tenant (derived from the target cluster, or the OIDC
   principal) is the only tenant passed to the store.
2. **Database (RLS)**: `chunks` has a Row-Level Security policy keyed on a session
   variable (`app.tenants`). Even a buggy or malicious query that forgets to filter
   returns **zero** out-of-tenant rows — the database refuses to show them.

```sql
CREATE POLICY chunks_tenant_isolation ON chunks
  USING (tenant = ANY (string_to_array(current_setting('app.tenants', true), ',')));
```

> ⚠️ **RLS only applies to a non-superuser, least-privilege role.** PostgreSQL
> **superusers and table owners bypass RLS** — a superuser connection returns rows
> from *every* tenant even with the policy in place (verified live; it's why
> `test_rls_tenant_isolation` connects as a dedicated read role). Two requirements,
> both shipped in the schema/guidance:
> 1. The agent must connect as a **non-superuser** role that does **not own** the tables
>    (own them with a separate migration/admin role).
> 2. The schema sets `ALTER TABLE chunks FORCE ROW LEVEL SECURITY` so the policy also
>    binds the owner, as defense in depth.

Other controls:
* The agent connects with a **least-privilege DB role** (`SELECT`/`INSERT` on KB tables only) —
  this is **load-bearing** for tenant isolation, not just hygiene (see warning above).
* Postmortems often contain sensitive detail; ingestion can redact PII and the `severity`/
  `tenant` tags gate who ever retrieves them.
* Secrets are never ingested; runbooks reference secret *names*, not values.

## 5. Operating it

| Concern | Approach |
|---------|----------|
| Freshness | CronJob re-ingest nightly; webhook-trigger on wiki/postmortem merge |
| Cost | Embeddings are cheap and one-time per chunk; retrieval adds ~1 small query per RCA |
| Eval | Keep a labeled set of (incident → correct runbook); track retrieval recall@k after re-ingest |
| Scale | HNSW index on the vector column; partition `chunks` by tenant at very large scale |
