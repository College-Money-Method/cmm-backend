"""Rebuild ``assistant_chunks`` from published content, touching only what changed.

Reconcile by hash: a source whose ``index_hash`` (content plus build settings)
matches the one on its stored chunks is skipped (its scope is patched in place
if that alone changed); a changed source has its chunks replaced; a
source that is gone (unpublished, deleted, newly restricted) has its chunks
removed. Each source commits on its own, so an interrupted run keeps its
progress and the next run picks up the rest.

An admin's edit to a webinar's Q&A reindexes that one source straight away
(``webinar_qa_reindex``), so this run can meet a source that changed after it
read it. Both paths write a source only under ``lock_source``, and this run
writes or removes one only if its stored hash is still the one the run started
from — otherwise the admin's newer copy stands. An admin's access change to a
resource (schools, audience, unpublish) keeps the hash, so who may see what is
re-read under the lock (``live_source_access``) rather than taken from the
copy this run read before it.

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

from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from src.assistant.chunking import MAX_CHUNK_CHARS, MIN_CHUNK_CHARS, chunk_sections
from src.assistant.embeddings import embed_documents
from src.assistant.enrichment import enrich_chunk
from src.assistant.live_source_access import SourceAccess, live_access
from src.assistant.models import AssistantChunk
from src.assistant.retrieval_scope import ChunkScope
from src.assistant.sources import SourceDocument, load_source_documents
from src.config import settings
from src.db.base import get_session_factory
from src.utils import single_writer

logger = logging.getLogger(__name__)

_SYNC_JOB_ID = "assistant_content_sync"
# Every uvicorn worker runs its own scheduler; one of them does the nightly run.
_LEADER_LOCK = "assistant-sync"
# Bedrock Haiku tolerates a few concurrent calls per account comfortably.
_ENRICH_WORKERS = 4


@dataclass
class SyncResult:
    unchanged: int = 0
    updated: int = 0
    removed: int = 0
    # Only who may see the source changed: scope patched, nothing re-embedded.
    rescoped: int = 0
    chunks_written: int = 0
    failed: int = 0
    # Changed by an admin's reindex while this run was building it; theirs stands.
    superseded: int = 0


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


def _stored_state(db: Session) -> dict[tuple[str, uuid.UUID], tuple[str, ChunkScope]]:
    """Each indexed source's (hash, scope) as currently stored."""
    rows = db.execute(
        select(
            AssistantChunk.source_type,
            AssistantChunk.source_id,
            AssistantChunk.content_hash,
            AssistantChunk.scope_school_ids,
            AssistantChunk.scope_cohort_ids,
            AssistantChunk.scope_states,
        ).distinct()
    )
    return {
        (kind, sid): (hash_, ChunkScope.from_chunk_columns(schools, cohorts, states))
        for kind, sid, hash_, schools, cohorts, states in rows
    }


def stored_hash(db: Session, source_type: str, source_id: uuid.UUID) -> str | None:
    return db.scalar(
        select(AssistantChunk.content_hash)
        .where(AssistantChunk.source_type == source_type, AssistantChunk.source_id == source_id)
        .limit(1)
    )


def lock_source(db: Session, source_type: str, source_id: uuid.UUID) -> None:
    """Hold one source's chunks until this transaction ends.

    Two writers replacing the same source at once could both delete and then
    both insert, leaving every chunk twice, or the one that read older content
    could commit last. A transaction-scoped advisory lock serialises them and
    works through a transaction pooler too. SQLite (tests) has no such lock and
    no concurrent writers, so it is skipped there.
    """
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"assistant_source:{source_type}:{source_id}"},
        )


def delete_source(db: Session, source_type: str, source_id: uuid.UUID) -> None:
    db.execute(
        delete(AssistantChunk).where(
            AssistantChunk.source_type == source_type, AssistantChunk.source_id == source_id
        )
    )


def set_source_scope(db: Session, source_type: str, source_id: uuid.UUID, scope: ChunkScope) -> None:
    """Rewrite who may see a source's chunks, without touching their embeddings.

    The caller holds ``lock_source`` and commits, so the change lands atomically
    with whatever else it is doing.
    """
    db.execute(
        update(AssistantChunk)
        .where(AssistantChunk.source_type == source_type, AssistantChunk.source_id == source_id)
        .values(
            scope_school_ids=list(scope.school_ids),
            scope_cohort_ids=list(scope.cohort_ids),
            scope_states=list(scope.states),
        )
    )


def _build_chunks(doc: SourceDocument, enrich: bool) -> list[AssistantChunk]:
    chunks = chunk_sections(doc.sections)
    if enrich:
        # Counselor-only content is phrased for counselors, everything else for families.
        audience = "family" if doc.for_family else "counselor"
        with ThreadPoolExecutor(max_workers=_ENRICH_WORKERS) as pool:
            enrichments = list(pool.map(lambda c: enrich_chunk(doc.title, c.heading, c.text, audience), chunks))
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
            scope_school_ids=list(doc.scope.school_ids),
            scope_cohort_ids=list(doc.scope.cohort_ids),
            scope_states=list(doc.scope.states),
            embedding=vector,
        )
        for index, (chunk, enrichment, vector) in enumerate(zip(chunks, enrichments, vectors, strict=True))
    ]


def _write_chunks(db: Session, doc: SourceDocument, rows: list[AssistantChunk]) -> int:
    delete_source(db, doc.source_type, doc.source_id)
    db.add_all(rows)
    db.commit()
    return len(rows)


def replace_source(db: Session, doc: SourceDocument, enrich: bool) -> int:
    """Swap a source's chunks for freshly built ones in one commit; returns how many."""
    return _write_chunks(db, doc, _build_chunks(doc, enrich))


def _apply_access(rows: list[AssistantChunk], access: SourceAccess) -> None:
    for row in rows:
        row.for_family = access.for_family
        row.for_counselor = access.for_counselor
        row.url_path = access.url_path
        row.scope_school_ids = list(access.scope.school_ids)
        row.scope_cohort_ids = list(access.scope.cohort_ids)
        row.scope_states = list(access.scope.states)


def _drop_withdrawn(db: Session, key: tuple[str, uuid.UUID]) -> str:
    """The source stopped being indexable after this run read it: make sure
    nothing of it stays, and leave the rest to whoever withdrew it."""
    delete_source(db, *key)
    db.commit()
    return "superseded"


def _rescope(db: Session, doc: SourceDocument, started_hash: str) -> str:
    """Patch an unchanged source's scope. Returns the ``SyncResult`` counter to bump:
    "rescoped", "superseded" (an admin got there first) or "failed"."""
    key = (doc.source_type, doc.source_id)
    try:
        lock_source(db, *key)
        if stored_hash(db, *key) != started_hash:
            db.rollback()
            return "superseded"
        access = live_access(db, doc)
        if access is None:
            return _drop_withdrawn(db, key)
        set_source_scope(db, *key, access.scope)
        db.commit()
    except Exception:  # noqa: BLE001 - one bad source must not stop the run
        logger.exception("Assistant rescope failed for %s %s", *key)
        db.rollback()
        return "failed"
    return "rescoped"


def sync_assistant_chunks(db: Session, *, enrich: bool = True, rebuild: bool = False) -> SyncResult:
    """Bring the chunk index in line with published content."""
    result = SyncResult()
    started_state = _stored_state(db)
    started_with = {key: hash_ for key, (hash_, _) in started_state.items()}
    stored = {} if rebuild else started_state
    docs = load_source_documents(db)
    live_keys = {(d.source_type, d.source_id) for d in docs}

    for doc in docs:
        key = (doc.source_type, doc.source_id)
        current = stored.get(key)
        if current is not None and current[0] == index_hash(doc, enrich):
            if current[1] == doc.scope:
                result.unchanged += 1
            else:
                outcome = _rescope(db, doc, started_with[key])
                setattr(result, outcome, getattr(result, outcome) + 1)
            continue
        try:
            # Built before taking the lock: enrichment and embedding take seconds
            # per source, and an admin's reindex should not wait on them.
            rows = _build_chunks(doc, enrich)
            lock_source(db, *key)
            if stored_hash(db, *key) != started_with.get(key):
                db.rollback()
                result.superseded += 1
                continue
            access = live_access(db, doc)
            if access is None:
                _drop_withdrawn(db, key)
                result.superseded += 1
                continue
            _apply_access(rows, access)
            written = _write_chunks(db, doc, rows)
        except Exception:  # noqa: BLE001 - one bad source must not stop the run
            logger.exception("Assistant sync failed for %s %s", doc.source_type, doc.source_id)
            db.rollback()
            result.failed += 1
            continue
        result.updated += 1
        result.chunks_written += written

    # Stale means indexed when the run started and gone from the content it read.
    # A source indexed since then is not judged against content read before it.
    for key in set(started_with) - live_keys:
        lock_source(db, *key)
        if stored_hash(db, *key) == started_with[key]:
            delete_source(db, *key)
            result.removed += 1
        db.commit()
    logger.info("Assistant sync done: %s", result)
    return result


def run_assistant_sync() -> None:
    """Scheduler entry point: own session, never raises.

    Only one worker per container runs it. Several backend tasks can still run
    it at once; that only spends their Bedrock calls twice, since every write
    goes through ``lock_source`` and the hash check above.
    """
    if not single_writer.is_leader(_LEADER_LOCK):
        return
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
