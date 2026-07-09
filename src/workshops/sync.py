"""Airtable → DB sync orchestrator for workshops and webinars.

Pipeline (one direction, no drift):
    Airtable ──> workshops  (sync_workshops_from_airtable — match/rename/create)
    Airtable ──> webinars   (sync_webinars_from_airtable — needs workshop airtable_ids)

The public entry points below preserve the original function names/response
shapes used by the routers.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from src.workshops.models import AirtableSyncLog
from src.workshops.sync_webinars import sync_webinars_from_airtable
from src.workshops.sync_workshops import sync_workshops_from_airtable

logger = logging.getLogger(__name__)

__all__ = [
    "sync_all_from_airtable",
    "sync_workshops_from_airtable",
    "sync_webinars_from_airtable",
]


def sync_all_from_airtable(db: Session) -> dict:
    """Run workshop sync then webinar sync, commit once, return combined stats."""
    w = sync_workshops_from_airtable(db)
    v = sync_webinars_from_airtable(db)

    logger.info(
        "Airtable sync summary — workshops: matched=%d updated=%d created=%d skipped=%d | "
        "webinars: matched=%d updated=%d created=%d skipped=%d",
        w["matched"], w["updated"], w["created"], w["skipped"],
        v["matched"], v["updated"], v["created"], v["skipped"],
    )

    synced_at = datetime.now(timezone.utc)
    combined = {
        "matched": w["matched"] + v["matched"],
        "updated": w["updated"] + v["updated"],
        "skipped": w["skipped"] + v["skipped"],
        "created": w["created"] + v["created"],
    }
    log = AirtableSyncLog(synced_at=synced_at, matched=combined["matched"], updated=combined["updated"], skipped=combined["skipped"])
    db.add(log)
    db.commit()
    return {**combined, "synced_at": synced_at}
