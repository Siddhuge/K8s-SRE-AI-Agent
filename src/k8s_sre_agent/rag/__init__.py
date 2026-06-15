"""RAG over internal SRE knowledge: runbooks, SOPs, Confluence, postmortems, ADRs.

Backed by PostgreSQL + pgvector. Kept optional (RAG_ENABLED) so the core agent runs
without a database. See docs/rag.md for the ingestion / embedding / retrieval design.
"""
