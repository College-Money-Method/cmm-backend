"""Airtable → DB sync logic for workshops and webinars."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.integrations.airtable import get_webinar_records, get_workshops_records
from src.workshops.models import AirtableSyncLog, Webinar, Workshop

_WEBINAR_FIELD_MAP = [
    ("Video Embed Code", "video_embed_code"),
    ("StartURL", "start_url"),
    ("JoinURL", "join_url"),
    ("RegistrationURL", "registration_url"),
]


def sync_workshops_from_airtable(db: Session) -> dict:
    """
    Pull all Airtable workshop records and update matched DB workshops.

    Matching strategy:
      1. By airtable_id (fast path after first sync)
      2. By sequence_number (first-run linkage via Airtable "Webinar Sequence" field)

    Updates name and stores airtable_id on first match.
    """
    records = get_workshops_records()

    all_workshops: list[Workshop] = db.execute(select(Workshop)).scalars().all()
    by_airtable_id: dict[str, Workshop] = {w.airtable_id: w for w in all_workshops if w.airtable_id}
    by_sequence: dict[int, Workshop] = {w.sequence_number: w for w in all_workshops if w.sequence_number is not None}

    matched = updated = skipped = 0

    for rec in records:
        fields = rec["fields"]
        airtable_rec_id: str = rec["id"]
        raw_seq = fields.get("Webinar Sequence")
        seq: int | None = int(raw_seq) if raw_seq is not None else None

        workshop = by_airtable_id.get(airtable_rec_id) or (by_sequence.get(seq) if seq is not None else None)
        if not workshop:
            skipped += 1
            continue

        matched += 1
        changed = False

        if not workshop.airtable_id:
            workshop.airtable_id = airtable_rec_id
            changed = True

        new_name: str | None = fields.get("Name") or None
        if new_name and workshop.name != new_name:
            workshop.name = new_name
            changed = True

        if changed:
            updated += 1

    db.flush()
    return {"matched": matched, "updated": updated, "skipped": skipped}


def _parse_dt(val: str | None) -> datetime | None:
    """Parse an Airtable ISO-8601 datetime string to an aware datetime."""
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except ValueError:
        return None


def sync_webinars_from_airtable(db: Session) -> dict:
    """
    Pull all Airtable webinar records, update matched DB webinars, and create new ones.

    Matching strategy:
      1. By airtable_id (fast path after first sync — Airtable rec["id"])
      2. By zoom_webinar_id (first-run linkage via Airtable "Webinar ID" field)

    Unmatched records are created as new Webinar rows if a linked workshop can be
    resolved (requires workshop sync to have run first so airtable_id is set).
    """
    records = get_webinar_records()

    all_webinars: list[Webinar] = db.execute(select(Webinar)).scalars().all()
    by_airtable_id: dict[str, Webinar] = {w.airtable_id: w for w in all_webinars if w.airtable_id}
    by_zoom_id: dict[str, Webinar] = {str(w.zoom_webinar_id): w for w in all_webinars if w.zoom_webinar_id}

    # Build workshop lookup by airtable_id — populated by workshop sync that ran first
    all_workshops: list[Workshop] = db.execute(select(Workshop)).scalars().all()
    workshop_by_airtable_id: dict[str, Workshop] = {w.airtable_id: w for w in all_workshops if w.airtable_id}

    matched = updated = skipped = created = 0

    for rec in records:
        fields = rec["fields"]
        airtable_rec_id: str = rec["id"]
        zoom_id: str | None = str(fields["Webinar ID"]) if fields.get("Webinar ID") is not None else None

        webinar = by_airtable_id.get(airtable_rec_id) or (by_zoom_id.get(zoom_id) if zoom_id else None)

        if not webinar:
            # Attempt to create — requires a resolvable workshop
            linked_workshops: list[str] = fields.get("Workshops") or []
            workshop_at_id = linked_workshops[0] if linked_workshops else None
            workshop = workshop_by_airtable_id.get(workshop_at_id) if workshop_at_id else None
            if not workshop:
                skipped += 1
                continue

            webinar = Webinar(
                workshop_id=workshop.id,
                airtable_id=airtable_rec_id,
                zoom_webinar_id=zoom_id,
                webinar_name=fields.get("Webinar Name") or None,
                start_datetime=_parse_dt(fields.get("Start Date and Time")),
                end_datetime=_parse_dt(fields.get("End Date and Time")),
                join_url=fields.get("JoinURL") or None,
                start_url=fields.get("StartURL") or None,
                registration_url=fields.get("RegistrationURL") or None,
                video_embed_code=fields.get("Video Embed Code") or None,
                zoom_link=fields.get("Zoom Link") or None,
            )
            db.add(webinar)
            created += 1
            continue

        matched += 1
        changed = False

        if not webinar.airtable_id:
            webinar.airtable_id = airtable_rec_id
            changed = True

        for at_field, db_col in _WEBINAR_FIELD_MAP:
            val: str | None = fields.get(at_field) or None
            if val and getattr(webinar, db_col) != val:
                setattr(webinar, db_col, val)
                changed = True

        if changed:
            updated += 1

    db.flush()
    return {"matched": matched, "updated": updated, "skipped": skipped, "created": created}


def sync_all_from_airtable(db: Session) -> dict:
    """Run workshop sync then webinar sync, commit once, return combined stats."""
    w = sync_workshops_from_airtable(db)
    v = sync_webinars_from_airtable(db)

    synced_at = datetime.now(timezone.utc)
    combined = {
        "matched": w["matched"] + v["matched"],
        "updated": w["updated"] + v["updated"],
        "skipped": w["skipped"] + v["skipped"],
        "created": v["created"],
    }
    log = AirtableSyncLog(synced_at=synced_at, matched=combined["matched"], updated=combined["updated"], skipped=combined["skipped"])
    db.add(log)
    db.commit()
    return {**combined, "synced_at": synced_at}
