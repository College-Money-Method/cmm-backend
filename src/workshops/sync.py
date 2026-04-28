"""Airtable → DB sync logic for webinars."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.integrations.airtable import get_webinar_records
from src.workshops.models import AirtableSyncLog, Webinar

_FIELD_MAP = [
    ("Video Embed Code", "video_embed_code"),
    ("StartURL", "start_url"),
    ("JoinURL", "join_url"),
    ("RegistrationURL", "registration_url"),
]


def sync_webinars_from_airtable(db: Session) -> dict:
    """
    Pull all Airtable webinar records and update matched DB webinars.

    Matching strategy:
      1. By airtable_id (fast path after first sync — Airtable rec["id"])
      2. By zoom_webinar_id (first-run linkage via Airtable "Webinar ID" field)

    Updates video_embed_code, start_url, join_url, registration_url, and stores
    airtable_id on first match so subsequent syncs are O(1) lookups.
    """
    records = get_webinar_records()

    all_webinars: list[Webinar] = db.execute(select(Webinar)).scalars().all()
    by_airtable_id: dict[str, Webinar] = {w.airtable_id: w for w in all_webinars if w.airtable_id}
    by_zoom_id: dict[str, Webinar] = {w.zoom_webinar_id: w for w in all_webinars if w.zoom_webinar_id}

    matched = updated = skipped = 0

    for rec in records:
        fields = rec["fields"]
        airtable_rec_id: str = rec["id"]
        zoom_id: str | None = fields.get("Webinar ID")

        webinar = by_airtable_id.get(airtable_rec_id) or (by_zoom_id.get(zoom_id) if zoom_id else None)
        if not webinar:
            skipped += 1
            continue

        matched += 1
        changed = False

        if not webinar.airtable_id:
            webinar.airtable_id = airtable_rec_id
            changed = True

        for at_field, db_col in _FIELD_MAP:
            val: str | None = fields.get(at_field) or None
            if val and getattr(webinar, db_col) != val:
                setattr(webinar, db_col, val)
                changed = True

        if changed:
            updated += 1

    synced_at = datetime.now(timezone.utc)
    log = AirtableSyncLog(synced_at=synced_at, matched=matched, updated=updated, skipped=skipped)
    db.add(log)
    db.commit()
    return {"matched": matched, "updated": updated, "skipped": skipped, "synced_at": synced_at}
