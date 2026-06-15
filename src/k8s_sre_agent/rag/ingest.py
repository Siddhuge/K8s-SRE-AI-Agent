"""Document ingestion CLI.

  python -m k8s_sre_agent.rag.ingest --source runbook --tenant payments docs/runbooks/*.md

Connectors (Confluence, Git postmortems, Jira) feed the same pipeline: each yields
(metadata, text); we chunk, embed, and upsert. Re-ingesting a document replaces its
chunks (idempotent by title+source). Run on a schedule (CronJob) to keep the KB fresh.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import embed, store


def ingest_text(*, text: str, source: str, title: str, tenant: str, service: str = "", severity: str = "", url: str = "") -> int:
    chunks = embed.chunk_document(text)
    if not chunks:
        return 0
    vectors = embed.embed_texts(chunks)
    store.upsert_chunks(
        {"source": source, "title": title, "service": service or None,
         "tenant": tenant, "severity": severity or None, "url": url or None},
        chunks, vectors,
    )
    return len(chunks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest docs into the pgvector KB")
    parser.add_argument("paths", nargs="+", help="markdown/text files to ingest")
    parser.add_argument("--source", default="runbook")
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--service", default="")
    parser.add_argument("--severity", default="")
    parser.add_argument("--init", action="store_true", help="create schema first")
    args = parser.parse_args(argv)

    if args.init:
        store.init_schema()

    total = 0
    for path in args.paths:
        p = Path(path)
        n = ingest_text(
            text=p.read_text(), source=args.source, title=p.stem,
            tenant=args.tenant, service=args.service, severity=args.severity, url=str(p),
        )
        print(f"ingested {p.name}: {n} chunks")
        total += n
    print(f"done — {total} chunks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
