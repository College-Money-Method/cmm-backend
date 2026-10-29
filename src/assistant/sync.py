"""Rebuild ``assistant_chunks`` from published content, touching only what changed.

Reconcile by hash: a source whose ``index_hash`` (content plus build settings)
matches the one on its stored chunks is skipped; a changed source has its chunks replaced; a
source that is gone (unpublished, deleted, newly restricted) has its chunks
removed. Each source commits on its own, so an interrupted run keeps its
progress and the next run picks up the rest.

Run by hand:   uv run python -m src.assistant.sync [--no-enrich] [--rebuild]
Scheduled:     nightly when ``ASSISTANT_SYNC_ENABLED`` is true.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from src.assistant.chunking import MAX_CHUNK_CHARS, MIN_CHUNK_CHARS, chunk_sections
from src.assistant.embeddings import embed_documents
from src.assistant.enrichment import enrich_chunk
from src.assistant.models import AssistantChunk
from src.assistant.sources import SourceDocument, load_source_documents
from src.config import settings
from src.db.base import get_session_factory

logger = logging.getLogger(__name__)

_SYNC_JOB_ID = "assistant_content_sync"
# Bedrock Haiku tolerates a few concurrent calls per account comfortably.
_ENRICH_WORKERS = 4


@dataclass
class SyncResult:
    unchanged: int = 0
    updated: int = 0
    removed: int = 0
    chunks_written: int = 0
    failed: int = 0


def embedding_input(title: str, heading: str | None, text: str, enrichment: str | None) -> str:
    """What gets embedded for a chunk: its context, its text and its questions."""
    parts = [title, heading or "", text]
    if enrichment:
        parts.append(f"Questions this answers:\n{enrichment}")
    return "\n\n".join(p for p in parts if p)


def index_hash(doc: SourceDocument, enrich: bool) -> str:
    """The doc's content hash plus how its chunks were built.

    A change to the chunk sizes, the embed model, or an earlier ``--no-enrich``
    run then re-chunks the source on the next sync instead of keeping stale rows.
    """
    recipe = f"{doc.content_hash}|{MAX_CHUNK_CHARS}|{MIN_CHUNK_CHARS}|{settings.assistant_embed_model_id}|{enrich}"
    return hashlib.sha256(recipe.encode()).hexdigest()


def _stored_hashes(db: Session) -> dict[tuple[str, uuid.UUID], str]:
    rows = db.execute(
        select(AssistantChunk.source_type, AssistantChunk.source_id, AssistantChunk.content_hash).distinct()
    )
    return {(t, sid): h for t, sid, h in rows}


def _delete_source(db: Session, source_type: str, source_id: uuid.UUID) -> None:
    db.execute(
        delete(AssistantChunk).where(
            AssistantChunk.source_type == source_type, AssistantChunk.source_id == source_id
        )
    )


def _build_chunks(doc: SourceDocument, enrich: bool) -> list[AssistantChunk]:
    chunks = chunk_sections(doc.sections)
    if enrich:
        with ThreadPoolExecutor(max_workers=_ENRICH_WORKERS) as pool:
            enrichments = list(pool.map(lambda c: enrich_chunk(doc.title, c.heading, c.text), chunks))
    else:
        enrichments = [None] * len(chunks)
    vectors = embed_documents(
        [embedding_input(doc.title, c.heading, c.text, e) for c, e in zip(chunks, enrichments, strict=True)]
    )
    content_hash = index_hash(doc, enrich)
    return [
        AssistantChunk(
            source_type=doc.source_type,
            source_id=doc.source_id,
            chunk_index=index,
            title=doc.title,
            heading=chunk.heading,
            url_path=doc.url_path,
            text=chunk.text,
            enrichment=enrichment,
            for_family=doc.for_family,
            for_counselor=doc.for_counselor,
            content_hash=content_hash,
            embedding=vector,
        )
        for index, (chunk, enrichment, vector) in enumerate(zip(chunks, enrichments, vectors, strict=True))
    ]


def sync_assistant_chunks(db: Session, *, enrich: bool = True, rebuild: bool = False) -> SyncResult:
    """Bring the chunk index in line with published content."""
    result = SyncResult()
    stored = {} if rebuild else _stored_hashes(db)
    docs = load_source_documents(db)
    live_keys = {(d.source_type, d.source_id) for d in docs}

    for doc in docs:
        key = (doc.source_type, doc.source_id)
        if stored.get(key) == index_hash(doc, enrich):
            result.unchanged += 1
            continue
        try:
            rows = _build_chunks(doc, enrich)
        except Exception:  # noqa: BLE001 - one bad source must not stop the run
            logger.exception("Assistant sync failed for %s %s", doc.source_type, doc.source_id)
            result.failed += 1
            continue
        _delete_source(db, *key)
        db.add_all(rows)
        db.commit()
        result.updated += 1
        result.chunks_written += len(rows)

    stale = set(_stored_hashes(db)) - live_keys
    for key in stale:
        _delete_source(db, *key)
    db.commit()
    result.removed = len(stale)
    logger.info("Assistant sync done: %s", result)
    return result


def run_assistant_sync() -> None:
    """Scheduler entry point: own session, never raises."""
    try:
        with get_session_factory()() as db:
            sync_assistant_chunks(db)
    except Exception:  # noqa: BLE001 - a scheduler job must not crash the loop
        logger.exception("Assistant sync job failed")


def register_assistant_sync_job(scheduler) -> None:
    """Nightly sync, only where enabled (off by default; local runs use the CLI)."""
    if not settings.assistant_sync_enabled:
        return
    scheduler.add_job(
        run_assistant_sync, "cron", hour=3, minute=30, id=_SYNC_JOB_ID, replace_existing=True
    )
    logger.info("Assistant sync job registered (nightly 03:30)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync published content into assistant_chunks.")
    parser.add_argument("--no-enrich", action="store_true", help="skip the Haiku question pass")
    parser.add_argument("--rebuild", action="store_true", help="re-embed every source, ignoring hashes")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    import src.db.models  # noqa: F401, PLC0415 - register every mapper before querying

    with get_session_factory()() as db:
        result = sync_assistant_chunks(db, enrich=not args.no_enrich, rebuild=args.rebuild)
    print(result)  # noqa: T201


if __name__ == "__main__":
    main()
