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

from sqlalchemy.orm import Session

from src.assistant.sources import webinar_qa_document
from src.assistant.sync import (
    delete_source,
    index_hash,
    lock_source,
    replace_source,
    stored_hash,
)
from src.db.base import get_session_factory

logger = logging.getLogger(__name__)

SOURCE_TYPE = "webinar_qa"


def sync_webinar_qa_chunks(db: Session, webinar_id: uuid.UUID, *, enrich: bool = True) -> str:
    """Bring one webinar's chunks in line with its questions.

    Returns "updated", "removed" or "unchanged". Uses the same hash as the
    nightly sync, so neither redoes the other's work.
    """
    doc = webinar_qa_document(db, webinar_id)
    stored = stored_hash(db, SOURCE_TYPE, webinar_id)
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


def reindex_webinar_qa(webinar_id: uuid.UUID) -> None:
    """Background-task entry point: own session, never raises.

    Locks before reading, so two quick edits reindex one after the other and the
    second reads the first's result; the nightly sync waits on the same lock.
    """
    try:
        with get_session_factory()() as db:
            lock_source(db, SOURCE_TYPE, webinar_id)
            outcome = sync_webinar_qa_chunks(db, webinar_id)
        logger.info("Assistant reindex of webinar %s Q&A: %s", webinar_id, outcome)
    except Exception:  # noqa: BLE001 - the admin's edit is saved; the nightly sync can retry
        logger.exception("Assistant reindex failed for webinar %s Q&A", webinar_id)
