"""Reindex one webinar's Q&A as soon as an admin changes it.

The content sync would pick the change up too, but it runs nightly at best. An
admin who excludes a question or corrects an answer expects the assistant to
stop (or start) saying it now. This rebuilds just that webinar's document, as a
background task after the admin's request returns, because enrichment and
embedding take a few seconds.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from src.assistant.models import AssistantChunk
from src.assistant.sources import webinar_qa_document
from src.assistant.sync import delete_source, index_hash, replace_source
from src.db.base import get_session_factory

logger = logging.getLogger(__name__)

SOURCE_TYPE = "webinar_qa"


def sync_webinar_qa_chunks(db: Session, webinar_id: uuid.UUID, *, enrich: bool = True) -> str:
    """Bring one webinar's chunks in line with its questions.

    Returns "updated", "removed" or "unchanged". Uses the same hash as the
    nightly sync, so neither redoes the other's work.
    """
    doc = webinar_qa_document(db, webinar_id)
    stored = db.scalar(
        select(AssistantChunk.content_hash)
        .where(AssistantChunk.source_type == SOURCE_TYPE, AssistantChunk.source_id == webinar_id)
        .limit(1)
    )
    if doc is None:
        # Nothing usable left (say the last answer was excluded): drop what is indexed.
        if stored is None:
            return "unchanged"
        delete_source(db, SOURCE_TYPE, webinar_id)
        db.commit()
        return "removed"
    if stored == index_hash(doc, enrich):
        return "unchanged"
    replace_source(db, doc, enrich)
    return "updated"


def _lock_webinar(db: Session, webinar_id: uuid.UUID) -> None:
    """Run one reindex per webinar at a time, until this transaction ends.

    Two quick edits start two reindexes. Without the lock both could delete and
    then insert, leaving every chunk twice, or the slower one could write the
    older text last. Waiting here means the second run reads the first's result.
    """
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"assistant_reindex:{SOURCE_TYPE}:{webinar_id}"},
        )


def reindex_webinar_qa(webinar_id: uuid.UUID) -> None:
    """Background-task entry point: own session, never raises."""
    try:
        with get_session_factory()() as db:
            _lock_webinar(db, webinar_id)
            outcome = sync_webinar_qa_chunks(db, webinar_id)
        logger.info("Assistant reindex of webinar %s Q&A: %s", webinar_id, outcome)
    except Exception:  # noqa: BLE001 - the admin's edit is saved; the nightly sync can retry
        logger.exception("Assistant reindex failed for webinar %s Q&A", webinar_id)
